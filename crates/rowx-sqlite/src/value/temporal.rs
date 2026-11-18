use rusqlite::types::{Value, ValueRef};

use super::{DecodeError, EncodeError, mismatch, text, trunc_i64};

const MIN_YEAR: i64 = 1;
const MAX_YEAR: i64 = 9999;
const SECONDS_PER_DAY: i64 = 86_400;
// the Julian day number of 1970-01-01T00:00:00Z
const UNIX_EPOCH_JULIAN_DAY: f64 = 2_440_587.5;
const MILLIS_PER_DAY: f64 = 86_400_000.0;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Date {
    pub year: u16,
    pub month: u8,
    pub day: u8,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Time {
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
    pub microsecond: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DateTime {
    pub date: Date,
    pub time: Time,
}

/// A local datetime and its offset east of UTC in seconds (strictly inside one day). Writing converts it
/// to UTC; reading always returns UTC with an offset of 0.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Aware {
    pub local: DateTime,
    pub offset_seconds: i32,
}

const fn is_leap(year: i64) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

const fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if is_leap(year) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

fn valid_date(date: Date) -> bool {
    let (year, month, day) = (
        i64::from(date.year),
        i64::from(date.month),
        i64::from(date.day),
    );
    (MIN_YEAR..=MAX_YEAR).contains(&year)
        && (1..=12).contains(&month)
        && (1..=days_in_month(year, month)).contains(&day)
}

const fn valid_time(time: Time) -> bool {
    time.hour < 24 && time.minute < 60 && time.second < 60 && time.microsecond < 1_000_000
}

// Hinnant's days_from_civil: days since 1970-01-01
fn days_from_civil(year: i64, month: i64, day: i64) -> Option<i64> {
    let year = if month <= 2 {
        year.checked_sub(1)?
    } else {
        year
    };
    let era = year.div_euclid(400);
    let yoe = year.rem_euclid(400);
    let mp = if month > 2 {
        month.checked_sub(3)?
    } else {
        month.checked_add(9)?
    };
    let doy = mp.checked_mul(153)?.checked_add(2)? / 5;
    let doy = doy.checked_add(day)?.checked_sub(1)?;
    let doe = yoe
        .checked_mul(365)?
        .checked_add(yoe / 4)?
        .checked_sub(yoe / 100)?
        .checked_add(doy)?;
    era.checked_mul(146_097)?
        .checked_add(doe)?
        .checked_sub(719_468)
}

// Hinnant's civil_from_days: the year, month and day of a day count since 1970-01-01
fn civil_from_days(days: i64) -> Option<(i64, i64, i64)> {
    let shifted = days.checked_add(719_468)?;
    let era = shifted.div_euclid(146_097);
    let doe = shifted.rem_euclid(146_097);
    let yoe = doe
        .checked_sub(doe / 1460)?
        .checked_add(doe / 36_524)?
        .checked_sub(doe / 146_096)?
        / 365;
    let doy = doe.checked_sub(
        yoe.checked_mul(365)?
            .checked_add(yoe / 4)?
            .checked_sub(yoe / 100)?,
    )?;
    let mp = doy.checked_mul(5)?.checked_add(2)? / 153;
    let day = doy
        .checked_sub(mp.checked_mul(153)?.checked_add(2)? / 5)?
        .checked_add(1)?;
    let month = if mp < 10 {
        mp.checked_add(3)?
    } else {
        mp.checked_sub(9)?
    };
    let year = yoe
        .checked_add(era.checked_mul(400)?)?
        .checked_add(i64::from(month <= 2))?;
    Some((year, month, day))
}

fn unix_seconds(datetime: &DateTime) -> Option<i64> {
    let date = &datetime.date;
    let days = days_from_civil(
        i64::from(date.year),
        i64::from(date.month),
        i64::from(date.day),
    )?;
    let time = &datetime.time;
    days.checked_mul(SECONDS_PER_DAY)?
        .checked_add(i64::from(time.hour).checked_mul(3600)?)?
        .checked_add(i64::from(time.minute).checked_mul(60)?)?
        .checked_add(i64::from(time.second))
}

// the datetime of Unix seconds plus microseconds, `None` outside years 1..=9999
fn from_unix(seconds: i64, microsecond: u32) -> Option<DateTime> {
    let (year, month, day) = civil_from_days(seconds.div_euclid(SECONDS_PER_DAY))?;
    if !(MIN_YEAR..=MAX_YEAR).contains(&year) {
        return None;
    }
    let of_day = u32::try_from(seconds.rem_euclid(SECONDS_PER_DAY)).ok()?;
    Some(DateTime {
        date: Date {
            year: u16::try_from(year).ok()?,
            month: u8::try_from(month).ok()?,
            day: u8::try_from(day).ok()?,
        },
        time: Time {
            hour: u8::try_from(of_day / 3600).ok()?,
            minute: u8::try_from(of_day / 60 % 60).ok()?,
            second: u8::try_from(of_day % 60).ok()?,
            microsecond,
        },
    })
}

// SQLite computes a Julian day to the millisecond, so does this
fn from_julian_day(day: f64) -> Option<DateTime> {
    let millis = trunc_i64(((day - UNIX_EPOCH_JULIAN_DAY) * MILLIS_PER_DAY).round())?;
    let micros = u32::try_from(millis.rem_euclid(1000))
        .ok()?
        .checked_mul(1000)?;
    from_unix(millis.div_euclid(1000), micros)
}

fn format_date(date: Date) -> String {
    format!("{:04}-{:02}-{:02}", date.year, date.month, date.day)
}

fn format_time(time: Time) -> String {
    let base = format!("{:02}:{:02}:{:02}", time.hour, time.minute, time.second);
    if time.microsecond == 0 {
        base
    } else {
        format!("{base}.{:06}", time.microsecond)
    }
}

fn format_datetime(datetime: &DateTime) -> String {
    format!(
        "{} {}",
        format_date(datetime.date),
        format_time(datetime.time)
    )
}

/// # Errors
/// A date outside years 1..=9999 or with an impossible month or day.
pub fn encode_date(date: Date) -> Result<Value, EncodeError> {
    if !valid_date(date) {
        return Err(EncodeError::new("the date is out of range"));
    }
    Ok(Value::Text(format_date(date)))
}

/// # Errors
/// An hour, minute, second or microsecond out of range.
pub fn encode_time(time: Time) -> Result<Value, EncodeError> {
    if !valid_time(time) {
        return Err(EncodeError::new("the time is out of range"));
    }
    Ok(Value::Text(format_time(time)))
}

/// Writes `YYYY-MM-DD HH:MM:SS[.ffffff]` with no offset.
///
/// # Errors
/// A date or time out of range.
pub fn encode_datetime(datetime: &DateTime) -> Result<Value, EncodeError> {
    if !valid_date(datetime.date) || !valid_time(datetime.time) {
        return Err(EncodeError::new("the datetime is out of range"));
    }
    Ok(Value::Text(format_datetime(datetime)))
}

/// Writes the UTC instant as `YYYY-MM-DD HH:MM:SS[.ffffff]+00:00`.
///
/// # Errors
/// A date or time out of range, an offset of a day or more, or an instant that falls outside years
/// 1..=9999 once converted to UTC.
pub fn encode_aware(aware: &Aware) -> Result<Value, EncodeError> {
    let range = || EncodeError::new("the datetime is out of range once converted to UTC");
    if !valid_date(aware.local.date)
        || !valid_time(aware.local.time)
        || aware.offset_seconds.unsigned_abs() >= 86_400
    {
        return Err(EncodeError::new(
            "the datetime or its UTC offset is out of range",
        ));
    }
    let utc = unix_seconds(&aware.local)
        .and_then(|seconds| seconds.checked_sub(i64::from(aware.offset_seconds)))
        .and_then(|seconds| from_unix(seconds, aware.local.time.microsecond))
        .ok_or_else(range)?;
    Ok(Value::Text(format!("{}+00:00", format_datetime(&utc))))
}

// a cursor over ASCII text that never indexes
struct Cursor<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Cursor<'a> {
    const fn new(text: &'a str) -> Self {
        Self {
            bytes: text.as_bytes(),
            at: 0,
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.at).copied()
    }

    const fn bump(&mut self) {
        self.at = self.at.saturating_add(1);
    }

    fn eat(&mut self, byte: u8) -> bool {
        let found = self.peek() == Some(byte);
        if found {
            self.bump();
        }
        found
    }

    const fn done(&self) -> bool {
        self.at >= self.bytes.len()
    }

    // exactly `count` ASCII digits
    fn number(&mut self, count: usize) -> Option<u32> {
        let mut value = 0u32;
        for _ in 0..count {
            let digit = char::from(self.peek()?).to_digit(10)?;
            value = value.checked_mul(10)?.checked_add(digit)?;
            self.bump();
        }
        Some(value)
    }

    // `.` already consumed: at least one digit; digits beyond the sixth must be zero, so nothing is lost
    fn fraction(&mut self) -> Option<u32> {
        let mut micros = 0u32;
        let mut count = 0usize;
        while let Some(digit) = self.peek().and_then(|byte| char::from(byte).to_digit(10)) {
            if count < 6 {
                micros = micros.checked_mul(10)?.checked_add(digit)?;
            } else if digit != 0 {
                return None;
            }
            count = count.saturating_add(1);
            self.bump();
        }
        if count == 0 {
            return None;
        }
        for _ in count..6 {
            micros = micros.checked_mul(10)?;
        }
        Some(micros)
    }

    fn date(&mut self) -> Option<Date> {
        let year = self.number(4)?;
        if !self.eat(b'-') {
            return None;
        }
        let month = self.number(2)?;
        if !self.eat(b'-') {
            return None;
        }
        let day = self.number(2)?;
        let date = Date {
            year: u16::try_from(year).ok()?,
            month: u8::try_from(month).ok()?,
            day: u8::try_from(day).ok()?,
        };
        valid_date(date).then_some(date)
    }

    fn time(&mut self) -> Option<Time> {
        let hour = self.number(2)?;
        if !self.eat(b':') {
            return None;
        }
        let minute = self.number(2)?;
        if !self.eat(b':') {
            return None;
        }
        let second = self.number(2)?;
        let microsecond = if self.eat(b'.') { self.fraction()? } else { 0 };
        let time = Time {
            hour: u8::try_from(hour).ok()?,
            minute: u8::try_from(minute).ok()?,
            second: u8::try_from(second).ok()?,
            microsecond,
        };
        valid_time(time).then_some(time)
    }

    // `Z` or `+HH:MM` / `-HH:MM`; `Ok(None)` when there is none
    fn offset(&mut self) -> Result<Option<i32>, ()> {
        let sign: i32 = match self.peek() {
            None => return Ok(None),
            Some(b'Z' | b'z') => {
                self.bump();
                return Ok(Some(0));
            }
            Some(b'+') => 1,
            Some(b'-') => -1,
            Some(_) => return Err(()),
        };
        self.bump();
        let hours = self.number(2).ok_or(())?;
        if !self.eat(b':') {
            return Err(());
        }
        let minutes = self.number(2).ok_or(())?;
        if hours >= 24 || minutes >= 60 {
            return Err(());
        }
        let total = i32::try_from(hours)
            .map_err(|_| ())?
            .checked_mul(3600)
            .ok_or(())?
            .checked_add(
                i32::try_from(minutes)
                    .map_err(|_| ())?
                    .checked_mul(60)
                    .ok_or(())?,
            )
            .ok_or(())?;
        total.checked_mul(sign).map(Some).ok_or(())
    }
}

// `YYYY-MM-DD` then a space or `T`, then the time, then an optional `Z` or numeric offset
fn parse_datetime(text: &str) -> Option<(DateTime, Option<i32>)> {
    let mut cursor = Cursor::new(text);
    let date = cursor.date()?;
    if !(cursor.eat(b' ') || cursor.eat(b'T')) {
        return None;
    }
    let time = cursor.time()?;
    let offset = cursor.offset().ok()?;
    cursor.done().then_some((DateTime { date, time }, offset))
}

fn unix_datetime(seconds: i64) -> Result<DateTime, DecodeError> {
    from_unix(seconds, 0).ok_or_else(|| {
        DecodeError::new(format!(
            "the stored Unix time {seconds} is outside years 1 to 9999"
        ))
    })
}

fn julian_datetime(day: f64) -> Result<DateTime, DecodeError> {
    from_julian_day(day).ok_or_else(|| {
        DecodeError::new(format!(
            "the stored Julian day {day} is outside years 1 to 9999"
        ))
    })
}

/// Reads ISO-8601 text (a space or `T` separator, an optional fraction), an INTEGER as Unix seconds and
/// a REAL as a Julian day. Text with an offset is an error in a naive column.
///
/// # Errors
/// A BLOB, text in another form or with an offset, and a number outside years 1..=9999.
pub fn decode_datetime(value: ValueRef<'_>) -> Result<DateTime, DecodeError> {
    match value {
        ValueRef::Text(_) => match parse_datetime(text::decode_str(value)?) {
            Some((datetime, None)) => Ok(datetime),
            Some((_, Some(_))) => Err(DecodeError::new(
                "the stored datetime has a UTC offset but the column is naive; declare it TIMESTAMPTZ",
            )),
            None => Err(DecodeError::new(
                "the stored TEXT is not an ISO-8601 datetime (YYYY-MM-DD HH:MM:SS[.ffffff])",
            )),
        },
        ValueRef::Integer(seconds) => unix_datetime(seconds),
        ValueRef::Real(day) => julian_datetime(day),
        other => Err(mismatch("TEXT, INTEGER or REAL for a datetime", other)),
    }
}

/// As [`decode_datetime`] for an aware column: text with an offset (or `Z`) is converted to UTC, and
/// Unix seconds and Julian days are UTC already. The result is always UTC.
///
/// # Errors
/// As [`decode_datetime`], and text without an offset, which would leave the zone a guess.
pub fn decode_aware(value: ValueRef<'_>) -> Result<Aware, DecodeError> {
    let utc = |local| Aware {
        local,
        offset_seconds: 0,
    };
    match value {
        ValueRef::Text(_) => match parse_datetime(text::decode_str(value)?) {
            Some((datetime, Some(offset))) => unix_seconds(&datetime)
                .and_then(|seconds| seconds.checked_sub(i64::from(offset)))
                .and_then(|seconds| from_unix(seconds, datetime.time.microsecond))
                .map(utc)
                .ok_or_else(|| {
                    DecodeError::new("the stored datetime is outside years 1 to 9999 in UTC")
                }),
            Some((_, None)) => Err(DecodeError::new(
                "the stored datetime has no UTC offset but the column is TIMESTAMPTZ; store an offset or `Z`",
            )),
            None => Err(DecodeError::new(
                "the stored TEXT is not an ISO-8601 datetime with an offset",
            )),
        },
        ValueRef::Integer(seconds) => unix_datetime(seconds).map(utc),
        ValueRef::Real(day) => julian_datetime(day).map(utc),
        other => Err(mismatch("TEXT, INTEGER or REAL for a datetime", other)),
    }
}

/// # Errors
/// Anything but TEXT in `YYYY-MM-DD` form.
pub fn decode_date(value: ValueRef<'_>) -> Result<Date, DecodeError> {
    let ValueRef::Text(_) = value else {
        return Err(mismatch("TEXT (YYYY-MM-DD) for a DATE", value));
    };
    let mut cursor = Cursor::new(text::decode_str(value)?);
    cursor
        .date()
        .filter(|_| cursor.done())
        .ok_or_else(|| DecodeError::new("the stored TEXT is not an ISO date (YYYY-MM-DD)"))
}

/// # Errors
/// Anything but TEXT in `HH:MM:SS[.ffffff]` form.
pub fn decode_time(value: ValueRef<'_>) -> Result<Time, DecodeError> {
    let ValueRef::Text(_) = value else {
        return Err(mismatch("TEXT (HH:MM:SS) for a TIME", value));
    };
    let mut cursor = Cursor::new(text::decode_str(value)?);
    cursor
        .time()
        .filter(|_| cursor.done())
        .ok_or_else(|| DecodeError::new("the stored TEXT is not an ISO time (HH:MM:SS[.ffffff])"))
}

#[cfg(test)]
mod tests {
    use super::{
        Aware, Date, DateTime, Time, decode_aware, decode_date, decode_datetime, decode_time,
        encode_aware, encode_date, encode_datetime, encode_time, from_julian_day, from_unix,
        unix_seconds,
    };
    use rusqlite::types::{Value, ValueRef};

    const fn date(year: u16, month: u8, day: u8) -> Date {
        Date { year, month, day }
    }

    const fn time(hour: u8, minute: u8, second: u8, microsecond: u32) -> Time {
        Time {
            hour,
            minute,
            second,
            microsecond,
        }
    }

    const fn dt(date: Date, time: Time) -> DateTime {
        DateTime { date, time }
    }

    fn text(value: &str) -> ValueRef<'_> {
        ValueRef::Text(value.as_bytes())
    }

    fn written(value: Result<Value, super::EncodeError>) -> Option<String> {
        match value {
            Ok(Value::Text(text)) => Some(text),
            _ => None,
        }
    }

    #[test]
    fn datetime_writes_without_offset_and_omits_a_zero_fraction() {
        let whole = dt(date(2024, 2, 29), time(23, 59, 58, 0));
        let fractional = dt(date(1, 1, 1), time(0, 0, 0, 120_000));
        assert_eq!(
            written(encode_datetime(&whole)).as_deref(),
            Some("2024-02-29 23:59:58")
        );
        assert_eq!(
            written(encode_datetime(&fractional)).as_deref(),
            Some("0001-01-01 00:00:00.120000")
        );
        assert_eq!(
            written(encode_datetime(&dt(
                date(9999, 12, 31),
                time(23, 59, 59, 999_999)
            )))
            .as_deref(),
            Some("9999-12-31 23:59:59.999999")
        );
    }

    #[test]
    fn datetime_reads_iso_text_with_either_separator() {
        let expected = Ok(dt(date(2024, 5, 6), time(7, 8, 9, 0)));
        assert_eq!(decode_datetime(text("2024-05-06 07:08:09")), expected);
        assert_eq!(decode_datetime(text("2024-05-06T07:08:09")), expected);
        assert_eq!(
            decode_datetime(text("2024-05-06 07:08:09.5")),
            Ok(dt(date(2024, 5, 6), time(7, 8, 9, 500_000)))
        );
        assert_eq!(
            decode_datetime(text("2024-05-06T07:08:09.123456")),
            Ok(dt(date(2024, 5, 6), time(7, 8, 9, 123_456)))
        );
        // SQLite writes milliseconds; trailing zeros past the sixth digit lose nothing
        assert_eq!(
            decode_datetime(text("2024-05-06 07:08:09.123000000")),
            Ok(dt(date(2024, 5, 6), time(7, 8, 9, 123_000)))
        );
    }

    #[test]
    fn datetime_rejects_malformed_text() {
        for value in [
            "",
            "2024-05-06",
            "2024-05-06 07:08",
            "2024-5-6 07:08:09",
            "2024-13-06 07:08:09",
            "2024-02-30 07:08:09",
            "2023-02-29 07:08:09",
            "2024-05-06 24:00:00",
            "2024-05-06 07:60:00",
            "2024-05-06 07:08:60",
            "2024-05-06  07:08:09",
            "2024-05-06 07:08:09.",
            "2024-05-06 07:08:09.1234567",
            "2024-05-06 07:08:09 ",
            "0000-01-01 00:00:00",
            "2024-05-06t07:08:09",
            "now",
        ] {
            assert!(decode_datetime(text(value)).is_err(), "{value}");
        }
    }

    #[test]
    fn naive_column_rejects_an_offset_in_the_text() {
        assert!(decode_datetime(text("2024-05-06 07:08:09Z")).is_err());
        assert!(decode_datetime(text("2024-05-06 07:08:09+02:00")).is_err());
    }

    #[test]
    fn unix_seconds_and_julian_days_read_as_datetimes() {
        assert_eq!(
            decode_datetime(ValueRef::Integer(0)),
            Ok(dt(date(1970, 1, 1), time(0, 0, 0, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Integer(1_714_979_289)),
            Ok(dt(date(2024, 5, 6), time(7, 8, 9, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Integer(-1)),
            Ok(dt(date(1969, 12, 31), time(23, 59, 59, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Real(2_440_587.5)),
            Ok(dt(date(1970, 1, 1), time(0, 0, 0, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Real(2_460_436.5)),
            Ok(dt(date(2024, 5, 6), time(0, 0, 0, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Real(2_440_587.75)),
            Ok(dt(date(1970, 1, 1), time(6, 0, 0, 0)))
        );
    }

    #[test]
    fn out_of_range_numbers_and_other_classes_raise() {
        for value in [
            ValueRef::Integer(i64::MAX),
            ValueRef::Integer(i64::MIN),
            ValueRef::Integer(253_402_300_800),
            ValueRef::Integer(-62_135_596_801),
            ValueRef::Real(f64::NAN),
            ValueRef::Real(f64::INFINITY),
            ValueRef::Real(1e300),
            ValueRef::Real(-1.0),
            ValueRef::Blob(b"2024-05-06 07:08:09"),
            ValueRef::Null,
            ValueRef::Text(&[0xff]),
        ] {
            assert!(decode_datetime(value).is_err(), "{value:?}");
            assert!(decode_aware(value).is_err(), "{value:?}");
        }
        assert_eq!(
            decode_datetime(ValueRef::Integer(253_402_300_799)),
            Ok(dt(date(9999, 12, 31), time(23, 59, 59, 0)))
        );
        assert_eq!(
            decode_datetime(ValueRef::Integer(-62_135_596_800)),
            Ok(dt(date(1, 1, 1), time(0, 0, 0, 0)))
        );
    }

    #[test]
    fn aware_writes_utc_with_an_explicit_offset() {
        let utc = |local, offset_seconds| {
            written(encode_aware(&Aware {
                local,
                offset_seconds,
            }))
        };
        let local = dt(date(2024, 5, 6), time(7, 8, 9, 0));
        assert_eq!(utc(local, 0).as_deref(), Some("2024-05-06 07:08:09+00:00"));
        assert_eq!(
            utc(local, 2 * 3600).as_deref(),
            Some("2024-05-06 05:08:09+00:00")
        );
        assert_eq!(
            utc(local, -(5 * 3600 + 1800)).as_deref(),
            Some("2024-05-06 12:38:09+00:00")
        );
        // the conversion rolls the date over
        assert_eq!(
            utc(dt(date(2024, 3, 1), time(0, 30, 0, 250)), 3600).as_deref(),
            Some("2024-02-29 23:30:00.000250+00:00")
        );
        assert_eq!(
            utc(dt(date(2023, 12, 31), time(23, 0, 0, 0)), -3600).as_deref(),
            Some("2024-01-01 00:00:00+00:00")
        );
    }

    #[test]
    fn aware_write_outside_the_year_range_or_offset_range_is_an_error() {
        let edge = |local, offset_seconds| {
            encode_aware(&Aware {
                local,
                offset_seconds,
            })
        };
        assert!(edge(dt(date(1, 1, 1), time(0, 0, 0, 0)), 3600).is_err());
        assert!(edge(dt(date(9999, 12, 31), time(23, 59, 59, 0)), -3600).is_err());
        assert!(edge(dt(date(2024, 1, 1), time(0, 0, 0, 0)), 86_400).is_err());
        assert!(edge(dt(date(2024, 1, 1), time(0, 0, 0, 0)), i32::MIN).is_err());
    }

    #[test]
    fn aware_reads_convert_offsets_to_utc() {
        let utc = |year, month, day, hour, minute, second, micro| {
            Ok(Aware {
                local: dt(date(year, month, day), time(hour, minute, second, micro)),
                offset_seconds: 0,
            })
        };
        assert_eq!(
            decode_aware(text("2024-05-06 07:08:09+00:00")),
            utc(2024, 5, 6, 7, 8, 9, 0)
        );
        assert_eq!(
            decode_aware(text("2024-05-06T07:08:09Z")),
            utc(2024, 5, 6, 7, 8, 9, 0)
        );
        assert_eq!(
            decode_aware(text("2024-05-06 07:08:09z")),
            utc(2024, 5, 6, 7, 8, 9, 0)
        );
        assert_eq!(
            decode_aware(text("2024-05-06 07:08:09.5+02:00")),
            utc(2024, 5, 6, 5, 8, 9, 500_000)
        );
        assert_eq!(
            decode_aware(text("2024-01-01 00:30:00+01:00")),
            utc(2023, 12, 31, 23, 30, 0, 0)
        );
        assert_eq!(
            decode_aware(text("2024-05-06 07:08:09-05:30")),
            utc(2024, 5, 6, 12, 38, 9, 0)
        );
        assert_eq!(
            decode_aware(ValueRef::Integer(0)),
            utc(1970, 1, 1, 0, 0, 0, 0)
        );
        assert_eq!(
            decode_aware(ValueRef::Real(2_440_587.5)),
            utc(1970, 1, 1, 0, 0, 0, 0)
        );
    }

    #[test]
    fn aware_read_without_an_offset_or_with_a_bad_one_raises() {
        for value in [
            "2024-05-06 07:08:09",
            "2024-05-06 07:08:09+2:00",
            "2024-05-06 07:08:09+02",
            "2024-05-06 07:08:09+0200",
            "2024-05-06 07:08:09+24:00",
            "2024-05-06 07:08:09+02:60",
            "2024-05-06 07:08:09 UTC",
            "0001-01-01 00:00:00+01:00",
            "9999-12-31 23:59:59-01:00",
        ] {
            assert!(decode_aware(text(value)).is_err(), "{value}");
        }
    }

    #[test]
    fn aware_write_then_read_round_trips() {
        let local = dt(date(2024, 5, 6), time(7, 8, 9, 123_456));
        let aware = Aware {
            local,
            offset_seconds: -7 * 3600,
        };
        let stored = written(encode_aware(&aware));
        let read = stored.as_deref().map(|stored| decode_aware(text(stored)));
        assert_eq!(
            read,
            Some(Ok(Aware {
                local: dt(date(2024, 5, 6), time(14, 8, 9, 123_456)),
                offset_seconds: 0,
            }))
        );
    }

    #[test]
    fn dates_use_iso_text() {
        assert_eq!(
            written(encode_date(date(2024, 2, 29))).as_deref(),
            Some("2024-02-29")
        );
        assert_eq!(
            written(encode_date(date(1, 1, 1))).as_deref(),
            Some("0001-01-01")
        );
        assert_eq!(decode_date(text("2024-02-29")), Ok(date(2024, 2, 29)));
        assert_eq!(decode_date(text("9999-12-31")), Ok(date(9999, 12, 31)));
        for value in [
            "",
            "2023-02-29",
            "2024-2-29",
            "2024-02-29 00:00:00",
            "2024-02-29T",
            "24-02-29",
            "2024/02/29",
            "0000-01-01",
            "2024-00-10",
            "2024-01-00",
            "2024-01-32",
        ] {
            assert!(decode_date(text(value)).is_err(), "{value}");
        }
        for value in [
            ValueRef::Integer(20_240_229),
            ValueRef::Real(1.0),
            ValueRef::Blob(b"x"),
            ValueRef::Null,
        ] {
            assert!(decode_date(value).is_err(), "{value:?}");
        }
        assert!(encode_date(date(2023, 2, 29)).is_err());
        assert!(encode_date(date(0, 1, 1)).is_err());
        assert!(encode_date(date(2024, 13, 1)).is_err());
    }

    #[test]
    fn times_use_iso_text() {
        assert_eq!(
            written(encode_time(time(1, 2, 3, 0))).as_deref(),
            Some("01:02:03")
        );
        assert_eq!(
            written(encode_time(time(23, 59, 59, 5))).as_deref(),
            Some("23:59:59.000005")
        );
        assert_eq!(decode_time(text("01:02:03")), Ok(time(1, 2, 3, 0)));
        assert_eq!(decode_time(text("01:02:03.25")), Ok(time(1, 2, 3, 250_000)));
        for value in [
            "",
            "1:02:03",
            "01:02",
            "24:00:00",
            "01:60:00",
            "01:02:60",
            "01:02:03Z",
            "01:02:03.",
            " 01:02:03",
        ] {
            assert!(decode_time(text(value)).is_err(), "{value}");
        }
        for value in [ValueRef::Integer(3600), ValueRef::Real(0.5), ValueRef::Null] {
            assert!(decode_time(value).is_err(), "{value:?}");
        }
        assert!(encode_time(time(24, 0, 0, 0)).is_err());
        assert!(encode_time(time(0, 0, 0, 1_000_000)).is_err());
    }

    #[test]
    fn encode_rejects_an_impossible_calendar_date() {
        assert!(encode_datetime(&dt(date(2023, 2, 29), time(0, 0, 0, 0))).is_err());
        assert!(encode_datetime(&dt(date(2024, 2, 29), time(0, 60, 0, 0))).is_err());
    }

    #[test]
    fn calendar_helpers_agree_on_known_instants() {
        let datetime = dt(date(2000, 2, 29), time(12, 0, 0, 0));
        assert_eq!(unix_seconds(&datetime), Some(951_825_600));
        assert_eq!(from_unix(951_825_600, 0), Some(datetime));
        assert_eq!(from_julian_day(2_451_604.0), Some(datetime));
        assert_eq!(
            unix_seconds(&dt(date(1969, 12, 31), time(23, 59, 59, 0))),
            Some(-1)
        );
        // a full cycle of every day from the epoch to 2100 round-trips
        let mut seconds = -62_135_596_800_i64;
        while seconds < 4_102_444_800 {
            let datetime = from_unix(seconds, 0);
            assert_eq!(datetime.as_ref().and_then(unix_seconds), Some(seconds));
            seconds += 86_400 * 37 + 1234;
        }
    }
}
