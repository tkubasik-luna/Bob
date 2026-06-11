"""Augment the harvested sub-agent SFT set with validated synthetic examples.

201 real turns (``harvest.py``) is a thin seed for a robust tool-call fine-tune,
and the action mix is skewed (no ``deep`` scope, few ``done``). This module grows
the corpus to a target size with synthetic examples that are:

- **grounded** — each one is attached to a REAL system prompt mined from the logs
  that actually advertises the target tool (so we never teach calling a tool the
  catalogue doesn't list, and the big French contract text is authentic);
- **validated** — every gold envelope is run through Bob's real
  :func:`bob.sub_agent.actions.parse_action` before it is kept; anything the
  runner would reject is dropped, never trained on;
- **diverse** — per-tool ``tool_call`` variety, ``progress`` reasoning, ``done``
  in both ``fact`` (short + ``confidence``) and ``deep`` (Markdown ``ui_payload``)
  shapes, the ``result_ref`` finishing path after a tool result, and
  "no tool needed → done directly".

Run AFTER ``harvest.py``::

    python -m bob.dataset.augment --target 800
    # -> out/subagent_sft.augmented.jsonl  (real + synthetic, shuffled)
    # -> out/train.jsonl  +  out/val.jsonl  (90/10 split)
"""

from __future__ import annotations

import argparse
import json
import os
import random
from typing import Any, Callable, Iterator

from bob.sub_agent.actions import SubAgentActionParseError, parse_action

OUT_DIR_DEFAULT = "src/bob/dataset/out"
SEED = 0xB0B


# --------------------------------------------------------------------------- #
# Validation — the same gate harvest.py uses, so synthetic == inference shape. #
# --------------------------------------------------------------------------- #
def _valid(envelope: dict[str, Any]) -> bool:
    payload = dict(envelope)
    if payload.get("action") == "done":
        payload.setdefault("status", "complete")
        payload.setdefault("reason_code", "ok")
        payload.setdefault("cost", {})
    try:
        parse_action(payload)
        return True
    except SubAgentActionParseError:
        return False


def _example(system_msgs: list[dict], turns: list[dict], gold: dict, scope: str) -> dict | None:
    if not _valid(gold):
        return None
    gold_str = json.dumps(gold, ensure_ascii=False)
    return {
        "messages": system_msgs + turns + [{"role": "assistant", "content": gold_str}],
        "meta": {"category": "synthetic", "scope": scope, "action": gold["action"]},
    }


# --------------------------------------------------------------------------- #
# Real system-prompt pool, keyed by which tools each advertises + scope tag.   #
# --------------------------------------------------------------------------- #
class SysPool:
    def __init__(self, harvested_path: str):
        self.by_tool: dict[str, list[list[dict]]] = {}
        self.by_scope: dict[str, list[list[dict]]] = {"brief": [], "fact": [], "deep": []}
        self._seen: set[str] = set()
        for line in open(harvested_path):
            msgs = json.loads(line)["messages"]
            sys_msgs = [m for m in msgs if m.get("role") == "system"]
            if not sys_msgs:
                continue
            text = " ".join(m["content"] for m in sys_msgs)
            key = text[:300]
            if key in self._seen:
                continue
            self._seen.add(key)
            scope = ("fact" if "ONE precise fact" in text
                     else "deep" if "complete information" in text else "brief")
            self.by_scope[scope].append(sys_msgs)
            for tool in ("gmail_search", "web_search", "web_fetch", "maps_directions",
                         "get_crypto_price", "market_data_api"):
                if tool in text:
                    self.by_tool.setdefault(tool, []).append(sys_msgs)

    def for_tool(self, rng: random.Random, tool: str) -> list[dict] | None:
        pool = self.by_tool.get(tool)
        return [dict(m) for m in rng.choice(pool)] if pool else None

    def for_scope(self, rng: random.Random, scope: str) -> list[dict] | None:
        pool = self.by_scope.get(scope) or self.by_scope["brief"]
        return [dict(m) for m in rng.choice(pool)] if pool else None


# --------------------------------------------------------------------------- #
# Synthetic content banks (French user-facing, English internal reasoning).   #
# --------------------------------------------------------------------------- #
SENDERS = ["Datadog", "Olivier Berni", "GitHub", "Stripe", "Linear", "AWS", "Notion", "Slack"]
SUBJECTS = ["facture", "alerte", "rapport hebdo", "Kili", "incident prod", "review", "release"]
DATES = ["2026-05-24", "2026-05-29", "2026-06-01", "2026-06-05", "2026-06-08", "2026-06-10"]
CITIES = ["Chambéry", "Lyon", "Paris", "Turin, Italie", "Grenoble", "Annecy", "Genève"]
MODES = ["driving", "transit", "bicycling"]
CRYPTO = ["Bitcoin", "Ethereum", "Solana"]
WEB_TOPICS = [
    "actualités France aujourd'hui", "météo {city} cette semaine",
    "résultats Ligue 1 ce week-end", "prix essence {city} juin 2026",
    "horaires train {city} Paris", "programmation festival été 2026 {city}",
]


def _tool_call_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    g = lambda **a: {"action": "tool_call", **a}

    # gmail_search — varied arg combos seen in real data.
    gmail_specs = [
        lambda: ("Récupère mes 10 derniers e-mails non lus.",
                 {"label": "INBOX", "max_results": rng.choice([5, 10, 20, 50])}),
        lambda: (f"Cherche les mails de {(s := rng.choice(SENDERS))}.",
                 {"from_name": s, "max_results": rng.choice([10, 20])}),
        lambda: (f"Trouve les mails dont le sujet contient « {(sub := rng.choice(SUBJECTS))} ».",
                 {"subject_contains": sub, "max_results": 20}),
        lambda: (f"Liste les e-mails reçus entre le {(a := rng.choice(DATES))} et après.",
                 {"after": a, "before": rng.choice(DATES), "label": "INBOX", "max_results": 50}),
    ]
    for spec in gmail_specs:
        for _ in range(8):
            goal, args = spec()
            sm = pool.for_tool(rng, "gmail_search")
            if sm:
                yield from _y(sm, goal, g(name="gmail_search", args=args), "brief")

    # web_search
    for _ in range(40):
        topic = rng.choice(WEB_TOPICS).format(city=rng.choice(CITIES))
        sm = pool.for_tool(rng, "web_search")
        if sm:
            args = {"query": topic} if rng.random() < 0.2 else {"query": topic, "max_results": rng.choice([3, 5, 8, 10])}
            yield from _y(sm, f"Renseigne-toi sur : {topic}.", g(name="web_search", args=args), "brief")

    # web_fetch
    for _ in range(20):
        sm = pool.for_tool(rng, "web_fetch")
        if sm:
            url = f"https://fr.wikipedia.org/wiki/{rng.choice(['Juin_2026','Chambéry','Ligue_1'])}"
            yield from _y(sm, f"Lis le contenu de cette page : {url}",
                          g(name="web_fetch", args={"url": url}), "brief")

    # maps_directions
    for _ in range(20):
        sm = pool.for_tool(rng, "maps_directions")
        if sm:
            o, d = rng.sample(CITIES, 2)
            args = {"origin": o, "destination": d}
            if rng.random() < 0.7:
                args["mode"] = rng.choice(MODES)
            yield from _y(sm, f"Comment aller de {o} à {d} ?", g(name="maps_directions", args=args), "brief")

    # get_crypto_price
    for _ in range(10):
        sm = pool.for_tool(rng, "get_crypto_price")
        if sm:
            asset = rng.choice(CRYPTO)
            yield from _y(sm, f"Quel est le cours du {asset} ?",
                          g(name="get_crypto_price", args={"crypto_asset": asset, "currency": "EUR"}), "fact")


def _progress_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    thoughts = [
        "The user wants recent unread emails. I'll query gmail_search on INBOX, then summarise the senders and subjects in French.",
        "This needs current web information. I'll search first, then fetch the most relevant source before answering.",
        "Two-step task: get the route, then format a short French itinerary. Starting with maps_directions.",
        "I should narrow the search by sender to avoid noise, then present a compact French digest.",
    ]
    for t in thoughts:
        for _ in range(6):
            sm = pool.for_scope(rng, "brief")
            if sm:
                yield from _y(sm, "Prépare la réponse.", {"action": "progress", "thought": t}, "brief")


def _done_fact_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    facts = [
        ("La tour Eiffel mesure 330 mètres de haut.", "confirmed"),
        ("Le marché parisien était ouvert le 5 juin 2026.", "confirmed"),
        ("Patrick Bruel n'est, à ma connaissance, pas en prison à cette date.", "probable"),
        ("Le Bitcoin se situe autour de 58 000 € aujourd'hui.", "probable"),
        ("Oui, il y avait 3 e-mails non lus dans ta boîte ce matin.", "confirmed"),
    ]
    for summary, conf in facts:
        for _ in range(6):
            sm = pool.for_scope(rng, "fact")
            if sm:
                yield from _y(sm, "Réponds en une phrase.",
                              {"action": "done", "result_summary": summary, "ui_payload": None,
                               "result_ref": None, "confidence": conf}, "fact")


def _done_deep_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    docs = [
        ("Briefing actualités du jour préparé.",
         "## Actualités — 11 juin 2026\n\n### Politique\n- ...\n\n### Économie\n- ...\n\n### International\n- ..."),
        ("Itinéraire Chambéry → Turin détaillé.",
         "## Itinéraire Chambéry → Turin\n\n**Durée estimée :** ~2 h 30 en voiture\n\n1. A43 direction Modane\n2. Tunnel du Fréjus\n3. A32 vers Turin"),
        ("Synthèse hebdo de tes e-mails importants.",
         "## E-mails de la semaine\n\n| Expéditeur | Sujet | Date |\n|---|---|---|\n| Datadog | Alerte | 09/06 |\n| GitHub | Review | 10/06 |"),
    ]
    for summary, md in docs:
        for _ in range(8):
            sm = pool.for_scope(rng, "deep")
            if sm:
                yield from _y(sm, "Rédige le livrable complet.",
                              {"action": "done", "result_summary": summary, "ui_payload": md,
                               "result_ref": None, "confidence": "confirmed"}, "deep")


def _done_result_ref_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    """Multi-turn: a tool returned a result; gold = ``done`` referencing it.

    This is the weak-model finishing path (PRD 0009): conclude by *referencing*
    the stored result instead of re-emitting the whole payload — and crucially
    STOP after the one JSON object (no hallucinated ``<function_results>`` tail).
    """
    cases = [
        ("gmail_search", "gmail_search#1",
         {"tool": "gmail_search", "status": "ok", "result_ref": "gmail_search#1",
          "result": {"count": 5, "messages": [{"from": "Datadog", "subject": "Alerte"}]}},
         "Voici tes 5 e-mails non lus les plus récents."),
        ("web_search", "web_search#1",
         {"tool": "web_search", "status": "ok", "result_ref": "web_search#1",
          "result": {"query": "actualités France", "count": 8, "results": []}},
         "J'ai trouvé les principales actualités du jour."),
    ]
    for tool, ref, tool_msg, summary in cases:
        for _ in range(8):
            sm = pool.for_tool(rng, tool)
            if sm:
                turns = [
                    {"role": "user", "content": "Fais la recherche puis présente le résultat."},
                    {"role": "assistant", "content": json.dumps(
                        {"action": "tool_call", "name": tool, "args": {}}, ensure_ascii=False)},
                    {"role": "tool", "content": json.dumps(tool_msg, ensure_ascii=False)},
                ]
                ex = _example(sm, turns,
                              {"action": "done", "result_summary": summary, "ui_payload": None,
                               "result_ref": ref, "confidence": "confirmed",
                               "status": "complete", "reason_code": "ok", "cost": {}}, "brief")
                if ex:
                    yield ex


def _done_no_tool_examples(pool: SysPool, rng: random.Random) -> Iterator[dict]:
    """Trivial goal answerable without any tool → ``done`` directly (anti over-call)."""
    qa = [
        ("Dis bonjour.", "Bonjour Tom ! Comment puis-je t'aider ?"),
        ("Combien font 12 × 8 ?", "12 × 8 = 96."),
        ("Donne-moi un synonyme de « rapide ».", "Un synonyme de « rapide » est « véloce »."),
    ]
    for goal, ans in qa:
        for _ in range(6):
            sm = pool.for_scope(rng, "fact")
            if sm:
                yield from _y(sm, goal,
                              {"action": "done", "result_summary": ans, "ui_payload": None,
                               "result_ref": None, "confidence": "confirmed"}, "fact")


def _y(sys_msgs: list[dict], goal: str, gold: dict, scope: str) -> Iterator[dict]:
    if gold.get("action") == "done":
        gold = {**gold, "status": "complete", "reason_code": "ok", "cost": {}}
    ex = _example(sys_msgs, [{"role": "user", "content": goal}], gold, scope)
    if ex:
        yield ex


GENERATORS: list[Callable[[SysPool, random.Random], Iterator[dict]]] = [
    _tool_call_examples,
    _progress_examples,
    _done_fact_examples,
    _done_deep_examples,
    _done_result_ref_examples,
    _done_no_tool_examples,
]


def augment(out_dir: str, target: int, val_frac: float) -> None:
    rng = random.Random(SEED)
    harvested = os.path.join(out_dir, "subagent_sft.jsonl")
    pool = SysPool(harvested)

    synthetic: list[dict] = []
    for gen in GENERATORS:
        synthetic.extend(gen(pool, rng))

    real = [json.loads(l) for l in open(harvested)]
    # Top up / trim synthetic so real + synthetic ≈ target.
    want_synth = max(0, target - len(real))
    rng.shuffle(synthetic)
    synthetic = synthetic[:want_synth]

    # Dedup on the full message list so template reuse / repeated real turns
    # don't over-weight the set.
    combined, seen = [], set()
    for ex in real + synthetic:
        key = json.dumps(ex["messages"], ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        combined.append(ex)
    rng.shuffle(combined)

    aug_path = os.path.join(out_dir, "subagent_sft.augmented.jsonl")
    with open(aug_path, "w") as f:
        for ex in combined:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    n_val = int(len(combined) * val_frac)
    val, train = combined[:n_val], combined[n_val:]
    for name, rows in (("train.jsonl", train), ("val.jsonl", val)):
        with open(os.path.join(out_dir, name), "w") as f:
            for ex in rows:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    from collections import Counter
    mix = Counter(json.loads(e["messages"][-1]["content"]).get("action") for e in combined)
    cat = Counter(e.get("meta", {}).get("category", "real") for e in combined)
    print(f"real={len(real)}  synthetic_kept={len(synthetic)}  total={len(combined)}")
    print(f"action mix : {dict(mix)}")
    print(f"category   : {dict(cat)}")
    print(f"-> {aug_path}")
    print(f"-> {os.path.join(out_dir, 'train.jsonl')}  ({len(train)})")
    print(f"-> {os.path.join(out_dir, 'val.jsonl')}  ({len(val)})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=OUT_DIR_DEFAULT)
    ap.add_argument("--target", type=int, default=800, help="approx total examples (real + synthetic)")
    ap.add_argument("--val-frac", type=float, default=0.1)
    args = ap.parse_args()
    augment(args.out, args.target, args.val_frac)


if __name__ == "__main__":
    main()
