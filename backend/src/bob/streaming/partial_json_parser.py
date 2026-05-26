"""Battle-tested partial-JSON parser wrapper (PRD 0006 / issue 0049).

Thin facade over the ``partial-json-parser`` PyPI package
(`<https://pypi.org/project/partial-json-parser/>`_). The wrapper is
*intentionally tiny*: PRD 0006 explicitly forbids hand-rolling a tolerant
scanner — every flag we expose maps directly onto the upstream
``Allow`` bitmask.

Why a wrapper at all?
---------------------

Three small benefits over importing :func:`partial_json_parser.loads`
directly at call sites:

1. **Single import boundary.** If we ever swap the underlying library
   (or fork it for a Bob-specific quirk), only this file needs editing
   — every call site keeps importing :class:`PartialJsonParser`.
2. **Stable contract.** We return either ``dict[str, Any] | None`` (no
   exception on the empty-buffer edge case) and surface only the fields
   the streaming layer actually uses. The upstream library returns
   ``Any`` and may raise :class:`partial_json_parser.MalformedJSON` on
   truly malformed input — we wrap both into a single, easy-to-mock
   surface.
3. **Documentation.** The escape semantics, UTF-8 split-mid-codepoint
   behaviour, escaped-quote handling, etc. all live here as docstrings
   on a class call sites grep for.

Streaming model
---------------

Tool-call arguments arrive as a sequence of byte / string chunks from the
LLM (OpenAI-compatible ``delta.tool_calls[0].function.arguments``). Each
delta extends the accumulated buffer; we re-parse the whole buffer on
every tick. The library is happy to parse incomplete JSON in O(n) so the
cost is fine for the small payloads ``say`` tool arguments produce
(usually < 4 KB).

The library returns the deepest valid sub-tree visible so far. For a
buffer like ``{"speech":"Hello`` it yields ``{"speech": "Hello"}`` — the
string is open but the lib treats the visible prefix as the current value
of the field. That's exactly what the :class:`bob.streaming.StreamEmitter`
needs to compute the incremental delta on the ``speech`` key.

The library currently rejects trailing commas inside objects (a strict
JSON-conform choice) even at temperature where the LLM may emit one. We
opt into a small pre-pass that drops a trailing ``,`` before ``}`` or
``]`` if present. This is the only liberty we take over the library's
defaults; we document it loudly here because future contributors should
not "polish" it without realising it is a deliberate divergence.
"""

from __future__ import annotations

import json
import re
from typing import Any

import partial_json_parser
from partial_json_parser import Allow, MalformedJSON

#: Regexes used by :meth:`PartialJsonParser.parse` to strip the single
#: source of intentional divergence from the upstream library: a trailing
#: comma before a closing ``}`` or ``]``. The LLM at temperature > 0 emits
#: them occasionally; rejecting the whole turn over a stray comma would
#: defeat the streaming UX. The substitution is conservative: only the
#: ``,(\s*)[}\]]`` pattern is rewritten — no other comma is touched.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


class PartialJsonParser:
    """Stateless wrapper exposing ``parse(buffer) -> dict | None``.

    Stateless because :func:`partial_json_parser.loads` is a pure
    function. The instance carries no buffer of its own — call sites
    accumulate the streaming bytes in their own buffer and pass the
    full string on every tick.

    The contract is intentionally narrow:

    - Returns ``None`` when ``buffer`` is empty or the upstream library
      can't make sense of even a partial prefix (returns its
      ``MalformedJSON`` exception). The streaming layer treats ``None``
      as "no parseable state yet" and waits for more bytes.
    - Returns ``dict[str, Any]`` when the buffer parses to a JSON object
      (the only shape the ``say`` tool emits). Non-dict roots (string,
      number, bool, array) yield ``None`` because the streaming layer
      only cares about object-shaped tool arguments.
    """

    #: Whether to strip a stray trailing comma before a closing bracket
    #: before delegating to the library. Defaults to ``True``; tests
    #: that want to verify the library's raw behaviour can pass
    #: ``tolerate_trailing_comma=False``.
    tolerate_trailing_comma: bool

    def __init__(self, *, tolerate_trailing_comma: bool = True) -> None:
        self.tolerate_trailing_comma = tolerate_trailing_comma

    def parse(self, buffer: str) -> dict[str, Any] | None:
        """Parse a (possibly truncated) JSON buffer.

        ``buffer`` is the full accumulated argument string seen so far,
        NOT just the latest delta. The wrapper re-parses on every call —
        the library is O(n) on the buffer size which is fine for the
        small JSON shapes ``say.args`` produces.

        UTF-8 split-mid-codepoint behaviour: the upstream parser
        operates on a Python ``str`` so the bytes have already been
        decoded by the LLM client (OpenAI SDK guarantees a valid UTF-8
        string per delta). Call sites that own the raw byte stream MUST
        accumulate bytes themselves and decode on a UTF-8-safe boundary
        before invoking :meth:`parse` — see
        :class:`bob.streaming.StreamEmitter` for the reference pattern.

        Escape handling: the library handles ``\\"``, ``\\\\``,
        ``\\n``, ``\\t``, ``\\u…`` exactly like :func:`json.loads`. A
        truncated escape sequence (``"Hello\\``) is treated as an
        open string with no escape applied yet — the next tick that
        completes the escape will render the resolved character.

        Nested objects: when ``ui`` is a non-null dict the parser
        descends into it and returns a fully nested structure on every
        tick (deepest valid sub-tree visible).
        """

        if not buffer:
            return None

        text = buffer
        if self.tolerate_trailing_comma:
            text = _TRAILING_COMMA_RE.sub(r"\1", text)

        try:
            value = partial_json_parser.loads(text, Allow.ALL)
        except (MalformedJSON, json.JSONDecodeError, ValueError):
            # The buffer doesn't even look like the start of valid
            # JSON yet (e.g. first few characters of a non-object
            # response, or a completely garbled string), or it carries
            # a hard violation the upstream lib delegates to
            # :func:`json.loads` (trailing comma in strict mode). Treat
            # as "nothing to emit yet" — the next delta might fix it,
            # and if not the final ``json.loads`` in
            # :mod:`bob.llm_client` will surface a structured error.
            return None

        if not isinstance(value, dict):
            return None
        return value


__all__ = ["PartialJsonParser"]
