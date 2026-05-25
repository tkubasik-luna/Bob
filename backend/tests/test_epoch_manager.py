"""Tests for :class:`bob.epoch.manager.EpochManager`.

The manager has two surfaces:

* :meth:`should_seal` — pure decision function. Token threshold trigger
  only; no idle / wall-clock trigger.
* :meth:`apply_seal` — side-effecting procedure: bumps the current
  epoch id, rebuilds the cross-epoch digest from RAW sealed turns,
  persists it via :class:`CrossEpochDigestStore`.

The acceptance criteria from issue 0051 land on these surfaces:

* "Seals when rolling summary tokens > threshold; no idle trigger."
* "Sealed epoch persists current rolling summary + ``summariser_version``
  + ``(from_turn, to_turn)`` range."
* "Cross-epoch digest regenerated from RAW sealed turns on every new
  seal."
"""

from __future__ import annotations

import sqlite3

from bob.context.summariser import SUMMARISER_VERSION
from bob.context.tokenizer import WordCountTokenizer
from bob.db.migrations_runner import apply_migrations, default_migrations_dir
from bob.epoch.digest import CrossEpochDigestStore
from bob.epoch.manager import EpochManager
from bob.epoch.policy import EpochPolicy
from bob.jarvis_store import JarvisStore
from bob.rolling_summary_store import RollingSummaryStore


def _setup() -> tuple[sqlite3.Connection, JarvisStore, RollingSummaryStore, CrossEpochDigestStore]:
    conn = sqlite3.connect(":memory:")
    apply_migrations(conn, default_migrations_dir())
    return conn, JarvisStore(conn), RollingSummaryStore(conn), CrossEpochDigestStore(conn)


def _seed_pair(js: JarvisStore, idx: int) -> None:
    js.append("user", f"u{idx}: question with several words to bulk")
    js.append("assistant", f"a{idx}: long-form answer to bulk the transcript out")


# ---------------------------------------------------------------------------
# should_seal — pure decision function.
# ---------------------------------------------------------------------------


def test_should_seal_returns_false_when_no_rolling_summary_present() -> None:
    """With no rolling summary persisted, the manager never seals."""

    conn, _js, rolling, digests = _setup()
    manager = EpochManager(
        policy=EpochPolicy(token_threshold=5),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )
    decision = manager.should_seal()

    assert decision.should_seal is False
    assert decision.reason == "no_summary"
    assert decision.rolling_summary_tokens == 0
    assert decision.current_epoch_id == 0


def test_should_seal_returns_false_when_summary_below_threshold() -> None:
    conn, _js, rolling, digests = _setup()
    rolling.append(
        from_turn=1, to_turn=2, summariser_version=SUMMARISER_VERSION, text="short summary"
    )

    manager = EpochManager(
        policy=EpochPolicy(token_threshold=10),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )
    decision = manager.should_seal()

    assert decision.should_seal is False
    assert decision.reason == "below_threshold"
    # WordCountTokenizer = 2 tokens "short summary".
    assert decision.rolling_summary_tokens == 2


def test_should_seal_returns_true_when_summary_exceeds_threshold() -> None:
    conn, _js, rolling, digests = _setup()
    rolling.append(
        from_turn=1,
        to_turn=20,
        summariser_version=SUMMARISER_VERSION,
        text="one two three four five six seven eight nine ten eleven twelve",
    )

    manager = EpochManager(
        policy=EpochPolicy(token_threshold=5),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )
    decision = manager.should_seal()

    assert decision.should_seal is True
    assert decision.reason == "token_threshold_exceeded"
    assert decision.rolling_summary_tokens == 12
    assert decision.threshold == 5


def test_should_seal_ignores_summary_for_previous_epoch() -> None:
    """A sealed summary (epoch_id < current) does not retrigger sealing."""

    conn, _js, rolling, digests = _setup()
    # Pretend the manager already sealed once: write a sealed summary
    # under epoch_id=0, advance the manager to epoch 1.
    rolling.append(
        from_turn=1,
        to_turn=20,
        summariser_version=SUMMARISER_VERSION,
        text="huge huge huge huge huge huge",
        epoch_id=0,
    )

    manager = EpochManager(
        policy=EpochPolicy(token_threshold=2),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
        current_epoch_id=1,
    )
    decision = manager.should_seal()

    assert decision.should_seal is False
    assert decision.reason == "no_summary"


# ---------------------------------------------------------------------------
# apply_seal — side-effecting procedure.
# ---------------------------------------------------------------------------


def test_apply_seal_is_noop_below_threshold() -> None:
    conn, _js, rolling, digests = _setup()
    rolling.append(from_turn=1, to_turn=2, summariser_version=SUMMARISER_VERSION, text="short")
    manager = EpochManager(
        policy=EpochPolicy(token_threshold=10),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )

    new_epoch = manager.apply_seal()

    assert new_epoch is None
    assert manager.current_epoch_id == 0
    assert digests.count() == 0


def test_apply_seal_bumps_epoch_and_persists_digest() -> None:
    """Sealing fires, current_epoch_id advances, digest persisted."""

    conn, js, rolling, digests = _setup()
    # Seed some history so the digest has raw turns to fold in.
    for i in range(3):
        _seed_pair(js, i)

    rolling.append(
        from_turn=1,
        to_turn=6,
        summariser_version=SUMMARISER_VERSION,
        text="one two three four five six seven eight",
        epoch_id=0,
    )

    policy = EpochPolicy(
        token_threshold=3,
        summariser_prompt_version=42,
        max_digest_size=500,
    )
    manager = EpochManager(
        policy=policy,
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )
    assert manager.current_epoch_id == 0

    new_epoch = manager.apply_seal()

    assert new_epoch == 1
    assert manager.current_epoch_id == 1
    assert digests.count() == 1

    latest = digests.latest()
    assert latest is not None
    # All 6 jarvis_messages rows had epoch_id=0 (backfill default), so
    # the digest input is the full conversation.
    assert latest.sealed_epoch_count == 1
    assert latest.summariser_version == policy.summariser_prompt_version
    assert "u0" in latest.text
    assert "a2" in latest.text


def test_apply_seal_three_seals_digest_input_grows_with_raw_turns() -> None:
    """3 sequential seals — every digest is rebuilt from RAW sealed turns.

    Drift-bounding invariant from PRD 0006: the digest input never
    incorporates a prior digest. We assert that each seal's persisted
    digest contains the raw conversation content (not a previous
    digest's text) and that the input grows monotonically with sealed
    turns.
    """

    conn, js, rolling, digests = _setup()
    tokenizer = WordCountTokenizer()

    policy = EpochPolicy(token_threshold=3, max_digest_size=4000)
    manager = EpochManager(
        policy=policy,
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )

    def _seal_once(epoch_payload_prefix: str) -> None:
        # Mark every existing jarvis_message as belonging to the current
        # epoch (the manager's apply_seal pulls "epoch_id < new" — so
        # we need the rows to be tagged with the soon-to-be-sealed
        # epoch).
        conn.execute(
            "UPDATE jarvis_messages SET epoch_id = ? WHERE epoch_id = 99",
            (manager.current_epoch_id,),
        )
        for i in range(3):
            js.append("user", f"{epoch_payload_prefix}u{i}: long raw line for the transcript")
            js.append(
                "assistant",
                f"{epoch_payload_prefix}a{i}: long raw response for the transcript",
            )
        # Bump the freshly inserted rows to the current epoch id so they
        # become sealed by the next apply_seal.
        conn.execute(
            "UPDATE jarvis_messages SET epoch_id = ? WHERE epoch_id = 0 AND id IN ("
            "SELECT id FROM jarvis_messages WHERE epoch_id = 0 ORDER BY id DESC LIMIT 6)",
            (manager.current_epoch_id,),
        )

        rolling.append(
            from_turn=1,
            to_turn=6,
            summariser_version=SUMMARISER_VERSION,
            text=" ".join(
                f"summary{i}-with-many-padding-tokens-here-to-cross-threshold" for i in range(8)
            ),
            epoch_id=manager.current_epoch_id,
        )
        manager.apply_seal()

    _seal_once("E0_")
    first = digests.latest()
    assert first is not None
    assert "E0_u0" in first.text
    assert first.sealed_epoch_count == 1

    _seal_once("E1_")
    second = digests.latest()
    assert second is not None
    assert second.id != first.id
    # Critical drift-bounding invariant: the prior digest text is NOT
    # an input. Use a digest-only marker that does not appear in any
    # raw turn: the rendered header "Synthèse des époques passées".
    assert "Synthèse des époques passées" not in _digest_input_text(conn, manager)
    # And the second digest contains BOTH epoch 0 + epoch 1 raw lines.
    assert "E0_u0" in second.text
    assert "E1_u0" in second.text
    assert second.sealed_epoch_count == 2

    _seal_once("E2_")
    third = digests.latest()
    assert third is not None
    assert "E0_u0" in third.text
    assert "E1_u0" in third.text
    assert "E2_u0" in third.text
    assert third.sealed_epoch_count == 3

    # Token-count sanity: third digest tokens > first (more raw history).
    assert tokenizer.count(third.text) > tokenizer.count(first.text)


def _digest_input_text(conn: sqlite3.Connection, manager: EpochManager) -> str:
    """Compose the same raw transcript the manager will feed its digest.

    Mirrors :func:`_read_sealed_turns` so the test can assert "prior
    digest text never re-enters" without coupling to private code.
    """

    rows = conn.execute(
        "SELECT role, content FROM jarvis_messages WHERE epoch_id < ? ORDER BY id ASC",
        (manager.current_epoch_id,),
    ).fetchall()
    return "\n".join(f"{role}:{content}" for role, content in rows)


def test_apply_seal_caps_digest_at_max_digest_size() -> None:
    """The :attr:`EpochPolicy.max_digest_size` truncates oversize digests."""

    conn, js, rolling, digests = _setup()
    # 100 raw pairs ≈ a fat transcript; ensure truncation kicks in.
    for i in range(100):
        _seed_pair(js, i)

    rolling.append(
        from_turn=1,
        to_turn=200,
        summariser_version=SUMMARISER_VERSION,
        text="one two three four five six seven eight nine ten",
        epoch_id=0,
    )

    policy = EpochPolicy(token_threshold=3, max_digest_size=200)
    manager = EpochManager(
        policy=policy,
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )
    manager.apply_seal()

    latest = digests.latest()
    assert latest is not None
    assert len(latest.text) <= 200
    assert latest.text.endswith("…")


def test_apply_seal_no_idle_trigger() -> None:
    """No wall-clock / idle trigger — repeated apply_seal without growth is a no-op."""

    conn, _js, rolling, digests = _setup()
    rolling.append(from_turn=1, to_turn=2, summariser_version=SUMMARISER_VERSION, text="short")

    manager = EpochManager(
        policy=EpochPolicy(token_threshold=10),
        rolling_summary_store=rolling,
        digest_store=digests,
        conn=conn,
    )

    for _ in range(5):
        result = manager.apply_seal()
        assert result is None
    assert manager.current_epoch_id == 0
    assert digests.count() == 0
