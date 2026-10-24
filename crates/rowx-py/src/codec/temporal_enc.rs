use crate::codec::extract::PyParam;

const MICROS_PER_SECOND: i64 = 1_000_000;
const MICROS_PER_DAY: i64 = 86_400 * MICROS_PER_SECOND;
// days from 1970-01-01 to the postgres epoch, 2000-01-01
const PG_EPOCH_DAYS: i64 = 10_957;
// postgres rejects a timetz offset beyond ±15:59:59
const MAX_TIMETZ_OFFSET_SECONDS: i64 = 15 * 3600 + 59 * 60 + 59;

#[derive(Clone, Copy, Debug)]
pub struct Ymd {
    pub year: i32,
    pub month: u32,
    pub day: u32,
}

impl Ymd {
    // days since 2000-01-01 in the proleptic Gregorian calendar
    fn pg_days(self) -> i64 {
        let year = i64::from(self.year) - i64::from(self.month <= 2);
        let era = year.div_euclid(400);
        let year_of_era = year.rem_euclid(400);
        let shifted_month = (i64::from(self.month) + 9) % 12;
        let day_of_year = (153 * shifted_month + 2) / 5 + i64::from(self.day) - 1;
        let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
        era * 146_097 + day_of_era - 719_468 - PG_EPOCH_DAYS
    }
}

fn mismatch(param: &PyParam, ty: &str) -> String {
    format!("a Python {} cannot be encoded as {ty}", param.type_name())
}

/// # Errors
/// A description of why the value cannot be encoded as `date`.
pub fn encode_date(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Date(date) => i32::try_from(date.pg_days())
            .map(|days| days.to_be_bytes().to_vec())
            .map_err(|_| "the date is out of range".to_owned()),
        _ => Err(mismatch(param, "date")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `time`.
pub fn encode_time(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Time {
            micros,
            offset: None,
        } => Ok(micros.to_be_bytes().to_vec()),
        PyParam::Time { .. } => {
            Err("a timezone-aware time cannot be encoded as time; pass a naive time".to_owned())
        }
        _ => Err(mismatch(param, "time")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timetz`.
pub fn encode_timetz(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::Time {
            micros,
            offset: Some(offset),
        } => {
            if offset % MICROS_PER_SECOND != 0 {
                return Err("the UTC offset must be a whole number of seconds".to_owned());
            }
            let offset_seconds = offset / MICROS_PER_SECOND;
            if offset_seconds.abs() > MAX_TIMETZ_OFFSET_SECONDS {
                return Err("the UTC offset is out of range for timetz".to_owned());
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
            )
            .into_bytes())
        }
        PyParam::Time { .. } => {
            Err("a naive time cannot be encoded as timetz; pass an aware time".to_owned())
        }
        _ => Err(mismatch(param, "timetz")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timestamp`.
pub fn encode_timestamp(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::DateTime {
            date,
            micros,
            offset: None,
        } => Ok((date.pg_days() * MICROS_PER_DAY + micros)
            .to_be_bytes()
            .to_vec()),
        PyParam::DateTime { .. } => Err(
            "a timezone-aware datetime cannot be encoded as timestamp; pass a naive datetime"
                .to_owned(),
        ),
        _ => Err(mismatch(param, "timestamp")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `timestamptz`.
pub fn encode_timestamptz(param: &PyParam) -> Result<Vec<u8>, String> {
    match param {
        PyParam::DateTime {
            date,
            micros,
            offset: Some(offset),
        } => Ok((date.pg_days() * MICROS_PER_DAY + micros - offset)
            .to_be_bytes()
            .to_vec()),
        PyParam::DateTime { .. } => Err(
            "a naive datetime cannot be encoded as timestamptz; pass an aware datetime".to_owned(),
        ),
        _ => Err(mismatch(param, "timestamptz")),
    }
}

/// # Errors
/// A description of why the value cannot be encoded as `interval`.
pub fn encode_interval(param: &PyParam) -> Result<Vec<u8>, String> {
    let PyParam::Timedelta { days, micros } = param else {
        return Err(mismatch(param, "interval"));
    };
    let days = i32::try_from(*days).map_err(|_| "the timedelta is out of range".to_owned())?;
    let mut out = micros.to_be_bytes().to_vec();
    out.extend_from_slice(&days.to_be_bytes());
    // the months component is always zero
    out.extend_from_slice(&0_i32.to_be_bytes());
    Ok(out)
}
