import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Final, cast
from zipfile import ZipFile

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[4]
SCRIPT: Final[Path] = REPO / "scripts" / "validate-distributions.py"
CHECKER_MODULE: Final[str] = "rowx_test_validate_distributions"
VERSION: Final[str] = "0.2.0"
ROWX: Final[str] = "rowx"
ROWX_CORE: Final[str] = "rowx_core"
ROWX_POSTGRES: Final[str] = "rowx_postgres"
ROWX_WHEEL: Final[str] = "rowx-0.2.0-py3-none-any.whl"
CORE_WHEEL: Final[str] = "rowx_core-0.2.0-py3-none-any.whl"
POSTGRES_WHEEL: Final[str] = "rowx_postgres-0.2.0-py3-none-any.whl"
WHEELS: Final[tuple[str, ...]] = (ROWX_WHEEL, CORE_WHEEL, POSTGRES_WHEEL)
KEYWORDS: Final[str] = "sql,postgresql,postgres,typed,async,t-string"
DOTTED_ROWX_POSTGRES: Final[str] = "rowx.postgres==0.2.0"
REQUESTS_REQUIREMENT: Final[str] = "requests>=2"
WRONG_NAME: Final[str] = "not-rowx"
WRONG_VERSION: Final[str] = "9.9.9"
CLASSIFIERS: Final[tuple[str, ...]] = (
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
    "License :: OSI Approved :: MIT License",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.14",
    "Programming Language :: Python :: 3 :: Only",
    "Typing :: Typed",
    "Topic :: Database",
    "Framework :: AsyncIO",
)
PROJECT_URLS: Final[tuple[tuple[str, str], ...]] = (
    ("Documentation", "https://mplemay.github.io/rowx/"),
    ("Repository", "https://github.com/mplemay/rowx"),
    ("Issues", "https://github.com/mplemay/rowx/issues"),
    ("Changelog", "https://github.com/mplemay/rowx/blob/main/docs/changelog.md"),
)
DISTRIBUTION_NAMES: Final[dict[str, str]] = {
    ROWX: ROWX,
    ROWX_CORE: "rowx-core",
    ROWX_POSTGRES: "rowx-postgres",
}
LICENSE_PATHS: Final[dict[str, Path]] = {
    ROWX: REPO / "LICENSE",
    ROWX_CORE: REPO / "packages" / "rowx-core" / "LICENSE",
    ROWX_POSTGRES: REPO / "packages" / "rowx-postgres" / "LICENSE",
}

type ValidateWheel = Callable[[Path], None]


@dataclass(slots=True, kw_only=True, frozen=True)
class WheelBuild:
    filename: str
    metadata_name: str | None = None
    metadata_version: str | None = None
    requires: tuple[str, ...] | None = None
    license_entry: str | None = None
    license_body: bytes | None = None
    include_license: bool = True
    extras: tuple[tuple[str, bytes], ...] = ()


def _load_validate_wheel() -> ValidateWheel:
    spec = spec_from_file_location(CHECKER_MODULE, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[CHECKER_MODULE] = module
    spec.loader.exec_module(module)
    return cast("ValidateWheel", module.__dict__["validate_wheel"])


VALIDATE_WHEEL: Final[ValidateWheel] = _load_validate_wheel()


def _default_requires(namespace: str) -> tuple[str, ...]:
    core = f"rowx-core=={VERSION}"
    if namespace == ROWX:
        return (
            core,
            f"rowx-postgres=={VERSION} ; extra == 'postgres'",
            "pytest>=9.2.0 ; extra == 'pytest'",
        )
    if namespace == ROWX_CORE:
        return ()
    return (core,)


def _metadata(name: str, version: str, requires: tuple[str, ...]) -> str:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        "License-Expression: MIT",
        "License-File: LICENSE",
    ]
    lines.extend(f"Classifier: {classifier}" for classifier in CLASSIFIERS)
    lines.append(f"Keywords: {KEYWORDS}")
    lines.extend(f"Project-URL: {label}, {url}" for label, url in PROJECT_URLS)
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    return "\n".join(lines) + "\n"


def _write_wheel(directory: Path, build: WheelBuild) -> Path:
    namespace = build.filename.split("-", maxsplit=1)[0]
    dist_info = f"{namespace}-{VERSION}.dist-info"
    name = DISTRIBUTION_NAMES[namespace] if build.metadata_name is None else build.metadata_name
    version = VERSION if build.metadata_version is None else build.metadata_version
    requires = _default_requires(namespace) if build.requires is None else build.requires
    path = directory / build.filename
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{namespace}/py.typed", b"")
        wheel.writestr(f"{dist_info}/METADATA", _metadata(name, version, requires))
        if build.include_license:
            entry = f"{dist_info}/licenses/LICENSE" if build.license_entry is None else build.license_entry
            body = LICENSE_PATHS[namespace].read_bytes() if build.license_body is None else build.license_body
            wheel.writestr(f"{dist_info}/licenses/", b"")
            wheel.writestr(entry, body)
        for entry, body in build.extras:
            wheel.writestr(entry, body)
    return path


def test_minimal_valid_wheels_pass(tmp_path: Path) -> None:
    for filename in WHEELS:
        VALIDATE_WHEEL(_write_wheel(tmp_path, WheelBuild(filename=filename)))


def test_core_wheel_rejects_requires_dist_rowx_postgres(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=CORE_WHEEL, requires=(DOTTED_ROWX_POSTGRES,)))
    with pytest.raises(SystemExit, match="must not declare Requires-Dist"):
        VALIDATE_WHEEL(path)


def test_core_wheel_rejects_requires_dist_requests(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=CORE_WHEEL, requires=(REQUESTS_REQUIREMENT,)))
    with pytest.raises(SystemExit, match="must not declare Requires-Dist"):
        VALIDATE_WHEEL(path)


def test_renamed_license_entry_fails(tmp_path: Path) -> None:
    entry = f"{ROWX}-{VERSION}.dist-info/licenses/NOT-LICENSE"
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, license_entry=entry))
    with pytest.raises(SystemExit, match="license file is not"):
        VALIDATE_WHEEL(path)


def test_decoy_license_path_fails(tmp_path: Path) -> None:
    decoy = f"{ROWX}/data/.dist-info/licenses/decoy.txt"
    path = _write_wheel(
        tmp_path,
        WheelBuild(filename=ROWX_WHEEL, include_license=False, extras=((decoy, b"decoy"),)),
    )
    with pytest.raises(SystemExit, match="license file is not"):
        VALIDATE_WHEEL(path)


def test_license_bytes_mismatch_fails(tmp_path: Path) -> None:
    body = b"x" + LICENSE_PATHS[ROWX].read_bytes()
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, license_body=body))
    with pytest.raises(SystemExit, match="license bytes do not match"):
        VALIDATE_WHEEL(path)


def test_rowx_name_mismatch_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, metadata_name=WRONG_NAME))
    with pytest.raises(SystemExit, match="Name is 'not-rowx'"):
        VALIDATE_WHEEL(path)


def test_rowx_version_mismatch_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, metadata_version=WRONG_VERSION))
    with pytest.raises(SystemExit, match=r"Version is '9\.9\.9'"):
        VALIDATE_WHEEL(path)
