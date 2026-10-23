from importlib.metadata import version


def main() -> None:
    print(version("rowx"))  # noqa: T201
