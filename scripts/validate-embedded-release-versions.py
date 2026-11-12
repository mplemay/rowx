#!/usr/bin/env python3
from os import environ
from pathlib import Path
from re import fullmatch
from tomllib import TOMLDecodeError, loads
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PACKAGE: Final[Path] = ROOT / "packages" / "rowx-embedded"
PUSH_EVENT: Final[str] = "push"
TAG_PREFIX: Final[str] = "embedded-v"
CRATES: Final[frozenset[str]] = frozenset({"rowx-embedded", "rowx-embedded-host"})


def main() -> int:
    project = _table(_read(PACKAGE / "pyproject.toml"), "project")
    version = project.get("version")
    if not isinstance(version, str) or fullmatch(r"\d+\.\d+\.\d+", version) is None:
        message = f"rowx-embedded needs a static <pg major>.<pg minor>.<rev> version, found {version!r}"
        raise SystemExit(message)
    if "dynamic" in project:
        message = "rowx-embedded metadata must be fully static (no dynamic fields)"
        raise SystemExit(message)
    versions: dict[str, str] = {
        "Cargo.toml": _version(_table(_read(PACKAGE / "Cargo.toml"), "package")),
        "host/Cargo.toml": _version(_table(_read(PACKAGE / "crates/rowx-embedded-host/Cargo.toml"), "package")),
    }
    locked = _locked(_read(PACKAGE / "Cargo.lock"))
    if locked.keys() != CRATES:
        message = f"packages/rowx-embedded/Cargo.lock holds {sorted(locked)}, expected {sorted(CRATES)}"
        raise SystemExit(message)
    versions.update({f"Cargo.lock:{name}": found for name, found in locked.items()})
    if environ.get("EVENT_NAME") == PUSH_EVENT:
        tag = _required("TAG_NAME")
        versions["tag"] = tag.removeprefix(TAG_PREFIX)
    if any(found != version for found in versions.values()):
        message = f"Release version mismatch: expected {version}, got {versions}"
        raise SystemExit(message)
    _check_extra(version)
    if output := environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
    return 0


def _check_extra(version: str) -> None:
    # the root `embedded` extra must admit this release, or `rowx[embedded]` would never select it
    extras = _table(_read(ROOT / "pyproject.toml"), "project").get("optional-dependencies")
    requirements = extras.get("embedded", []) if isinstance(extras, dict) else []
    for requirement in requirements:
        if not isinstance(requirement, str) or not requirement.startswith("rowx-embedded"):
            continue
        bounds = fullmatch(r"rowx-embedded>=(\d+)\.(\d+),<(\d+)\s*;.*", requirement)
        if bounds is None:
            message = f"unrecognised rowx-embedded requirement in the embedded extra: {requirement}"
            raise SystemExit(message)
        low = (int(bounds[1]), int(bounds[2]))
        major, minor, _revision = (int(part) for part in version.split("."))
        if not low <= (major, minor) or major >= int(bounds[3]):
            message = f"the embedded extra ({requirement}) does not admit rowx-embedded {version}"
            raise SystemExit(message)
        return
    message = "the root pyproject.toml has no rowx-embedded requirement in the embedded extra"
    raise SystemExit(message)


def _required(name: str) -> str:
    if not (value := environ.get(name)):
        message = f"{name} is not set"
        raise SystemExit(message)
    return value


def _read(path: Path) -> dict[str, object]:
    try:
        loaded = loads(path.read_text(encoding="utf-8"))
    except (TOMLDecodeError, UnicodeDecodeError) as error:
        message = f"{path.name}: {error}"
        raise SystemExit(message) from None
    return {key: value for key, value in loaded.items() if isinstance(key, str)}


def _table(document: dict[str, object], name: str) -> dict[str, object]:
    table = document.get(name)
    if not isinstance(table, dict):
        message = f"missing [{name}]"
        raise SystemExit(message)
    return {key: value for key, value in table.items() if isinstance(key, str)}


def _version(table: dict[str, object]) -> str:
    version = table.get("version")
    if not isinstance(version, str):
        message = "missing a string version"
        raise SystemExit(message)
    return version


def _locked(document: dict[str, object]) -> dict[str, str]:
    packages = document.get("package")
    if not isinstance(packages, list):
        message = "Cargo.lock is missing package"
        raise SystemExit(message)
    return {
        package["name"]: package["version"]
        for package in packages
        if isinstance(package, dict)
        and package.get("name") in CRATES
        and isinstance(package.get("name"), str)
        and isinstance(package.get("version"), str)
    }


if __name__ == "__main__":
    raise SystemExit(main())
