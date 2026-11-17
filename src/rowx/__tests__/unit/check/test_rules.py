import pytest

from rowx.check.config import ConfigError
from rowx.check.rules import resolve_levels


def test_rule_precedence() -> None:
    levels = resolve_levels({"all": "ignore", "sql-error": "warn"}, [("all", "error"), ("sql-error", "ignore")])
    assert levels["sql-error"] == "ignore"
    assert levels["invalid-ignore-comment"] == "error"


@pytest.mark.parametrize("rules", [{"typo": "warn"}, {"sql-error": "warning"}])
def test_invalid_rule_configuration(rules: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        resolve_levels(rules, [])
