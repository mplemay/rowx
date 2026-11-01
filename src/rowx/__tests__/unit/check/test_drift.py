from rowx.check.cli import _parser
from rowx.check.drift import summarize
from rowx.postgres import MigrationStatus


def statuses(**states: list[int]) -> list[MigrationStatus]:
    return [
        MigrationStatus(version=version, description="d", state=state)  # ty: ignore[invalid-argument-type]
        for state, versions in states.items()
        for version in versions
    ]


def test_up_to_date_has_no_summary() -> None:
    assert summarize(statuses(applied=[1, 2]), "migrations") is None
    assert summarize([], "migrations") is None


def test_summary_counts_each_state_in_a_fixed_order() -> None:
    summary = summarize(statuses(dirty=[9], applied=[1], pending=[3, 2], modified=[1]), "db/migrations")
    assert summary == (
        "database has drifted from `db/migrations/`:\n"
        "  2 pending: 2, 3\n"
        "  1 modified: 1\n"
        "  1 dirty: 9\n"
        "run `rowx migrate run`, or `rowx check --scratch`"
    )


def test_summary_lists_at_most_five_versions_per_state() -> None:
    summary = summarize(statuses(pending=list(range(1, 9))), "migrations")
    assert summary is not None
    assert "  8 pending: 1, 2, 3, 4, 5, ...\n" in summary


def test_no_drift_check_flag() -> None:
    assert _parser().parse_args(["check"]).no_drift_check is False
    assert _parser().parse_args(["check", "--no-drift-check"]).no_drift_check is True
