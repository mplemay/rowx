import ast
from typing import TYPE_CHECKING, Final

from rowx.check.model import Diagnostic, IndirectKind
from rowx.check.render import Report

if TYPE_CHECKING:
    from pathlib import Path

    from rowx.check.model import Span, TemplateCall, TemplateFacts
    from rowx.check.params import ParamInference
    from rowx.check.sourcemap import SourceMapper

HELP: Final[str] = "build dynamic SQL from t-string literals; `Template(...)` with a non-literal string is raw SQL"
INTERPOLATION_SPECS: Final[frozenset[str]] = frozenset({"conversion", "format_spec"})
CONVERSION_INDEX: Final[int] = 2
FORMAT_SPEC_INDEX: Final[int] = 3
INDIRECT_MESSAGES: Final[dict[IndirectKind, str]] = {
    "value": "`Template` used as a value, so the calls to it can't be seen",
    "attribute": "an attribute access on `Template` other than a direct call",
    "private": "`string.templatelib.{name}` is private and may build a template",
    "star-import": "`from string.templatelib import *` hides the uses of `Template`",
    "type-call": "`type(<t-string>)` or `<t-string>.__class__` used as a callable builds a template unseen",
    "module-value": "`string`, `string.templatelib` or a private `rowx` module used as a value, so `Template` can be reached unseen",
    "alias": "an alias of `Template`, so the calls to it can't be seen",
    "re-export": "`{name}` re-exports a `string.templatelib` name",
    "ambiguous": "`{name}` is also bound another way in its scope, so it may be `Template`",
}


def _is_str(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _is_literal_or_none(node: ast.expr) -> bool:
    return _is_str(node) or (isinstance(node, ast.Constant) and node.value is None)


class _Rule:
    def __init__(self, path: Path, mapper: SourceMapper, inference: ParamInference | None) -> None:
        self._path = path
        self._mapper = mapper
        self._inference = inference
        self.reports: list[Report] = []

    def report_node(self, message: str, node: ast.expr | ast.keyword) -> None:
        self.report(message, self._mapper.span(node))

    def report(self, message: str, span: Span) -> None:
        diagnostic = Diagnostic(rule="template-construction", message=message, path=self._path, span=span)
        self.reports.append(Report(diagnostic=diagnostic, help=HELP))

    def template_call(self, call: TemplateCall, interpolations: set[int]) -> None:
        node = call.node
        if call.private:
            self.report(f"`string.templatelib.{call.name}` is private and may build a template", call.span)
        elif call.name == "Template":
            self._template_arguments(node, interpolations)
        elif call.name == "Interpolation":
            self._interpolation_arguments(node)

    def _template_arguments(self, node: ast.Call, interpolations: set[int]) -> None:
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                self.report_node("a starred argument to `Template(...)` can hold raw SQL", arg)
            elif not (_is_str(arg) or isinstance(arg, ast.TemplateStr) or id(arg) in interpolations):
                self.report_node(
                    "`Template(...)` takes only `str` literals, t-strings and `Interpolation(...)` calls",
                    arg,
                )
        for keyword in node.keywords:
            self.report_node("`Template(...)` takes only positional arguments", keyword)

    def _interpolation_arguments(self, node: ast.Call) -> None:
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                self.report_node("a starred argument to `Interpolation(...)` is not allowed", arg)
        for keyword in node.keywords:
            if keyword.arg is None:
                self.report_node("a `**` argument to `Interpolation(...)` is not allowed", keyword)
        specs = [
            *node.args[CONVERSION_INDEX : FORMAT_SPEC_INDEX + 1],
            *(keyword.value for keyword in node.keywords if keyword.arg in INTERPOLATION_SPECS),
        ]
        for spec in specs:
            if not isinstance(spec, ast.Starred) and not _is_literal_or_none(spec):
                self.report_node("the `conversion` and `format_spec` of `Interpolation(...)` must be literals", spec)

    def evident(self, operand: ast.expr) -> bool:
        if self._inference is None:
            return False
        found = self._inference.infer(operand)
        return found is not None and found.name == "Template"


def template_reports(
    path: Path,
    facts: TemplateFacts,
    mapper: SourceMapper,
    inference: ParamInference | None,
) -> list[Report]:
    rule = _Rule(path, mapper, inference)
    interpolations = {id(call.node) for call in facts.calls if call.name == "Interpolation" and not call.private}
    for call in facts.calls:
        rule.template_call(call, interpolations)
    for use in facts.indirect_uses:
        rule.report(INDIRECT_MESSAGES[use.kind].format(name=use.name), use.span)
    for concat in facts.concats:
        if not (concat.evident or rule.evident(concat.operand)):
            rule.report(
                "a t-string can only be added to a t-string or a `Template`, not an unknown operand",
                concat.operand_span,
            )
    for spec in facts.dynamic_specs:
        # a direct argument of `query` or `query_unchecked` is already `dynamic-query`
        if spec.direct_argument_of is None:
            rule.report("a format spec in a t-string must be a literal", spec.span)
    return rule.reports
