from dataclasses import replace
from typing import TYPE_CHECKING, Final, Literal, get_args

from rowx.check.config import ConfigError
from rowx.check.model import Rule

if TYPE_CHECKING:
    from rowx.check.render import Report

type Level = Literal["error", "warn", "ignore"]
RULES: Final[tuple[str, ...]] = tuple(str(rule) for rule in get_args(Rule.__value__))
LEVELS: Final[tuple[str, ...]] = ("error", "warn", "ignore")


def validate_rule(name: str) -> None:
    if name not in RULES and name != "all":
        msg = f"unknown rule {name!r}"
        raise ConfigError(msg)


def resolve_levels(config: dict[str, str], actions: list[tuple[str, str]]) -> dict[str, str]:
    levels = {
        rule: "warn" if rule in {"invalid-ignore-comment", "unused-ignore-comment"} else "error" for rule in RULES
    }
    selections = [("all", config["all"])] if "all" in config else []
    selections.extend((k, v) for k, v in config.items() if k != "all")
    selections.extend(actions)
    for name, level in selections:
        validate_rule(name)
        if level not in LEVELS:
            msg = f"invalid rule level {level!r}; expected error, warn or ignore"
            raise ConfigError(msg)
        if name == "all":
            levels.update(dict.fromkeys(RULES, level))
        else:
            levels[name] = level
    return levels


def apply_levels(reports: list[Report], levels: dict[str, str]) -> list[Report]:
    return [
        replace(
            report,
            severity="warning" if not report.operational and levels[report.diagnostic.rule] == "warn" else "error",
        )
        for report in reports
        if report.operational or levels[report.diagnostic.rule] != "ignore"
    ]
