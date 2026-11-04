// a cargo-fuzz target: encoding a value and decoding it again must give the value back
#![no_main]

use libfuzzer_sys::arbitrary::{self, Unstructured};
use libfuzzer_sys::fuzz_target;
use rowx_postgres::wire::temporal::{self, Date, Interval, Span, TimeOfDay, pg_days};

const MICROS_PER_DAY: i64 = 86_400_000_000;

fn time_parts(micros: i64) -> TimeOfDay {
    let seconds = micros / 1_000_000;
    TimeOfDay {
        hour: u8::try_from(seconds / 3600).unwrap_or(u8::MAX),
        minute: u8::try_from(seconds / 60 % 60).unwrap_or(u8::MAX),
        second: u8::try_from(seconds % 60).unwrap_or(u8::MAX),
        microsecond: u32::try_from(micros % 1_000_000).unwrap_or(u32::MAX),
    }
}

fn date(u: &mut Unstructured<'_>) -> arbitrary::Result<(i32, u32, u32)> {
    let year = u.int_in_range(2..=9998_i32)?;
    let month = u.int_in_range(1..=12_u32)?;
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    let last = match month {
        2 => 28 + u32::from(leap),
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    };
    Ok((year, month, u.int_in_range(1..=last)?))
}

fn check(u: &mut Unstructured<'_>) -> arbitrary::Result<()> {
    let (year, month, day) = date(u)?;
    let want = Date {
        year: u16::try_from(year).unwrap_or(0),
        month: u8::try_from(month).unwrap_or(0),
        day: u8::try_from(day).unwrap_or(0),
    };
    let raw = temporal::encode_date(year, month, day).unwrap_or_default();
    assert_eq!(temporal::decode_date(&raw), Ok(want));

    let micros = u.int_in_range(0..=MICROS_PER_DAY - 1)?;
    assert_eq!(temporal::decode_time(&temporal::encode_time(micros)), Ok(time_parts(micros)));

    // timetz takes the zone in seconds west of UTC and reads it back as seconds east
    let west = u.int_in_range(-57_599..=57_599_i32)?;
    let raw = temporal::encode_timetz(micros, west).unwrap_or_default();
    let decoded = temporal::decode_timetz(&raw);
    assert_eq!(decoded.as_ref().map(|t| t.utc_offset), Ok(-west));
    assert_eq!(decoded.map(|t| t.time), Ok(time_parts(micros)));
    // 24:00:00 encodes but is not a Python time
    assert!(temporal::encode_timetz(MICROS_PER_DAY, west).is_ok());
    assert!(temporal::decode_timetz(&temporal::encode_timetz(MICROS_PER_DAY, west).unwrap_or_default()).is_err());
    assert!(temporal::encode_timetz(-1, west).is_err());
    assert!(temporal::encode_timetz(micros, 57_600).is_err());

    // a timestamp with an offset is the instant in UTC; a naive one has none
    let offset = u.int_in_range(-57_599..=57_599_i64)? * 1_000_000;
    for (ty, shift) in [("timestamp", 0), ("timestamptz", offset)] {
        let raw = temporal::encode_timestamp(year, month, day, micros, shift);
        let decoded = temporal::decode_timestamp(ty, &raw);
        let total = pg_days(year, month, day) * MICROS_PER_DAY + micros - shift;
        let back = decoded.map(|t| {
            let days = pg_days(i32::from(t.date.year), u32::from(t.date.month), u32::from(t.date.day));
            days * MICROS_PER_DAY
                + (i64::from(t.time.hour) * 3600 + i64::from(t.time.minute) * 60 + i64::from(t.time.second))
                    * 1_000_000
                + i64::from(t.time.microsecond)
        });
        assert_eq!(back, Ok(total));
    }

    let days = u.arbitrary::<i32>()?;
    let months = u.arbitrary::<i32>()?;
    let micros = u.arbitrary::<i64>()?;
    let raw = temporal::encode_interval(i64::from(days), micros).unwrap_or_default();
    assert_eq!(temporal::decode_interval(&raw), Ok(Interval { days, microseconds: micros }));
    let raw = temporal::encode_span(i64::from(months), i64::from(days), micros).unwrap_or_default();
    assert_eq!(temporal::decode_span(&raw), Ok(Span { months, days, microseconds: micros }));
    assert!(temporal::encode_span(i64::from(months) + (1 << 32), 0, 0).is_err());
    assert!(temporal::encode_interval(i64::from(days) + (1 << 32), 0).is_err());
    Ok(())
}

fuzz_target!(|data: &[u8]| {
    let _ = check(&mut Unstructured::new(data));
});
