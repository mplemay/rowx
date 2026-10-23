from argparse import ArgumentParser
from importlib.metadata import version


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="rowx")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--version", action="version", version=version("rowx"))
    parser.parse_args(argv)
    return 0
