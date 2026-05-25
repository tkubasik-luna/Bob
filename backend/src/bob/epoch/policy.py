"""Configuration centralising every epoch-sealing knob.

PRD 0006 / issue 0051. The :class:`EpochPolicy` is the single source of
truth for:

- ``token_threshold`` — when the *current* rolling summary's token count
  crosses this, :class:`bob.epoch.manager.EpochManager` seals the epoch.
  Picked deterministically — no idle / wall-clock trigger (would make
  the trigger untestable).
- ``summariser_model_id`` — purely informational at this slice; logged
  on seal so future telemetry can correlate digest quality with the
  model that produced it. Not used by the digest rebuild path (which
  is summariser-agnostic — it composes a deterministic header + raw
  transcript wrapped by the summariser prompt fragments).
- ``summariser_prompt_version`` — stamped on every digest row so a
  future wording change is auditable at the data layer.
- ``max_digest_size`` — soft cap (in characters) on the rebuilt
  cross-epoch digest. Excess is truncated with a trailing ``…`` marker.
  Keeps the bounded prompt's digest term bounded even after many seals.

Centralising all four knobs here makes tuning a one-stop affair and
ensures the long-session smoke test can pin them deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Default token-budget trigger for sealing. The bounded ``ContextPolicy``
#: keeps the rolling summary under a few hundred tokens in practice;
#: 512 is comfortably above that so a real session has to sustain growth
#: for the seal to fire. Tests override this aggressively.
DEFAULT_EPOCH_TOKEN_THRESHOLD = 512

#: Default summariser model id stamped on the seal log. Production wires
#: the LM Studio model name; tests use a fixed string so log assertions
#: are stable.
DEFAULT_SUMMARISER_MODEL_ID = "lm_studio:default"

#: Default summariser prompt version. Matches
#: :data:`bob.context.summariser.SUMMARISER_VERSION` initially but kept
#: distinct so the epoch layer can pin its own digest rebuild revision
#: independently of the rolling-summary summariser.
DEFAULT_SUMMARISER_PROMPT_VERSION = 1

#: Default soft cap on the cross-epoch digest text. ~1200 chars keeps
#: the digest term well under the bounded policy's overall token budget
#: (default 2048 words) while still affording a few sealed epochs worth
#: of context.
DEFAULT_MAX_DIGEST_SIZE = 1200


@dataclass(frozen=True)
class EpochPolicy:
    """Knobs that drive sealing + cross-epoch digest behavior.

    Fields:

    - ``token_threshold`` — sealing fires when the current rolling
      summary's :class:`bob.context.tokenizer.Tokenizer` count exceeds
      this. ``> threshold`` is the trigger, not ``>=`` — equality keeps
      the seal step idempotent on a fresh seal that lands exactly on the
      threshold value (tests rely on this).
    - ``summariser_model_id`` — informational; persisted on seal events.
    - ``summariser_prompt_version`` — stamped on every persisted digest
      row.
    - ``max_digest_size`` — character cap on the rebuilt digest text.
    """

    token_threshold: int = DEFAULT_EPOCH_TOKEN_THRESHOLD
    summariser_model_id: str = DEFAULT_SUMMARISER_MODEL_ID
    summariser_prompt_version: int = DEFAULT_SUMMARISER_PROMPT_VERSION
    max_digest_size: int = DEFAULT_MAX_DIGEST_SIZE


#: Process-wide default policy. The orchestrator wires this; tests build
#: their own narrower policy when they need a low threshold to force a
#: seal at small turn counts.
DEFAULT_EPOCH_POLICY = EpochPolicy()
