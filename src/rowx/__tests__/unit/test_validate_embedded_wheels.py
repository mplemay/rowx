import sys
from collections.abc import Callable
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Final, cast
from zipfile import ZipFile

import pytest

REPO: Final[Path] = Path(__file__).resolve().parents[4]
SCRIPT: Final[Path] = REPO / "scripts" / "validate-embedded-wheels.py"
PACKAGE: Final[Path] = REPO / "packages" / "rowx-embedded"
CHECKER_MODULE: Final[str] = "rowx_test_validate_embedded_wheels"
VERSION: Final[str] = "0.3.0"
REQUIRES: Final[tuple[str, ...]] = (f"rowx-core=={VERSION}", f"rowx-postgres=={VERSION}")
WHEEL_NAME: Final[str] = f"rowx_embedded-{VERSION}-cp314-abi3-manylinux_2_17_x86_64.whl"
DIST_INFO: Final[str] = f"rowx_embedded-{VERSION}.dist-info"

type ValidateWheel = Callable[[Path], tuple[str, int]]


def _load() -> ValidateWheel:
    spec = spec_from_file_location(CHECKER_MODULE, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[CHECKER_MODULE] = module
    spec.loader.exec_module(module)
    return cast("ValidateWheel", module.__dict__["validate_wheel"])


VALIDATE_WHEEL: Final[ValidateWheel] = _load()


def _write_wheel(directory: Path, requires: tuple[str, ...], extra: tuple[str, ...] = ()) -> Path:
    metadata = "\n".join([
        "Metadata-Version: 2.4",
        "Name: rowx-embedded",
        f"Version: {VERSION}",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
        "License-File: LICENSE",
        "License-File: NOTICE",
        "",
    ])
    path = directory / WHEEL_NAME
    with ZipFile(path, "w") as wheel:
        wheel.writestr("rowx_embedded/_engine.abi3.so", b"engine")
        wheel.writestr("rowx_embedded/py.typed", b"")
        for name in extra:
            wheel.writestr(name, b"")
        wheel.writestr(f"{DIST_INFO}/METADATA", metadata)
        for name in ("LICENSE", "NOTICE"):
            wheel.writestr(f"{DIST_INFO}/licenses/{name}", (PACKAGE / name).read_bytes())
    return path


def test_wheel_requiring_the_matching_postgres_passes(tmp_path: Path) -> None:
    assert VALIDATE_WHEEL(_write_wheel(tmp_path, REQUIRES))[0] == WHEEL_NAME


@pytest.mark.parametrize(
    "requires",
    [
        (),
        (f"rowx-postgres=={VERSION}",),
        (f"rowx-core=={VERSION}", "rowx-postgres==9.9.9"),
        (*REQUIRES, "requests>=2"),
        (f"rowx-core=={VERSION}", "rowx-postgres>=0.3.0"),
    ],
)
def test_wheel_with_wrong_requires_dist_fails(tmp_path: Path, requires: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit, match="must depend only on"):
        VALIDATE_WHEEL(_write_wheel(tmp_path, requires))


@pytest.mark.parametrize("name", ["rowx_embedded/__tests__/conftest.py", "rowx_embedded/__pycache__/_api.pyc"])
def test_wheel_with_test_or_cache_files_fails(tmp_path: Path, name: str) -> None:
    with pytest.raises(SystemExit, match="development file"):
        VALIDATE_WHEEL(_write_wheel(tmp_path, REQUIRES, (name,)))


def test_wheel_with_the_python_glue_passes(tmp_path: Path) -> None:
    names = ("rowx_embedded/__init__.py", "rowx_embedded/_database.py", "rowx_embedded/_backend.py")
    assert VALIDATE_WHEEL(_write_wheel(tmp_path, REQUIRES, names))[0] == WHEEL_NAME
