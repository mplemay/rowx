#!/usr/bin/env python3
from argparse import ArgumentParser
from email.parser import Parser
from os import environ
from pathlib import Path, PurePosixPath
from tarfile import TarFile
from typing import TYPE_CHECKING, Final
from zipfile import ZipFile

if TYPE_CHECKING:
    from email.message import Message

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DIST: Final[Path] = ROOT / "dist"
EXPECTED_FILE_COUNT: Final[int] = 16
EXPECTED_WHEEL_COUNT: Final[int] = 5
MATCH_COUNT: Final[int] = 1
WHEEL_TAGS: Final[tuple[tuple[str, ...], ...]] = (
    ("manylinux", "x86_64"),
    ("manylinux", "aarch64"),
    ("macosx", "x86_64"),
    ("macosx", "arm64"),
    ("win_amd64",),
)
MIT_LICENSE: Final[str] = "MIT"
LICENSE_ENTRY_SUFFIX: Final[str] = ".dist-info/licenses/LICENSE"
LICENSE_FILE_NAME: Final[str] = "LICENSE"
METADATA_SUFFIX: Final[str] = ".dist-info/METADATA"
WHEEL_PARTS_WITHOUT_BUILD: Final[int] = 5
WHEEL_PARTS_WITH_BUILD: Final[int] = 6
VERSION_INDEX: Final[int] = 1
PAIR_LENGTH: Final[int] = 2
ROWX: Final[str] = "rowx"
ROWX_CORE: Final[str] = "rowx_core"
ROWX_POSTGRES: Final[str] = "rowx_postgres"
ROWX_SQLITE: Final[str] = "rowx_sqlite"
CORE_DISTRIBUTION: Final[str] = "rowx-core"
POSTGRES_DISTRIBUTION: Final[str] = "rowx-postgres"
PYTEST_DISTRIBUTION: Final[str] = "pytest"
PATHSPEC_DISTRIBUTION: Final[str] = "pathspec"
PATHSPEC_CONSTRAINTS: Final[frozenset[str]] = frozenset({">=1.1.1", "<2"})
POSTGRES_EXTRA: Final[str] = "postgres"
PYTEST_EXTRA: Final[str] = "pytest"
EMBEDDED_DISTRIBUTION: Final[str] = "rowx-embedded"
EMBEDDED_EXTRA: Final[str] = "embedded"
SQLITE_DISTRIBUTION: Final[str] = "rowx-sqlite"
SQLITE_EXTRA: Final[str] = "sqlite"
MARKER_AND: Final[str] = "and"
MARKER_OR: Final[str] = "or"
MARKER_EQUALS: Final[str] = "=="
MARKER_OPEN: Final[str] = "("
MARKER_CLOSE: Final[str] = ")"
MARKER_QUOTES: Final[frozenset[str]] = frozenset({"'", '"'})
# the platforms the companion ships wheels for; every disjunct of its marker is one of these plus the extra
EMBEDDED_PLATFORMS: Final[frozenset[frozenset[tuple[str, str]]]] = frozenset({
    frozenset({("sys_platform", "darwin"), ("platform_machine", "arm64")}),
    frozenset({("sys_platform", "linux"), ("platform_machine", "x86_64")}),
    frozenset({("sys_platform", "linux"), ("platform_machine", "aarch64")}),
})
EXPECTED_CLASSIFIERS: Final[frozenset[str]] = frozenset({
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.14",
    "Programming Language :: Python :: 3 :: Only",
    "Typing :: Typed",
    "Topic :: Database",
    "Framework :: AsyncIO",
})
EXPECTED_KEYWORDS: Final[tuple[str, ...]] = ("sql", "postgresql", "postgres", "typed", "async", "t-string")
SQLITE_KEYWORDS: Final[tuple[str, ...]] = ("sql", "sqlite", "typed", "async", "t-string")
# the namespaces that ship a compiled extension: five platform wheels and a source distribution each
NATIVE_NAMESPACES: Final[tuple[str, ...]] = (ROWX_POSTGRES, ROWX_SQLITE)
EXPECTED_URLS: Final[dict[str, str]] = {
    "Documentation": "https://mplemay.github.io/rowx/",
    "Repository": "https://github.com/mplemay/rowx",
    "Issues": "https://github.com/mplemay/rowx/issues",
    "Changelog": "https://github.com/mplemay/rowx/blob/main/docs/changelog.md",
}
DISTRIBUTION_NAMES: Final[dict[str, str]] = {
    ROWX: ROWX,
    ROWX_CORE: CORE_DISTRIBUTION,
    ROWX_POSTGRES: POSTGRES_DISTRIBUTION,
    ROWX_SQLITE: SQLITE_DISTRIBUTION,
}
LICENSE_SOURCES: Final[dict[str, Path]] = {
    ROWX: ROOT / "LICENSE",
    ROWX_CORE: ROOT / "packages" / "rowx-core" / "LICENSE",
    ROWX_POSTGRES: ROOT / "packages" / "rowx-postgres" / "LICENSE",
    ROWX_SQLITE: ROOT / "packages" / "rowx-sqlite" / "LICENSE",
}
KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    ROWX: EXPECTED_KEYWORDS,
    ROWX_CORE: EXPECTED_KEYWORDS,
    ROWX_POSTGRES: EXPECTED_KEYWORDS,
    ROWX_SQLITE: SQLITE_KEYWORDS,
}
NATIVE_SUFFIXES: Final[tuple[str, ...]] = (".so", ".pyd", ".dll", ".dylib")
PURE_SDIST_PREFIXES: Final[tuple[str, ...]] = ("rowx-", "rowx_core-")

type Requirement = tuple[str, str | None]
type MarkerAtom = tuple[str, str]
type MarkerToken = str | MarkerAtom
type MarkerForm = set[frozenset[MarkerAtom]]


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--contents-only", action="store_true")
    parser.add_argument("--directory", type=Path, default=DIST)
    args = parser.parse_args()
    if args.contents_only:
        for path in args.directory.glob("*.whl"):
            validate_wheel(path)
        return 0
    validate_release(args.directory, _required("VERSION"))
    return 0


def _has_platform_wheels(files: list[Path], namespace: str, version: str) -> bool:
    wheels = [path.name for path in files if path.name.startswith(f"{namespace}-{version}-") and path.suffix == ".whl"]
    # each tag tuple must occur in exactly one wheel name of the native distribution
    return len(wheels) == EXPECTED_WHEEL_COUNT and all(
        sum(all(tag in name for tag in tags) for name in wheels) == MATCH_COUNT for tags in WHEEL_TAGS
    )


def validate_release(directory: Path, version: str) -> None:
    files = list(directory.iterdir())
    matched = all(_has_platform_wheels(files, namespace, version) for namespace in NATIVE_NAMESPACES)
    if len(files) != EXPECTED_FILE_COUNT or not matched:
        message = f"Unexpected distributions: {[path.name for path in files]}"
        raise SystemExit(message)
    for name in (ROWX, ROWX_CORE, *NATIVE_NAMESPACES):
        if not (directory / f"{name}-{version}.tar.gz").is_file():
            message = f"{name} source distribution is missing or has the wrong version"
            raise SystemExit(message)
    for name in (ROWX, ROWX_CORE):
        if not (directory / f"{name}-{version}-py3-none-any.whl").is_file():
            message = f"{name} pure Python wheel is missing"
            raise SystemExit(message)
    _validate_archive_contents(files)


def _validate_archive_contents(files: list[Path]) -> None:
    for path in files:
        if path.suffix == ".whl":
            validate_wheel(path)
        elif _pure_sdist(path):
            validate_pure_sdist(path)


def validate_wheel(path: Path) -> None:
    namespace = path.name.split("-", maxsplit=1)[0]
    with ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = _read_metadata(path, wheel)
        license_bytes = _read_packaged_license(path, wheel, names)
    _reject_development_files(path, namespace, names)
    _validate_metadata(path, namespace, metadata, license_bytes)


def _reject_development_files(path: Path, namespace: str, names: list[str]) -> None:
    for name in names:
        parts = Path(name).parts
        if (
            "__tests__" in parts
            or "__pycache__" in parts
            or ".agents" in parts
            or name.endswith((".rs", ".pyc", "Cargo.toml", "SKILL.md"))
        ):
            message = f"{path.name} contains a development file: {name}"
            raise SystemExit(message)
        if parts and parts[0] != namespace and not parts[0].endswith(".dist-info"):
            message = f"{path.name} owns an unexpected namespace: {name}"
            raise SystemExit(message)
        if namespace not in NATIVE_NAMESPACES and name.endswith(NATIVE_SUFFIXES):
            message = f"{path.name} must be pure Python"
            raise SystemExit(message)
    if f"{namespace}/py.typed" not in names:
        message = f"{path.name} is missing its typing marker"
        raise SystemExit(message)


def _pure_sdist(path: Path) -> bool:
    # the native namespaces are absent from PURE_SDIST_PREFIXES; they stay native source builds
    name = path.name
    return name.endswith(".tar.gz") and name.startswith(PURE_SDIST_PREFIXES)


def validate_pure_sdist(path: Path) -> None:
    with TarFile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.name.endswith(NATIVE_SUFFIXES):
                message = f"{path.name} must be pure Python"
                raise SystemExit(message)


def _validate_metadata(path: Path, namespace: str, metadata: Message, license_bytes: bytes) -> None:
    version = _wheel_version(path)
    _validate_identity(path, namespace, version, metadata)
    _validate_license(path, metadata)
    _validate_license_file(path, namespace, metadata, license_bytes)
    _validate_classifiers(path, metadata)
    _validate_keywords(path, namespace, metadata)
    _validate_urls(path, metadata)
    _validate_requires(path, namespace, version, metadata)


def _read_metadata(path: Path, wheel: ZipFile) -> Message:
    matches = [name for name in wheel.namelist() if name.endswith(METADATA_SUFFIX)]
    if len(matches) != MATCH_COUNT:
        message = f"{path.name} metadata files are {matches}"
        raise SystemExit(message)
    return Parser().parsestr(wheel.read(matches[0]).decode("utf-8"))


def _wheel_version(path: Path) -> str:
    parts = path.name.removesuffix(".whl").split("-")
    if len(parts) not in {WHEEL_PARTS_WITHOUT_BUILD, WHEEL_PARTS_WITH_BUILD}:
        message = f"{path.name} is not a wheel filename"
        raise SystemExit(message)
    return parts[VERSION_INDEX]


def _validate_identity(path: Path, namespace: str, version: str, metadata: Message) -> None:
    expected_name = DISTRIBUTION_NAMES.get(namespace)
    if expected_name is None:
        message = f"{path.name} has an unexpected distribution name"
        raise SystemExit(message)
    name = metadata.get("Name")
    if name != expected_name:
        message = f"{path.name} Name is {name!r}, expected {expected_name!r}"
        raise SystemExit(message)
    found_version = metadata.get("Version")
    if found_version != version:
        message = f"{path.name} Version is {found_version!r}, expected {version!r}"
        raise SystemExit(message)


def _validate_license(path: Path, metadata: Message) -> None:
    expression = metadata.get("License-Expression")
    legacy = metadata.get("License")
    if expression == MIT_LICENSE or (expression is None and legacy == MIT_LICENSE):
        return
    message = f"{path.name} license metadata is License-Expression={expression!r} License={legacy!r}"
    raise SystemExit(message)


def _read_packaged_license(path: Path, wheel: ZipFile, names: list[str]) -> bytes:
    packaged = [
        name
        for name in names
        if not name.endswith("/") and PurePosixPath(name).full_match("*.dist-info/licenses/LICENSE")
    ]
    if len(packaged) != MATCH_COUNT:
        present = [name for name in names if not name.endswith("/") and "LICENSE" in name.upper()]
        message = f"{path.name} license file is not {LICENSE_ENTRY_SUFFIX}: {present}"
        raise SystemExit(message)
    return wheel.read(packaged[0])


def _validate_license_file(path: Path, namespace: str, metadata: Message, license_bytes: bytes) -> None:
    headers = metadata.get_all("License-File", [])
    if headers != [LICENSE_FILE_NAME]:
        message = f"{path.name} License-File is {headers}, expected [{LICENSE_FILE_NAME}]"
        raise SystemExit(message)
    source = LICENSE_SOURCES.get(namespace)
    if source is None:
        message = f"{path.name} has an unexpected distribution name"
        raise SystemExit(message)
    if license_bytes != source.read_bytes():
        message = f"{path.name} license bytes do not match {source.relative_to(ROOT)}"
        raise SystemExit(message)


def _validate_classifiers(path: Path, metadata: Message) -> None:
    found = frozenset(metadata.get_all("Classifier", []))
    if found != EXPECTED_CLASSIFIERS:
        message = f"{path.name} classifiers are {sorted(found)}"
        raise SystemExit(message)


def _validate_keywords(path: Path, namespace: str, metadata: Message) -> None:
    raw = metadata.get("Keywords")
    keywords = tuple(part.strip() for part in raw.split(",")) if raw is not None else ()
    expected = KEYWORDS.get(namespace, EXPECTED_KEYWORDS)
    if keywords != expected:
        message = f"{path.name} keywords are {keywords}, expected {expected}"
        raise SystemExit(message)


def _validate_urls(path: Path, metadata: Message) -> None:
    found: dict[str, str] = {}
    for value in metadata.get_all("Project-URL", []):
        label, separator, url = value.partition(",")
        if not separator:
            message = f"{path.name} has a Project-URL without a comma: {value}"
            raise SystemExit(message)
        found[label.strip()] = url.strip()
    if found != EXPECTED_URLS:
        message = f"{path.name} project URLs are {found}"
        raise SystemExit(message)


def _validate_requires(path: Path, namespace: str, version: str, metadata: Message) -> None:
    requirements = [_split_requirement(value) for value in metadata.get_all("Requires-Dist", [])]
    if namespace == ROWX:
        _validate_rowx_requires(path, version, requirements)
        return
    if namespace == ROWX_CORE:
        _validate_core_requires(path, requirements)
        return
    if namespace in {ROWX_POSTGRES, ROWX_SQLITE}:
        _validate_backend_requires(path, version, requirements)
        return
    message = f"{path.name} has an unexpected distribution name"
    raise SystemExit(message)


def _validate_rowx_requires(path: Path, version: str, requirements: list[Requirement]) -> None:
    direct = [requirement for requirement, marker in requirements if marker is None]
    expected_core = f"{CORE_DISTRIBUTION}=={version}"
    pathspec = [requirement for requirement in direct if _distribution_name(requirement) == PATHSPEC_DISTRIBUTION]
    valid_pathspec = (
        len(pathspec) == 1
        and frozenset(part.strip() for part in pathspec[0].removeprefix(PATHSPEC_DISTRIBUTION).split(","))
        == PATHSPEC_CONSTRAINTS
    )
    if len(direct) != PAIR_LENGTH or direct.count(expected_core) != 1 or not valid_pathspec:
        message = f"{path.name} direct requirements are {direct}, expected {expected_core} and pathspec>=1.1.1,<2"
        raise SystemExit(message)
    extras, embedded = _split_extras(path, requirements)
    expected_postgres = f"{POSTGRES_DISTRIBUTION}=={version}"
    postgres = sorted(
        (requirement, extra)
        for requirement, extra in extras
        if _distribution_name(requirement) == POSTGRES_DISTRIBUTION
    )
    expected_pairs = [(expected_postgres, EMBEDDED_EXTRA), (expected_postgres, POSTGRES_EXTRA)]
    if postgres != expected_pairs:
        message = f"{path.name} postgres requirements are {postgres}, expected {expected_pairs}"
        raise SystemExit(message)
    expected_embedded = f"{EMBEDDED_DISTRIBUTION}=={version}"
    if embedded != [expected_embedded]:
        message = f"{path.name} embedded requirements are {embedded}, expected [{expected_embedded}]"
        raise SystemExit(message)
    expected_sqlite = [(f"{SQLITE_DISTRIBUTION}=={version}", SQLITE_EXTRA)]
    sqlite = [
        (requirement, extra) for requirement, extra in extras if _distribution_name(requirement) == SQLITE_DISTRIBUTION
    ]
    if sqlite != expected_sqlite:
        message = f"{path.name} sqlite requirements are {sqlite}, expected {expected_sqlite}"
        raise SystemExit(message)
    for requirement, extra in extras:
        if _distribution_name(requirement) in {POSTGRES_DISTRIBUTION, SQLITE_DISTRIBUTION}:
            continue
        if _distribution_name(requirement) != PYTEST_DISTRIBUTION or extra != PYTEST_EXTRA:
            message = f"{path.name} has an unexpected extra requirement: {requirement} ; extra == {extra}"
            raise SystemExit(message)


def _split_extras(path: Path, requirements: list[Requirement]) -> tuple[list[tuple[str, str]], list[str]]:
    extras: list[tuple[str, str]] = []
    embedded: list[str] = []
    for requirement, marker in requirements:
        if marker is None:
            continue
        if _distribution_name(requirement) == EMBEDDED_DISTRIBUTION and _is_embedded_marker(marker):
            embedded.append(requirement)
            continue
        extra = _extra_name(marker)
        if extra is None:
            message = f"{path.name} has a requirement marker that is not a single extra: {requirement} ; {marker}"
            raise SystemExit(message)
        extras.append((requirement, extra))
    return extras, embedded


# uv reorders clauses and factors repeated conditions, so compare the marker's normal form to the platforms
def _is_embedded_marker(marker: str) -> bool:
    if (form := _marker_form(marker)) is None:
        return False
    platforms: set[frozenset[MarkerAtom]] = set()
    for conjunction in form:
        if (platform := _embedded_platform(conjunction)) is None:
            return False
        platforms.add(platform)
    return frozenset(platforms) == EMBEDDED_PLATFORMS


def _embedded_platform(conjunction: frozenset[MarkerAtom]) -> frozenset[MarkerAtom] | None:
    extra: MarkerAtom = ("extra", EMBEDDED_EXTRA)
    if extra not in conjunction:
        return None
    return conjunction - {extra}


def _marker_form(marker: str) -> MarkerForm | None:
    if (tokens := _marker_tokens(marker)) is None:
        return None
    parsed = _parse_or(tokens, 0)
    if parsed is None:
        return None
    form, index = parsed
    if index != len(tokens):
        return None
    return form


def _marker_tokens(marker: str) -> list[MarkerToken] | None:
    tokens: list[MarkerToken] = []
    index = 0
    while index < len(marker):
        if marker[index].isspace():
            index += 1
            continue
        if marker[index] in {MARKER_OPEN, MARKER_CLOSE}:
            tokens.append(marker[index])
            index += 1
            continue
        if (scanned := _scan_word(marker, index)) is None:
            return None
        word, index = scanned
        if word in {MARKER_AND, MARKER_OR}:
            tokens.append(word)
            continue
        if (comparison := _scan_comparison(marker, index, word)) is None:
            return None
        atom, index = comparison
        tokens.append(atom)
    return tokens


def _scan_word(marker: str, index: int) -> tuple[str, int] | None:
    if not _identifier_char(marker[index], start=True):
        return None
    start = index
    index += 1
    while index < len(marker) and _identifier_char(marker[index], start=False):
        index += 1
    return marker[start:index], index


def _identifier_char(char: str, *, start: bool) -> bool:
    if not char.isascii():
        return False
    if start:
        return char.isalpha() or char == "_"
    return char.isalnum() or char == "_"


def _scan_comparison(marker: str, index: int, name: str) -> tuple[MarkerAtom, int] | None:
    index = _skip_space(marker, index)
    if not marker.startswith(MARKER_EQUALS, index):
        return None
    index = _skip_space(marker, index + len(MARKER_EQUALS))
    if index >= len(marker) or marker[index] not in MARKER_QUOTES:
        return None
    quote = marker[index]
    end = marker.find(quote, index + 1)
    if end < 0:
        return None
    return (name, marker[index + 1 : end]), end + 1


def _skip_space(marker: str, index: int) -> int:
    while index < len(marker) and marker[index].isspace():
        index += 1
    return index


def _parse_or(tokens: list[MarkerToken], index: int) -> tuple[MarkerForm, int] | None:
    parsed = _parse_and(tokens, index)
    if parsed is None:
        return None
    form, index = parsed
    while index < len(tokens) and tokens[index] == MARKER_OR:
        parsed = _parse_and(tokens, index + 1)
        if parsed is None:
            return None
        right, index = parsed
        form |= right
    return form, index


# `and` binds tighter than `or`; distribute so each conjunction is a flat set of comparisons
def _parse_and(tokens: list[MarkerToken], index: int) -> tuple[MarkerForm, int] | None:
    parsed = _parse_primary(tokens, index)
    if parsed is None:
        return None
    form, index = parsed
    while index < len(tokens) and tokens[index] == MARKER_AND:
        parsed = _parse_primary(tokens, index + 1)
        if parsed is None:
            return None
        right, index = parsed
        form = {left | clause for left in form for clause in right}
    return form, index


def _parse_primary(tokens: list[MarkerToken], index: int) -> tuple[MarkerForm, int] | None:
    if index >= len(tokens):
        return None
    token = tokens[index]
    if token == MARKER_OPEN:
        return _parse_group(tokens, index + 1)
    if isinstance(token, str):
        return None
    return {frozenset({token})}, index + 1


def _parse_group(tokens: list[MarkerToken], index: int) -> tuple[MarkerForm, int] | None:
    parsed = _parse_or(tokens, index)
    if parsed is None:
        return None
    form, index = parsed
    if index >= len(tokens) or tokens[index] != MARKER_CLOSE:
        return None
    return form, index + 1


def _validate_core_requires(path: Path, requirements: list[Requirement]) -> None:
    if not requirements:
        return
    found = [requirement if marker is None else f"{requirement}; {marker}" for requirement, marker in requirements]
    message = f"{path.name} must not declare Requires-Dist, found {found}"
    raise SystemExit(message)


def _validate_backend_requires(path: Path, version: str, requirements: list[Requirement]) -> None:
    expected = f"{CORE_DISTRIBUTION}=={version}"
    found = [requirement if marker is None else f"{requirement}; {marker}" for requirement, marker in requirements]
    if found != [expected]:
        message = f"{path.name} requirements are {found}, expected [{expected}]"
        raise SystemExit(message)


def _split_requirement(value: str) -> Requirement:
    requirement, separator, marker = value.partition(";")
    if separator:
        return requirement.strip(), marker.strip()
    return requirement.strip(), None


def _extra_name(marker: str) -> str | None:
    text = marker.strip()
    if len(text) >= PAIR_LENGTH and text[0] == "(" and text[-1] == ")":
        text = text[1:-1].strip()
    left, separator, right = text.partition("==")
    if not separator or left.strip() != "extra":
        return None
    value = right.strip()
    quoted = (
        len(value) >= PAIR_LENGTH
        and value[0] in {"'", '"'}
        and value.endswith(value[0])
        and value.count(value[0]) == PAIR_LENGTH
    )
    if not quoted:
        return None
    return value[1:-1]


def _distribution_name(requirement: str) -> str:
    name = requirement.strip()
    for index, char in enumerate(name):
        if char in "<>=!~[;( ":
            return name[:index].strip().lower().replace("_", "-")
    return name.lower().replace("_", "-")


def _required(name: str) -> str:
    if not (value := environ.get(name)):
        message = f"{name} is not set"
        raise SystemExit(message)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
