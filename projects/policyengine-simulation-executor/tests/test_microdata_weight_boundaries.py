"""Weighted-microdata guards: weights aren't values.

Microdata columns come in four kinds (weights, values, labels and counts), and
each allows different operations; docs/engineering/skills/microdata-weights.md
sets them out. These checks read the source, so they recognise a weight by its
name: a string or f-string ending in ``_weight`` or ``_weights``, a name
assigned from one, or a ``.weights`` attribute. They don't follow data through
function calls or containers.
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PROJECT_ROOT.parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
TESTS_ROOT = PROJECT_ROOT / "tests"
FIXTURES_ROOT = REPOSITORY_ROOT / "rules" / "fixtures"
WEIGHT_STRIPPING_BASELINE = (
    REPOSITORY_ROOT / "rules" / "baselines" / "sim-microdata-weight-stripping.txt"
)
# The one module allowed to read raw values from weighted frames: it holds
# _count_output_entities, the plain sum of an entity's weights from #767.
WEIGHT_STRIPPING_MODULE = (
    SOURCE_ROOT / "policyengine_simulation_executor" / "simulation_output_common.py"
)
# Counting entities is the one permanent read there: a plain sum of the
# weights, which weights-not-values allows. Every other read in the module is
# a recorded exception, listed in the baseline, and that list may only shrink.
ENTITY_COUNT_FUNCTION = "_count_output_entities"
# Output-builder tests, which build entity frames only through the shared
# factory in fixtures/weighted_frames.py.
BUILDER_TEST_PATTERN = "test_simulation_output_*.py"

WEIGHT_SUFFIXES = ("_weight", "_weights")
# Reductions that a weighted MicroDataFrame column weights.
WEIGHTED_REDUCTIONS = frozenset(
    {"sum", "mean", "median", "quantile", "std", "count", "groupby", "cumsum"}
)
# Weighted aggregators, with the position and keyword of the argument each one
# aggregates as a value.
WEIGHTED_AGGREGATORS: dict[str, tuple[int | None, str]] = {
    "_sum_output_variable": (1, "variable"),
    "_change_output_variable": (2, "variable"),
    "Aggregate": (None, "variable"),
    "ChangeAggregate": (None, "variable"),
}
# The executor's accessor for one entity's weighted output frame.
ENTITY_FRAME_ACCESSOR = "_entity_data"
# Calls and attributes that return a weighted column's or frame's raw values.
STRIPPING_FUNCTIONS = frozenset(
    {
        "np.array",
        "np.asarray",
        "numpy.array",
        "numpy.asarray",
        "pd.DataFrame",
        "pd.Series",
        "pandas.DataFrame",
        "pandas.Series",
    }
)
STRIPPING_METHODS = frozenset({"to_numpy", "tolist"})
STRIPPING_ATTRIBUTES = frozenset({"values"})
# Methods whose result is weighted exactly as their receiver is.
WEIGHTING_PRESERVING_METHODS = frozenset(
    {"abs", "astype", "clip", "copy", "fillna", "round"}
)
# Entity frame constructors that builder tests reach only through the factory.
ENTITY_FRAME_CONSTRUCTORS = frozenset(
    {
        "DataFrame",
        "MicroDataFrame",
        "microdf.MicroDataFrame",
        "pd.DataFrame",
        "pandas.DataFrame",
    }
)

AGGREGATION = "weight aggregated as a value"
STRIPPING = "weights stripped"
UNWEIGHTED_TEST_FRAME = "builder test frame built outside the weighted factory"


@dataclass(frozen=True)
class Finding:
    """One place a check flags, with a line-independent baseline entry."""

    check: str
    path: str
    line: int
    function: str
    source: str

    @property
    def entry(self) -> str:
        """Return the baseline entry: path, enclosing function and expression."""
        return f"{self.path}::{self.function}::{self.source}"

    def __str__(self) -> str:
        """Describe the finding with its line, for assertion messages."""
        return f"{self.path}:{self.line} ({self.function}): {self.source}"


@dataclass
class Scope:
    """The names a module or function assigns, sorted by what they hold."""

    weight_keys: set[str] = field(default_factory=set)
    weight_columns: set[str] = field(default_factory=set)
    weight_arrays: set[str] = field(default_factory=set)
    weighted: set[str] = field(default_factory=set)

    def child(self) -> Scope:
        """Return a nested function's scope, which starts with these names."""
        return Scope(
            weight_keys=set(self.weight_keys),
            weight_columns=set(self.weight_columns),
            weight_arrays=set(self.weight_arrays),
            weighted=set(self.weighted),
        )


def _dotted_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    return ""


def _called_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return ""


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _argument(call: ast.Call, position: int | None, name: str) -> ast.expr | None:
    if position is not None and len(call.args) > position:
        return call.args[position]
    return _keyword(call, name)


def _names_a_weight(node: ast.expr) -> bool:
    """Whether a string or f-string literal ends in ``_weight`` or ``_weights``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.endswith(WEIGHT_SUFFIXES)
    if isinstance(node, ast.JoinedStr) and node.values:
        tail = node.values[-1]
        return (
            isinstance(tail, ast.Constant)
            and isinstance(tail.value, str)
            and tail.value.endswith(WEIGHT_SUFFIXES)
        )
    return False


def _is_weight_key(node: ast.expr, scope: Scope) -> bool:
    if isinstance(node, ast.Name):
        return node.id in scope.weight_keys
    return _names_a_weight(node)


def _is_weight_column(node: ast.expr, scope: Scope) -> bool:
    """Whether the expression is ``<frame>[<weight>]``, or a name assigned one."""
    if isinstance(node, ast.Name):
        return node.id in scope.weight_columns
    return isinstance(node, ast.Subscript) and _is_weight_key(node.slice, scope)


def _is_weight_array(node: ast.expr, scope: Scope) -> bool:
    if isinstance(node, ast.Name):
        return node.id in scope.weight_arrays
    return isinstance(node, ast.Attribute) and node.attr == "weights"


def _is_weight(node: ast.expr, scope: Scope) -> bool:
    return (
        _is_weight_key(node, scope)
        or _is_weight_column(node, scope)
        or _is_weight_array(node, scope)
    )


def _is_output_data(node: ast.expr) -> bool:
    """Whether the expression is ``<simulation>.output_dataset.data``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "data"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "output_dataset"
    )


def _is_weighted_call(call: ast.Call, scope: Scope) -> bool:
    name = _called_name(call)
    if name == ENTITY_FRAME_ACCESSOR:
        return True
    if name == "getattr" and call.args:
        return _is_output_data(call.args[0])
    if name in {"MicroSeries", "MicroDataFrame"}:
        weights = _keyword(call, "weights")
        return weights is not None and not (
            isinstance(weights, ast.Constant) and weights.value is None
        )
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in WEIGHTING_PRESERVING_METHODS
        and _is_weighted(call.func.value, scope)
    )


def _operands(node: ast.expr) -> list[ast.expr]:
    """Return the operands whose weighting an operator's result keeps."""
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if isinstance(node, ast.UnaryOp):
        return [node.operand]
    if isinstance(node, ast.Compare):
        return [node.left, *node.comparators]
    if isinstance(node, ast.BoolOp):
        return list(node.values)
    if isinstance(node, ast.IfExp):
        return [node.body, node.orelse]
    return []


def _is_weighted(node: ast.expr, scope: Scope) -> bool:
    """Whether the expression holds a weighted frame or column.

    That is an entity's output frame (``<simulation>.output_dataset.data.<entity>``
    or ``_entity_data(...)``), a MicroDataFrame or MicroSeries built with
    weights, or anything indexed, filtered or computed from one.
    """
    if isinstance(node, ast.Name):
        return node.id in scope.weighted
    if isinstance(node, ast.Attribute):
        return _is_output_data(node.value)
    if isinstance(node, ast.Call):
        return _is_weighted_call(node, scope)
    if isinstance(node, ast.Subscript):
        receiver = node.value
        if isinstance(receiver, ast.Attribute) and receiver.attr in {"loc", "iloc"}:
            receiver = receiver.value
        return _is_weighted(receiver, scope)
    return any(_is_weighted(operand, scope) for operand in _operands(node))


def _assignments(body: list[ast.stmt]) -> list[tuple[str, ast.expr]]:
    """Return each ``name = value`` in a body, outside nested functions and classes."""
    assignments: list[tuple[str, ast.expr]] = []
    pending: list[ast.AST] = list(body)
    while pending:
        node = pending.pop()
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        if isinstance(node, ast.Assign):
            assignments.extend(
                (target.id, node.value)
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif (
            isinstance(node, (ast.AnnAssign, ast.NamedExpr))
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments.append((node.target.id, node.value))
        pending.extend(ast.iter_child_nodes(node))
    return assignments


def _with_assignments(scope: Scope, body: list[ast.stmt]) -> Scope:
    """Sort the body's assigned names into the scope, until nothing changes."""
    assignments = _assignments(body)
    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            groups = (
                (scope.weight_keys, _is_weight_key(value, scope)),
                (scope.weight_columns, _is_weight_column(value, scope)),
                (scope.weight_arrays, _is_weight_array(value, scope)),
                (scope.weighted, _is_weighted(value, scope)),
            )
            for names, matches in groups:
                if matches and name not in names:
                    names.add(name)
                    changed = True
    return scope


class _MicrodataWalker:
    """Walk a module with each function's scope and collect every finding."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.findings: list[Finding] = []
        self._called_functions: set[int] = set()

    def walk(self, tree: ast.Module) -> None:
        self._walk(tree, _with_assignments(Scope(), tree.body), "<module>")

    def _walk(self, node: ast.AST, scope: Scope, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = (
                    child.name if function == "<module>" else f"{function}.{child.name}"
                )
                if isinstance(child, ast.ClassDef):
                    self._walk(child, scope, name)
                else:
                    self._walk(
                        child, _with_assignments(scope.child(), child.body), name
                    )
                continue
            self._check(child, scope, function)
            self._walk(child, scope, function)

    def _report(self, check: str, node: ast.expr, function: str) -> None:
        self.findings.append(
            Finding(
                check=check,
                path=self.relative_path,
                line=node.lineno,
                function=function,
                source=ast.unparse(node),
            )
        )

    def _check(self, node: ast.AST, scope: Scope, function: str) -> None:
        if isinstance(node, ast.Call):
            self._called_functions.add(id(node.func))
            self._check_aggregation(node, scope, function)
            self._check_stripping_call(node, scope, function)
            self._check_test_frame(node, function)
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in STRIPPING_ATTRIBUTES
            and id(node) not in self._called_functions
            and _is_weighted(node.value, scope)
        ):
            self._report(STRIPPING, node, function)

    def _check_aggregation(self, call: ast.Call, scope: Scope, function: str) -> None:
        name = _called_name(call)
        aggregated: ast.expr | None = None
        if name in WEIGHTED_AGGREGATORS:
            aggregated = _argument(call, *WEIGHTED_AGGREGATORS[name])
        elif name == "MicroSeries" and _keyword(call, "weights") is not None:
            aggregated = _argument(call, 0, "data")
        if aggregated is not None and _is_weight(aggregated, scope):
            self._report(AGGREGATION, call, function)
            return
        if (
            isinstance(call.func, ast.Attribute)
            and call.func.attr in WEIGHTED_REDUCTIONS
            and _is_weight_column(call.func.value, scope)
        ):
            self._report(AGGREGATION, call, function)

    def _check_stripping_call(
        self, call: ast.Call, scope: Scope, function: str
    ) -> None:
        if _dotted_name(call.func) in STRIPPING_FUNCTIONS:
            stripped = _argument(call, 0, "data")
        elif (
            isinstance(call.func, ast.Attribute) and call.func.attr in STRIPPING_METHODS
        ):
            stripped = call.func.value
        else:
            return
        if stripped is not None and _is_weighted(stripped, scope):
            self._report(STRIPPING, call, function)

    def _check_test_frame(self, call: ast.Call, function: str) -> None:
        name = _dotted_name(call.func)
        constructor = (
            name.rsplit(".", 1)[0]
            if name.endswith((".from_dict", ".from_records"))
            else name
        )
        if constructor in ENTITY_FRAME_CONSTRUCTORS:
            self._report(UNWEIGHTED_TEST_FRAME, call, function)


@cache
def _module_findings(path: Path) -> tuple[Finding, ...]:
    walker = _MicrodataWalker(path.relative_to(REPOSITORY_ROOT).as_posix())
    walker.walk(ast.parse(path.read_text(encoding="utf-8")))
    return tuple(walker.findings)


def _findings(paths: list[Path], check: str) -> list[Finding]:
    assert paths, f"no modules to check for {check}"
    return [
        finding
        for path in paths
        for finding in _module_findings(path)
        if finding.check == check
    ]


def _source_modules() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def _fixture_sources(rule_id: str, fixture: str, check: str) -> list[str]:
    path = FIXTURES_ROOT / rule_id / fixture
    return [
        finding.source for finding in _module_findings(path) if finding.check == check
    ]


def _baseline_entries(path: Path) -> Counter[str]:
    """Read a baseline file: one entry per line, skipping blanks and ``#`` comments."""
    return Counter(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _described(findings: list[Finding]) -> str:
    return "\n".join(str(finding) for finding in findings)


def test_weights_are_never_aggregated_as_values() -> None:
    findings = _findings(_source_modules(), AGGREGATION)
    assert findings == [], (
        "a weight is aggregated as a value, which weights it by itself (the sum "
        "of squared weights, as in #767); count entities with "
        "_count_output_entities instead:\n" + _described(findings)
    )


@dataclass(frozen=True)
class StrippingReview:
    """Weight-stripping findings, sorted by what the rule allows."""

    outside_designated_module: list[Finding]
    unrecorded_exceptions: list[Finding]
    stale_exceptions: list[str]


def _review_weight_stripping(
    findings: list[Finding], *, designated: str, recorded: Counter[str]
) -> StrippingReview:
    """Sort stripping findings against the designated module and its exceptions.

    Nothing outside the designated module may read raw values from weighted
    frames. Inside it, counting entities is always allowed, and every other
    read must be a recorded exception; the recorded list may only shrink.
    """
    exceptions = [
        finding
        for finding in findings
        if finding.path == designated and finding.function != ENTITY_COUNT_FUNCTION
    ]
    found = Counter(finding.entry for finding in exceptions)
    return StrippingReview(
        outside_designated_module=[
            finding for finding in findings if finding.path != designated
        ],
        unrecorded_exceptions=[
            finding for finding in exceptions if finding.entry in found - recorded
        ],
        stale_exceptions=sorted((recorded - found).elements()),
    )


def test_weight_stripping_stays_in_the_designated_module() -> None:
    assert WEIGHT_STRIPPING_MODULE.is_file()
    review = _review_weight_stripping(
        _findings(_source_modules(), STRIPPING),
        designated=WEIGHT_STRIPPING_MODULE.relative_to(REPOSITORY_ROOT).as_posix(),
        recorded=_baseline_entries(WEIGHT_STRIPPING_BASELINE),
    )
    assert review.outside_designated_module == [], (
        "raw values read from a weighted frame outside "
        f"{WEIGHT_STRIPPING_MODULE.name}; an unweighted total of them is a sample "
        "total, the mirror image of #767. Keep the weights, or decide with a "
        "reviewer to move the read into the designated module:\n"
        + _described(review.outside_designated_module)
    )
    assert review.unrecorded_exceptions == [], (
        f"raw values read from a weighted frame in {WEIGHT_STRIPPING_MODULE.name} "
        "for something other than counting entities. Each such read is a recorded "
        f"exception in {WEIGHT_STRIPPING_BASELINE.name}, a list that may only "
        "shrink; keep the weights instead:\n" + _described(review.unrecorded_exceptions)
    )
    assert review.stale_exceptions == [], (
        "these exceptions no longer apply; remove them from "
        f"{WEIGHT_STRIPPING_BASELINE.name} and lower the rule's baseline count "
        "in rules/registry.toml:\n" + "\n".join(review.stale_exceptions)
    )


def test_weight_stripping_review_ratchets_the_designated_module() -> None:
    designated = "src/simulation_output_common.py"
    entity_count = Finding(
        STRIPPING, designated, 1, ENTITY_COUNT_FUNCTION, "np.asarray(data[weight])"
    )
    recorded_strip = Finding(
        STRIPPING, designated, 2, "_unweighted_household_frame", "pd.DataFrame(frame)"
    )
    new_strip = Finding(STRIPPING, designated, 3, "_new_reader", "frame.values")
    outside_strip = Finding(STRIPPING, "src/other.py", 4, "build", "frame.to_numpy()")
    removed_entry = f"{designated}::_removed_reader::frame.tolist()"

    review = _review_weight_stripping(
        [entity_count, recorded_strip, new_strip, outside_strip],
        designated=designated,
        recorded=Counter([recorded_strip.entry, removed_entry]),
    )

    assert review.outside_designated_module == [outside_strip]
    assert review.unrecorded_exceptions == [new_strip]
    assert review.stale_exceptions == [removed_entry]


def test_builder_tests_build_entity_frames_with_weights() -> None:
    findings = _findings(
        sorted(TESTS_ROOT.glob(BUILDER_TEST_PATTERN)), UNWEIGHTED_TEST_FRAME
    )
    assert findings == [], (
        "output-builder tests build entity frames only with "
        "fixtures.weighted_frames.weighted_entity_frame, which weights them as "
        "policyengine does; a plain DataFrame hid #767:\n" + _described(findings)
    )


def test_rule_proof_weights_not_aggregated_as_values() -> None:
    """Proof for rule sim.microdata.weights-not-aggregated-as-values."""
    rule_id = "sim.microdata.weights-not-aggregated-as-values"
    bad = "bad_simulation_output_budget.py"
    good = "good_simulation_output_budget.py"
    assert _fixture_sources(rule_id, bad, AGGREGATION) == [
        "_sum_output_variable(baseline, 'household_weight', entity='household')"
    ]
    assert _fixture_sources(rule_id, good, AGGREGATION) == []


def test_rule_proof_weight_stripping_confined() -> None:
    """Proof for rule sim.microdata.weight-stripping-confined."""
    rule_id = "sim.microdata.weight-stripping-confined"
    bad = "bad_simulation_output_budget.py"
    good = "good_simulation_output_budget.py"
    assert _fixture_sources(rule_id, bad, STRIPPING) == [
        "np.asarray(household['household_net_income'])"
    ]
    assert _fixture_sources(rule_id, good, STRIPPING) == []


def test_rule_proof_builder_tests_use_weighted_frames() -> None:
    """Proof for rule sim.microdata.builder-tests-use-weighted-frames."""
    rule_id = "sim.microdata.builder-tests-use-weighted-frames"
    bad = "bad_test_simulation_output_budget.py"
    good = "good_test_simulation_output_budget.py"
    assert _fixture_sources(rule_id, bad, UNWEIGHTED_TEST_FRAME) == [
        "pd.DataFrame(columns)",
        "MicroDataFrame(pd.DataFrame(columns), weights='household_weight')",
        "pd.DataFrame(columns)",
    ]
    assert _fixture_sources(rule_id, good, UNWEIGHTED_TEST_FRAME) == []
