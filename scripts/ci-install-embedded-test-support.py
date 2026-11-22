#!/usr/bin/env python3
from pathlib import Path
from shutil import copytree

from rowx_embedded import __file__ as embedded_file

if __name__ == "__main__":
    source = Path("packages/rowx-embedded/src/rowx_embedded/__tests__/support")
    destination = Path(embedded_file).resolve().parent / "__tests__" / "support"
    copytree(source, destination, dirs_exist_ok=True)
