//! Recovers the nullability and storage class of result columns from the `EXPLAIN` bytecode.
//!
//! The bytecode of a prepared statement is read and register values are followed to the result
//! columns, for expressions that SQLite reports no declared type or origin for.
//!
//! The analysis is sound or silent. It is an abstract interpretation of the program: every register
//! holds a [`Val`] (may it be NULL, which storage classes can it have), every cursor a [`Cursor`], and
//! both successors of every conditional jump are explored until the states stop growing. An opcode the
//! analysis does not model, an `EXPLAIN` failure, a step bound or a program with no `ResultRow` makes
//! [`analyze`] return a [`Silent`] error, and the caller reports nothing. `EXPLAIN` opcodes are not
//! stable between SQLite versions, so [`REVIEWED_SQLITE_VERSION`] pins the version the list in
//! [`MODELED_OPCODES`] was reviewed against.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::rc::Rc;

use rusqlite::Connection;
use rusqlite::types::Value;
use thiserror::Error;

use crate::describe::quote;

/// The SQLite version whose `EXPLAIN` output the modelled opcodes were reviewed against. A test fails
/// when the bundled version differs, so a bump has to review [`MODELED_OPCODES`].
#[cfg(test)]
const REVIEWED_SQLITE_VERSION: &str = "3.51.3";

const MAX_PROGRAM: usize = 20_000;
const MAX_STEPS: usize = 400_000;

const INT: u8 = 1;
const REAL: u8 = 2;
const TEXT: u8 = 4;
const BLOB: u8 = 8;

// the rows a cursor can be on: a real row, or the all-NULL row of an outer join's missing side
const ROW: u8 = 1;
const NULL_ROW: u8 = 2;

// the flags of a comparison's P5 are irrelevant to where it jumps: both successors are explored

macro_rules! opcodes {
    ($($name:ident),* $(,)?) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        enum Op {
            $($name),*
        }

        /// Every opcode the analysis models. A statement that reaches any other opcode is analysed to
        /// nothing.
        const MODELED_OPCODES: &[&str] = &[$(stringify!($name)),*];

        impl Op {
            const ALL: &'static [Self] = &[$(Self::$name),*];

            fn parse(name: &str) -> Option<Self> {
                MODELED_OPCODES
                    .iter()
                    .position(|known| *known == name)
                    .and_then(|index| Self::ALL.get(index).copied())
            }
        }
    };
}

opcodes! {
    Init, Goto, Halt, Transaction, Noop, Once, Close,
    OpenRead, OpenPseudo, SorterOpen,
    Rewind, Last, Next, Prev, SorterSort, SorterNext, SorterInsert, SorterData,
    SeekGE, SeekGT, SeekLE, SeekLT, SeekRowid, NotExists, DeferredSeek,
    IdxGE, IdxGT, IdxLE, IdxLT,
    Column, Rowid, IdxRowid, NullRow, IfNullRow, Count,
    Integer, Int64, Real, String8, Blob, Null, Variable,
    Copy, SCopy, Move,
    NotNull, IsNull, If, IfNot, IsTrue, Not, And, Or, ZeroOrNull,
    Eq, Ne, Lt, Le, Gt, Ge, Compare, Jump,
    Add, Subtract, Multiply, Divide, Remainder, Concat, AddImm,
    BitAnd, BitOr, ShiftLeft, ShiftRight, BitNot,
    Cast, Affinity, RealAffinity, CollSeq, ClrSubtype,
    Function, AggStep, AggFinal,
    MakeRecord, ResultRow,
    Gosub, Return, MustBeInt, IfPos, DecrJumpZero, OffsetLimit,
    Filter, FilterAdd,
}

/// Why the analysis reports nothing. The caller treats every one the same; the text is for tests and
/// debugging.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum Silent {
    #[error("EXPLAIN failed: {0}")]
    Explain(String),
    #[error("the statement is itself an EXPLAIN")]
    ExplainStatement,
    #[error("opcode {0} is not modelled")]
    Unmodelled(String),
    #[error("jump target {0} is outside the program")]
    Target(i64),
    #[error("the program is too large or too slow to analyse")]
    Bound,
    #[error("the program never reaches a result row")]
    NoResult,
    #[error("a return address is not known")]
    ReturnAddress,
    #[error("the listing is not numbered from zero")]
    Listing,
    #[error("the result rows disagree on their width")]
    Columns,
}

/// A storage class.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    Integer,
    Real,
    Text,
    Blob,
}

impl Class {
    /// The class name, as SQLite spells a column type.
    #[must_use]
    pub const fn name(self) -> &'static str {
        match self {
            Self::Integer => "INTEGER",
            Self::Real => "REAL",
            Self::Text => "TEXT",
            Self::Blob => "BLOB",
        }
    }
}

/// What the bytecode proves about one result column.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Fact {
    /// No path reaching a result row leaves the column NULL.
    pub non_null: bool,
    /// The one storage class every non-NULL value has, when it is certain.
    pub class: Option<Class>,
    /// The column is a plain read of a base table column, which was never null-extended.
    pub pass_through: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Val {
    /// The value may be NULL.
    null: bool,
    /// The storage classes the value may have when it is not NULL.
    classes: u8,
    /// The value may have a class that is not tracked.
    wild: bool,
    /// The base table column the value is exactly a read of, from a row that exists.
    origin: Option<Rc<Origin>>,
    /// The value is a record (`MakeRecord`, a sorter row), with its fields.
    rec: Option<Rc<Vec<Self>>>,
}

impl Val {
    const fn base(null: bool, classes: u8, wild: bool) -> Self {
        Self {
            null,
            classes,
            wild,
            origin: None,
            rec: None,
        }
    }

    const fn null() -> Self {
        Self::base(true, 0, false)
    }

    const fn unknown() -> Self {
        Self::base(true, 0, true)
    }

    const fn of(classes: u8) -> Self {
        Self::base(false, classes, false)
    }

    const fn maybe(classes: u8, null: bool) -> Self {
        Self::base(null, classes, false)
    }

    fn join(&self, other: &Self) -> Self {
        let rec = match (&self.rec, &other.rec) {
            (Some(a), Some(b)) if a.len() == b.len() => Some(Rc::new(
                a.iter().zip(b.iter()).map(|(x, y)| x.join(y)).collect(),
            )),
            _ => None,
        };
        Self {
            null: self.null || other.null,
            classes: self.classes | other.classes,
            wild: self.wild || other.wild,
            origin: match (&self.origin, &other.origin) {
                (Some(a), Some(b)) if a == b => Some(Rc::clone(a)),
                _ => None,
            },
            rec,
        }
    }

    // the value is never anything but NULL
    const fn only_null(&self) -> bool {
        self.null && self.classes == 0 && !self.wild
    }
}

// A base table column, named by its table and column rather than by the cursor that reads it. A cid of -1 is
// the rowid.
#[derive(Debug, PartialEq, Eq)]
struct Origin {
    db: String,
    table: String,
    cid: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Slot {
    not_null: bool,
    classes: u8,
    wild: bool,
    origin: Option<Rc<Origin>>,
}

impl Slot {
    fn value(&self) -> Val {
        Val {
            null: !self.not_null,
            classes: self.classes,
            wild: self.wild,
            origin: self.origin.clone().filter(|_| self.not_null),
            rec: None,
        }
    }
}

/// How the stored record of a table or index maps onto its columns.
#[derive(Debug, Clone, PartialEq, Eq)]
struct Layout {
    db: String,
    table: String,
    slots: Vec<Option<Slot>>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Rows {
    Empty,
    Known(Vec<Val>),
    Unknown,
}

impl Rows {
    fn join(&self, other: &Self) -> Self {
        match (self, other) {
            (Self::Empty, rest) | (rest, Self::Empty) => rest.clone(),
            (Self::Known(a), Self::Known(b)) if a.len() == b.len() => {
                Self::Known(a.iter().zip(b).map(|(x, y)| x.join(y)).collect())
            }
            _ => Self::Unknown,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Kind {
    Stored(Rc<Layout>),
    /// A cursor over the record in a register.
    Pseudo(i64),
    Sorter(Rows),
    Opaque,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct Cursor {
    kind: Kind,
    rows: u8,
}

impl Cursor {
    fn join(&self, other: &Self) -> Self {
        let kind = match (&self.kind, &other.kind) {
            (Kind::Sorter(a), Kind::Sorter(b)) => Kind::Sorter(a.join(b)),
            (a, b) if a == b => a.clone(),
            _ => Kind::Opaque,
        };
        Self {
            kind,
            rows: self.rows | other.rows,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct State {
    regs: BTreeMap<i64, Val>,
    cursors: BTreeMap<i64, Cursor>,
    /// Where each `Gosub` return address register returns to.
    returns: BTreeMap<i64, BTreeSet<usize>>,
}

impl State {
    // a register that was never written holds NULL
    fn get(&self, reg: i64) -> Val {
        self.regs.get(&reg).cloned().unwrap_or_else(Val::null)
    }

    fn set(&mut self, reg: i64, value: Val) {
        self.returns.remove(&reg);
        if value == Val::null() {
            self.regs.remove(&reg);
        } else {
            self.regs.insert(reg, value);
        }
    }

    fn set_range_null(&mut self, first: i64, last: i64) {
        for reg in first..=last.max(first) {
            self.set(reg, Val::null());
        }
    }

    fn set_rows(&mut self, cursor: i64, rows: u8) {
        self.cursors
            .entry(cursor)
            .and_modify(|found| found.rows = rows)
            .or_insert(Cursor {
                kind: Kind::Opaque,
                rows,
            });
    }

    // joins `other` into the state; true when this state grew
    fn absorb(&mut self, other: &Self) -> bool {
        let before = self.clone();
        let keys: BTreeSet<i64> = self.regs.keys().chain(other.regs.keys()).copied().collect();
        for key in keys {
            let joined = self.get(key).join(&other.get(key));
            if joined == Val::null() {
                self.regs.remove(&key);
            } else {
                self.regs.insert(key, joined);
            }
        }
        for (key, cursor) in &other.cursors {
            let joined = self
                .cursors
                .get(key)
                .map_or_else(|| cursor.clone(), |mine| mine.join(cursor));
            self.cursors.insert(*key, joined);
        }
        for (key, addrs) in &other.returns {
            self.returns
                .entry(*key)
                .or_default()
                .extend(addrs.iter().copied());
        }
        *self != before
    }
}

#[derive(Debug, Clone)]
struct Insn {
    op: Option<Op>,
    name: String,
    p1: i64,
    p2: i64,
    p3: i64,
    p4: String,
}

type Edge = (usize, State);

const fn int(value: &Value) -> i64 {
    match value {
        Value::Integer(number) => *number,
        _ => 0,
    }
}

fn text(value: &Value) -> String {
    match value {
        Value::Text(text) => text.clone(),
        _ => String::new(),
    }
}

fn read_program(conn: &Connection, sql: &str) -> Result<Vec<Insn>, Silent> {
    let explain = |err: rusqlite::Error| Silent::Explain(err.to_string());
    let mut stmt = conn.prepare(&format!("EXPLAIN {sql}")).map_err(explain)?;
    if stmt.is_explain() != 1 {
        return Err(Silent::ExplainStatement);
    }
    // EXPLAIN lists the bytecode without running it, and the parameters it would read stay unbound
    let mut rows = stmt.raw_query();
    let mut program = Vec::new();
    while let Some(row) = rows.next().map_err(explain)? {
        let column = |index: usize| row.get::<_, Value>(index).map_err(explain);
        let name = text(&column(1)?);
        if int(&column(0)?) != i64::try_from(program.len()).unwrap_or(-1) {
            return Err(Silent::Listing);
        }
        program.push(Insn {
            op: Op::parse(&name),
            name,
            p1: int(&column(2)?),
            p2: int(&column(3)?),
            p3: int(&column(4)?),
            p4: text(&column(5)?),
        });
        if program.len() > MAX_PROGRAM {
            return Err(Silent::Bound);
        }
    }
    Ok(program)
}

fn declared_class(declared: &str, strict: bool) -> (u8, bool) {
    if !strict {
        return (0, true);
    }
    match declared.trim().to_ascii_uppercase().as_str() {
        "INT" | "INTEGER" => (INT, false),
        "REAL" => (REAL, false),
        "TEXT" => (TEXT, false),
        "BLOB" => (BLOB, false),
        _ => (0, true),
    }
}

struct TableColumn {
    cid: i64,
    declared: String,
    not_null: bool,
    hidden: i64,
}

fn table_columns(conn: &Connection, db: &str, table: &str) -> rusqlite::Result<Vec<TableColumn>> {
    let mut columns = Vec::new();
    // table_xinfo columns are cid, name, type, notnull, dflt_value, pk, hidden
    conn.pragma(Some(db), "table_xinfo", table, |row| {
        columns.push(TableColumn {
            cid: row.get(0)?,
            declared: row.get(2)?,
            not_null: row.get::<_, i64>(3)? != 0,
            hidden: row.get(6)?,
        });
        Ok(())
    })?;
    Ok(columns)
}

fn origin(db: &str, table: &str, cid: i64) -> Rc<Origin> {
    Rc::new(Origin {
        db: db.to_owned(),
        table: table.to_owned(),
        cid,
    })
}

fn table_slot(db: &str, table: &str, column: &TableColumn, strict: bool) -> Slot {
    let (classes, wild) = declared_class(&column.declared, strict);
    Slot {
        not_null: column.not_null,
        classes,
        wild,
        origin: Some(origin(db, table, column.cid)),
    }
}

// The layout of the b-tree rooted at page `root` of database `db`, or `None` when it cannot be mapped
// with certainty: a WITHOUT ROWID table, a view or virtual table, a root page the schema does not list.
fn load_layout(conn: &Connection, db: &str, root: i64) -> rusqlite::Result<Option<Layout>> {
    let sql = format!(
        "SELECT type, name, tbl_name FROM {}.sqlite_schema WHERE rootpage = ?1",
        quote(db)
    );
    let mut stmt = conn.prepare(&sql)?;
    let found: Vec<(String, String, String)> = stmt
        .query_map([root], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let [(kind, name, table)] = found.as_slice() else {
        return Ok(None);
    };
    // table_list columns are schema, name, type, ncol, wr, strict
    let mut listed = Vec::new();
    conn.pragma(Some(db), "table_list", table.as_str(), |row| {
        listed.push((
            row.get::<_, String>(2)?,
            row.get::<_, i64>(4)? != 0,
            row.get::<_, i64>(5)? != 0,
        ));
        Ok(())
    })?;
    let [(table_kind, without_rowid, strict)] = listed.as_slice() else {
        return Ok(None);
    };
    if table_kind != "table" || *without_rowid {
        return Ok(None);
    }
    let columns = table_columns(conn, db, table)?;
    let slots = match kind.as_str() {
        // the record holds the stored columns in declaration order, and a virtual generated column is not stored
        "table" => columns
            .iter()
            .filter(|column| matches!(column.hidden, 0 | 3))
            .map(|column| Some(table_slot(db, table, column, *strict)))
            .collect(),
        "index" => {
            let mut slots = Vec::new();
            // index_xinfo columns are seqno, cid, name, desc, coll, key; the rowid is the last entry
            conn.pragma(Some(db), "index_xinfo", name.as_str(), |row| {
                let cid: i64 = row.get(1)?;
                slots.push(match cid {
                    -1 => Some(Slot {
                        not_null: true,
                        classes: INT,
                        wild: false,
                        origin: Some(origin(db, table, -1)),
                    }),
                    cid if cid >= 0 => columns
                        .iter()
                        .find(|column| column.cid == cid)
                        .map(|column| table_slot(db, table, column, *strict)),
                    _ => None,
                });
                Ok(())
            })?;
            slots
        }
        _ => return Ok(None),
    };
    Ok(Some(Layout {
        db: db.to_owned(),
        table: table.clone(),
        slots,
    }))
}

fn database_name(conn: &Connection, index: i64) -> rusqlite::Result<Option<String>> {
    let mut found = None;
    // database_list columns are seq, name, file
    conn.pragma_query(None, "database_list", |row| {
        if row.get::<_, i64>(0)? == index {
            found = Some(row.get::<_, String>(1)?);
        }
        Ok(())
    })?;
    Ok(found)
}

fn read_layouts(conn: &Connection, program: &[Insn]) -> HashMap<usize, Rc<Layout>> {
    let mut cache: HashMap<(i64, i64), Option<Rc<Layout>>> = HashMap::new();
    let mut layouts = HashMap::new();
    for (pc, insn) in program.iter().enumerate() {
        if insn.op != Some(Op::OpenRead) {
            continue;
        }
        let key = (insn.p3, insn.p2);
        let layout = cache
            .entry(key)
            .or_insert_with(|| {
                let name = database_name(conn, insn.p3).ok().flatten()?;
                load_layout(conn, &name, insn.p2)
                    .ok()
                    .flatten()
                    .map(Rc::new)
            })
            .clone();
        if let Some(layout) = layout {
            layouts.insert(pc, layout);
        }
    }
    layouts
}

// `name(count)`, as EXPLAIN prints a function in P4
fn function_of(p4: &str) -> Option<(String, usize)> {
    let (name, rest) = p4.rsplit_once('(')?;
    let count = rest.strip_suffix(')')?.trim().parse().ok()?;
    Some((name.to_owned(), count))
}

// A scalar function's result. Only functions whose behaviour is documented to be this way are listed; every
// other one may return NULL and any class.
fn scalar(name: &str, args: &[Val]) -> Val {
    let any_null = args.iter().any(|arg| arg.null);
    let propagate = |classes: u8| Val::maybe(classes, any_null);
    match (name.to_ascii_lowercase().as_str(), args.len()) {
        // never NULL, even for a NULL argument
        ("typeof" | "quote" | "hex", 1) | ("sqlite_version", 0) => Val::of(TEXT),
        ("random" | "changes" | "total_changes" | "last_insert_rowid", 0) => Val::of(INT),
        // NULL exactly when an argument is NULL
        ("length", 1) | ("instr", 2) => propagate(INT),
        ("abs", 1) => propagate(INT | REAL),
        ("round", 1 | 2) => propagate(REAL),
        ("upper" | "lower", 1) | ("replace", 3) => propagate(TEXT),
        // these return a blob for a blob argument
        ("trim" | "ltrim" | "rtrim", 1 | 2) | ("substr" | "substring", 2 | 3) => Val {
            wild: true,
            ..propagate(0)
        },
        // returns its first argument, or NULL
        ("nullif", 2) => args.first().map_or_else(Val::unknown, |first| Val {
            null: true,
            origin: None,
            rec: None,
            ..first.clone()
        }),
        _ => Val::unknown(),
    }
}

// An aggregate's value once it is finalised, over any number of rows including none.
fn aggregate(name: &str, count: usize) -> Val {
    match (name.to_ascii_lowercase().as_str(), count) {
        ("count", _) => Val::of(INT),
        // a total of +inf and -inf is NaN, which is NULL
        ("total" | "avg", 1) => Val::maybe(REAL, true),
        ("sum", 1) => Val::maybe(INT | REAL, true),
        ("group_concat" | "string_agg", _) => Val::maybe(TEXT, true),
        // min and max of no rows are NULL, and otherwise one of the values
        _ => Val::unknown(),
    }
}

fn apply_affinity(value: &Val, affinity: u8) -> Val {
    match affinity {
        // the value is left as it is
        b'A' | b'@' => value.clone(),
        _ => Val {
            null: value.null,
            classes: 0,
            wild: !value.only_null(),
            origin: None,
            rec: None,
        },
    }
}

struct Machine<'p> {
    program: &'p [Insn],
    layouts: HashMap<usize, Rc<Layout>>,
}

impl Machine<'_> {
    fn target(&self, addr: i64) -> Result<usize, Silent> {
        usize::try_from(addr)
            .ok()
            .filter(|addr| *addr < self.program.len())
            .ok_or(Silent::Target(addr))
    }

    fn column(st: &State, cursor: i64, slot: i64) -> Val {
        let Some(found) = st.cursors.get(&cursor) else {
            return Val::unknown();
        };
        let index = usize::try_from(slot).ok();
        let base = match &found.kind {
            Kind::Stored(layout) => index
                .and_then(|index| layout.slots.get(index))
                .and_then(Option::as_ref)
                .map_or_else(Val::unknown, Slot::value),
            Kind::Pseudo(reg) => st
                .get(*reg)
                .rec
                .as_ref()
                .and_then(|rec| index.and_then(|index| rec.get(index).cloned()))
                .unwrap_or_else(Val::unknown),
            Kind::Sorter(_) | Kind::Opaque => Val::unknown(),
        };
        Self::through_null_row(found, base)
    }

    // a null-extended row reads NULL whatever the column holds
    fn through_null_row(found: &Cursor, base: Val) -> Val {
        match (found.rows & ROW != 0, found.rows & NULL_ROW != 0) {
            (_, false) => base,
            (false, true) => Val::null(),
            (true, true) => Val {
                null: true,
                origin: None,
                ..base
            },
        }
    }

    fn step(&self, pc: usize, st: &State) -> Result<Vec<Edge>, Silent> {
        let insn = self.program.get(pc).ok_or(Silent::Target(-1))?;
        let op = insn
            .op
            .ok_or_else(|| Silent::Unmodelled(insn.name.clone()))?;
        if let Some(edges) = self.flow(pc, op, insn, st)? {
            return Ok(edges);
        }
        if let Some(edges) = self.open(pc, op, insn, st) {
            return Ok(edges);
        }
        if let Some(edges) = self.cursor(pc, op, insn, st)? {
            return Ok(edges);
        }
        let mut next = st.clone();
        Self::write(op, insn, &mut next);
        Ok(vec![(pc + 1, next)])
    }

    // jumps and branches
    fn flow(
        &self,
        pc: usize,
        op: Op,
        insn: &Insn,
        st: &State,
    ) -> Result<Option<Vec<Edge>>, Silent> {
        let both = |st: &State| -> Result<Vec<Edge>, Silent> {
            Ok(vec![
                (pc + 1, st.clone()),
                (self.target(insn.p2)?, st.clone()),
            ])
        };
        Ok(Some(match op {
            Op::Init => {
                if insn.p2 == 0 {
                    vec![(pc + 1, st.clone())]
                } else {
                    vec![(self.target(insn.p2)?, st.clone())]
                }
            }
            Op::Goto => vec![(self.target(insn.p2)?, st.clone())],
            Op::Halt => Vec::new(),
            Op::Once
            | Op::If
            | Op::IfNot
            | Op::Eq
            | Op::Ne
            | Op::Lt
            | Op::Le
            | Op::Gt
            | Op::Ge
            | Op::IfPos
            | Op::DecrJumpZero
            | Op::Filter => both(st)?,
            Op::Jump => vec![
                (self.target(insn.p1)?, st.clone()),
                (self.target(insn.p2)?, st.clone()),
                (self.target(insn.p3)?, st.clone()),
            ],
            Op::NotNull | Op::IsNull => self.null_test(pc, op == Op::NotNull, insn, st)?,
            Op::MustBeInt => {
                let mut next = st.clone();
                next.set(insn.p1, Val::of(INT));
                let mut edges = vec![(pc + 1, next)];
                if insn.p2 != 0 {
                    edges.push((self.target(insn.p2)?, st.clone()));
                }
                edges
            }
            Op::Gosub => {
                let mut next = st.clone();
                next.set(insn.p1, Val::of(INT));
                next.returns.insert(insn.p1, BTreeSet::from([pc + 1]));
                vec![(self.target(insn.p2)?, next)]
            }
            Op::Return => {
                // P3 set marks the BeginSubrtn form, whose register may hold NULL; that is not modelled
                let addrs = st.returns.get(&insn.p1).filter(|_| insn.p3 == 0);
                let addrs = addrs.ok_or(Silent::ReturnAddress)?;
                addrs.iter().map(|addr| (*addr, st.clone())).collect()
            }
            _ => return Ok(None),
        }))
    }

    // NotNull jumps when the register is not NULL and IsNull when it is; each edge knows which it took
    fn null_test(
        &self,
        pc: usize,
        jump_on_non_null: bool,
        insn: &Insn,
        st: &State,
    ) -> Result<Vec<Edge>, Silent> {
        let value = st.get(insn.p1);
        let mut edges = Vec::new();
        let mut non_null = st.clone();
        let mut null = st.clone();
        let non_null_possible = !value.only_null();
        let null_possible = value.null;
        null.set(insn.p1, Val::null());
        if null_possible {
            non_null.set(
                insn.p1,
                Val {
                    null: false,
                    origin: None,
                    ..value
                },
            );
        }
        let (on_jump, on_fall, jump_possible, fall_possible) = if jump_on_non_null {
            (non_null, null, non_null_possible, null_possible)
        } else {
            (null, non_null, null_possible, non_null_possible)
        };
        if fall_possible {
            edges.push((pc + 1, on_fall));
        }
        if jump_possible {
            edges.push((self.target(insn.p2)?, on_jump));
        }
        Ok(edges)
    }

    // everything that opens a cursor or moves rows through a sorter
    fn open(&self, pc: usize, op: Op, insn: &Insn, st: &State) -> Option<Vec<Edge>> {
        let mut next = st.clone();
        let edges = match op {
            Op::OpenRead => {
                let kind = self
                    .layouts
                    .get(&pc)
                    .map_or(Kind::Opaque, |layout| Kind::Stored(Rc::clone(layout)));
                next.cursors.insert(insn.p1, Cursor { kind, rows: ROW });
                vec![(pc + 1, next)]
            }
            Op::OpenPseudo => {
                next.cursors.insert(
                    insn.p1,
                    Cursor {
                        kind: Kind::Pseudo(insn.p2),
                        rows: ROW,
                    },
                );
                vec![(pc + 1, next)]
            }
            Op::SorterOpen => {
                next.cursors.insert(
                    insn.p1,
                    Cursor {
                        kind: Kind::Sorter(Rows::Empty),
                        rows: ROW,
                    },
                );
                vec![(pc + 1, next)]
            }
            Op::SorterInsert => {
                let inserted = st
                    .get(insn.p2)
                    .rec
                    .map_or(Rows::Unknown, |rec| Rows::Known(rec.to_vec()));
                if let Some(Cursor {
                    kind: Kind::Sorter(rows),
                    ..
                }) = next.cursors.get_mut(&insn.p1)
                {
                    *rows = rows.join(&inserted);
                }
                vec![(pc + 1, next)]
            }
            Op::SorterData => {
                let rows = match st.cursors.get(&insn.p1) {
                    Some(Cursor {
                        kind: Kind::Sorter(Rows::Known(rows)),
                        ..
                    }) => Some(Rc::new(rows.clone())),
                    // nothing was inserted on any path so far, so no row can be read yet
                    Some(Cursor {
                        kind: Kind::Sorter(Rows::Empty),
                        ..
                    }) => return Some(Vec::new()),
                    _ => None,
                };
                next.set(
                    insn.p2,
                    Val {
                        rec: rows,
                        ..Val::of(BLOB)
                    },
                );
                vec![(pc + 1, next)]
            }
            _ => return None,
        };
        Some(edges)
    }

    // everything that moves or reads a cursor
    fn cursor(
        &self,
        pc: usize,
        op: Op,
        insn: &Insn,
        st: &State,
    ) -> Result<Option<Vec<Edge>>, Silent> {
        let mut next = st.clone();
        let both = |next: &State| -> Result<Vec<Edge>, Silent> {
            Ok(vec![
                (pc + 1, next.clone()),
                (self.target(insn.p2)?, next.clone()),
            ])
        };
        let edges = match op {
            // the edge that finds a row has one; the edge that does not leaves the cursor on a null row or off
            // any row, where a read gives NULL
            Op::Rewind | Op::Last | Op::SorterSort => {
                let mut empty = next.clone();
                empty.set_rows(insn.p1, ROW | NULL_ROW);
                next.set_rows(insn.p1, ROW);
                vec![(pc + 1, next), (self.target(insn.p2)?, empty)]
            }
            Op::Next | Op::Prev | Op::SorterNext => {
                let mut done = next.clone();
                done.set_rows(insn.p1, ROW | NULL_ROW);
                next.set_rows(insn.p1, ROW);
                vec![(pc + 1, done), (self.target(insn.p2)?, next)]
            }
            Op::SeekGE | Op::SeekGT | Op::SeekLE | Op::SeekLT | Op::SeekRowid | Op::NotExists => {
                let mut missed = next.clone();
                missed.set_rows(insn.p1, ROW | NULL_ROW);
                next.set_rows(insn.p1, ROW);
                vec![(pc + 1, next), (self.target(insn.p2)?, missed)]
            }
            Op::IdxGE | Op::IdxGT | Op::IdxLE | Op::IdxLT => both(&next)?,
            Op::DeferredSeek => {
                // the table cursor lands on the row the index cursor is on
                let rows = st
                    .cursors
                    .get(&insn.p1)
                    .map_or(ROW | NULL_ROW, |found| found.rows);
                next.set_rows(insn.p3, rows);
                vec![(pc + 1, next)]
            }
            Op::NullRow => {
                next.set_rows(insn.p1, NULL_ROW);
                vec![(pc + 1, next)]
            }
            Op::IfNullRow => self.if_null_row(pc, insn, st)?,
            Op::Column => {
                let value = Self::column(st, insn.p1, insn.p2);
                next.set(insn.p3, value);
                vec![(pc + 1, next)]
            }
            Op::Rowid | Op::IdxRowid => {
                let value =
                    st.cursors
                        .get(&insn.p1)
                        .map_or_else(Val::unknown, |found| match &found.kind {
                            Kind::Stored(layout) => Self::through_null_row(
                                found,
                                Val {
                                    origin: Some(origin(&layout.db, &layout.table, -1)),
                                    ..Val::of(INT)
                                },
                            ),
                            _ => Val::unknown(),
                        });
                next.set(insn.p2, value);
                vec![(pc + 1, next)]
            }
            _ => return Ok(None),
        };
        Ok(Some(edges))
    }

    // jumps, with the register set to NULL, when the cursor is on a null row
    fn if_null_row(&self, pc: usize, insn: &Insn, st: &State) -> Result<Vec<Edge>, Silent> {
        let rows = st
            .cursors
            .get(&insn.p1)
            .map_or(ROW | NULL_ROW, |found| found.rows);
        let mut edges = Vec::new();
        if rows & ROW != 0 {
            let mut next = st.clone();
            next.set_rows(insn.p1, ROW);
            edges.push((pc + 1, next));
        }
        if rows & NULL_ROW != 0 {
            let mut next = st.clone();
            next.set_rows(insn.p1, NULL_ROW);
            next.set(insn.p3, Val::null());
            edges.push((self.target(insn.p2)?, next));
        }
        Ok(edges)
    }

    // the opcodes that only write registers
    fn write(op: Op, insn: &Insn, st: &mut State) {
        let (p1, p2, p3) = (insn.p1, insn.p2, insn.p3);
        match op {
            Op::Integer | Op::Int64 | Op::Count | Op::OffsetLimit | Op::IsTrue => {
                st.set(p2, Val::of(INT));
            }
            // a non-zero P1 initialises that register to integer 0, as min and max use it
            Op::CollSeq if p1 != 0 => st.set(p1, Val::of(INT)),
            Op::Real => st.set(p2, Val::of(REAL)),
            Op::String8 => st.set(p2, Val::of(TEXT)),
            Op::Blob => st.set(p2, Val::of(BLOB)),
            Op::Null => st.set_range_null(p2, p3),
            Op::Variable => st.set(p2, Val::unknown()),
            Op::Copy => {
                for offset in 0..=p3 {
                    let value = st.get(p1 + offset);
                    st.set(p2 + offset, value);
                }
            }
            Op::SCopy => {
                let value = st.get(p1);
                st.set(p2, value);
            }
            Op::Move => {
                for offset in 0..p3 {
                    let value = st.get(p1 + offset);
                    st.set(p2 + offset, value);
                    st.set(p1 + offset, Val::null());
                }
            }
            Op::ZeroOrNull => {
                let null = st.get(p1).null || st.get(p3).null;
                st.set(p2, Val::maybe(INT, null));
            }
            Op::Not | Op::BitNot => {
                let null = st.get(p1).null;
                st.set(p2, Val::maybe(INT, null));
            }
            Op::And | Op::Or | Op::BitAnd | Op::BitOr | Op::ShiftLeft | Op::ShiftRight => {
                let null = st.get(p1).null || st.get(p2).null;
                st.set(p3, Val::maybe(INT, null));
            }
            _ => Self::compute(op, insn, st),
        }
    }

    // arithmetic, conversions and calls
    fn compute(op: Op, insn: &Insn, st: &mut State) {
        let (p1, p2, p3) = (insn.p1, insn.p2, insn.p3);
        match op {
            // a division or remainder by zero is NULL, so those two are always nullable
            Op::Divide | Op::Remainder => st.set(p3, Val::maybe(INT | REAL, true)),
            // A floating-point NaN is NULL. An infinity plus or minus a finite integer is infinite, and an
            // integer overflow is a finite real, but two reals may cancel to NaN; `0 * inf` is NaN too, so a
            // product needs both operands to be integers.
            Op::Add | Op::Subtract | Op::Multiply => {
                let (left, right) = (st.get(p1), st.get(p2));
                let certain = |val: &Val| val.classes == INT && !val.wild;
                let never_nan = if matches!(op, Op::Multiply) {
                    certain(&left) && certain(&right)
                } else {
                    certain(&left) || certain(&right)
                };
                let null = left.null || right.null || !never_nan;
                st.set(p3, Val::maybe(INT | REAL, null));
            }
            Op::Concat => {
                let null = st.get(p1).null || st.get(p2).null;
                st.set(p3, Val::maybe(TEXT, null));
            }
            Op::AddImm => st.set(p1, Val::of(INT)),
            Op::RealAffinity => {
                let mut value = st.get(p1);
                if value.classes & INT != 0 {
                    value.classes = (value.classes & !INT) | REAL;
                }
                st.set(p1, value);
            }
            Op::Cast => {
                let null = st.get(p1).null;
                let value = match u8::try_from(p2) {
                    Ok(b'A') => Val::maybe(BLOB, null),
                    Ok(b'B') => Val::maybe(TEXT, null),
                    Ok(b'C') => Val::maybe(INT | REAL, null),
                    Ok(b'D') => Val::maybe(INT, null),
                    Ok(b'E') => Val::maybe(REAL, null),
                    _ => Val {
                        wild: true,
                        ..Val::maybe(0, null)
                    },
                };
                st.set(p1, value);
            }
            Op::Affinity => {
                for (offset, affinity) in insn
                    .p4
                    .bytes()
                    .take(usize::try_from(p2).unwrap_or(0))
                    .enumerate()
                {
                    let reg = p1 + i64::try_from(offset).unwrap_or(0);
                    let value = apply_affinity(&st.get(reg), affinity);
                    st.set(reg, value);
                }
            }
            Op::Function => {
                let value = function_of(&insn.p4).map_or_else(Val::unknown, |(name, count)| {
                    let args: Vec<Val> = (0..i64::try_from(count).unwrap_or(0))
                        .map(|offset| st.get(p2 + offset))
                        .collect();
                    scalar(&name, &args)
                });
                st.set(p3, value);
            }
            Op::AggFinal => {
                let value = function_of(&insn.p4)
                    .map_or_else(Val::unknown, |(name, count)| aggregate(&name, count));
                st.set(p1, value);
            }
            Op::MakeRecord => Self::make_record(insn, st),
            // the accumulator is only read by AggFinal; the other opcodes here change nothing
            _ => {}
        }
    }

    fn make_record(insn: &Insn, st: &mut State) {
        let count = usize::try_from(insn.p2).unwrap_or(0);
        let mut fields = Vec::with_capacity(count);
        for (offset, index) in (0..count).enumerate() {
            let reg = insn.p1 + i64::try_from(offset).unwrap_or(0);
            let mut value = st.get(reg);
            // the record's affinities are applied to the registers themselves
            if let Some(affinity) = insn.p4.as_bytes().get(index).copied() {
                value = apply_affinity(&value, affinity);
                st.set(reg, value.clone());
            }
            fields.push(value);
        }
        st.set(
            insn.p3,
            Val {
                rec: Some(Rc::new(fields)),
                ..Val::of(BLOB)
            },
        );
    }
}

/// Reads the bytecode of `sql` and follows it to its result columns, one [`Fact`] per column.
///
/// # Errors
/// [`Silent`] when the statement cannot be analysed with certainty: `EXPLAIN` fails, the statement is an
/// `EXPLAIN`, it reaches an opcode that is not modelled (a trigger, a virtual table, an ephemeral
/// table, a coroutine), a step bound is hit, or no result row is reachable.
pub fn analyze(conn: &Connection, sql: &str) -> Result<Vec<Fact>, Silent> {
    let program = read_program(conn, sql)?;
    let layouts = read_layouts(conn, &program);
    run(&Machine {
        program: &program,
        layouts,
    })
}

fn run(machine: &Machine<'_>) -> Result<Vec<Fact>, Silent> {
    let size = machine.program.len();
    let mut states: Vec<Option<State>> = vec![None; size];
    let mut queue = VecDeque::from([0_usize]);
    let mut queued = vec![false; size];
    *states.first_mut().ok_or(Silent::NoResult)? = Some(State::default());
    *queued.first_mut().ok_or(Silent::NoResult)? = true;
    let mut result: Option<Vec<Val>> = None;
    let mut steps = 0_usize;
    while let Some(pc) = queue.pop_front() {
        steps += 1;
        if steps > MAX_STEPS {
            return Err(Silent::Bound);
        }
        if let Some(flag) = queued.get_mut(pc) {
            *flag = false;
        }
        let Some(state) = states.get(pc).cloned().flatten() else {
            continue;
        };
        if let Some(insn) = machine
            .program
            .get(pc)
            .filter(|insn| insn.op == Some(Op::ResultRow))
        {
            let row: Vec<Val> = (0..insn.p2)
                .map(|offset| state.get(insn.p1 + offset))
                .collect();
            result = Some(match result {
                Some(seen) if seen.len() == row.len() => {
                    seen.iter().zip(&row).map(|(a, b)| a.join(b)).collect()
                }
                Some(_) => return Err(Silent::Columns),
                None => row,
            });
        }
        for (target, edge) in machine.step(pc, &state)? {
            let slot = states.get_mut(target).ok_or(Silent::Target(-1))?;
            let grew = if let Some(known) = slot {
                known.absorb(&edge)
            } else {
                *slot = Some(edge);
                true
            };
            let Some(flag) = queued.get_mut(target) else {
                continue;
            };
            if grew && !*flag {
                *flag = true;
                queue.push_back(target);
            }
        }
    }
    let row = result.ok_or(Silent::NoResult)?;
    Ok(row.iter().map(fact).collect())
}

const fn fact(value: &Val) -> Fact {
    let class = match (value.wild, value.classes) {
        (false, INT) => Some(Class::Integer),
        (false, REAL) => Some(Class::Real),
        (false, TEXT) => Some(Class::Text),
        (false, BLOB) => Some(Class::Blob),
        _ => None,
    };
    Fact {
        non_null: !value.null,
        class,
        pass_through: value.origin.is_some() && !value.null,
    }
}

#[cfg(test)]
mod tests;
