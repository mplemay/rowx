#!/usr/bin/env python3
from argparse import ArgumentParser
from email.parser import Parser
from os import environ
from pathlib import Path
from re import fullmatch
from typing import Final
from zipfile import ZipFile

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PACKAGE: Final[Path] = ROOT / "packages" / "rowx-embedded"
NAME: Final[str] = "rowx-embedded"
NAMESPACE: Final[str] = "rowx_embedded"
BUDGET_BYTES: Final[int] = 40 * 1000 * 1000
PYPI_LIMIT_BYTES: Final[int] = 100 * 1024 * 1024
WHEEL_PARTS: Final[int] = 5
VERSION_INDEX: Final[int] = 1
ABI_TAGS: Final[tuple[str, str]] = ("cp314", "abi3")
LINUX_TAG: Final[str] = r"manylinux_2_\d+_(x86_64|aarch64)"
LICENSE_FILES: Final[tuple[str, ...]] = ("LICENSE", "NOTICE")
NOTICE_SECTIONS: Final[tuple[str, ...]] = ("PostgreSQL", "oliphaunt-wasix", "Wasmer", "ICU", "OpenSSL")
DEVELOPMENT_SUFFIXES: Final[tuple[str, ...]] = (".rs", ".pyc", "Cargo.toml", "Cargo.lock")
MEGABYTE: Final[int] = 1_000_000


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--platform", action="append", default=[], help="a platform that must be present exactly once")
    args = parser.parse_args()
    wheels = sorted(args.directory.glob("*.whl"))
    others = sorted(path.name for path in args.directory.iterdir() if path.suffix != ".whl")
    if others:
        message = f"unexpected non-wheel files (rowx-embedded ships no sdist): {others}"
        raise SystemExit(message)
    if not wheels:
        message = f"no wheels in {args.directory}"
        raise SystemExit(message)
    rows = [validate_wheel(path) for path in wheels]
    for platform in args.platform:
        count = sum(platform in row[0] for row in rows)
        if count != 1:
            message = f"expected exactly one {platform} wheel, found {count}"
            raise SystemExit(message)
    report = "\n".join(f"| {name} | {size / MEGABYTE:.1f} MB |" for name, size in rows)
    table = f"| wheel | compressed size |\n|---|---|\n{report}\n"
    print(table)  # ruff: ignore[print]
    if summary := environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(table)
    return 0


def validate_wheel(path: Path) -> tuple[str, int]:
    parts = path.name.removesuffix(".whl").split("-")
    if len(parts) != WHEEL_PARTS or parts[0] != NAMESPACE or tuple(parts[2:4]) != ABI_TAGS:
        message = f"{path.name} is not a {NAMESPACE} cp314-abi3 wheel"
        raise SystemExit(message)
    platform = parts[4]
    if not _platform_ok(platform):
        message = f"{path.name} has platform tag {platform}, expected macosx arm64 or manylinux_2_N x86_64/aarch64"
        raise SystemExit(message)
    size = path.stat().st_size
    if size >= PYPI_LIMIT_BYTES:
        message = f"{path.name} is {size} bytes, over PyPI's default 100 MiB file limit"
        raise SystemExit(message)
    if size > BUDGET_BYTES:
        message = f"{path.name} is {size / MEGABYTE:.1f} MB, over the {BUDGET_BYTES // MEGABYTE} MB budget (#198)"
        raise SystemExit(message)
    with ZipFile(path) as wheel:
        names = wheel.namelist()
        _validate_contents(path, names)
        _validate_licenses(path, wheel, names)
        _validate_metadata(path, wheel, names, parts[VERSION_INDEX])
    return path.name, size


def _platform_ok(platform: str) -> bool:
    if platform.startswith("macosx_"):
        return platform.endswith("_arm64")
    # maturin may also add the legacy alias tags (manylinux2014_*) after the primary one
    return any(fullmatch(LINUX_TAG, tag) for tag in platform.split("."))


def _validate_contents(path: Path, names: list[str]) -> None:
    engines = [name for name in names if name.startswith(f"{NAMESPACE}/_engine") and name.endswith((".so", ".pyd"))]
    if len(engines) != 1:
        message = f"{path.name} must hold exactly one compiled engine, found {engines}"
        raise SystemExit(message)
    if f"{NAMESPACE}/py.typed" not in names:
        message = f"{path.name} is missing its typing marker"
        raise SystemExit(message)
    for name in names:
        parts = Path(name).parts
        if parts[0] != NAMESPACE and not parts[0].endswith(".dist-info"):
            message = f"{path.name} owns an unexpected top-level entry: {name}"
            raise SystemExit(message)
        if name.endswith(DEVELOPMENT_SUFFIXES) or "__pycache__" in parts or "tests" in parts or "__tests__" in parts:
            message = f"{path.name} contains a development file: {name}"
            raise SystemExit(message)


def _validate_licenses(path: Path, wheel: ZipFile, names: list[str]) -> None:
    for file_name in LICENSE_FILES:
        matches = [name for name in names if name.endswith(f".dist-info/licenses/{file_name}")]
        if len(matches) != 1:
            message = f"{path.name} must carry {file_name} under dist-info/licenses, found {matches}"
            raise SystemExit(message)
        if wheel.read(matches[0]) != (PACKAGE / file_name).read_bytes():
            message = f"{path.name} {file_name} differs from packages/rowx-embedded/{file_name}"
            raise SystemExit(message)
    notice = (PACKAGE / "NOTICE").read_text(encoding="utf-8")
    missing = [section for section in NOTICE_SECTIONS if section not in notice]
    if missing:
        message = f"NOTICE does not cover {missing}"
        raise SystemExit(message)


def _validate_metadata(path: Path, wheel: ZipFile, names: list[str], version: str) -> None:
    (entry,) = (name for name in names if name.endswith(".dist-info/METADATA"))
    metadata = Parser().parsestr(wheel.read(entry).decode("utf-8"))
    problems = []
    if metadata.get("Name") != NAME:
        problems.append(f"Name is {metadata.get('Name')!r}")
    if metadata.get("Version") != version:
        problems.append(f"Version is {metadata.get('Version')!r}, expected {version!r}")
    expected = [f"rowx-core=={version}", f"rowx-postgres=={version}"]
    if sorted(requires := metadata.get_all("Requires-Dist") or []) != expected:
        problems.append(f"it must depend only on {expected}, found {requires}")
    if sorted(metadata.get_all("License-File", [])) != sorted(LICENSE_FILES):
        problems.append(f"License-File is {metadata.get_all('License-File')}")
    if problems:
        message = f"{path.name}: {'; '.join(problems)}"
        raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())
