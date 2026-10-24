use thiserror::Error;

const MICROS_PER_SECOND: i64 = 1_000_000;
const MICROS_PER_DAY: i64 = 86_400_000_000;
const MIN_YEAR: i64 = 1;
const MAX_YEAR: i64 = 9999;
// days from 0000-03-01 to 2000-01-01, the postgres epoch
const EPOCH_SHIFT: i64 = 730_425;
const HINT: &str = "cast the column to text to read it";

#[derive(Debug, Error, PartialEq, Eq)]
pub enum TemporalError {
    #[error("{ty} needs {expected} bytes, got {got}")]
    Length {
        ty: &'static str,
        expected: usize,
        got: usize,
    },
    #[error("{ty} is infinite and has no Python equivalent; {HINT}")]
    Infinite { ty: &'static str },
    #[error("{ty} has a year outside 1..9999, which Python cannot represent; {HINT}")]
    YearRange { ty: &'static str },
    #[error("{ty} value is outside a single day (24:00:00 or malformed); {HINT}")]
    TimeOfDay { ty: &'static str },
    #[error("timetz has a zone offset of {0} seconds, which Python cannot represent; {HINT}")]
    Offset(i32),
    #[error("interval has a non-zero months component, which a timedelta cannot hold; {HINT}")]
    IntervalMonths,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Date {
    pub year: u16,
    pub month: u8,
    pub day: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeOfDay {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TimeTz {
    pub time: TimeOfDay,
    // seconds east of UTC, the sign Python uses
    pub utc_offset: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DateTime {
    pub date: Date,
    pub time: TimeOfDay,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Interval {
    pub days: i32,
    pub microseconds: i64,
}

fn array<const N: usize>(ty: &'static str, raw: &[u8]) -> Result<[u8; N], TemporalError> {
    <[u8; N]>::try_from(raw).map_err(|_| TemporalError::Length {
        ty,
        expected: N,
        got: raw.len(),
    })
}

// civil date from days since 2000-01-01 (Hinnant's civil_from_days)
fn civil(ty: &'static str, days: i64) -> Result<Date, TemporalError> {
    let range = || TemporalError::YearRange { ty };
    let z = days.checked_add(EPOCH_SHIFT).ok_or_else(range)?;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    if !(MIN_YEAR..=MAX_YEAR).contains(&year) {
        return Err(range());
    }
    Ok(Date {
        year: u16::try_from(year).map_err(|_| range())?,
        month: u8::try_from(month).map_err(|_| range())?,
        day: u8::try_from(day).map_err(|_| range())?,
    })
}

fn time_of_day(ty: &'static str, micros: i64) -> Result<TimeOfDay, TemporalError> {
    let bad = || TemporalError::TimeOfDay { ty };
    if !(0..MICROS_PER_DAY).contains(&micros) {
        return Err(bad());
    }
    let seconds = micros / MICROS_PER_SECOND;
    Ok(TimeOfDay {
        hour: u8::try_from(seconds / 3600).map_err(|_| bad())?,
        minute: u8::try_from(seconds / 60 % 60).map_err(|_| bad())?,
        second: u8::try_from(seconds % 60).map_err(|_| bad())?,
        microsecond: u32::try_from(micros % MICROS_PER_SECOND).map_err(|_| bad())?,
    })
}

/// # Errors
/// `TemporalError` for a bad length, ±infinity or a year outside 1..9999.
pub fn decode_date(raw: &[u8]) -> Result<Date, TemporalError> {
    let days = i32::from_be_bytes(array("date", raw)?);
    if days == i32::MAX || days == i32::MIN {
        return Err(TemporalError::Infinite { ty: "date" });
    }
    civil("date", i64::from(days))
}

/// # Errors
/// `TemporalError` for a bad length or a value outside one day.
pub fn decode_time(raw: &[u8]) -> Result<TimeOfDay, TemporalError> {
    time_of_day("time", i64::from_be_bytes(array("time", raw)?))
}

/// The zone is stored as seconds west of UTC, so it is negated.
///
/// # Errors
/// `TemporalError` for a bad length, a value outside one day or an offset Python rejects.
pub fn decode_timetz(raw: &[u8]) -> Result<TimeTz, TemporalError> {
    let raw: [u8; 12] = array("timetz", raw)?;
    let [micros @ .., z0, z1, z2, z3] = raw;
    let west = i32::from_be_bytes([z0, z1, z2, z3]);
    // Python requires a strictly sub-day offset
    let utc_offset = west
        .checked_neg()
        .filter(|east| east.unsigned_abs() < 86_400)
        .ok_or(TemporalError::Offset(west))?;
    Ok(TimeTz {
        time: time_of_day("timetz", i64::from_be_bytes(micros))?,
        utc_offset,
    })
}

/// Shared by `timestamp` (naive) and `timestamptz` (read as UTC).
///
/// # Errors
/// `TemporalError` for a bad length, ±infinity or a year outside 1..9999.
pub fn decode_timestamp(ty: &'static str, raw: &[u8]) -> Result<DateTime, TemporalError> {
    let micros = i64::from_be_bytes(array(ty, raw)?);
    if micros == i64::MAX || micros == i64::MIN {
        return Err(TemporalError::Infinite { ty });
    }
    Ok(DateTime {
        date: civil(ty, micros.div_euclid(MICROS_PER_DAY))?,
        time: time_of_day(ty, micros.rem_euclid(MICROS_PER_DAY))?,
    })
}

/// # Errors
/// `TemporalError` for a bad length, ±infinity or a non-zero months component.
pub fn decode_interval(raw: &[u8]) -> Result<Interval, TemporalError> {
    let raw: [u8; 16] = array("interval", raw)?;
    let [
        m0,
        m1,
        m2,
        m3,
        m4,
        m5,
        m6,
        m7,
        d0,
        d1,
        d2,
        d3,
        n0,
        n1,
        n2,
        n3,
    ] = raw;
    let microseconds = i64::from_be_bytes([m0, m1, m2, m3, m4, m5, m6, m7]);
    let days = i32::from_be_bytes([d0, d1, d2, d3]);
    let months = i32::from_be_bytes([n0, n1, n2, n3]);
    // infinity is stored as the maximum (or minimum) of every field; one extreme field alone is finite
    if (microseconds, days, months) == (i64::MAX, i32::MAX, i32::MAX)
        || (microseconds, days, months) == (i64::MIN, i32::MIN, i32::MIN)
    {
        return Err(TemporalError::Infinite { ty: "interval" });
    }
    if months != 0 {
        return Err(TemporalError::IntervalMonths);
    }
    Ok(Interval { days, microseconds })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ymd(year: u16, month: u8, day: u8) -> Date {
        Date { year, month, day }
    }

    fn hms(hour: u8, minute: u8, second: u8, microsecond: u32) -> TimeOfDay {
        TimeOfDay {
            hour,
            minute,
            second,
            microsecond,
        }
    }

    #[test]
    fn date_epoch_and_bounds() {
        assert_eq!(decode_date(&0i32.to_be_bytes()), Ok(ymd(2000, 1, 1)));
        assert_eq!(decode_date(&(-730_119i32).to_be_bytes()), Ok(ymd(1, 1, 1)));
        assert_eq!(
            decode_date(&2_921_939i32.to_be_bytes()),
            Ok(ymd(9999, 12, 31))
        );
        assert_eq!(decode_date(&8825i32.to_be_bytes()), Ok(ymd(2024, 2, 29)));
        assert_eq!(decode_date(&(-1i32).to_be_bytes()), Ok(ymd(1999, 12, 31)));
    }

    #[test]
    fn date_rejects_infinity_and_out_of_range_years() {
        for days in [i32::MAX, i32::MIN] {
            assert_eq!(
                decode_date(&days.to_be_bytes()),
                Err(TemporalError::Infinite { ty: "date" })
            );
        }
        assert_eq!(
            decode_date(&(-730_120i32).to_be_bytes()),
            Err(TemporalError::YearRange { ty: "date" })
        );
        assert_eq!(
            decode_date(&2_921_940i32.to_be_bytes()),
            Err(TemporalError::YearRange { ty: "date" })
        );
    }

    #[test]
    fn date_rejects_bad_length() {
        assert_eq!(
            decode_date(&[0, 0]),
            Err(TemporalError::Length {
                ty: "date",
                expected: 4,
                got: 2
            })
        );
    }

    #[test]
    fn time_of_day_splits_fields() {
        let micros = ((13 * 3600 + 45 * 60 + 7) * MICROS_PER_SECOND) + 123_456;
        assert_eq!(
            decode_time(&micros.to_be_bytes()),
            Ok(hms(13, 45, 7, 123_456))
        );
        assert_eq!(decode_time(&0i64.to_be_bytes()), Ok(hms(0, 0, 0, 0)));
    }

    #[test]
    fn time_rejects_24_hours() {
        assert_eq!(
            decode_time(&MICROS_PER_DAY.to_be_bytes()),
            Err(TemporalError::TimeOfDay { ty: "time" })
        );
    }

    fn timetz(micros: i64, west: i32) -> Vec<u8> {
        let mut out = micros.to_be_bytes().to_vec();
        out.extend_from_slice(&west.to_be_bytes());
        out
    }

    #[test]
    fn timetz_negates_the_zone() {
        // '12:00+02' is stored as 43200s with 7200s west -> east is -7200 negated
        let decoded = decode_timetz(&timetz(12 * 3600 * MICROS_PER_SECOND, -7200));
        assert_eq!(
            decoded,
            Ok(TimeTz {
                time: hms(12, 0, 0, 0),
                utc_offset: 7200
            })
        );
        let west = decode_timetz(&timetz(0, 18_000));
        assert_eq!(west.map(|t| t.utc_offset), Ok(-18_000));
    }

    #[test]
    fn timetz_rejects_a_day_long_offset() {
        assert_eq!(
            decode_timetz(&timetz(0, 86_400)),
            Err(TemporalError::Offset(86_400))
        );
    }

    #[test]
    fn timestamp_epoch_and_negative() {
        let epoch = decode_timestamp("timestamp", &0i64.to_be_bytes());
        assert_eq!(
            epoch,
            Ok(DateTime {
                date: ymd(2000, 1, 1),
                time: hms(0, 0, 0, 0)
            })
        );
        // one microsecond before the epoch must borrow from the date
        let before = decode_timestamp("timestamp", &(-1i64).to_be_bytes());
        assert_eq!(
            before,
            Ok(DateTime {
                date: ymd(1999, 12, 31),
                time: hms(23, 59, 59, 999_999)
            })
        );
    }

    #[test]
    fn timestamp_bounds() {
        let last = 2_921_939 * MICROS_PER_DAY + MICROS_PER_DAY - 1;
        assert_eq!(
            decode_timestamp("timestamptz", &last.to_be_bytes()),
            Ok(DateTime {
                date: ymd(9999, 12, 31),
                time: hms(23, 59, 59, 999_999)
            })
        );
        assert_eq!(
            decode_timestamp("timestamptz", &(last + 1).to_be_bytes()),
            Err(TemporalError::YearRange { ty: "timestamptz" })
        );
        for micros in [i64::MAX, i64::MIN] {
            assert_eq!(
                decode_timestamp("timestamp", &micros.to_be_bytes()),
                Err(TemporalError::Infinite { ty: "timestamp" })
            );
        }
    }

    fn interval(micros: i64, days: i32, months: i32) -> Vec<u8> {
        let mut out = micros.to_be_bytes().to_vec();
        out.extend_from_slice(&days.to_be_bytes());
        out.extend_from_slice(&months.to_be_bytes());
        out
    }

    #[test]
    fn interval_days_and_time() {
        assert_eq!(
            decode_interval(&interval(2 * 3600 * MICROS_PER_SECOND, 1, 0)),
            Ok(Interval {
                days: 1,
                microseconds: 7_200_000_000
            })
        );
        assert_eq!(
            decode_interval(&interval(-5, -2, 0)),
            Ok(Interval {
                days: -2,
                microseconds: -5
            })
        );
    }

    #[test]
    fn interval_rejects_months_and_infinity() {
        assert_eq!(
            decode_interval(&interval(0, 0, 1)),
            Err(TemporalError::IntervalMonths)
        );
        assert_eq!(
            decode_interval(&interval(i64::MAX, i32::MAX, i32::MAX)),
            Err(TemporalError::Infinite { ty: "interval" })
        );
        assert_eq!(
            decode_interval(&interval(i64::MIN, i32::MIN, i32::MIN)),
            Err(TemporalError::Infinite { ty: "interval" })
        );
    }

    #[test]
    fn interval_with_one_extreme_field_is_finite() {
        for micros in [i64::MAX, i64::MIN] {
            assert_eq!(
                decode_interval(&interval(micros, 0, 0)),
                Ok(Interval {
                    days: 0,
                    microseconds: micros
                })
            );
        }
        assert_eq!(
            decode_interval(&interval(i64::MAX, i32::MAX, 0)),
            Ok(Interval {
                days: i32::MAX,
                microseconds: i64::MAX
            })
        );
    }
}
