#!/usr/bin/env python3
from os import environ
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PUSH_EVENT: Final[str] = "push"
PACKAGE_NAME: Final[str] = "rowx"
WORKSPACE_PACKAGES: Final[frozenset[str]] = frozenset({
    "rowx-core",
    "rowx-postgres",
    "rowx-postgres-py",
    "rowx-py-core",
})


def main() -> int:
    version = _version(ROOT / "pyproject.toml", ("project", "version"))
    versions: dict[str, str] = {"Cargo.toml": _version(ROOT / "Cargo.toml", ("workspace", "package", "version"))}
    locked = _package_versions(ROOT / "Cargo.lock")
    if not locked.keys() >= WORKSPACE_PACKAGES:
        message = "Missing workspace packages in Cargo.lock"
        raise SystemExit(message)
    versions.update(locked)
    versions["uv.lock"] = _named_version(ROOT / "uv.lock", PACKAGE_NAME)
    if _required("EVENT_NAME") == PUSH_EVENT:
        versions["tag"] = _required("TAG_NAME")[1:]
    if any(value != version for value in versions.values()):
        message = f"Release version mismatch: expected {version}, got {versions}"
        raise SystemExit(message)
    output = Path(_required("GITHUB_OUTPUT"))
    with output.open("a", encoding="utf-8") as handle:
        handle.write(f"version={version}\n")
    return 0


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


def _version(path: Path, keys: tuple[str, ...]) -> str:
    current: object = _read(path)
    label = ".".join(keys)
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            message = f"{path.name} is missing {label}"
            raise SystemExit(message)
        current = current[key]
    if not isinstance(current, str):
        message = f"{path.name} {label} is not a string"
        raise SystemExit(message)
    return current


def _package_versions(path: Path) -> dict[str, str]:
    packages = _read(path).get("package")
    if not isinstance(packages, list):
        message = f"{path.name} is missing package"
        raise SystemExit(message)
    versions: dict[str, str] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if isinstance(name, str) and name in WORKSPACE_PACKAGES and isinstance(version, str):
            versions[name] = version
    return versions


def _named_version(path: Path, name: str) -> str:
    packages = _read(path).get("package")
    if not isinstance(packages, list):
        message = f"{path.name} is missing package"
        raise SystemExit(message)
    for package in packages:
        if not isinstance(package, dict):
            continue
        if package.get("name") == name and isinstance(version := package.get("version"), str):
            return version
    message = f"{path.name} is missing {name}"
    raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())
