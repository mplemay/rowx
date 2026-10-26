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
    #[error("the {0} is out of range")]
    OutOfRange(&'static str),
    #[error("the UTC offset must be a whole number of seconds")]
    OffsetFraction,
    #[error("the UTC offset is out of range for timetz")]
    OffsetRange,
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

/// An interval as the server stores it. Infinity is the maximum (or minimum) of every field, kept as is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Span {
    pub months: i32,
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

/// Every field of an interval, lossless; ±infinity comes back as the sentinel fields.
///
/// # Errors
/// `TemporalError::Length` for a value that is not 16 bytes.
pub fn decode_span(raw: &[u8]) -> Result<Span, TemporalError> {
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
    Ok(Span {
        months: i32::from_be_bytes([n0, n1, n2, n3]),
        days: i32::from_be_bytes([d0, d1, d2, d3]),
        microseconds: i64::from_be_bytes([m0, m1, m2, m3, m4, m5, m6, m7]),
    })
}

impl Span {
    // infinity is stored as the maximum (or minimum) of every field; one extreme field alone is finite
    #[must_use]
    pub const fn is_infinite(&self) -> bool {
        (self.microseconds == i64::MAX && self.days == i32::MAX && self.months == i32::MAX)
            || (self.microseconds == i64::MIN && self.days == i32::MIN && self.months == i32::MIN)
    }
}

/// # Errors
/// `TemporalError` for a bad length, ±infinity or a non-zero months component.
pub fn decode_interval(raw: &[u8]) -> Result<Interval, TemporalError> {
    let span = decode_span(raw)?;
    if span.is_infinite() {
        return Err(TemporalError::Infinite { ty: "interval" });
    }
    if span.months != 0 {
        return Err(TemporalError::IntervalMonths);
    }
    Ok(Interval {
        days: span.days,
        microseconds: span.microseconds,
    })
}

// postgres rejects a timetz offset beyond ±15:59:59
const MAX_TIMETZ_OFFSET_SECONDS: i64 = 15 * 3600 + 59 * 60 + 59;
// days from 1970-01-01 to the postgres epoch, 2000-01-01
const PG_EPOCH_DAYS: i64 = 10_957;

/// Days since 2000-01-01 in the proleptic Gregorian calendar.
#[must_use]
pub fn pg_days(year: i32, month: u32, day: u32) -> i64 {
    let year = i64::from(year) - i64::from(month <= 2);
    let era = year.div_euclid(400);
    let year_of_era = year.rem_euclid(400);
    let shifted_month = (i64::from(month) + 9) % 12;
    let day_of_year = (153 * shifted_month + 2) / 5 + i64::from(day) - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468 - PG_EPOCH_DAYS
}

/// # Errors
/// `OutOfRange` when the day count does not fit the 4-byte wire form.
pub fn encode_date(year: i32, month: u32, day: u32) -> Result<Vec<u8>, TemporalError> {
    i32::try_from(pg_days(year, month, day))
        .map(|days| days.to_be_bytes().to_vec())
        .map_err(|_| TemporalError::OutOfRange("date"))
}

#[must_use]
pub fn encode_time(micros: i64) -> Vec<u8> {
    micros.to_be_bytes().to_vec()
}

/// Text form (`HH:MM:SS.ffffff+HH:MM:SS`), because the binary zone is seconds west and the text sign is not.
///
/// # Errors
/// `OffsetFraction` for a sub-second offset, `OffsetRange` beyond ±15:59:59.
pub fn encode_timetz_text(micros: i64, offset_micros: i64) -> Result<String, TemporalError> {
    if offset_micros % MICROS_PER_SECOND != 0 {
        return Err(TemporalError::OffsetFraction);
    }
    let offset_seconds = offset_micros / MICROS_PER_SECOND;
    if offset_seconds.abs() > MAX_TIMETZ_OFFSET_SECONDS {
        return Err(TemporalError::OffsetRange);
    }
    let seconds = micros / MICROS_PER_SECOND;
    let sign = if offset_seconds < 0 { '-' } else { '+' };
    let abs = offset_seconds.abs();
    Ok(format!(
        "{:02}:{:02}:{:02}.{:06}{sign}{:02}:{:02}:{:02}",
        seconds / 3600,
        seconds / 60 % 60,
        seconds % 60,
        micros % MICROS_PER_SECOND,
        abs / 3600,
        abs / 60 % 60,
        abs % 60,
    ))
}

/// `micros` is the time of day; `offset_micros` is the UTC offset, zero for a naive timestamp.
#[must_use]
pub fn encode_timestamp(
    year: i32,
    month: u32,
    day: u32,
    micros: i64,
    offset_micros: i64,
) -> Vec<u8> {
    (pg_days(year, month, day) * MICROS_PER_DAY + micros - offset_micros)
        .to_be_bytes()
        .to_vec()
}

/// The months component is always zero.
///
/// # Errors
/// `OutOfRange` when `days` does not fit an `i32`.
pub fn encode_interval(days: i64, micros: i64) -> Result<Vec<u8>, TemporalError> {
    let days = i32::try_from(days).map_err(|_| TemporalError::OutOfRange("timedelta"))?;
    let mut out = micros.to_be_bytes().to_vec();
    out.extend_from_slice(&days.to_be_bytes());
    out.extend_from_slice(&0_i32.to_be_bytes());
    Ok(out)
}

/// # Errors
/// `OutOfRange` when `months` or `days` does not fit an `i32`.
pub fn encode_span(months: i64, days: i64, micros: i64) -> Result<Vec<u8>, TemporalError> {
    let months = i32::try_from(months).map_err(|_| TemporalError::OutOfRange("months"))?;
    let days = i32::try_from(days).map_err(|_| TemporalError::OutOfRange("days"))?;
    let mut out = micros.to_be_bytes().to_vec();
    out.extend_from_slice(&days.to_be_bytes());
    out.extend_from_slice(&months.to_be_bytes());
    Ok(out)
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
    fn span_keeps_months_and_infinity() {
        assert_eq!(
            decode_span(&interval(7, 2, 14)),
            Ok(Span {
                months: 14,
                days: 2,
                microseconds: 7
            })
        );
        let infinity = decode_span(&interval(i64::MAX, i32::MAX, i32::MAX));
        assert_eq!(infinity.map(|s| s.is_infinite()), Ok(true));
        assert_eq!(
            encode_span(i64::from(i32::MAX), i64::from(i32::MAX), i64::MAX),
            Ok(interval(i64::MAX, i32::MAX, i32::MAX))
        );
        assert_eq!(
            encode_span(i64::from(i32::MAX) + 1, 0, 0),
            Err(TemporalError::OutOfRange("months"))
        );
        assert_eq!(
            encode_span(0, i64::from(i32::MIN) - 1, 0),
            Err(TemporalError::OutOfRange("days"))
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

#[cfg(test)]
mod proptests {
    use super::*;
    use proptest::prelude::*;

    const MAX_DAYS: i32 = 2_921_939;
    const MIN_DAYS: i32 = -730_119;

    fn is_leap(year: i32) -> bool {
        (year % 4 == 0 && year % 100 != 0) || year % 400 == 0
    }

    fn days_in_month(year: i32, month: u32) -> u32 {
        match month {
            2 if is_leap(year) => 29,
            2 => 28,
            4 | 6 | 9 | 11 => 30,
            _ => 31,
        }
    }

    fn civil_date() -> impl Strategy<Value = (i32, u32, u32)> {
        (1i32..=9999, 1u32..=12, 1u32..=31)
            .prop_map(|(year, month, day)| (year, month, day.min(days_in_month(year, month))))
    }

    fn as_date((year, month, day): (i32, u32, u32)) -> Date {
        Date {
            year: u16::try_from(year).unwrap_or(0),
            month: u8::try_from(month).unwrap_or(0),
            day: u8::try_from(day).unwrap_or(0),
        }
    }

    proptest! {
        #[test]
        fn date_encode_then_decode_is_the_identity(ymd in civil_date()) {
            let raw = encode_date(ymd.0, ymd.1, ymd.2).unwrap_or_default();
            prop_assert_eq!(decode_date(&raw), Ok(as_date(ymd)));
        }

        #[test]
        fn date_day_counts_decode_exactly_inside_the_year_bounds(days in any::<i32>()) {
            let decoded = decode_date(&days.to_be_bytes());
            if days == i32::MAX || days == i32::MIN {
                prop_assert_eq!(decoded, Err(TemporalError::Infinite { ty: "date" }));
            } else if (MIN_DAYS..=MAX_DAYS).contains(&days) {
                let date = decoded.unwrap_or_else(|_| as_date((0, 0, 0)));
                let again = encode_date(i32::from(date.year), u32::from(date.month), u32::from(date.day));
                prop_assert_eq!(again, Ok(days.to_be_bytes().to_vec()));
            } else {
                prop_assert_eq!(decoded, Err(TemporalError::YearRange { ty: "date" }));
            }
        }

        #[test]
        fn time_encode_then_decode_splits_the_micros(micros in 0..MICROS_PER_DAY) {
            let decoded = decode_time(&encode_time(micros));
            let total = decoded.map(|t| {
                ((i64::from(t.hour) * 60 + i64::from(t.minute)) * 60 + i64::from(t.second))
                    * MICROS_PER_SECOND
                    + i64::from(t.microsecond)
            });
            prop_assert_eq!(total, Ok(micros));
        }

        #[test]
        fn times_outside_one_day_are_rejected(micros in prop_oneof![i64::MIN..0, MICROS_PER_DAY..i64::MAX]) {
            prop_assert_eq!(
                decode_time(&encode_time(micros)),
                Err(TemporalError::TimeOfDay { ty: "time" })
            );
        }

        #[test]
        fn timestamp_encode_then_decode_is_the_identity(
            ymd in civil_date(),
            micros in 0..MICROS_PER_DAY,
        ) {
            let raw = encode_timestamp(ymd.0, ymd.1, ymd.2, micros, 0);
            let decoded = decode_timestamp("timestamp", &raw);
            prop_assert_eq!(decoded.as_ref().map(|d| d.date), Ok(as_date(ymd)));
            let time = decoded.map_or(TimeOfDay { hour: 0, minute: 0, second: 0, microsecond: 0 }, |d| d.time);
            prop_assert_eq!(
                (i64::from(time.hour) * 3600 + i64::from(time.minute) * 60 + i64::from(time.second))
                    * MICROS_PER_SECOND
                    + i64::from(time.microsecond),
                micros
            );
        }

        #[test]
        fn timestamptz_subtracts_the_offset(
            ymd in civil_date(),
            micros in 0..MICROS_PER_DAY,
            offset in -57_599i64..=57_599,
        ) {
            let offset_micros = offset * MICROS_PER_SECOND;
            let local = encode_timestamp(ymd.0, ymd.1, ymd.2, micros, 0);
            let zoned = encode_timestamp(ymd.0, ymd.1, ymd.2, micros, offset_micros);
            let local = i64::from_be_bytes(<[u8; 8]>::try_from(local.as_slice()).unwrap_or([0; 8]));
            let zoned = i64::from_be_bytes(<[u8; 8]>::try_from(zoned.as_slice()).unwrap_or([0; 8]));
            prop_assert_eq!(local - zoned, offset_micros);
        }

        #[test]
        fn timestamp_micros_outside_the_year_bounds_are_rejected(micros in any::<i64>()) {
            let decoded = decode_timestamp("timestamp", &micros.to_be_bytes());
            let first = i64::from(MIN_DAYS) * MICROS_PER_DAY;
            let last = i64::from(MAX_DAYS) * MICROS_PER_DAY + MICROS_PER_DAY - 1;
            if micros == i64::MAX || micros == i64::MIN {
                prop_assert_eq!(decoded, Err(TemporalError::Infinite { ty: "timestamp" }));
            } else if (first..=last).contains(&micros) {
                prop_assert!(decoded.is_ok());
            } else {
                prop_assert_eq!(decoded, Err(TemporalError::YearRange { ty: "timestamp" }));
            }
        }

        #[test]
        fn interval_encode_then_decode_is_the_identity(days in any::<i32>(), micros in any::<i64>()) {
            let raw = encode_interval(i64::from(days), micros).unwrap_or_default();
            prop_assert_eq!(decode_interval(&raw), Ok(Interval { days, microseconds: micros }));
        }

        #[test]
        fn span_encode_then_decode_is_the_identity(months in any::<i32>(), days in any::<i32>(), micros in any::<i64>()) {
            let raw = encode_span(i64::from(months), i64::from(days), micros).unwrap_or_default();
            prop_assert_eq!(decode_span(&raw), Ok(Span { months, days, microseconds: micros }));
        }

        #[test]
        fn interval_days_beyond_i32_cannot_be_encoded(days in prop_oneof![i64::MIN..i64::from(i32::MIN), (i64::from(i32::MAX) + 1)..i64::MAX]) {
            prop_assert_eq!(encode_interval(days, 0), Err(TemporalError::OutOfRange("timedelta")));
        }

        #[test]
        fn timetz_text_names_every_field(
            micros in 0..MICROS_PER_DAY,
            offset in -57_599i64..=57_599,
        ) {
            let text = encode_timetz_text(micros, offset * MICROS_PER_SECOND).unwrap_or_default();
            let sign = if offset < 0 { '-' } else { '+' };
            prop_assert_eq!(text.len(), 15 + 9);
            prop_assert_eq!(text.chars().nth(15), Some(sign));
        }

        #[test]
        fn a_fractional_or_oversized_timetz_offset_is_rejected(
            seconds in 57_600i64..1_000_000,
            fraction in 1i64..MICROS_PER_SECOND,
        ) {
            prop_assert_eq!(
                encode_timetz_text(0, seconds * MICROS_PER_SECOND),
                Err(TemporalError::OffsetRange)
            );
            prop_assert_eq!(encode_timetz_text(0, fraction), Err(TemporalError::OffsetFraction));
        }

        #[test]
        fn decoding_random_bytes_never_panics(raw in prop::collection::vec(any::<u8>(), 0..24)) {
            let _ = decode_date(&raw);
            let _ = decode_time(&raw);
            let _ = decode_timetz(&raw);
            let _ = decode_timestamp("timestamp", &raw);
            let _ = decode_timestamp("timestamptz", &raw);
            let _ = decode_interval(&raw);
        }

        #[test]
        fn decoding_exact_width_random_bytes_never_panics(
            four in any::<[u8; 4]>(),
            eight in any::<[u8; 8]>(),
            twelve in any::<[u8; 12]>(),
            sixteen in any::<[u8; 16]>(),
        ) {
            let _ = decode_date(&four);
            let _ = decode_time(&eight);
            let _ = decode_timestamp("timestamp", &eight);
            let _ = decode_timetz(&twelve);
            let _ = decode_interval(&sixteen);
        }

        #[test]
        fn a_wrong_length_is_always_a_length_error(len in (0usize..32).prop_filter("not 4", |n| *n != 4)) {
            let raw = vec![0u8; len];
            let is_length = matches!(decode_date(&raw), Err(TemporalError::Length { .. }));
            prop_assert!(is_length);
        }
    }

    #[test]
    fn the_year_bounds_encode_to_the_documented_day_counts() {
        assert_eq!(encode_date(1, 1, 1), Ok(MIN_DAYS.to_be_bytes().to_vec()));
        assert_eq!(
            encode_date(9999, 12, 31),
            Ok(MAX_DAYS.to_be_bytes().to_vec())
        );
        assert_eq!(encode_date(2000, 1, 1), Ok(0i32.to_be_bytes().to_vec()));
    }
}
