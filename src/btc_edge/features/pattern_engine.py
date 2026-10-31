"""Deep Pattern Builder — a serializable boolean rule engine.

Replicates institutional "Deep Pattern Builder" logic:

* :class:`Operand` — a feature reference or literal with math offsets
  (``+, -, *, /`` expressed via ``scale`` and ``offset``).
* :class:`Condition` — ``left OPERATOR right`` with ``> >= < <= == !=``.
* :class:`RuleGroup` — ``AND`` / ``OR`` composition of conditions and nested
  groups, fully serializable to/from JSON.

Every strategy entry evaluates an explicit :class:`RuleGroup` tree and logs the
exact boolean snapshot to SQLite for nightly grading.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Union

VALID_OPERATORS = {">", ">=", "<", "<=", "==", "!="}


@dataclass(slots=True)
class Operand:
    """A feature reference or literal number, with an optional math transform.

    ``value`` is resolved as: ``(feature_or_literal) * scale + offset``. The
    four arithmetic operators are expressed as:
      * ``*`` -> ``scale``          * ``/`` -> ``scale = 1/x``
      * ``+`` -> ``offset``         * ``-`` -> negative ``offset``
    """

    value: str | float | int
    scale: float = 1.0
    offset: float = 0.0

    def resolve(self, features: dict[str, float]) -> float:
        if isinstance(self.value, (int, float)):
            base = float(self.value)
        else:
            base = float(features.get(str(self.value), 0.0))
        return base * self.scale + self.offset

    @classmethod
    def literal(cls, num: float) -> Operand:
        return cls(value=num)

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "scale": self.scale, "offset": self.offset}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Operand:
        return cls(value=d["value"], scale=d.get("scale", 1.0), offset=d.get("offset", 0.0))


@dataclass(slots=True)
class Condition:
    left: Operand
    operator: str
    right: Operand

    def __post_init__(self) -> None:
        if self.operator not in VALID_OPERATORS:
            raise ValueError(f"invalid operator {self.operator!r}; must be one of {sorted(VALID_OPERATORS)}")

    def evaluate(self, features: dict[str, float]) -> bool:
        lhs = self.left.resolve(features)
        rhs = self.right.resolve(features)
        if self.operator == ">":
            return lhs > rhs
        if self.operator == ">=":
            return lhs >= rhs
        if self.operator == "<":
            return lhs < rhs
        if self.operator == "<=":
            return lhs <= rhs
        if self.operator == "==":
            return abs(lhs - rhs) < 1e-12
        if self.operator == "!=":
            return abs(lhs - rhs) >= 1e-12
        return False

    def snapshot(self, features: dict[str, float]) -> dict[str, Any]:
        lhs = self.left.resolve(features)
        rhs = self.right.resolve(features)
        return {
            "left": self.left.to_dict(),
            "operator": self.operator,
            "right": self.right.to_dict(),
            "left_resolved": lhs,
            "right_resolved": rhs,
            "result": self.evaluate(features),
        }

    def to_dict(self) -> dict[str, Any]:
        return {"left": self.left.to_dict(), "operator": self.operator, "right": self.right.to_dict()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Condition:
        return cls(
            left=Operand.from_dict(d["left"]),
            operator=d["operator"],
            right=Operand.from_dict(d["right"]),
        )


Node = Union[Condition, "RuleGroup"]


@dataclass(slots=True)
class RuleGroup:
    logical_op: str
    conditions: list[Node] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.logical_op not in ("AND", "OR"):
            raise ValueError(f"invalid logical_op {self.logical_op!r}; must be AND or OR")

    def evaluate(self, features: dict[str, float]) -> bool:
        if not self.conditions:
            return self.logical_op == "AND"  # vacuous truth for AND, false for OR
        results = [_eval_node(c, features) for c in self.conditions]
        if self.logical_op == "AND":
            return all(results)
        return any(results)

    def snapshot(self, features: dict[str, float]) -> dict[str, Any]:
        children = []
        for c in self.conditions:
            if isinstance(c, RuleGroup):
                children.append(c.snapshot(features))
            else:
                children.append(c.snapshot(features))
        return {
            "logical_op": self.logical_op,
            "children": children,
            "result": self.evaluate(features),
        }

    def to_dict(self) -> dict[str, Any]:
        children = []
        for c in self.conditions:
            children.append(c.to_dict())
        return {"logical_op": self.logical_op, "conditions": children}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RuleGroup:
        group = cls(logical_op=d["logical_op"])
        for c in d.get("conditions", []):
            group.conditions.append(_node_from_dict(c))
        return group


def _eval_node(node: Node, features: dict[str, float]) -> bool:
    return node.evaluate(features)


def _node_from_dict(d: dict[str, Any]) -> Node:
    if "logical_op" in d:
        return RuleGroup.from_dict(d)
    return Condition.from_dict(d)


def parse_rule(json_str: str) -> RuleGroup:
    """Parse a JSON-encoded rule tree."""
    d = json.loads(json_str)
    return RuleGroup.from_dict(d)


def serialize_rule(group: RuleGroup) -> str:
    """Serialize a rule tree to JSON."""
    return json.dumps(group.to_dict())
