"""Schema hygiene for tool parameters (PRD 0008 / issue 0063).

Local and OpenAI-compatible models — and the guided decoders that gate their
output — choke on three JSON Schema constructs Pydantic happily emits:
``$ref`` / ``$defs`` (nested models), ``anyOf`` / ``oneOf`` (unions, including
the ubiquitous ``Optional[X]`` → ``anyOf: [X, null]`` pattern), and deep
nesting. :func:`flatten_schema` runs once when a :class:`bob.llm.tooling.ToolSpec`
is derived from a Pydantic ``args_model`` and rewrites those constructs into the
flat shapes a picky decoder accepts:

- ``$ref`` is inlined against ``$defs`` / ``definitions``, and the now-unused
  defs containers are dropped.
- An ``anyOf`` / ``oneOf`` whose only non-null branch is a single type (the
  ``Optional`` pattern) collapses to that branch — the field's optionality is
  already carried by its absence from ``required``, so nothing is lost.
- An ``anyOf`` / ``oneOf`` of pure string ``const`` branches collapses to a
  single flat ``{"type": "string", "enum": [...]}`` (OpenClaw's "prefer flat
  string enum over a union" rule).
- A genuinely heterogeneous union (``str | int``, two object branches…) cannot
  be made flat without losing a branch: we **warn** (never silently drop —
  PRD 0008 acceptance criterion) and keep the first branch so the schema stays
  decodable.
- Nesting beyond ``max_depth`` is replaced with a permissive placeholder and
  **warned**, rather than emitted deep enough to crash a grammar compiler.

The deterministic :func:`order_specs` helper sorts a spec list by name before
it is injected into a model payload, so prompt-cache hits stay stable across
turns regardless of registration order (OpenClaw's "deterministic ordering of
tool lists" rule).

This module is pure data transformation — no LLM, no I/O — so it is cheap to
call at registration time and trivially unit-testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:  # pragma: no cover — typing-only import.
    from collections.abc import Sequence

    from bob.llm.tooling.spec import ToolSpec

_logger = structlog.get_logger(__name__)

#: Default cap on object/array nesting depth. Pydantic models for tool args are
#: shallow by convention (flat scalar fields); a generous cap catches a runaway
#: recursive model without truncating any realistic tool schema.
DEFAULT_MAX_DEPTH = 8

#: Keys carried from an ``anyOf`` / ``oneOf`` node onto its collapsed result so
#: the field's metadata (default value, human description, title) survives the
#: flattening. ``anyOf`` / ``oneOf`` themselves are intentionally excluded.
_CARRIED_SIBLING_KEYS = ("default", "description", "title")


def flatten_schema(schema: dict[str, Any], *, max_depth: int = DEFAULT_MAX_DEPTH) -> dict[str, Any]:
    """Return a flattened copy of a JSON Schema ``schema``.

    Inlines ``$ref``, collapses ``anyOf`` / ``oneOf`` to a flat type or enum
    where possible, and caps nesting depth. Lost expressiveness (a real union
    that cannot collapse, an over-deep subtree) is logged at warning level, not
    silently dropped. The input is never mutated.
    """

    defs: dict[str, Any] = {}
    defs.update(schema.get("$defs", {}) or {})
    defs.update(schema.get("definitions", {}) or {})

    # The public signature guarantees a dict input, so the root node always
    # flattens back to a dict (``_flatten_node``'s non-dict passthrough only
    # fires on nested leaves) — annotate to carry that through ``-> Any``.
    flattened: dict[str, Any] = _flatten_node(
        schema, defs=defs, depth=0, max_depth=max_depth, path="<root>"
    )
    # The defs containers existed only to back ``$ref`` — now inlined.
    flattened.pop("$defs", None)
    flattened.pop("definitions", None)
    return flattened


def order_specs(specs: Sequence[ToolSpec]) -> list[ToolSpec]:
    """Return ``specs`` sorted by name — deterministic order for cache stability.

    Tool lists are injected into the model payload in this order so a prompt
    prefix stays byte-stable across turns even if registration order changes
    (PRD 0008 / OpenClaw). Sorting by name is a total order over a tool set
    (names are unique per registry), so the result is fully deterministic.
    """

    return sorted(specs, key=lambda spec: spec.name)


def _flatten_node(
    node: Any,
    *,
    defs: dict[str, Any],
    depth: int,
    max_depth: int,
    path: str,
) -> Any:
    """Recursively flatten one schema node. Returns a new node (no mutation)."""

    if not isinstance(node, dict):
        return node

    if depth > max_depth:
        _logger.warning(
            "tool_schema.flatten.depth_capped",
            path=path,
            max_depth=max_depth,
            detail="subtree replaced with a permissive object placeholder",
        )
        return {"type": node.get("type", "object")}

    # 1. Resolve ``$ref`` first — the resolved target may itself carry unions /
    #    refs, so we recurse into the inlined result.
    if "$ref" in node:
        return _inline_ref(node, defs=defs, depth=depth, max_depth=max_depth, path=path)

    # 2. Collapse a union (``anyOf`` / ``oneOf``) into a flat shape.
    union_key = "anyOf" if "anyOf" in node else ("oneOf" if "oneOf" in node else None)
    if union_key is not None:
        return _collapse_union(
            node, union_key, defs=defs, depth=depth, max_depth=max_depth, path=path
        )

    # 3. Plain object — recurse into the structural children that carry
    #    subschemas, leaving scalar keys (``type``, ``enum``, ``description``…)
    #    untouched.
    return _flatten_children(node, defs=defs, depth=depth, max_depth=max_depth, path=path)


def _inline_ref(
    node: dict[str, Any],
    *,
    defs: dict[str, Any],
    depth: int,
    max_depth: int,
    path: str,
) -> Any:
    """Inline a ``{"$ref": "#/$defs/Name"}`` node against ``defs``."""

    ref = node["$ref"]
    target_name = ref.rsplit("/", 1)[-1] if isinstance(ref, str) else None
    target = defs.get(target_name) if target_name is not None else None
    if target is None:
        _logger.warning(
            "tool_schema.flatten.unresolved_ref",
            path=path,
            ref=ref,
            detail="ref target not found in $defs; emitting permissive object",
        )
        return {"type": "object"}

    # Merge any sibling keys on the ref node (e.g. ``description`` from the
    # referencing field) over the resolved target, then flatten the result.
    merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
    return _flatten_node(merged, defs=defs, depth=depth, max_depth=max_depth, path=path)


def _collapse_union(
    node: dict[str, Any],
    union_key: str,
    *,
    defs: dict[str, Any],
    depth: int,
    max_depth: int,
    path: str,
) -> dict[str, Any]:
    """Collapse an ``anyOf`` / ``oneOf`` node to a flat type or enum."""

    branches = node.get(union_key) or []
    non_null = [b for b in branches if not _is_null_branch(b)]
    carried = {k: node[k] for k in _CARRIED_SIBLING_KEYS if k in node}

    # Optional[X] (one real branch + null) → that branch. Optionality is
    # carried by ``required`` membership, so dropping the null is lossless.
    if len(non_null) == 1:
        collapsed = _flatten_node(
            non_null[0], defs=defs, depth=depth, max_depth=max_depth, path=path
        )
        if isinstance(collapsed, dict):
            return {**collapsed, **carried}
        return {**carried} if carried else {"type": "object"}

    # A union of pure string ``const`` branches is exactly a flat enum.
    enum_values = _string_const_enum(non_null)
    if enum_values is not None:
        return {"type": "string", "enum": enum_values, **carried}

    # Genuine heterogeneous union — cannot stay both flat and complete. Warn
    # (never silently drop) and keep the first branch so the schema decodes.
    _logger.warning(
        "tool_schema.flatten.union_narrowed",
        path=path,
        union=union_key,
        branch_count=len(non_null),
        detail="heterogeneous union narrowed to its first branch for flat decoding",
    )
    first = non_null[0] if non_null else {"type": "object"}
    collapsed = _flatten_node(first, defs=defs, depth=depth, max_depth=max_depth, path=path)
    if isinstance(collapsed, dict):
        return {**collapsed, **carried}
    return {**carried} if carried else {"type": "object"}


def _flatten_children(
    node: dict[str, Any],
    *,
    defs: dict[str, Any],
    depth: int,
    max_depth: int,
    path: str,
) -> dict[str, Any]:
    """Flatten the subschema-bearing children of a plain object node."""

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {
                prop_name: _flatten_node(
                    prop_schema,
                    defs=defs,
                    depth=depth + 1,
                    max_depth=max_depth,
                    path=f"{path}.{prop_name}",
                )
                for prop_name, prop_schema in value.items()
            }
        elif key == "items":
            if isinstance(value, list):
                result[key] = [
                    _flatten_node(
                        item, defs=defs, depth=depth + 1, max_depth=max_depth, path=f"{path}[]"
                    )
                    for item in value
                ]
            else:
                result[key] = _flatten_node(
                    value, defs=defs, depth=depth + 1, max_depth=max_depth, path=f"{path}[]"
                )
        elif key in ("additionalProperties", "prefixItems") and isinstance(value, dict | list):
            if isinstance(value, list):
                result[key] = [
                    _flatten_node(
                        item, defs=defs, depth=depth + 1, max_depth=max_depth, path=f"{path}.{key}"
                    )
                    for item in value
                ]
            else:
                result[key] = _flatten_node(
                    value, defs=defs, depth=depth + 1, max_depth=max_depth, path=f"{path}.{key}"
                )
        else:
            result[key] = value
    return result


def _is_null_branch(branch: Any) -> bool:
    """True when ``branch`` is the JSON Schema null type ``{"type": "null"}``."""

    return isinstance(branch, dict) and branch.get("type") == "null"


def _string_const_enum(branches: list[Any]) -> list[str] | None:
    """Return the enum values if every branch is a string ``const``, else None."""

    values: list[str] = []
    for branch in branches:
        if not isinstance(branch, dict):
            return None
        const = branch.get("const")
        if isinstance(const, str) and branch.get("type", "string") == "string":
            values.append(const)
            continue
        return None
    return values or None


__all__ = ["DEFAULT_MAX_DEPTH", "flatten_schema", "order_specs"]
