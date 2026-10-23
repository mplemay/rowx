# Decisions

Spec gaps and deviations found while implementing [`design.md`](design.md). Add one row per decision, in the
same PR as the task that hit it. Never edit `design.md` in a feature PR.

| Date | Task | Question | Decision | Why |
|---|---|---|---|---|
| 2026-10-23 | T00 | tasks.md §0.1 lists `ruff`, `pyright` and `mypy` in `just check`; AGENTS.md mandates `prek` and `ty`. | `just check` and `just fmt` run through `prek`; `ty` is the type checker; no pyright or mypy recipes. | AGENTS.md forbids running linters directly and changing linter configs without permission. |
| 2026-10-23 | T00 | The tracked guide was `agents.md`; T00 wants `AGENTS.md`. | Renamed with `git mv`; §0 content merged into the existing guide. | Keep the existing rules and one source of truth. |
| 2026-10-23 | T00 | Commit trailers: AGENTS.md requires single-line messages. | No `Co-Authored-By` trailer on commits. | Repo instructions override the default attribution. |
| 2026-10-23 | T00 | `CLAUDE.md` must be one line, but rumdl MD041 requires a heading first. | A line-scoped `rumdl-disable-next-line MD041` comment precedes `@AGENTS.md`. | Smallest-scope suppression; linter config stays untouched. |
| 2026-10-23 | T00 | `docs/design.md` and `docs/tasks.md` failed rumdl. | Accepted rumdl's autofixes (blank lines before lists, code fence languages) and wrapped one long line. | Content is unchanged; prek must pass. |
| 2026-10-23 | T02 | Task asks for pyright, mypy and `tests/test_errors.py`, `asyncio_mode="auto"`. | Only `ty` (via prek); tests in `src/rowx/__tests__/unit/`; added `db` and `timing` markers; skipped `asyncio_mode` and pytest-asyncio. | AGENTS.md mandates `ty` and the `__tests__` layout; no async tests exist yet, so the plugin lands with the first one. |
| 2026-10-23 | T02 | `DatabaseError` must keep five attributes and stay picklable. | Overrode `__reduce__` and `__str__` (`sqlstate: message`). | Default `BaseException` pickling would drop attributes with a custom `__init__` signature. |
