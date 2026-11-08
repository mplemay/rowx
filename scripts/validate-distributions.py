#!/usr/bin/env python3
from argparse import ArgumentParser
from os import environ
from pathlib import Path
from typing import Final
from zipfile import ZipFile

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DIST: Final[Path] = ROOT / "dist"
EXPECTED_FILE_COUNT: Final[int] = 10
EXPECTED_WHEEL_COUNT: Final[int] = 5
MATCH_COUNT: Final[int] = 1
WHEEL_TAGS: Final[tuple[tuple[str, ...], ...]] = (
    ("manylinux", "x86_64"),
    ("manylinux", "aarch64"),
    ("macosx", "x86_64"),
    ("macosx", "arm64"),
    ("win_amd64",),
)


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--contents-only", action="store_true")
    parser.add_argument("--directory", type=Path, default=DIST)
    args = parser.parse_args()
    if args.contents_only:
        for path in args.directory.glob("*.whl"):
            validate_wheel(path)
        return 0
    version = _required("VERSION")
    files = list(args.directory.iterdir())
    wheels = [
        path.name for path in files if path.name.startswith(f"rowx_postgres-{version}-") and path.suffix == ".whl"
    ]
    # each tag tuple must occur in exactly one wheel name
    matched = all(sum(all(tag in name for tag in tags) for name in wheels) == MATCH_COUNT for tags in WHEEL_TAGS)
    if len(files) != EXPECTED_FILE_COUNT or len(wheels) != EXPECTED_WHEEL_COUNT or not matched:
        message = f"Unexpected distributions: {[path.name for path in files]}"
        raise SystemExit(message)
    for name in ("rowx", "rowx_core", "rowx_postgres"):
        if not (args.directory / f"{name}-{version}.tar.gz").is_file():
            message = f"{name} source distribution is missing or has the wrong version"
            raise SystemExit(message)
    for name in ("rowx", "rowx_core"):
        if not (args.directory / f"{name}-{version}-py3-none-any.whl").is_file():
            message = f"{name} pure Python wheel is missing"
            raise SystemExit(message)
    for path in files:
        if path.suffix == ".whl":
            validate_wheel(path)
    return 0


def validate_wheel(path: Path) -> None:
    namespace = path.name.split("-", maxsplit=1)[0]
    with ZipFile(path) as wheel:
        names = wheel.namelist()
    for name in names:
        parts = Path(name).parts
        if "__tests__" in parts or "__pycache__" in parts or name.endswith((".rs", ".pyc", "Cargo.toml")):
            message = f"{path.name} contains a development file: {name}"
            raise SystemExit(message)
        if parts and parts[0] != namespace and not parts[0].endswith(".dist-info"):
            message = f"{path.name} owns an unexpected namespace: {name}"
            raise SystemExit(message)
        if namespace != "rowx_postgres" and name.endswith((".so", ".pyd", ".dll", ".dylib")):
            message = f"{path.name} must be pure Python"
            raise SystemExit(message)
    if f"{namespace}/py.typed" not in names:
        message = f"{path.name} is missing its typing marker"
        raise SystemExit(message)


def _required(name: str) -> str:
    if not (value := environ.get(name)):
        message = f"{name} is not set"
        raise SystemExit(message)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
