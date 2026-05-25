"""EpochManager — deterministic seal decision + apply procedure.

PRD 0006 / issue 0051. The sole sealing trigger is "current rolling
summary token count exceeds :attr:`EpochPolicy.token_threshold`". No
idle-gap trigger (would be clock-dependent and untestable).

The manager is intentionally split in two:

- :meth:`should_seal` — pure decision function. Given the current
  epoch id + a tokenizer reading the current rolling summary, returns a
  :class:`SealDecision`. Tested in isolation.
- :meth:`apply_seal` — side-effecting procedure. Reads ALL sealed
  turns (across every sealed epoch_id), rebuilds the cross-epoch digest
  from RAW turns and persists it via :class:`CrossEpochDigestStore`,
  then bumps the active ``epoch_id``. The "current rolling summary" is
  not deleted — it stays as a sealed row tagged with the previous
  epoch_id, queryable later by the retrieval stub.

This split is the same shape as ``maybe_regenerate_rolling_summary``:
the orchestrator calls one bounded entry point, but tests can drill
into the pure decision function without seeding a sqlite database.

State model. There is exactly one ``current_epoch_id`` per orchestrator
instance, persisted in-process. After a fresh boot every existing
``jarvis_messages`` / ``rolling_summaries`` row was backfilled to
``epoch_id = 0`` by migration ``0007_epoch_id_columns.sql``, so the
manager starts at ``current_epoch_id = 0`` (or whichever max it reads
back from the store on warm boot — see :meth:`from_stores`).
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass

import structlog

from bob.context.entry import CONTEXT_ENTRY_SCHEMA_VERSION, ContextEntry, ContextEntryKind
from bob.context.tokenizer import Tokenizer, WordCountTokenizer
from bob.epoch.digest import CrossEpochDigestStore, regenerate_cross_epoch_digest
from bob.epoch.policy import DEFAULT_EPOCH_POLICY, EpochPolicy
from bob.rolling_summary_store import RollingSummaryStore

_logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SealDecision:
    """Pure result of :meth:`EpochManager.should_seal`.

    Fields:

    - ``should_seal`` — top-level boolean. Callers only need this; the
      remaining fields are for logs + tests.
    - ``current_epoch_id`` — epoch id the decision was evaluated against.
    - ``rolling_summary_tokens`` — current rolling summary's tokenized
      length, or 0 when there is no summary yet.
    - ``threshold`` — :attr:`EpochPolicy.token_threshold` snapshot.
    - ``reason`` — short string for logs. ``"below_threshold"`` when no
      seal; ``"token_threshold_exceeded"`` when seal fires; ``"no_summary"``
      when nothing to seal yet.
    """

    should_seal: bool
    current_epoch_id: int
    rolling_summary_tokens: int
    threshold: int
    reason: str


class EpochManager:
    """Token-threshold sealer + cross-epoch digest rebuilder.

    Construction args:

    - ``policy`` — :class:`EpochPolicy` snapshot driving every knob.
    - ``rolling_summary_store`` — read access to the current rolling
      summary (latest row tagged with the active epoch_id).
    - ``digest_store`` — append-only sink for the rebuilt digest.
    - ``conn`` — the SQLite connection from which we read RAW sealed
      turns. The orchestrator owns the connection; the manager only
      reads.
    - ``tokenizer`` — defaults to :class:`WordCountTokenizer`. Reason
      for the default in the module docstring of
      :mod:`bob.context.tokenizer` — it is dependency-free and the
      threshold is a coarse "is the summary getting huge?" check, not
      a billing-grade count. Swap in tiktoken later behind the same
      interface without touching this module.

    Concurrency: a single instance-level lock serialises seal
    procedures. ``should_seal`` is read-only.
    """

    def __init__(
        self,
        *,
        policy: EpochPolicy = DEFAULT_EPOCH_POLICY,
        rolling_summary_store: RollingSummaryStore,
        digest_store: CrossEpochDigestStore,
        conn: sqlite3.Connection,
        tokenizer: Tokenizer | None = None,
        current_epoch_id: int = 0,
    ) -> None:
        self._policy = policy
        self._rolling = rolling_summary_store
        self._digests = digest_store
        self._conn = conn
        self._tokenizer = tokenizer or WordCountTokenizer()
        self._current_epoch_id = current_epoch_id
        self._lock = threading.Lock()

    @property
    def policy(self) -> EpochPolicy:
        return self._policy

    @property
    def current_epoch_id(self) -> int:
        """Active epoch id; ``apply_seal`` bumps this on success."""

        return self._current_epoch_id

    def should_seal(self) -> SealDecision:
        """Inspect the current rolling summary; decide whether to seal.

        Pure read-only call. Returns a :class:`SealDecision` describing
        the inputs that drove the decision (used for logging + tests).
        """

        latest = self._rolling.latest()
        if latest is None or not latest.text:
            return SealDecision(
                should_seal=False,
                current_epoch_id=self._current_epoch_id,
                rolling_summary_tokens=0,
                threshold=self._policy.token_threshold,
                reason="no_summary",
            )

        # We only consider the rolling summary for the CURRENT epoch.
        # Earlier-epoch summaries are sealed history — their tokens do
        # not contribute to the next seal decision.
        if latest.epoch_id != self._current_epoch_id:
            return SealDecision(
                should_seal=False,
                current_epoch_id=self._current_epoch_id,
                rolling_summary_tokens=0,
                threshold=self._policy.token_threshold,
                reason="no_summary",
            )

        token_count = self._tokenizer.count(latest.text)
        should = token_count > self._policy.token_threshold
        return SealDecision(
            should_seal=should,
            current_epoch_id=self._current_epoch_id,
            rolling_summary_tokens=token_count,
            threshold=self._policy.token_threshold,
            reason="token_threshold_exceeded" if should else "below_threshold",
        )

    def apply_seal(self) -> int | None:
        """Seal the current epoch and rebuild the cross-epoch digest.

        Idempotent on a no-op decision: if :meth:`should_seal` returns
        ``should_seal=False`` we return ``None`` without touching any
        store. On a seal:

        1. The current rolling summary's ``epoch_id`` stays as-is
           (sealed). Future :class:`bob.context.providers.rolling_summary.RollingSummaryProvider`
           reads filter on ``epoch_id = current_epoch_id`` so the sealed
           row drops out of the active prompt automatically.
        2. ``current_epoch_id`` increments by 1.
        3. Cross-epoch digest is rebuilt from RAW sealed turns across
           EVERY epoch_id below the new ``current_epoch_id``.
        4. The new digest row is persisted.

        Returns the new ``current_epoch_id`` on a successful seal, or
        ``None`` on a no-op.
        """

        with self._lock:
            decision = self.should_seal()
            if not decision.should_seal:
                return None

            sealed_epoch_id = self._current_epoch_id
            new_epoch_id = sealed_epoch_id + 1

            _logger.info(
                "epoch.seal_started",
                sealed_epoch_id=sealed_epoch_id,
                new_epoch_id=new_epoch_id,
                rolling_summary_tokens=decision.rolling_summary_tokens,
                token_threshold=decision.threshold,
                summariser_model_id=self._policy.summariser_model_id,
                summariser_prompt_version=self._policy.summariser_prompt_version,
            )

            # Bump the live epoch first so the rolling-summary row from
            # the just-sealed epoch is no longer considered "current".
            # The summary row itself is immutable — it is now sealed
            # history.
            self._current_epoch_id = new_epoch_id

            sealed_turns = _read_sealed_turns(self._conn, new_epoch_id)
            sealed_epoch_count = sealed_epoch_id + 1  # epochs 0..sealed inclusive.
            text = regenerate_cross_epoch_digest(
                sealed_turns=sealed_turns,
                sealed_epoch_count=sealed_epoch_count,
                policy=self._policy,
            )
            if text:
                self._digests.append(
                    text=text,
                    summariser_version=self._policy.summariser_prompt_version,
                    sealed_epoch_count=sealed_epoch_count,
                    token_estimate=self._tokenizer.count(text),
                )

            _logger.info(
                "epoch.seal_completed",
                sealed_epoch_id=sealed_epoch_id,
                new_epoch_id=new_epoch_id,
                sealed_turn_count=len(sealed_turns),
                digest_chars=len(text),
            )
            return new_epoch_id


def _read_sealed_turns(conn: sqlite3.Connection, current_epoch_id: int) -> list[ContextEntry]:
    """Return RAW :class:`ContextEntry` rows for every sealed epoch.

    "Sealed" means ``epoch_id < current_epoch_id``. We read from
    ``jarvis_messages`` directly (not via :class:`JarvisStore.history`)
    so we can filter on ``epoch_id`` without overfetching. Pure read —
    rows are not mutated.

    Lives at module scope so :func:`regenerate_cross_epoch_digest` can
    be tested independently of the manager (the tests build the entry
    list by hand).
    """

    cursor = conn.execute(
        "SELECT role, content, epoch_id, id FROM jarvis_messages "
        "WHERE epoch_id < ? ORDER BY id ASC",
        (current_epoch_id,),
    )
    out: list[ContextEntry] = []
    for role, content, epoch_id, row_id in cursor.fetchall():
        if not isinstance(role, str) or not isinstance(content, str):
            continue
        kind: ContextEntryKind
        if role == "user":
            kind = "user_turn"
        elif role == "assistant":
            kind = "assistant_turn"
        else:
            kind = "system_note"
        out.append(
            ContextEntry(
                id=f"sealed:{row_id}",
                kind=kind,
                source="jarvis_store",
                token_estimate=len(content) // 4,
                pinned=False,
                created_at="",
                provider_id="sealed_turns",
                payload={"role": role, "content": content, "epoch_id": epoch_id},
                schema_version=CONTEXT_ENTRY_SCHEMA_VERSION,
            )
        )
    return out
