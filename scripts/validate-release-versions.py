#!/usr/bin/env python3
from argparse import ArgumentParser
from os import environ
from pathlib import Path
from tomllib import TOMLDecodeError, loads
from typing import Final

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PUSH_EVENT: Final[str] = "push"
PACKAGE_NAME: Final[str] = "rowx"
PYTHON_PACKAGES: Final[tuple[str, ...]] = ("rowx", "rowx-core", "rowx-postgres", "rowx-embedded")
EMBEDDED_DIR: Final[str] = "packages/rowx-embedded"
# the embedded Cargo workspace is separate from the root one; its crates share the release version
EMBEDDED_CRATES: Final[frozenset[str]] = frozenset({"rowx-embedded", "rowx-embedded-host"})
EMBEDDED_MARKER: Final[str] = (
    "(sys_platform == 'darwin' and platform_machine == 'arm64') or "
    "(sys_platform == 'linux' and (platform_machine == 'x86_64' or platform_machine == 'aarch64'))"
)
WORKSPACE_PACKAGES: Final[frozenset[str]] = frozenset({
    "rowx-core",
    "rowx-postgres",
    "rowx-postgres-py",
    "rowx-py-core",
})


def main() -> int:
    parser = ArgumentParser()
    # CI checks this repository; tests pass --root to point at a fixture copy
    parser.add_argument("--root", type=Path, default=ROOT)
    root = parser.parse_args().root.resolve()
    version = _version(root / "pyproject.toml", ("project", "version"))
    versions: dict[str, str] = {"Cargo.toml": _version(root / "Cargo.toml", ("workspace", "package", "version"))}
    locked = _package_versions(root / "Cargo.lock")
    if not locked.keys() >= WORKSPACE_PACKAGES:
        message = "Missing workspace packages in Cargo.lock"
        raise SystemExit(message)
    versions.update(locked)
    versions.update({f"uv.lock:{name}": _named_version(root / "uv.lock", name) for name in PYTHON_PACKAGES})
    for name in PYTHON_PACKAGES[1:]:
        versions[f"{name}/pyproject.toml"] = _version(
            root / "packages" / name / "pyproject.toml", ("project", "version")
        )
    project = _read(root / "pyproject.toml")
    root_project = project["project"]
    postgres_project = _read(root / "packages/rowx-postgres/pyproject.toml")["project"]
    required_core = f"rowx-core=={version}"
    if not isinstance(root_project, dict) or required_core not in root_project.get("dependencies", []):
        message = "rowx must pin rowx-core to its own version"
        raise SystemExit(message)
    if not isinstance(postgres_project, dict) or postgres_project.get("dependencies") != [required_core]:
        message = "rowx-postgres must depend only on the matching rowx-core"
        raise SystemExit(message)
    extras = root_project.get("optional-dependencies")
    if not isinstance(extras, dict) or extras.get("postgres") != [f"rowx-postgres=={version}"]:
        message = "rowx[postgres] must pin the matching rowx-postgres"
        raise SystemExit(message)
    expected_embedded = [f"rowx-postgres=={version}", f"rowx-embedded=={version}; {EMBEDDED_MARKER}"]
    if extras.get("embedded") != expected_embedded:
        message = f"rowx[embedded] must be exactly {expected_embedded}"
        raise SystemExit(message)
    _check_embedded(root, version, versions)
    _require_dev_pins(project, version)
    if _required("EVENT_NAME") == PUSH_EVENT:
        versions["tag"] = _required("TAG_NAME")[1:]
    if any(value != version for value in versions.values()):
        message = f"Release version mismatch: expected {version}, got {versions}"
        raise SystemExit(message)
    if output := environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
    return 0


def _check_embedded(root: Path, version: str, versions: dict[str, str]) -> None:
    package = root / EMBEDDED_DIR
    embedded_project = _read(package / "pyproject.toml")["project"]
    required = [f"rowx-core=={version}", f"rowx-postgres=={version}"]
    if not isinstance(embedded_project, dict) or embedded_project.get("dependencies") != required:
        message = f"rowx-embedded must depend exactly on {required}"
        raise SystemExit(message)
    versions["rowx-embedded/Cargo.toml"] = _version(package / "Cargo.toml", ("package", "version"))
    versions["rowx-embedded-host/Cargo.toml"] = _version(
        package / "crates/rowx-embedded-host/Cargo.toml", ("package", "version")
    )
    locked = _package_versions(package / "Cargo.lock", EMBEDDED_CRATES)
    if locked.keys() != EMBEDDED_CRATES:
        message = f"{EMBEDDED_DIR}/Cargo.lock holds {sorted(locked)}, expected {sorted(EMBEDDED_CRATES)}"
        raise SystemExit(message)
    versions.update({f"embedded Cargo.lock:{name}": found for name, found in locked.items()})
    # the embedded workspace repeats the root lint table, so a lint change must land in both
    if _lints(root / "Cargo.toml") != _lints(package / "Cargo.toml"):
        message = f"[workspace.lints] in {EMBEDDED_DIR}/Cargo.toml differs from the root Cargo.toml"
        raise SystemExit(message)


def _lints(path: Path) -> object:
    workspace = _read(path).get("workspace")
    return workspace.get("lints") if isinstance(workspace, dict) else None


def _require_dev_pins(project: dict[str, object], version: str) -> None:
    groups = project.get("dependency-groups")
    dev = groups.get("dev") if isinstance(groups, dict) else None
    for name in ("rowx-postgres", "rowx-embedded"):
        if not isinstance(dev, list) or f"{name}=={version}" not in dev:
            message = f"dev dependency group must pin {name} to the release version"
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


def _package_versions(path: Path, names: frozenset[str] = WORKSPACE_PACKAGES) -> dict[str, str]:
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
        if isinstance(name, str) and name in names and isinstance(version, str):
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
