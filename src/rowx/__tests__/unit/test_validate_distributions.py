import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from pathlib import Path
from tarfile import TarInfo, open as tar_open
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
ROWX_SQLITE: Final[str] = "rowx_sqlite"
ROWX_WHEEL: Final[str] = "rowx-0.2.0-py3-none-any.whl"
CORE_WHEEL: Final[str] = "rowx_core-0.2.0-py3-none-any.whl"
POSTGRES_WHEEL: Final[str] = "rowx_postgres-0.2.0-py3-none-any.whl"
SQLITE_WHEEL: Final[str] = "rowx_sqlite-0.2.0-py3-none-any.whl"
WHEELS: Final[tuple[str, ...]] = (ROWX_WHEEL, CORE_WHEEL, POSTGRES_WHEEL, SQLITE_WHEEL)
KEYWORDS: Final[str] = "sql,postgresql,postgres,typed,async,t-string"
SQLITE_KEYWORDS: Final[str] = "sql,sqlite,typed,async,t-string"
NATIVE_PLATFORMS: Final[tuple[str, ...]] = (
    "cp314-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64",
    "cp314-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64",
    "cp314-abi3-macosx_10_12_x86_64",
    "cp314-abi3-macosx_11_0_arm64",
    "cp314-abi3-win_amd64",
)
DOTTED_ROWX_POSTGRES: Final[str] = "rowx.postgres==0.2.0"
REQUESTS_REQUIREMENT: Final[str] = "requests>=2"
CORE_REQUIREMENT: Final[str] = f"rowx-core=={VERSION}"
POSTGRES_PIN: Final[str] = f"rowx-postgres=={VERSION}"
EMBEDDED_PIN: Final[str] = f"rowx-embedded=={VERSION}"
SQLITE_EXTRA_REQUIREMENT: Final[str] = f"rowx-sqlite=={VERSION} ; extra == 'sqlite'"
PYTEST_REQUIREMENT: Final[str] = "pytest>=9.2.0 ; extra == 'pytest'"
DARWIN_CLAUSE: Final[str] = "platform_machine == 'arm64' and sys_platform == 'darwin' and extra == 'embedded'"
LINUX_AARCH64_CLAUSE: Final[str] = "platform_machine == 'aarch64' and sys_platform == 'linux' and extra == 'embedded'"
LINUX_X86_CLAUSE: Final[str] = "platform_machine == 'x86_64' and sys_platform == 'linux' and extra == 'embedded'"
WRONG_NAME: Final[str] = "not-rowx"
WRONG_VERSION: Final[str] = "9.9.9"
CLASSIFIERS: Final[tuple[str, ...]] = (
    "Development Status :: 4 - Beta",
    "Intended Audience :: Developers",
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
    ROWX_SQLITE: "rowx-sqlite",
}
LICENSE_PATHS: Final[dict[str, Path]] = {
    ROWX: REPO / "LICENSE",
    ROWX_CORE: REPO / "packages" / "rowx-core" / "LICENSE",
    ROWX_POSTGRES: REPO / "packages" / "rowx-postgres" / "LICENSE",
    ROWX_SQLITE: REPO / "packages" / "rowx-sqlite" / "LICENSE",
}

type ValidateWheel = Callable[[Path], None]
type ValidateSdist = Callable[[Path], None]
type ValidateRelease = Callable[[Path, str], None]


@dataclass(slots=True, kw_only=True, frozen=True)
class WheelBuild:
    filename: str
    metadata_name: str | None = None
    metadata_version: str | None = None
    requires: tuple[str, ...] | None = None
    license_entry: str | None = None
    license_body: bytes | None = None
    license_header: str = "LICENSE"
    include_license: bool = True
    extras: tuple[tuple[str, bytes], ...] = ()
    keywords: str | None = None


def _load_checkers() -> tuple[ValidateWheel, ValidateSdist, ValidateRelease]:
    spec = spec_from_file_location(CHECKER_MODULE, SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[CHECKER_MODULE] = module
    spec.loader.exec_module(module)
    return (
        cast("ValidateWheel", module.__dict__["validate_wheel"]),
        cast("ValidateSdist", module.__dict__["validate_pure_sdist"]),
        cast("ValidateRelease", module.__dict__["validate_release"]),
    )


CHECKERS: Final[tuple[ValidateWheel, ValidateSdist, ValidateRelease]] = _load_checkers()
VALIDATE_WHEEL: Final[ValidateWheel] = CHECKERS[0]
VALIDATE_PURE_SDIST: Final[ValidateSdist] = CHECKERS[1]
VALIDATE_RELEASE: Final[ValidateRelease] = CHECKERS[2]


def _embedded_requirement(*clauses: str, specifier: str = EMBEDDED_PIN) -> str:
    return f"{specifier} ; " + " or ".join(f"({clause})" for clause in clauses)


def _default_requires(namespace: str) -> tuple[str, ...]:
    core = f"rowx-core=={VERSION}"
    if namespace == ROWX:
        return (
            core,
            "pathspec<2,>=1.1.1",
            f"{POSTGRES_PIN} ; extra == 'embedded'",
            _embedded_requirement(DARWIN_CLAUSE, LINUX_AARCH64_CLAUSE, LINUX_X86_CLAUSE),
            f"{POSTGRES_PIN} ; extra == 'postgres'",
            SQLITE_EXTRA_REQUIREMENT,
            PYTEST_REQUIREMENT,
        )
    if namespace == ROWX_CORE:
        return ()
    return (core,)


def _metadata(name: str, version: str, requires: tuple[str, ...], license_header: str, keywords: str) -> str:
    lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        "License-Expression: MIT",
        f"License-File: {license_header}",
    ]
    lines.extend(f"Classifier: {classifier}" for classifier in CLASSIFIERS)
    lines.append(f"Keywords: {keywords}")
    lines.extend(f"Project-URL: {label}, {url}" for label, url in PROJECT_URLS)
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requires)
    return "\n".join(lines) + "\n"


def _write_wheel(directory: Path, build: WheelBuild) -> Path:
    namespace = build.filename.split("-", maxsplit=1)[0]
    dist_info = f"{namespace}-{VERSION}.dist-info"
    name = DISTRIBUTION_NAMES[namespace] if build.metadata_name is None else build.metadata_name
    version = VERSION if build.metadata_version is None else build.metadata_version
    requires = _default_requires(namespace) if build.requires is None else build.requires
    keywords = (SQLITE_KEYWORDS if namespace == ROWX_SQLITE else KEYWORDS) if build.keywords is None else build.keywords
    path = directory / build.filename
    with ZipFile(path, "w") as wheel:
        wheel.writestr(f"{namespace}/py.typed", b"")
        wheel.writestr(
            f"{dist_info}/METADATA",
            _metadata(name, version, requires, build.license_header, keywords),
        )
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


@pytest.mark.parametrize("requirement", [None, "pathspec>=1.1.1", "pathspec>=1.0,<2", "requests>=2"])
def test_rowx_wheel_rejects_missing_or_unapproved_direct_dependency(tmp_path: Path, requirement: str | None) -> None:
    requires = tuple(value for value in (f"rowx-core=={VERSION}", requirement) if value is not None)
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, requires=requires))
    with pytest.raises(SystemExit, match="direct requirements"):
        VALIDATE_WHEEL(path)


def test_postgres_wheel_rejects_pathspec_dependency(tmp_path: Path) -> None:
    path = _write_wheel(
        tmp_path,
        WheelBuild(filename=POSTGRES_WHEEL, requires=(f"rowx-core=={VERSION}", "pathspec<2,>=1.1.1")),
    )
    with pytest.raises(SystemExit, match="requirements are"):
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


def test_nested_license_path_fails(tmp_path: Path) -> None:
    entry = f"{ROWX}/data/.dist-info/licenses/LICENSE"
    path = _write_wheel(
        tmp_path,
        WheelBuild(
            filename=ROWX_WHEEL,
            include_license=False,
            extras=((entry, LICENSE_PATHS[ROWX].read_bytes()),),
        ),
    )
    with pytest.raises(SystemExit, match="license file is not"):
        VALIDATE_WHEEL(path)


def test_license_file_header_mismatch_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, license_header="COPYING"))
    with pytest.raises(SystemExit, match="License-File"):
        VALIDATE_WHEEL(path)


def test_two_license_entries_fail(tmp_path: Path) -> None:
    entry = "other.dist-info/licenses/LICENSE"
    path = _write_wheel(
        tmp_path,
        WheelBuild(filename=ROWX_WHEEL, extras=((entry, LICENSE_PATHS[ROWX].read_bytes()),)),
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


def test_packaged_skill_is_a_development_file(tmp_path: Path) -> None:
    entry = f"{ROWX}/.agents/skills/use-rowx/SKILL.md"
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, extras=((entry, b"# skill\n"),)))
    with pytest.raises(SystemExit, match=r"contains a development file: rowx/\.agents/skills/use-rowx/SKILL\.md"):
        VALIDATE_WHEEL(path)


def test_rowx_version_mismatch_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, metadata_version=WRONG_VERSION))
    with pytest.raises(SystemExit, match=r"Version is '9\.9\.9'"):
        VALIDATE_WHEEL(path)


def _rowx_requires(*requirements: str) -> tuple[str, ...]:
    return (CORE_REQUIREMENT, "pathspec<2,>=1.1.1", SQLITE_EXTRA_REQUIREMENT, *requirements)


EMBEDDED_POSTGRES: Final[str] = f"{POSTGRES_PIN} ; extra == 'embedded'"
POSTGRES_EXTRA_REQUIREMENT: Final[str] = f"{POSTGRES_PIN} ; extra == 'postgres'"
ALL_CLAUSES: Final[tuple[str, ...]] = (DARWIN_CLAUSE, LINUX_AARCH64_CLAUSE, LINUX_X86_CLAUSE)
# uv 0.13.9 factors the repeated linux machines into one parenthesized or
FACTORED_EMBEDDED_MARKER: Final[str] = (
    "platform_machine == 'arm64' and sys_platform == 'darwin' and extra == 'embedded'"
    " or (platform_machine == 'aarch64' or platform_machine == 'x86_64')"
    " and sys_platform == 'linux' and extra == 'embedded'"
)


def test_rowx_wheel_accepts_reordered_embedded_clauses(tmp_path: Path) -> None:
    requires = _rowx_requires(
        POSTGRES_EXTRA_REQUIREMENT,
        _embedded_requirement(*reversed(ALL_CLAUSES)),
        PYTEST_REQUIREMENT,
        EMBEDDED_POSTGRES,
    )
    VALIDATE_WHEEL(_write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, requires=requires)))


def test_rowx_wheel_accepts_factored_embedded_marker(tmp_path: Path) -> None:
    requires = _rowx_requires(
        POSTGRES_EXTRA_REQUIREMENT,
        f"{EMBEDDED_PIN} ; {FACTORED_EMBEDDED_MARKER}",
        PYTEST_REQUIREMENT,
        EMBEDDED_POSTGRES,
    )
    VALIDATE_WHEEL(_write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, requires=requires)))


@pytest.mark.parametrize(
    ("requires", "match"),
    [
        pytest.param(
            _rowx_requires(EMBEDDED_POSTGRES, POSTGRES_EXTRA_REQUIREMENT, PYTEST_REQUIREMENT),
            "embedded requirements are \\[\\]",
            id="missing-companion",
        ),
        pytest.param(
            _rowx_requires(
                POSTGRES_EXTRA_REQUIREMENT,
                PYTEST_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES),
            ),
            "postgres requirements are",
            id="missing-embedded-postgres",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES.replace(VERSION, WRONG_VERSION),
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES),
            ),
            "postgres requirements are",
            id="embedded-postgres-wrong-pin",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES, specifier=f"rowx-embedded=={WRONG_VERSION}"),
            ),
            "embedded requirements are",
            id="wrong-range",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES[:2]),
            ),
            "not a single extra",
            id="missing-platform",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(
                    *ALL_CLAUSES,
                    "platform_machine == 'AMD64' and sys_platform == 'win32' and extra == 'embedded'",
                ),
            ),
            "not a single extra",
            id="extra-platform",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES[:2], LINUX_X86_CLAUSE.replace(" and extra == 'embedded'", "")),
            ),
            "not a single extra",
            id="clause-without-extra",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*(clause.replace("'embedded'", "'postgres'") for clause in ALL_CLAUSES)),
            ),
            "not a single extra",
            id="wrong-extra",
        ),
        pytest.param(
            _rowx_requires(EMBEDDED_POSTGRES, POSTGRES_EXTRA_REQUIREMENT, f"{EMBEDDED_PIN} ; extra == 'embedded'"),
            "embedded requirements are",
            id="no-platform-marker",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES),
                EMBEDDED_PIN,
            ),
            "direct requirements are",
            id="unconditional-companion",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES),
                _embedded_requirement(*ALL_CLAUSES),
            ),
            "embedded requirements are",
            id="duplicate-companion",
        ),
        pytest.param(
            _rowx_requires(
                EMBEDDED_POSTGRES,
                POSTGRES_EXTRA_REQUIREMENT,
                _embedded_requirement(*ALL_CLAUSES),
                f"{REQUESTS_REQUIREMENT} ; sys_platform == 'linux' and extra == 'embedded'",
            ),
            "not a single extra",
            id="other-distribution-with-platform-marker",
        ),
    ],
)
def test_rowx_wheel_rejects_malformed_embedded_extra(tmp_path: Path, requires: tuple[str, ...], match: str) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, requires=requires))
    with pytest.raises(SystemExit, match=match):
        VALIDATE_WHEEL(path)


@pytest.mark.parametrize(
    "sqlite_requirements",
    [
        pytest.param((), id="missing"),
        pytest.param((f"rowx-sqlite=={WRONG_VERSION} ; extra == 'sqlite'",), id="wrong-pin"),
        pytest.param((f"rowx-sqlite=={VERSION} ; extra == 'postgres'",), id="wrong-extra"),
    ],
)
def test_rowx_wheel_rejects_malformed_sqlite_extra(tmp_path: Path, sqlite_requirements: tuple[str, ...]) -> None:
    requires = (
        CORE_REQUIREMENT,
        "pathspec<2,>=1.1.1",
        EMBEDDED_POSTGRES,
        POSTGRES_EXTRA_REQUIREMENT,
        _embedded_requirement(*ALL_CLAUSES),
        *sqlite_requirements,
    )
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, requires=requires))
    with pytest.raises(SystemExit, match="sqlite requirements are"):
        VALIDATE_WHEEL(path)


def test_second_import_root_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=ROWX_WHEEL, extras=(("rowx_core/__init__.py", b""),)))
    with pytest.raises(SystemExit, match="unexpected namespace"):
        VALIDATE_WHEEL(path)


def _write_sdist(directory: Path, name: str, members: tuple[tuple[str, bytes], ...]) -> Path:
    path = directory / name
    with tar_open(path, "w:gz") as archive:
        for member_name, body in members:
            info = TarInfo(member_name)
            info.size = len(body)
            archive.addfile(info, BytesIO(body))
    return path


@pytest.mark.parametrize(
    ("filename", "member"),
    [
        ("rowx-0.2.0.tar.gz", "rowx-0.2.0/rowx/_core.so"),
        ("rowx_core-0.2.0.tar.gz", "rowx_core-0.2.0/rowx_core/_core.pyd"),
        ("rowx-0.2.0.tar.gz", "rowx-0.2.0/rowx/_core.dll"),
        ("rowx_core-0.2.0.tar.gz", "rowx_core-0.2.0/rowx_core/_core.dylib"),
    ],
)
def test_pure_sdist_rejects_native_extension(tmp_path: Path, filename: str, member: str) -> None:
    path = _write_sdist(tmp_path, filename, ((member, b"native"),))
    with pytest.raises(SystemExit, match="must be pure Python"):
        VALIDATE_PURE_SDIST(path)


def test_pure_sdist_allows_python_sources(tmp_path: Path) -> None:
    path = _write_sdist(tmp_path, "rowx-0.2.0.tar.gz", (("rowx-0.2.0/rowx/__init__.py", b""),))
    VALIDATE_PURE_SDIST(path)


def test_sqlite_wheel_with_native_extension_passes_contents_validation(tmp_path: Path) -> None:
    path = _write_wheel(
        tmp_path,
        WheelBuild(filename=SQLITE_WHEEL, extras=((f"{ROWX_SQLITE}/_core.abi3.so", b"native"),)),
    )
    VALIDATE_WHEEL(path)


def test_postgres_wheel_with_native_extension_still_passes(tmp_path: Path) -> None:
    path = _write_wheel(
        tmp_path,
        WheelBuild(filename=POSTGRES_WHEEL, extras=((f"{ROWX_POSTGRES}/_core.abi3.so", b"native"),)),
    )
    VALIDATE_WHEEL(path)


@pytest.mark.parametrize("filename", [ROWX_WHEEL, CORE_WHEEL])
def test_pure_wheels_still_reject_native_extensions(tmp_path: Path, filename: str) -> None:
    namespace = filename.split("-", maxsplit=1)[0]
    path = _write_wheel(tmp_path, WheelBuild(filename=filename, extras=((f"{namespace}/_core.so", b"native"),)))
    with pytest.raises(SystemExit, match="must be pure Python"):
        VALIDATE_WHEEL(path)


@pytest.mark.parametrize(
    "requires",
    [
        pytest.param((), id="missing-core"),
        pytest.param((f"rowx-core=={WRONG_VERSION}",), id="wrong-core-pin"),
        pytest.param((CORE_REQUIREMENT, "pathspec<2,>=1.1.1"), id="extra-requirement"),
        pytest.param((f"{CORE_REQUIREMENT} ; extra == 'sqlite'",), id="marker"),
    ],
)
def test_sqlite_wheel_rejects_wrong_requires_dist(tmp_path: Path, requires: tuple[str, ...]) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=SQLITE_WHEEL, requires=requires))
    with pytest.raises(SystemExit, match="requirements are"):
        VALIDATE_WHEEL(path)


def test_sqlite_wheel_rejects_postgres_keywords(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=SQLITE_WHEEL, keywords=KEYWORDS))
    with pytest.raises(SystemExit, match="keywords are"):
        VALIDATE_WHEEL(path)


def test_postgres_wheel_rejects_sqlite_keywords(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=POSTGRES_WHEEL, keywords=SQLITE_KEYWORDS))
    with pytest.raises(SystemExit, match="keywords are"):
        VALIDATE_WHEEL(path)


def test_sqlite_wheel_rejects_postgres_license_bytes(tmp_path: Path) -> None:
    body = b"x" + LICENSE_PATHS[ROWX_SQLITE].read_bytes()
    path = _write_wheel(tmp_path, WheelBuild(filename=SQLITE_WHEEL, license_body=body))
    with pytest.raises(SystemExit, match=r"license bytes do not match packages/rowx-sqlite/LICENSE"):
        VALIDATE_WHEEL(path)


def test_sqlite_wheel_name_mismatch_fails(tmp_path: Path) -> None:
    path = _write_wheel(tmp_path, WheelBuild(filename=SQLITE_WHEEL, metadata_name="rowx-postgres"))
    with pytest.raises(SystemExit, match="Name is 'rowx-postgres'"):
        VALIDATE_WHEEL(path)


def _write_release(
    directory: Path, *, omit: tuple[str, ...] = (), sqlite_requires: tuple[str, ...] | None = None
) -> None:
    for filename in (ROWX_WHEEL, CORE_WHEEL):
        _write_wheel(directory, WheelBuild(filename=filename))
    for namespace in (ROWX_POSTGRES, ROWX_SQLITE):
        for platform in NATIVE_PLATFORMS:
            filename = f"{namespace}-{VERSION}-{platform}.whl"
            requires = sqlite_requires if namespace == ROWX_SQLITE else None
            _write_wheel(
                directory,
                WheelBuild(filename=filename, requires=requires, extras=((f"{namespace}/_core.abi3.so", b"native"),)),
            )
    for namespace in (ROWX, ROWX_CORE, ROWX_POSTGRES, ROWX_SQLITE):
        _write_sdist(directory, f"{namespace}-{VERSION}.tar.gz", ((f"{namespace}-{VERSION}/PKG-INFO", b""),))
    for name in omit:
        (directory / name).unlink()


def test_full_release_of_sixteen_files_passes(tmp_path: Path) -> None:
    _write_release(tmp_path)
    assert len(list(tmp_path.iterdir())) == 16
    VALIDATE_RELEASE(tmp_path, VERSION)


def test_release_without_the_sqlite_sdist_fails(tmp_path: Path) -> None:
    _write_release(tmp_path, omit=(f"{ROWX_SQLITE}-{VERSION}.tar.gz",))
    with pytest.raises(SystemExit, match="Unexpected distributions"):
        VALIDATE_RELEASE(tmp_path, VERSION)


def test_release_with_a_wrong_version_sqlite_sdist_fails(tmp_path: Path) -> None:
    _write_release(tmp_path, omit=(f"{ROWX_SQLITE}-{VERSION}.tar.gz",))
    _write_sdist(tmp_path, f"{ROWX_SQLITE}-{WRONG_VERSION}.tar.gz", ())
    with pytest.raises(SystemExit, match="rowx_sqlite source distribution is missing"):
        VALIDATE_RELEASE(tmp_path, VERSION)


@pytest.mark.parametrize("platform", NATIVE_PLATFORMS)
def test_release_missing_one_sqlite_platform_wheel_fails(tmp_path: Path, platform: str) -> None:
    _write_release(tmp_path, omit=(f"{ROWX_SQLITE}-{VERSION}-{platform}.whl",))
    with pytest.raises(SystemExit, match="Unexpected distributions"):
        VALIDATE_RELEASE(tmp_path, VERSION)


def test_release_missing_one_postgres_platform_wheel_still_fails(tmp_path: Path) -> None:
    _write_release(tmp_path, omit=(f"{ROWX_POSTGRES}-{VERSION}-{NATIVE_PLATFORMS[-1]}.whl",))
    with pytest.raises(SystemExit, match="Unexpected distributions"):
        VALIDATE_RELEASE(tmp_path, VERSION)


def test_release_with_a_wrong_sqlite_requires_dist_fails(tmp_path: Path) -> None:
    _write_release(tmp_path, sqlite_requires=(f"rowx-core=={WRONG_VERSION}",))
    with pytest.raises(SystemExit, match="requirements are"):
        VALIDATE_RELEASE(tmp_path, VERSION)
