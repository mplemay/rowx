// the few backend messages the front proxy builds itself, plus response scanning

fn message(tag: u8, body: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(body.len() + 5);
    out.push(tag);
    let len = i32::try_from(body.len() + 4).unwrap_or(i32::MAX);
    out.extend_from_slice(&len.to_be_bytes());
    out.extend_from_slice(body);
    out
}

fn cstr(out: &mut Vec<u8>, value: &str) {
    out.extend_from_slice(value.as_bytes());
    out.push(0);
}

#[must_use]
pub fn error_response(severity: &str, code: &str, text: &str, hint: Option<&str>) -> Vec<u8> {
    let mut body = Vec::new();
    for (tag, value) in [
        (b'S', severity),
        (b'V', severity),
        (b'C', code),
        (b'M', text),
    ] {
        body.push(tag);
        cstr(&mut body, value);
    }
    if let Some(hint) = hint {
        body.push(b'H');
        cstr(&mut body, hint);
    }
    body.push(0);
    message(b'E', &body)
}

#[must_use]
pub fn startup_response(parameters: &[(String, String)], key: i32) -> Vec<u8> {
    let mut out = message(b'R', &0_i32.to_be_bytes());
    for (name, value) in parameters {
        let mut body = Vec::new();
        cstr(&mut body, name);
        cstr(&mut body, value);
        out.extend(message(b'S', &body));
    }
    let mut body = Vec::new();
    body.extend_from_slice(&1_i32.to_be_bytes());
    body.extend_from_slice(&key.to_be_bytes());
    out.extend(message(b'K', &body));
    out.extend(message(b'Z', b"I"));
    out
}

#[must_use]
pub fn simple_query(sql: &str) -> Vec<u8> {
    let mut body = Vec::new();
    cstr(&mut body, sql);
    message(b'Q', &body)
}

// splits a backend response into (tag, body) messages; stops at the first malformed frame
#[must_use]
pub fn messages(response: &[u8]) -> Vec<(u8, &[u8])> {
    let mut out = Vec::new();
    let mut cursor = 0;
    while cursor + 5 <= response.len() {
        let Ok(len_bytes) = <[u8; 4]>::try_from(&response[cursor + 1..cursor + 5]) else {
            break;
        };
        let Ok(len) = usize::try_from(i32::from_be_bytes(len_bytes)) else {
            break;
        };
        let end = cursor + 1 + len;
        if len < 4 || end > response.len() {
            break;
        }
        out.push((response[cursor], &response[cursor + 5..end]));
        cursor = end;
    }
    out
}

#[must_use]
pub fn has_error(response: &[u8]) -> bool {
    messages(response).iter().any(|(tag, _)| *tag == b'E')
}

// the text columns of every DataRow
#[must_use]
pub fn data_rows(response: &[u8]) -> Vec<Vec<Option<String>>> {
    let mut rows = Vec::new();
    for (tag, body) in messages(response) {
        if tag != b'D' || body.len() < 2 {
            continue;
        }
        let columns = usize::from(u16::from_be_bytes([body[0], body[1]]));
        let mut cursor = 2;
        let mut row = Vec::with_capacity(columns);
        for _ in 0..columns {
            let Some(len_bytes) = body.get(cursor..cursor + 4) else {
                break;
            };
            let Ok(len_bytes) = <[u8; 4]>::try_from(len_bytes) else {
                break;
            };
            cursor += 4;
            let len = i32::from_be_bytes(len_bytes);
            match usize::try_from(len) {
                Ok(len) => {
                    let value = body
                        .get(cursor..cursor + len)
                        .map(|v| String::from_utf8_lossy(v).into_owned());
                    row.push(value);
                    cursor += len;
                }
                Err(_) => row.push(None),
            }
        }
        rows.push(row);
    }
    rows
}

// (SQLSTATE, message) of the first ErrorResponse
#[must_use]
pub fn error_parts(response: &[u8]) -> Option<(String, String)> {
    let (_, body) = messages(response)
        .into_iter()
        .find(|(tag, _)| *tag == b'E')?;
    let mut code = String::new();
    let mut text = String::new();
    for field in body.split(|b| *b == 0) {
        match field.split_first() {
            Some((b'C', value)) => code = String::from_utf8_lossy(value).into_owned(),
            Some((b'M', value)) => text = String::from_utf8_lossy(value).into_owned(),
            _ => {}
        }
    }
    Some((code, text))
}

// `-c k=v`, `-ck=v` and `--k=v` out of a startup `options` value, honouring backslash-escaped spaces
#[must_use]
pub fn parse_options(options: &str) -> Vec<(String, String)> {
    let mut tokens: Vec<String> = Vec::new();
    let mut current = String::new();
    let mut in_token = false;
    let mut chars = options.chars();
    while let Some(c) = chars.next() {
        match c {
            '\\' => {
                if let Some(next) = chars.next() {
                    current.push(next);
                    in_token = true;
                }
            }
            c if c.is_whitespace() => {
                if in_token {
                    tokens.push(std::mem::take(&mut current));
                    in_token = false;
                }
            }
            c => {
                current.push(c);
                in_token = true;
            }
        }
    }
    if in_token {
        tokens.push(current);
    }
    let mut out = Vec::new();
    let mut iter = tokens.into_iter();
    while let Some(token) = iter.next() {
        let assignment = if token == "-c" {
            iter.next()
        } else if let Some(rest) = token.strip_prefix("-c") {
            Some(rest.to_owned())
        } else {
            token.strip_prefix("--").map(str::to_owned)
        };
        if let Some((name, value)) = assignment.as_deref().and_then(|a| a.split_once('=')) {
            out.push((name.replace('-', "_"), value.to_owned()));
        }
    }
    out
}

#[must_use]
pub fn quote_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[must_use]
pub fn sync() -> Vec<u8> {
    message(b'S', &[])
}

// NegotiateProtocolVersion: the newest minor version of protocol 3 this server speaks, and the options it ignored
#[must_use]
pub fn negotiate_protocol_version(unrecognized: &[&str]) -> Vec<u8> {
    let mut body = Vec::new();
    body.extend_from_slice(&0_i32.to_be_bytes());
    body.extend_from_slice(&i32::try_from(unrecognized.len()).unwrap_or(0).to_be_bytes());
    for name in unrecognized {
        cstr(&mut body, name);
    }
    message(b'v', &body)
}

// the text of a simple Query message (a frame with its tag and length)
#[must_use]
pub fn query_text(frame: &[u8]) -> Option<std::borrow::Cow<'_, str>> {
    let body = frame.get(5..)?;
    let end = body
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(body.len());
    Some(String::from_utf8_lossy(&body[..end]))
}

// the query text of a Parse message: `name\0query\0 ...`
#[must_use]
pub fn parse_text(frame: &[u8]) -> Option<std::borrow::Cow<'_, str>> {
    let body = frame.get(5..)?;
    let name_end = body.iter().position(|byte| *byte == 0)?;
    let rest = &body[name_end + 1..];
    let end = rest
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(rest.len());
    Some(String::from_utf8_lossy(&rest[..end]))
}

// the first two NUL-terminated strings of a frame's body: Parse `name, query`, Bind `portal, statement`, Execute
// `portal`, and so on
#[must_use]
pub fn leading_strings(frame: &[u8]) -> Option<(&[u8], &[u8])> {
    let body = frame.get(5..)?;
    let first_end = body.iter().position(|byte| *byte == 0)?;
    let rest = &body[first_end + 1..];
    let second_end = rest
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(rest.len());
    Some((&body[..first_end], &rest[..second_end]))
}

/// Follows the backend messages of a streamed response, whatever the chunk boundaries.
///
/// It keeps the transaction status of the last `ReadyForQuery`, how many there were, whether an `ErrorResponse` went
/// by, and the body of a `CopyInResponse`.
#[derive(Debug, Default)]
pub struct ResponseScan {
    pending: Vec<u8>,
    pub ready_status: Option<u8>,
    pub ready_count: usize,
    pub errors: usize,
    pub copy_in: Option<Vec<u8>>,
}

impl ResponseScan {
    pub fn feed(&mut self, chunk: &[u8]) {
        self.pending.extend_from_slice(chunk);
        let mut cursor = 0;
        while let Some(header) = self.pending.get(cursor..cursor + 5) {
            let Ok(length_bytes) = <[u8; 4]>::try_from(&header[1..5]) else {
                break;
            };
            let Ok(length) = usize::try_from(i32::from_be_bytes(length_bytes)) else {
                // not a protocol stream: stop following it
                cursor = self.pending.len();
                break;
            };
            let end = cursor + 1 + length;
            if length < 4 || end > self.pending.len() {
                if length < 4 {
                    cursor = self.pending.len();
                }
                break;
            }
            let tag = header[0];
            let body = &self.pending[cursor + 5..end];
            match tag {
                b'Z' => {
                    self.ready_status = body.first().copied();
                    self.ready_count += 1;
                }
                b'E' => self.errors += 1,
                b'G' => self.copy_in = Some(body.to_vec()),
                _ => {}
            }
            cursor = end;
        }
        self.pending.drain(..cursor);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn frame(tag: u8, body: &[u8]) -> Vec<u8> {
        message(tag, body)
    }

    #[test]
    fn the_scan_follows_messages_across_arbitrary_chunk_boundaries() {
        let mut stream = frame(b'T', b"abc");
        stream.extend(frame(b'E', b"S\0"));
        stream.extend(frame(b'G', &[0, 0, 1, 0, 0]));
        stream.extend(frame(b'Z', b"T"));
        stream.extend(frame(b'Z', b"I"));
        for step in [1, 2, 3, 5, 7, stream.len()] {
            let mut scan = ResponseScan::default();
            for chunk in stream.chunks(step) {
                scan.feed(chunk);
            }
            assert_eq!(scan.ready_status, Some(b'I'), "step {step}");
            assert_eq!(scan.ready_count, 2);
            assert_eq!(scan.errors, 1);
            assert_eq!(scan.copy_in, Some(vec![0, 0, 1, 0, 0]));
        }
    }

    #[test]
    fn the_scan_drops_a_corrupt_frame_and_carries_on() {
        let mut scan = ResponseScan::default();
        scan.feed(&[b'Z', 0, 0, 0, 1, 9, 9]);
        scan.feed(&frame(b'Z', b"I"));
        assert_eq!(scan.ready_count, 1);
    }

    #[test]
    fn query_and_parse_text_are_extracted_without_the_terminator() {
        assert_eq!(
            query_text(&frame(b'Q', b"select 1\0")).as_deref(),
            Some("select 1")
        );
        assert_eq!(
            parse_text(&frame(b'P', b"name\0select 2\0\0\0")).as_deref(),
            Some("select 2")
        );
        assert_eq!(parse_text(&frame(b'P', b"")), None);
    }

    #[test]
    fn negotiation_lists_the_ignored_options() {
        let message = negotiate_protocol_version(&["_pq_.x"]);
        assert_eq!(message[0], b'v');
        assert!(message.ends_with(b"_pq_.x\0"));
    }

    #[test]
    fn options_parse() {
        assert_eq!(
            parse_options(
                "-c search_path=a,b -c statement_timeout=5s --work-mem=1MB -capplication_name=x\\ y"
            ),
            vec![
                ("search_path".to_owned(), "a,b".to_owned()),
                ("statement_timeout".to_owned(), "5s".to_owned()),
                ("work_mem".to_owned(), "1MB".to_owned()),
                ("application_name".to_owned(), "x y".to_owned()),
            ]
        );
    }
}
