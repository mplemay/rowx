import random
import re
from dataclasses import dataclass
from typing import Final, Literal

EOLS: Final[tuple[str, ...]] = ("\n", "\r\n", "\r")
LINE_BREAK: Final = re.compile(r"\r\n|\r|\n")
# the plain characters never include a quote, a brace, a backslash, `*`, `/` or a line separator
PLAIN: Final[tuple[str, ...]] = (*"abcdefghijklmnopqrstuvwxyz0123456789 _,.()", "é", "ß", "日", "本", "😀")
SIMPLE_ESCAPES: Final[tuple[tuple[str, str], ...]] = (
    ("\\n", "\n"),
    ("\\t", "\t"),
    ("\\r", "\r"),
    ("\\a", "\a"),
    ("\\b", "\b"),
    ("\\f", "\f"),
    ("\\v", "\v"),
    ("\\\\", "\\"),
    ("\\'", "'"),
    ('\\"', '"'),
)
CODE_ESCAPES: Final[tuple[tuple[str, str], ...]] = (
    ("\\x41", "A"),
    ("\\x7a", "z"),
    ("\\u00e9", "é"),
    ("\\u65e5", "日"),
    ("\\U0001f600", "😀"),
    ("\\101", "A"),
    ("\\007", "\a"),
    ("\\N{LATIN SMALL LETTER E WITH ACUTE}", "é"),
    ("\\N{SNOWMAN}", "☃"),
    ("\\N{BULLET}", "•"),
)
NUL_ESCAPES: Final[tuple[tuple[str, str], ...]] = (("\\000", "\x00"), ("\\x00", "\x00"))
# none of these starts a valid escape
UNKNOWN_ESCAPE_LETTERS: Final[str] = "qwykzCDEG"
HOLES: Final[tuple[str, ...]] = ("{x}", "{ x }", "{x + y}", '{"}"}', "{ {1: 2}[1] }", "{f(1, 2)}", "{'a{b' + 'c'}")
QUOTES: Final[tuple[str, ...]] = ('"', "'", '"""', "'''")
RAW_PREFIXES: Final[tuple[str, ...]] = ("rt", "tr", "Rt", "tR", "RT")
PREFIXES: Final[tuple[str, ...]] = ("t", "T")
LEADS: Final[tuple[str, ...]] = ("", "é = 1; ", "日本 = '😀'; ", "x = 1;  ")
JOINERS: Final[tuple[str, ...]] = (" ", "\n    ", "\n")
COMMENTS: Final[tuple[str, ...]] = ("", "# é日本\n", "# 😀 {x}\n")

type AtomKind = Literal["text", "noise", "hole", "mark"]
type Atom = tuple[AtomKind, str | int]


@dataclass(slots=True, frozen=True)
class Piece:
    # the source spelling and the one decoded character it stands for (empty for a line continuation)
    src: str
    text: str
    # `{{` and `}}` span two source characters for one decoded one, so only the start is fixed
    loose: bool = False


@dataclass(slots=True, frozen=True)
class Hole:
    src: str


@dataclass(slots=True, frozen=True)
class Placed:
    src: str
    text: str
    # code point offsets into the file text, with the line terminator already applied
    offset: int
    length: int
    loose: bool = False


@dataclass(slots=True, frozen=True)
class Part:
    prefix: str
    quote: str
    raw: bool
    items: tuple[Piece | Hole, ...]


@dataclass(slots=True, frozen=True)
class Case:
    source: str
    eol: str
    # the decoded characters of each static segment, in order
    segments: tuple[tuple[Placed, ...], ...]
    holes: tuple[Placed, ...]
    # where each segment begins in the source
    starts: tuple[int, ...]
    # the first character of each marked token, with the segment that holds it
    marks: dict[str, tuple[Placed, int]]


def seeded(seed: int) -> random.Random:
    # a fixed seed keeps every generated case reproducible
    return random.Random(seed)  # ruff: ignore[suspicious-non-cryptographic-random-usage]


def line_col(source: str, offset: int) -> tuple[int, int]:
    before = source[:offset]
    breaks = list(LINE_BREAK.finditer(before))
    line_start = breaks[-1].end() if breaks else 0
    return 1 + len(breaks), offset - line_start + 1


def _text_pieces(rng: random.Random, text: str, *, raw: bool) -> list[Piece]:
    pieces: list[Piece] = []
    for char in text:
        # an ASCII character can always be spelled as a hex escape
        if not raw and char.isascii() and rng.random() < 0.25:
            pieces.append(Piece(f"\\x{ord(char):02x}", char))
        else:
            pieces.append(Piece(char, char))
    return pieces


def _escape_pieces(rng: random.Random, *, raw: bool, safe: bool) -> list[Piece]:
    if raw:
        # a raw backslash is kept, and it is never the last character before a quote
        return [Piece("\\", "\\"), Piece(letter := rng.choice("nxuNq0"), letter)]
    roll = rng.random()
    if roll < 0.35:
        letter = rng.choice(UNKNOWN_ESCAPE_LETTERS)
        # an unknown escape is two characters
        return [Piece("\\", "\\"), Piece(letter, letter)]
    if roll < 0.5:
        return [Piece("\\\n", "")]
    choices = SIMPLE_ESCAPES + CODE_ESCAPES
    if not safe:
        choices += NUL_ESCAPES
    src, text = rng.choice(choices)
    return [Piece(src, text)]


def _noise_piece(rng: random.Random, *, raw: bool, triple: bool, safe: bool) -> list[Piece]:
    roll = rng.random()
    if roll < 0.5:
        char = rng.choice(PLAIN)
        return [Piece(char, char)]
    if roll < 0.62:
        return [Piece("{{", "{", loose=True)] if rng.random() < 0.5 else [Piece("}}", "}", loose=True)]
    if triple and roll < 0.72:
        return [Piece("\n", "\n")]
    return _escape_pieces(rng, raw=raw, safe=safe)


def _noise(rng: random.Random, count: int, *, raw: bool, triple: bool, safe: bool) -> list[Piece]:
    pieces: list[Piece] = []
    for index in range(count):
        if index == 0:
            # the first character is plain, so a test can swap it out
            char = rng.choice(PLAIN)
            pieces.append(Piece(char, char))
        else:
            pieces.extend(_noise_piece(rng, raw=raw, triple=triple, safe=safe))
    return pieces


def random_atoms(rng: random.Random) -> list[Atom]:
    atoms: list[Atom] = []
    for _ in range(rng.randint(1, 5)):
        atoms.extend((("noise", rng.randint(0, 14)), ("hole", rng.choice(HOLES))))
    atoms.append(("noise", rng.randint(0, 14)))
    return atoms


# a statement that fails at a marked token, with comments before and after it
def error_atoms(rng: random.Random) -> list[Atom]:
    return [
        ("text", "/* "),
        ("noise", rng.randint(0, 8)),
        ("text", " */ SELECT "),
        ("hole", rng.choice(HOLES)),
        ("text", "::int /* "),
        ("noise", rng.randint(1, 10)),
        ("text", " */, "),
        ("mark", "ZZBAD"),
        ("text", " /* "),
        ("noise", rng.randint(1, 10)),
        ("text", " */, "),
        ("hole", rng.choice(HOLES)),
        ("text", "::int"),
    ]


def _expand(rng: random.Random, atom: Atom, part: Part, *, safe: bool) -> list[Piece | Hole]:
    kind, payload = atom
    triple = len(part.quote) == 3
    match kind:
        case "hole":
            return [Hole(str(payload))]
        case "noise":
            return list(_noise(rng, int(payload), raw=part.raw, triple=triple, safe=safe))
        case _:
            return list(_text_pieces(rng, str(payload), raw=part.raw))


def _open_part(rng: random.Random) -> Part:
    raw = rng.random() < 0.3
    return Part(rng.choice(RAW_PREFIXES if raw else PREFIXES), rng.choice(QUOTES), raw, ())


def build_case(rng: random.Random, atoms: list[Atom], *, safe: bool = True) -> Case:
    count = rng.randint(1, 3)
    cuts = sorted(rng.randint(0, len(atoms)) for _ in range(count - 1))
    bounds = [0, *cuts, len(atoms)]
    eol = rng.choice(EOLS)
    chunks: list[str] = []
    offset = 0

    def emit(text: str) -> tuple[int, int]:
        nonlocal offset
        text = text.replace("\n", eol)
        chunks.append(text)
        start = offset
        offset += len(text)
        return start, len(text)

    emit("from rowx import query\n")
    emit(rng.choice(COMMENTS))
    emit(rng.choice(LEADS) + "q = query(")
    segments: list[list[Placed]] = [[]]
    holes: list[Placed] = []
    starts: list[int] = []
    marks: dict[str, tuple[Placed, int]] = {}
    for index in range(count):
        if index:
            emit(rng.choice(JOINERS))
        shell = _open_part(rng)
        emit(shell.prefix + shell.quote)
        if not starts:
            starts.append(offset)
        for atom in atoms[bounds[index] : bounds[index + 1]]:
            first = True
            for item in _expand(rng, atom, shell, safe=safe):
                start, length = emit(item.src)
                if isinstance(item, Hole):
                    holes.append(Placed(item.src, "", start, length))
                    starts.append(offset)
                    segments.append([])
                    continue
                placed = Placed(item.src, item.text, start, length, item.loose)
                if item.text:
                    segments[-1].append(placed)
                if first and atom[0] == "mark":
                    marks[str(atom[1])] = (placed, len(segments) - 1)
                first = False
        emit(shell.quote)
    emit(")\n")
    return Case(
        source="".join(chunks),
        eol=eol,
        segments=tuple(tuple(segment) for segment in segments),
        holes=tuple(holes),
        starts=tuple(starts),
        marks=marks,
    )
