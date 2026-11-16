#!/usr/bin/env python3
from argparse import ArgumentParser
from json import loads
from pathlib import Path
from shutil import which
from subprocess import PIPE, run  # ruff: ignore[suspicious-subprocess-import]
from tarfile import open as open_tar
from tempfile import TemporaryDirectory
from typing import Final, TypedDict, cast

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
PACKAGE: Final[Path] = ROOT / "packages" / "rowx-embedded"
VENDOR: Final[Path] = Path("vendor/wasmer-compiler")


class CargoPackage(TypedDict):
    name: str
    manifest_path: str
    source: str | None


class CargoMetadata(TypedDict):
    packages: list[CargoPackage]


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("archive", nargs="?", type=Path, help="validate an existing sdist instead of building one")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="rowx-embedded-sdist-") as directory:
        work = Path(directory)
        archive = args.archive
        if archive is None:
            output = work / "dist"
            run(  # ruff: ignore[subprocess-without-shell-equals-true]
                [_executable("uv"), "build", str(PACKAGE), "--sdist", "--out-dir", str(output)],
                cwd=work,
                check=True,
            )
            (archive,) = output.glob("*.tar.gz")
        validate_archive(archive, work / "source")
    return 0


def validate_archive(archive: Path, destination: Path) -> None:
    with open_tar(archive) as source:
        source.extractall(destination, filter="data")
    (extracted,) = destination.iterdir()
    vendor = PACKAGE / VENDOR
    required = [vendor / "Cargo.toml", vendor / "NOTICE.md", *sorted((vendor / "src").rglob("*.rs"))]
    for path in required:
        relative = path.relative_to(PACKAGE)
        packaged = extracted / relative
        if not packaged.is_file() or packaged.read_bytes() != path.read_bytes():
            message = f"{archive.name}: {relative} is missing or differs from the checkout"
            raise SystemExit(message)
    # Resolving outside the checkout proves that its vendor tree and workspace cannot hide missing archive inputs. Not
    # `--locked`: the archive carries the root Cargo.lock, which also lists crates of the members it leaves out.
    result = run(  # ruff: ignore[subprocess-without-shell-equals-true]
        [_executable("cargo"), "metadata", "--format-version", "1"],
        cwd=extracted,
        check=True,
        stdout=PIPE,
        text=True,
    )
    metadata = cast("CargoMetadata", loads(result.stdout))
    compilers = [package for package in metadata["packages"] if package["name"] == "wasmer-compiler"]
    expected = (extracted / VENDOR / "Cargo.toml").resolve()
    if (
        len(compilers) != 1
        or compilers[0]["source"] is not None
        or Path(compilers[0]["manifest_path"]).resolve() != expected
    ):
        message = f"{archive.name}: Cargo did not resolve wasmer-compiler from {expected}: {compilers}"
        raise SystemExit(message)


def _executable(name: str) -> str:
    if executable := which(name):
        return executable
    message = f"{name} is required to validate the embedded source archive"
    raise SystemExit(message)


if __name__ == "__main__":
    raise SystemExit(main())
