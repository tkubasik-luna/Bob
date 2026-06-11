# Bob sub-agent fine-tuning dataset

Goal: fine-tune a local model (Gemma / Qwen) to emit Bob's **sub-agent v2 action
envelope** reliably — fewer malformed tool calls, no hallucinated
`<function_calls>` tails, correct French/English split.

The envelope contract (one JSON object per turn) lives in
`bob.context.prompt_fragments.SUB_AGENT_V2_SYSTEM_PROMPT` and is validated by
`bob.sub_agent.actions.parse_action`:

```json
{"action": "progress", "thought": "..."}
{"action": "tool_call", "name": "<tool>", "args": {...}}
{"action": "done", "result_summary": "...", "ui_payload": "...|null",
 "result_ref": "...|null", "status": "complete", "reason_code": "ok",
 "confidence": "confirmed|probable", "cost": {}}
```

## Pipeline

| Stage | Module | Status |
|-------|--------|--------|
| 1. Harvest real traces | `harvest.py` | ✅ done — 201 examples from `logs/llm-*.jsonl` |
| 2. Repair failures | `needs_repair.jsonl` → relabel | ⬜ todo (LLM/manual) |
| 3. Augment edge cases | `augment.py` | ⬜ todo |
| 4. Format for trainer | already OpenAI-`messages` JSONL | ✅ (no extra step) |
| 5. Train QLoRA | external (unsloth / axolotl / llama-factory) | ⬜ |
| 6. Eval | `bob attest` scenario suite | ⬜ |

### 1. Harvest (done)

```bash
python -m bob.dataset.harvest          # logs/ -> src/bob/dataset/out/
```

Reads every `logs/llm-*.jsonl` line, keeps sub-agent calls (system prompt
contains the envelope-contract marker), and classifies each `raw_response`
through **Bob's real parser** so labels match the inference gate exactly:

- `clean` — parses, no trailing garbage → kept verbatim.
- `repair` — valid envelope + hallucinated tail → kept as the **leading object
  alone** (teaches "stop after one object"). Highest-value reliability examples.
- `fail` — malformed / schema-invalid → written to `needs_repair.jsonl`, never
  trained on raw.

Outputs:
- `out/subagent_sft.jsonl` — `{"messages": [...], "meta": {...}}` ready for SFT.
- `out/needs_repair.jsonl` — rejected outputs + parse error, for relabel.

### 2. Repair

For each `needs_repair.jsonl` line, replace `bad_output` with the gold envelope
that *should* have been emitted for that input (use Claude with the real tool
schemas, or hand-fix). Append the corrected `{messages:[...,assistant]}` to the
SFT set. These failure→fix pairs are what actually move tool-call reliability.

### 3. Augment

201 real examples is a thin seed. Generate synthetic examples to cover gaps and
balance the action mix (currently tool_call 118 / progress 61 / done 22):

- per-tool diversity: every sub-agent tool (`gmail_search`, `web_search`,
  `web_fetch`, MCP tools) with varied valid args.
- "no tool needed → `done` directly" (avoid over-calling).
- `fact` vs `deep` scope directives (short `done` vs full `ui_payload`).
- French user-facing / English reasoning rule.
- adversarial: tempting-but-wrong native `<function_calls>` → gold is the clean
  envelope.

**Validate every synthetic output through `parse_action` before keeping it** —
auto-reject anything the runner would reject. Target 500–2000 total; quality and
diversity beat volume for locking a format.

### 5. Train (external)

The JSONL is standard OpenAI `messages`. Apply the base model's chat template in
the trainer (unsloth/axolotl/llama-factory). QLoRA is enough — full fine-tune is
overkill for format-locking. Train on the exact checkpoint LM Studio will serve.

### 6. Eval — the regression gate

`bob attest scenarios/*.attest.yaml` is the existing headless harness. Build a
suite hitting each tool + each failure mode, point it at the LM Studio endpoint
serving the fine-tuned weights, and compare tool-call success / assertion pass
before vs after. No eval ⇒ no proof the fine-tune helped.
```bash
bob attest src/bob/dataset/scenarios/<scenario>.attest.yaml
```
