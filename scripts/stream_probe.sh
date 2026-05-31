#!/usr/bin/env bash
# stream_probe.sh — inspect what the Claude CLI streams.
#
# Runs `claude -p` in stream-json mode with partial messages enabled, then
# classifies every JSONL event so you can SEE which deltas actually arrive:
# text_delta (answer tokens), thinking_delta (reasoning), input_json_delta
# (tool args), tool_use starts, plus system/result envelopes.
#
# Usage:
#   ./scripts/stream_probe.sh "your prompt here"
#   ./scripts/stream_probe.sh --think "solve a puzzle that needs reasoning"
#   ./scripts/stream_probe.sh --raw "prompt"      # dump raw JSONL, no formatting
#
# Requires: claude CLI (logged in), jq.

set -euo pipefail

THINK=0
RAW=0
PROMPT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --think) THINK=1; shift ;;
    --raw)   RAW=1; shift ;;
    *)       PROMPT="$1"; shift ;;
  esac
done

[[ -z "$PROMPT" ]] && PROMPT="In one sentence, what is streaming? Then call no tools."

command -v claude >/dev/null || { echo "claude CLI not found" >&2; exit 1; }
command -v jq     >/dev/null || { echo "jq not found" >&2; exit 1; }

ARGS=(-p "$PROMPT" --output-format stream-json --include-partial-messages --verbose)

# Extended thinking is off by default; nudge the model toward a reasoning chain.
if [[ "$THINK" -eq 1 ]]; then
  ARGS=(-p "Think step by step before answering. $PROMPT" \
        --output-format stream-json --include-partial-messages --verbose)
fi

echo "▶ claude ${ARGS[*]}" >&2
echo "────────────────────────────────────────────" >&2

if [[ "$RAW" -eq 1 ]]; then
  exec claude "${ARGS[@]}"
fi

# Pretty classifier. Each line is one JSON event.
claude "${ARGS[@]}" | jq -rj '
  if .type == "system" then
    "\n[system/\(.subtype // "?")] model=\(.model // "?") session=\(.session_id // "?" | .[0:8])\n"
  elif .type == "stream_event" then
    (.event // {}) as $e
    | if $e.type == "content_block_start" and ($e.content_block.type == "tool_use") then
        "\n  🔧 tool_use START: \($e.content_block.name)\n"
      elif $e.type == "content_block_delta" then
          ($e.delta // {}) as $d
          | if   $d.type == "text_delta"       then $d.text
            elif $d.type == "thinking_delta"   then ("[35m" + ($d.thinking // "") + "[0m")
            elif $d.type == "input_json_delta" then ("[36m" + ($d.partial_json // "") + "[0m")
            else "" end
      elif $e.type == "message_stop" then "\n"
      else "" end
  elif .type == "result" then
    "\n[result] turns=\(.num_turns // "?") cost_usd=\(.total_cost_usd // "?") err=\(.is_error // false)\n"
  else "" end
'
echo "" >&2
echo "────────────────────────────────────────────" >&2
echo "magenta = thinking_delta · cyan = tool input_json_delta · plain = text_delta" >&2
