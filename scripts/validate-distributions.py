#!/usr/bin/env python3
from os import environ
from pathlib import Path
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DIST: Final[Path] = ROOT / "dist"
EXPECTED_FILE_COUNT: Final[int] = 6
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
    version = _required("VERSION")
    files = list(DIST.iterdir())
    wheels = [path.name for path in files if path.name.startswith(f"rowx-{version}-") and path.suffix == ".whl"]
    # each tag tuple must occur in exactly one wheel name
    matched = all(sum(all(tag in name for tag in tags) for name in wheels) == MATCH_COUNT for tags in WHEEL_TAGS)
    if len(files) != EXPECTED_FILE_COUNT or len(wheels) != EXPECTED_WHEEL_COUNT or not matched:
        message = f"Unexpected distributions: {[path.name for path in files]}"
        raise SystemExit(message)
    if not (DIST / f"rowx-{version}.tar.gz").is_file():
        message = "Source distribution is missing or has the wrong version"
        raise SystemExit(message)
    return 0


def _required(name: str) -> str:
    if not (value := environ.get(name)):
        message = f"{name} is not set"
        raise SystemExit(message)
    return value


if __name__ == "__main__":
    raise SystemExit(main())
