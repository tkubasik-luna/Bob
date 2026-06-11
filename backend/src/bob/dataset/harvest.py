"""Harvest sub-agent fine-tuning examples from Bob's real LLM-call logs.

Bob logs every LLM call to ``logs/llm-YYYY-MM-DD.jsonl`` via
:func:`bob.logging_setup.log_llm_call` — one JSON line per call carrying the
full input ``messages`` and the model's ``raw_response`` string. For sub-agent
turns the ``raw_response`` IS the v2 action envelope
(``{"action": "progress|tool_call|done", ...}``), so each line is a candidate
``(input, gold-output)`` training pair already in production shape.

This module mines those logs into a chat-template dataset for fine-tuning a
local model (Gemma / Qwen) to emit Bob's sub-agent envelope reliably. Crucially
it classifies each ``raw_response`` through *the same accept/reject gate the
runner uses at inference* (:func:`bob.sub_agent.actions.parse_action` + the
``raw_decode`` leading-JSON tolerance), so training labels never drift from what
the parser actually accepts:

- ``clean``    — leading JSON parses, no trailing garbage → use verbatim.
- ``repair``   — leading JSON parses but the model kept generating (hallucinated
                 ``<function_calls>`` tail); gold = the leading envelope ALONE,
                 tail discarded. These are the highest-value reliability examples.
- ``fail``     — leading JSON malformed or fails the schema. The INPUT is kept in
                 ``needs_repair.jsonl`` for an LLM/manual relabel pass — never
                 train on a broken output.

Run::

    python -m bob.dataset.harvest                     # all logs/ → out/
    python -m bob.dataset.harvest --logs-dir logs --out src/bob/dataset/out
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from bob.sub_agent.actions import SubAgentActionParseError, parse_action

# Marker present in every v2 sub-agent system prompt (SUB_AGENT_V2_SYSTEM_PROMPT).
_SUB_AGENT_MARKER = "emit EXACTLY ONE JSON action"


def _strip_code_fence(text: str) -> str:
    """Drop a ```json … ``` fence if the model wrapped its envelope in one.

    Mirrors the runner's tolerance so a fenced-but-valid envelope still counts
    as a clean positive rather than a parse failure.
    """

    s = text.strip()
    if s.startswith("```"):
        s = s[3:]
        if s[:4].lower() == "json":
            s = s[4:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def _classify(raw_response: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """Return ``(category, leading_payload, error)`` for one raw model output.

    Replicates :func:`bob.sub_agent.runner._normalise_payload` WITHOUT its
    debug-event side effects: leading-JSON ``raw_decode`` tolerance + the legacy
    ``progress``/``done`` key translation + :func:`parse_action` validation.
    """

    payload_text = _strip_code_fence(raw_response)
    try:
        payload, end = json.JSONDecoder().raw_decode(payload_text)
    except json.JSONDecodeError as exc:
        return "fail", None, f"invalid JSON: {exc.msg} (line {exc.lineno}, col {exc.colno})"

    trailing = len(payload_text) - end
    if not isinstance(payload, dict):
        return "fail", None, f"top-level value is {type(payload).__name__}, not object"

    # Legacy → v1 translation (same as the runner) so old logs still validate.
    action = payload.get("action")
    if action == "progress" and "thought" not in payload and "status" in payload:
        payload = {**payload, "thought": payload["status"]}
        payload.pop("status", None)
    if action == "done":
        payload = dict(payload)
        if "result_summary" not in payload:
            legacy = payload.pop("result", None)
            if isinstance(legacy, str):
                payload["result_summary"] = legacy
        payload.setdefault("status", "complete")
        payload.setdefault("reason_code", "ok")
        payload.setdefault("cost", {})

    try:
        parse_action(payload)
    except SubAgentActionParseError as exc:
        return "fail", None, f"schema: {exc}"

    return ("repair" if trailing > 0 else "clean"), payload, None


def _is_sub_agent(messages: list[dict[str, Any]]) -> bool:
    sys_txt = " ".join(
        str(m.get("content", ""))
        for m in messages
        if isinstance(m, dict) and m.get("role") == "system"
    )
    return _SUB_AGENT_MARKER in sys_txt


@dataclass
class HarvestStats:
    lines: int = 0
    sub_agent: int = 0
    by_category: Counter = field(default_factory=Counter)
    by_action: Counter = field(default_factory=Counter)
    deduped: int = 0


def harvest(logs_dir: str, out_dir: str) -> HarvestStats:
    os.makedirs(out_dir, exist_ok=True)
    stats = HarvestStats()
    seen: set[str] = set()

    pos_path = os.path.join(out_dir, "subagent_sft.jsonl")
    fail_path = os.path.join(out_dir, "needs_repair.jsonl")

    with open(pos_path, "w") as pos_f, open(fail_path, "w") as fail_f:
        for path in sorted(glob.glob(os.path.join(logs_dir, "llm-*.jsonl"))):
            for line in open(path):
                line = line.strip()
                if not line.startswith("{"):
                    continue  # RTK proxy comment lines / blanks
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats.lines += 1
                messages = rec.get("messages") or []
                raw = rec.get("raw_response")
                if not isinstance(raw, str) or not _is_sub_agent(messages):
                    continue
                stats.sub_agent += 1

                category, payload, err = _classify(raw)
                stats.by_category[category] += 1

                if category == "fail":
                    fail_f.write(json.dumps(
                        {"messages": messages, "bad_output": raw, "error": err,
                         "source": os.path.basename(path)},
                        ensure_ascii=False) + "\n")
                    continue

                stats.by_action[payload.get("action", "?")] += 1
                # Gold = the leading envelope alone, canonical-serialised. For
                # ``repair`` cases this strips the hallucinated tail — teaching
                # the model to STOP after one object.
                gold = json.dumps(payload, ensure_ascii=False)

                # Dedup on (input, output) so repeated identical turns don't
                # over-weight the set.
                key = hashlib.sha256(
                    (json.dumps(messages, ensure_ascii=False, sort_keys=True) + gold).encode()
                ).hexdigest()
                if key in seen:
                    stats.deduped += 1
                    continue
                seen.add(key)

                example = {
                    "messages": messages + [{"role": "assistant", "content": gold}],
                    "meta": {"category": category, "source": os.path.basename(path)},
                }
                pos_f.write(json.dumps(example, ensure_ascii=False) + "\n")

    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--logs-dir", default="logs")
    ap.add_argument("--out", default="src/bob/dataset/out")
    args = ap.parse_args()

    stats = harvest(args.logs_dir, args.out)
    print(f"lines scanned        : {stats.lines}")
    print(f"sub-agent turns      : {stats.sub_agent}")
    print(f"  clean / repair / fail: "
          f"{stats.by_category['clean']} / {stats.by_category['repair']} / {stats.by_category['fail']}")
    print(f"  deduped (dropped)  : {stats.deduped}")
    print(f"  action mix         : {dict(stats.by_action)}")
    print(f"-> {os.path.join(args.out, 'subagent_sft.jsonl')}")
    print(f"-> {os.path.join(args.out, 'needs_repair.jsonl')}")


if __name__ == "__main__":
    main()
