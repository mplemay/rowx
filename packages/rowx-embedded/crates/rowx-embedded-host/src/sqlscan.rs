// A small SQL lexer, just enough to recognise the statements the proxy must treat specially: the ones that reset
// session settings (the engine is one long-lived backend, so a reset must be followed by re-applying the startup
// options) and `COPY ... FROM STDIN`. It skips comments, quoted strings and identifiers and dollar-quoting, ignores
// whitespace differences, and looks at each statement of a multi-statement query on its own.

#[derive(Debug, Clone, PartialEq, Eq)]
enum Token {
    // an unquoted word, upper-cased
    Word(String),
    // a double-quoted identifier, as written between the quotes
    Quoted(String),
    Punct(char),
    // a string, number or anything else that never matters here
    Other,
}

// what a statement does to session settings
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Effect {
    // RESET ALL, DISCARD ALL
    All,
    // RESET name, SET name TO DEFAULT, RESET TIME ZONE, ...: the setting returns to its default (lower-case name)
    Setting(String),
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Analysis {
    pub effects: Vec<Effect>,
    pub copy_from_stdin: bool,
}

const fn is_word_start(byte: u8) -> bool {
    byte.is_ascii_alphabetic() || byte == b'_' || byte >= 0x80
}

const fn is_word_part(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'$' || byte >= 0x80
}

// index just past the closing quote of a quoted run starting at `start` (the opening quote byte)
fn skip_quoted(bytes: &[u8], start: usize, quote: u8, backslash: bool) -> usize {
    let mut at = start + 1;
    while at < bytes.len() {
        match bytes[at] {
            b'\\' if backslash => at += 2,
            byte if byte == quote => {
                if bytes.get(at + 1) == Some(&quote) {
                    at += 2;
                } else {
                    return at + 1;
                }
            }
            _ => at += 1,
        }
    }
    bytes.len()
}

// a `$tag$` opener at `at`, as the bytes between the dollars (possibly empty)
fn dollar_tag(bytes: &[u8], at: usize) -> Option<&[u8]> {
    let rest = bytes.get(at + 1..)?;
    let end = rest.iter().position(|byte| *byte == b'$')?;
    let tag = &rest[..end];
    let valid = tag
        .first()
        .is_none_or(|first| is_word_start(*first) && *first < 0x80)
        && tag.iter().all(|byte| is_word_part(*byte) && *byte != b'$');
    valid.then_some(tag)
}

fn tokenize(sql: &str) -> Vec<Token> {
    let bytes = sql.as_bytes();
    let mut tokens = Vec::new();
    let mut at = 0;
    while at < bytes.len() {
        let byte = bytes[at];
        match byte {
            b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c => at += 1,
            b'-' if bytes.get(at + 1) == Some(&b'-') => {
                at = bytes[at..]
                    .iter()
                    .position(|b| *b == b'\n')
                    .map_or(bytes.len(), |end| at + end + 1);
            }
            b'/' if bytes.get(at + 1) == Some(&b'*') => {
                // block comments nest
                let mut depth = 1_u32;
                at += 2;
                while at < bytes.len() && depth > 0 {
                    if bytes[at] == b'/' && bytes.get(at + 1) == Some(&b'*') {
                        depth += 1;
                        at += 2;
                    } else if bytes[at] == b'*' && bytes.get(at + 1) == Some(&b'/') {
                        depth -= 1;
                        at += 2;
                    } else {
                        at += 1;
                    }
                }
            }
            b'\'' => {
                at = skip_quoted(bytes, at, b'\'', false);
                tokens.push(Token::Other);
            }
            b'"' => {
                let end = skip_quoted(bytes, at, b'"', false);
                // an unterminated identifier runs to the end of the text, which need not end in a quote: only a
                // closing quote is dropped, so the slice never ends inside a multibyte character
                let closed = end > at + 1 && bytes.get(end - 1) == Some(&b'"');
                let inner = &sql[at + 1..if closed { end - 1 } else { end }];
                tokens.push(Token::Quoted(inner.replace("\"\"", "\"")));
                at = end;
            }
            b'$' => {
                if let Some(tag) = dollar_tag(bytes, at) {
                    let opener = tag.len() + 2;
                    let mut closer = Vec::with_capacity(opener);
                    closer.push(b'$');
                    closer.extend_from_slice(tag);
                    closer.push(b'$');
                    let body = &bytes[at + opener..];
                    at = body
                        .windows(closer.len())
                        .position(|window| window == closer.as_slice())
                        .map_or(bytes.len(), |end| at + opener + end + closer.len());
                } else {
                    // a parameter such as $1
                    at += 1;
                    while bytes.get(at).is_some_and(u8::is_ascii_digit) {
                        at += 1;
                    }
                }
                tokens.push(Token::Other);
            }
            byte if is_word_start(byte) => {
                let start = at;
                while bytes.get(at).is_some_and(|b| is_word_part(*b)) {
                    at += 1;
                }
                let word = &sql[start..at];
                // E'...' and the other prefixed strings: the quote follows the prefix directly
                if bytes.get(at) == Some(&b'\'')
                    && matches!(
                        word.to_ascii_uppercase().as_str(),
                        "E" | "B" | "X" | "N" | "U&"
                    )
                {
                    let escapes = word.eq_ignore_ascii_case("e");
                    at = skip_quoted(bytes, at, b'\'', escapes);
                    tokens.push(Token::Other);
                } else {
                    tokens.push(Token::Word(word.to_ascii_uppercase()));
                }
            }
            byte if byte.is_ascii_digit() => {
                while bytes
                    .get(at)
                    .is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'.')
                {
                    at += 1;
                }
                tokens.push(Token::Other);
            }
            byte if byte.is_ascii() => {
                tokens.push(Token::Punct(char::from(byte)));
                at += 1;
            }
            _ => at += 1,
        }
    }
    tokens
}

// a setting name made of words and dots (`work_mem`, `my.setting`), and how many tokens it used
fn setting_name(tokens: &[Token]) -> Option<(String, usize)> {
    let mut name = String::new();
    let mut used = 0;
    loop {
        match tokens.get(used)? {
            Token::Word(word) | Token::Quoted(word) => name.push_str(&word.to_ascii_lowercase()),
            _ => return None,
        }
        used += 1;
        if tokens.get(used) == Some(&Token::Punct('.'))
            && matches!(
                tokens.get(used + 1),
                Some(Token::Word(_) | Token::Quoted(_))
            )
        {
            name.push('.');
            used += 1;
        } else {
            return Some((name, used));
        }
    }
}

fn is_word(token: Option<&Token>, expected: &str) -> bool {
    matches!(token, Some(Token::Word(word)) if word == expected)
}

fn effect_of(statement: &[Token]) -> Option<Effect> {
    let rest = statement.get(1..)?;
    match statement.first()? {
        Token::Word(first) if first == "DISCARD" => {
            is_word(rest.first(), "ALL").then_some(Effect::All)
        }
        Token::Word(first) if first == "RESET" => {
            if is_word(rest.first(), "ALL") && rest.len() == 1 {
                return Some(Effect::All);
            }
            if is_word(rest.first(), "TIME") && is_word(rest.get(1), "ZONE") {
                return Some(Effect::Setting("timezone".into()));
            }
            if is_word(rest.first(), "SESSION") && is_word(rest.get(1), "AUTHORIZATION") {
                return Some(Effect::Setting("session_authorization".into()));
            }
            let (name, _) = setting_name(rest)?;
            Some(Effect::Setting(name))
        }
        Token::Word(first) if first == "SET" => {
            // SET LOCAL never touches the session value
            let rest = if is_word(rest.first(), "SESSION") {
                rest.get(1..)?
            } else {
                rest
            };
            if is_word(rest.first(), "LOCAL") {
                return None;
            }
            if is_word(rest.first(), "TIME") && is_word(rest.get(1), "ZONE") {
                return (is_word(rest.get(2), "DEFAULT") && rest.len() == 3)
                    .then(|| Effect::Setting("timezone".into()));
            }
            if is_word(rest.first(), "AUTHORIZATION") {
                return (is_word(rest.get(1), "DEFAULT") && rest.len() == 2)
                    .then(|| Effect::Setting("session_authorization".into()));
            }
            let (name, used) = setting_name(rest)?;
            let value = rest.get(used..)?;
            let separator = matches!(value.first(), Some(Token::Punct('=') | Token::Word(_)));
            let is_to = is_word(value.first(), "TO") || value.first() == Some(&Token::Punct('='));
            (separator && is_to && is_word(value.get(1), "DEFAULT") && value.len() == 2)
                .then_some(Effect::Setting(name))
        }
        _ => None,
    }
}

// `COPY ... FROM STDIN` (with the FROM at the top level, not inside a parenthesised query)
fn is_copy_from_stdin(statement: &[Token]) -> bool {
    if !is_word(statement.first(), "COPY") {
        return false;
    }
    let mut depth = 0_i32;
    for (index, token) in statement.iter().enumerate() {
        match token {
            Token::Punct('(') => depth += 1,
            Token::Punct(')') => depth -= 1,
            Token::Word(word)
                if word == "FROM" && depth == 0 && is_word(statement.get(index + 1), "STDIN") =>
            {
                return true;
            }
            _ => {}
        }
    }
    false
}

/// What the statements of a simple query (or a Parse message's query) do to session settings, and whether any of
/// them starts a copy-in.
#[must_use]
pub fn analyze(sql: &str) -> Analysis {
    let mut analysis = Analysis::default();
    for statement in tokenize(sql).split(|token| *token == Token::Punct(';')) {
        if let Some(effect) = effect_of(statement) {
            analysis.effects.push(effect);
        }
        analysis.copy_from_stdin |= is_copy_from_stdin(statement);
    }
    analysis
}

#[cfg(test)]
mod tests {
    use super::*;

    fn effects(sql: &str) -> Vec<Effect> {
        analyze(sql).effects
    }

    fn setting(name: &str) -> Effect {
        Effect::Setting(name.to_owned())
    }

    #[test]
    fn reset_all_and_discard_all_are_recognised_whatever_the_whitespace_or_case() {
        for sql in [
            "RESET ALL",
            "reset all",
            "RESET\nALL",
            "RESET\tALL",
            "  reset   all ;",
            "DISCARD\nALL",
            "discard all",
        ] {
            assert_eq!(effects(sql), vec![Effect::All], "{sql:?}");
        }
    }

    #[test]
    fn unterminated_text_that_ends_in_a_multibyte_character_is_scanned_to_its_end() {
        for sql in [
            "SELECT \"é",
            "SELECT \"",
            "SELECT \"é\"\"",
            "SELECT 'é",
            "SELECT E'é\\",
            "SELECT $é$é",
            "SELECT $$é",
            "SELECT 1 /* é",
            "SELECT 1 -- é",
            "é",
        ] {
            assert_eq!(effects(sql), vec![], "{sql:?}");
        }
        assert_eq!(effects("RESET ALL; SELECT \"é"), vec![Effect::All]);
    }

    #[test]
    fn strings_comments_and_identifiers_never_count() {
        for sql in [
            "SELECT 'reset all'",
            "SELECT 'reset '",
            "SELECT 1 AS preset",
            "SELECT 1 /* discard all */",
            "SELECT 1 -- reset all",
            "SELECT \"reset all\"",
            "SELECT $$ reset all $$",
            "SELECT $q$ discard all; reset all $q$",
            "SELECT E'it\\'s; reset all'",
            "/* a /* nested */ reset all */ SELECT 1",
            "SELECT preset FROM discards",
        ] {
            assert_eq!(effects(sql), Vec::<Effect>::new(), "{sql:?}");
        }
    }

    #[test]
    fn a_comment_before_the_statement_does_not_hide_it() {
        assert_eq!(effects("/* c */ RESET ALL"), vec![Effect::All]);
        assert_eq!(effects("-- c\nDISCARD ALL"), vec![Effect::All]);
    }

    #[test]
    fn each_statement_of_a_multi_statement_query_is_looked_at() {
        assert_eq!(
            effects("SET work_mem = '1MB'; RESET ALL"),
            vec![Effect::All]
        );
        assert_eq!(
            effects("RESET a; SELECT 'x;y'; RESET b"),
            vec![setting("a"), setting("b")]
        );
    }

    #[test]
    fn single_setting_resets_are_recognised() {
        assert_eq!(effects("RESET work_mem"), vec![setting("work_mem")]);
        assert_eq!(effects("RESET \"Work_Mem\""), vec![setting("work_mem")]);
        assert_eq!(effects("RESET my.setting"), vec![setting("my.setting")]);
        assert_eq!(effects("RESET TIME ZONE"), vec![setting("timezone")]);
        assert_eq!(
            effects("RESET SESSION AUTHORIZATION"),
            vec![setting("session_authorization")]
        );
        assert_eq!(
            effects("SET work_mem TO DEFAULT"),
            vec![setting("work_mem")]
        );
        assert_eq!(
            effects("set session work_mem = default"),
            vec![setting("work_mem")]
        );
        assert_eq!(effects("SET TIME ZONE DEFAULT"), vec![setting("timezone")]);
        assert_eq!(
            effects("SET SESSION AUTHORIZATION DEFAULT"),
            vec![setting("session_authorization")]
        );
    }

    #[test]
    fn sets_that_do_not_return_to_the_default_are_not_effects() {
        for sql in [
            "SET work_mem = '9MB'",
            "SET work_mem TO 'default'",
            "SET LOCAL work_mem TO DEFAULT",
            "SET work_mem TO DEFAULT, 1",
            "SELECT set_config('work_mem', NULL, false)",
            "SET TIME ZONE 'UTC'",
        ] {
            assert_eq!(effects(sql), Vec::<Effect>::new(), "{sql:?}");
        }
    }

    #[test]
    fn copy_from_stdin_is_recognised_only_at_the_top_level() {
        for sql in [
            "COPY t FROM STDIN",
            "copy t (a, b) from stdin with (format csv)",
            "COPY\nt\nFROM\nSTDIN",
            "/* c */ COPY t FROM STDIN; SELECT 1",
        ] {
            assert!(analyze(sql).copy_from_stdin, "{sql:?}");
        }
        for sql in [
            "COPY t TO STDOUT",
            "COPY t FROM '/tmp/file'",
            "COPY (SELECT * FROM stdin) TO STDOUT",
            "COPY (SELECT 1 FROM stdin) TO STDOUT",
            "SELECT 'COPY t FROM STDIN'",
            "SELECT * FROM copy_log WHERE x = 'from stdin'",
        ] {
            assert!(!analyze(sql).copy_from_stdin, "{sql:?}");
        }
    }

    #[test]
    fn unterminated_input_never_loops_or_panics() {
        for sql in [
            "SELECT 'abc",
            "SELECT \"abc",
            "/* open",
            "SELECT $$ open",
            "SELECT $",
            "SELECT $1",
            "",
            ";;;",
        ] {
            let _ = analyze(sql);
        }
    }
}
