"""Pure model-memory budget core (PRD 0016 / issue 0107, Annexe J + G).

This module answers ONE question, deterministically and offline: *can this set
of models stay resident together on this host without OOM-ing?* It is the
guard-rail that makes the :mod:`bob.lm_studio_manager` v2 reversion from
offload-first to **multi-load** safe — before the manager loads a role's model
it sums the resident footprints and refuses if they would exceed the host
ceiling (Annexe G "Budget dépassé (check)").

Design contract — PURE + exhaustively unit-testable:

- :func:`footprint_gib` estimates a model's resident memory: the on-disk file
  size of its GGUF / MLX weights (≈ the resident weight bytes) **plus** a
  KV-cache margin proportional to ``context_length``. Both inputs are passed
  in (or read through an INJECTABLE probe) — the pure core never touches the
  real disk.
- :func:`local_ceiling_gib` / :func:`remote_ceiling_gib` derive the per-host
  ceiling: local = detected RAM (injected; e.g. macOS ``sysctl hw.memsize``)
  minus ``reserve_gib`` OS head-room; remote = ``per_host_override`` if set, else
  ``None`` ("no readable ceiling → skip the check, fall back to a try+catch on
  the real load", Annexe J step 3).
- :func:`fits` is the fit-check: ``sum(resident footprints) ≤ ceiling``. A
  ``None`` ceiling (remote, no override) always fits — the OOM safety net is
  the load itself, not this module.

The thin, side-effecting probes (``sysctl`` / ``os.stat``) live at the bottom
behind :class:`HostRamProbe` / :class:`ModelFileSizeProbe` so a caller can wire
the real machine in while every test injects fixed numbers. Sizes are GiB
(``2**30`` bytes) throughout — consistent with the ``*_gib`` budget fields in
:class:`bob.llm_selection_store.BudgetSelection`.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from bob.lm_studio_manager import host_from_base_url

#: Bytes in one GiB. All public sizes in this module are GiB.
BYTES_PER_GIB = 1024**3

#: Default OS head-room subtracted from detected local RAM when the budget block
#: pins no ``reserve_gib`` (mirrors
#: :data:`bob.llm_selection_store.DEFAULT_BUDGET_RESERVE_GIB`).
DEFAULT_RESERVE_GIB = 8.0

#: KV-cache margin model. The resident KV cache grows ≈ linearly with the
#: context window; we approximate it as ``KV_GIB_PER_1K_TOKENS`` GiB per 1024
#: tokens of ``context_length``. This is a deliberately conservative,
#: model-agnostic upper bound (a 7B-class model at ~0.18 GiB/1K covers most
#: local quantised setups with head-room) — the exact per-layer figure depends
#: on hidden size / head count / kv dtype, but the budget only needs a safe
#: margin, not an exact byte count. Tunable; injected into estimates so tests
#: pin it. When ``context_length`` is unknown (``None``) the margin is
#: :data:`DEFAULT_KV_MARGIN_GIB` (a flat fallback), never zero.
KV_GIB_PER_1K_TOKENS = 0.18
DEFAULT_KV_MARGIN_GIB = 1.0


def kv_cache_margin_gib(
    context_length: int | None,
    *,
    gib_per_1k_tokens: float = KV_GIB_PER_1K_TOKENS,
    default_margin_gib: float = DEFAULT_KV_MARGIN_GIB,
) -> float:
    """Return the KV-cache memory margin (GiB) for ``context_length`` tokens.

    Proportional to the context window: ``gib_per_1k_tokens`` per 1024 tokens.
    A missing / non-positive context length collapses to ``default_margin_gib``
    (a flat, non-zero fallback) so a model with an unknown window still books a
    margin instead of being budgeted as weights-only.
    """

    if context_length is None or context_length <= 0:
        return default_margin_gib
    return (context_length / 1024.0) * gib_per_1k_tokens


def footprint_gib(
    file_size_gib: float,
    context_length: int | None,
    *,
    gib_per_1k_tokens: float = KV_GIB_PER_1K_TOKENS,
    default_margin_gib: float = DEFAULT_KV_MARGIN_GIB,
) -> float:
    """Estimate a model's resident footprint (GiB) = weights + KV-cache margin.

    ``file_size_gib`` is the on-disk size of the model's GGUF / MLX weights
    (≈ the resident weight bytes); the KV margin is
    :func:`kv_cache_margin_gib` of ``context_length``. Pure: both inputs are
    supplied by the caller, so the function is fully deterministic and never
    reads the disk.
    """

    return file_size_gib + kv_cache_margin_gib(
        context_length,
        gib_per_1k_tokens=gib_per_1k_tokens,
        default_margin_gib=default_margin_gib,
    )


def local_ceiling_gib(detected_ram_gib: float, reserve_gib: float = DEFAULT_RESERVE_GIB) -> float:
    """Return the resident-memory ceiling (GiB) for a LOCAL host.

    Detected RAM minus the OS head-room ``reserve_gib``. Clamped at ``0.0`` so a
    reserve larger than the detected RAM yields a zero ceiling (everything
    refuses) rather than a negative one — a misconfigured reserve degrades to a
    safe refusal, never a silent "infinite" budget.
    """

    return max(0.0, detected_ram_gib - reserve_gib)


def remote_ceiling_gib(
    host: str,
    per_host_override: Mapping[str, float],
) -> float | None:
    """Return the ceiling (GiB) for a REMOTE host, or ``None`` to skip the check.

    A remote LM Studio server's RAM cannot be probed, so its ceiling comes
    ONLY from an explicit ``per_host_override`` entry. Absent an override we
    return ``None`` — "no readable ceiling" — and the caller falls back to a
    try+catch on the real load (Annexe J step 3 / Annexe G remote rows). The
    override map is keyed by the bare ``host:port`` (the same key
    :func:`bob.lm_studio_manager.host_from_base_url` derives); a base-URL key is
    also accepted and normalised so a hand-edited JSON with a full URL still
    matches.
    """

    if host in per_host_override:
        return per_host_override[host]
    # Tolerate a base-URL-shaped key in the override map (hand-edited JSON).
    for key, value in per_host_override.items():
        if host_from_base_url(key) == host:
            return value
    return None


def fits(resident_footprints_gib: Iterable[float], ceiling_gib: float | None) -> bool:
    """Fit-check: do the resident footprints fit under ``ceiling_gib``?

    ``True`` when the sum of ``resident_footprints_gib`` is ``≤ ceiling_gib``.
    A ``None`` ceiling (remote host with no override) ALWAYS fits — there is no
    readable limit to check against, so the real-load try+catch is the only
    OOM net (by design, Annexe J step 3).
    """

    if ceiling_gib is None:
        return True
    return sum(resident_footprints_gib) <= ceiling_gib


@dataclass(frozen=True)
class ResidentModel:
    """One model's contribution to a host's resident budget.

    ``model_id`` is the LM Studio model key; ``footprint_gib`` is its estimated
    resident size (:func:`footprint_gib`). Equality is by ``model_id`` only so a
    set of residents de-duplicates the same model loaded once (ref-count semantics
    live in the manager; this value object just carries the per-model number).
    """

    model_id: str
    footprint_gib: float

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ResidentModel) and other.model_id == self.model_id

    def __hash__(self) -> int:
        return hash(self.model_id)


@dataclass(frozen=True)
class BudgetDecision:
    """The outcome of a fit-check for ADDING a candidate model to a host.

    ``ok`` is whether the candidate fits alongside the already-resident set.
    ``ceiling_gib`` is the host ceiling used (``None`` = remote/no-override, the
    check was skipped → always ``ok``). ``required_gib`` / ``resident_gib`` are
    the projected total and the pre-existing total so a refusal message is
    self-explanatory. :meth:`message` renders the Annexe G refusal string.
    """

    ok: bool
    ceiling_gib: float | None
    resident_gib: float
    candidate_gib: float
    candidate_model: str

    @property
    def required_gib(self) -> float:
        """Projected resident total if the candidate were added."""

        return self.resident_gib + self.candidate_gib

    def message(self) -> str:
        """Human-facing refusal message (Annexe G "dépasse le plafond …")."""

        if self.ok:
            return (
                f"model {self.candidate_model!r} fits "
                f"({self.required_gib:.1f}/{self._ceiling_str()} GiB)"
            )
        return (
            f"chargement de {self.candidate_model!r} refusé : dépasse le plafond mémoire "
            f"({self.required_gib:.1f} GiB requis > {self._ceiling_str()} GiB) — "
            f"libère un rôle pour ce host."
        )

    def _ceiling_str(self) -> str:
        return "∞" if self.ceiling_gib is None else f"{self.ceiling_gib:.1f}"


@dataclass
class HostBudget:
    """Per-host resident budget tracker (pure, in-memory).

    Holds the host ceiling (``None`` = skip-check) and the set of currently
    resident models with their footprints. The manager owns one of these per
    host and asks :meth:`check_add` BEFORE a load. ``check_add`` is side-effect
    free — the manager mutates the resident set itself once the SDK load
    actually succeeds (so a failed load never leaves the tracker out of sync).

    Resident models de-duplicate by id (a model loaded once counts once even if
    several roles reference it — that IS the ref-count budget invariant).
    """

    ceiling_gib: float | None
    _resident: dict[str, float] = field(default_factory=dict)

    def resident_gib(self) -> float:
        """Sum of resident footprints (GiB)."""

        return sum(self._resident.values())

    def is_resident(self, model_id: str) -> bool:
        """Whether ``model_id`` is already counted as resident."""

        return model_id in self._resident

    def resident_ids(self) -> frozenset[str]:
        """The set of resident model ids (snapshot)."""

        return frozenset(self._resident)

    def check_add(self, model_id: str, candidate_gib: float) -> BudgetDecision:
        """Fit-check ADDING ``model_id`` (``candidate_gib``) to the resident set.

        Re-selecting an ALREADY-resident model is always ``ok`` and adds nothing
        (it is already counted) — the ref-count case where a second role picks a
        model that is already loaded must never be refused. Otherwise the
        candidate's footprint is summed on top of the resident total and checked
        against the ceiling via :func:`fits`.
        """

        if self.is_resident(model_id):
            return BudgetDecision(
                ok=True,
                ceiling_gib=self.ceiling_gib,
                resident_gib=self.resident_gib(),
                candidate_gib=0.0,
                candidate_model=model_id,
            )
        resident = self.resident_gib()
        ok = fits([resident, candidate_gib], self.ceiling_gib)
        return BudgetDecision(
            ok=ok,
            ceiling_gib=self.ceiling_gib,
            resident_gib=resident,
            candidate_gib=candidate_gib,
            candidate_model=model_id,
        )

    def add(self, model_id: str, footprint_gib: float) -> None:
        """Record ``model_id`` as resident (idempotent by id)."""

        self._resident[model_id] = footprint_gib

    def remove(self, model_id: str) -> None:
        """Drop ``model_id`` from the resident set (no-op if absent)."""

        self._resident.pop(model_id, None)


def host_ceiling_gib(
    host: str,
    *,
    is_local: bool,
    detected_ram_gib: float | None,
    reserve_gib: float,
    per_host_override: Mapping[str, float],
) -> float | None:
    """Resolve the ceiling (GiB) for ``host`` — the local/remote dispatch.

    - ``is_local`` + a ``detected_ram_gib`` → :func:`local_ceiling_gib`
      (detected minus reserve), UNLESS the host has an explicit override, which
      wins (an operator pinning a local ceiling overrides RAM detection).
    - remote (or local with RAM detection failed) →
      :func:`remote_ceiling_gib` (override or ``None`` skip).

    Pure: RAM detection is injected as ``detected_ram_gib`` (``None`` = could not
    detect → fall back to the override/skip path).
    """

    override = remote_ceiling_gib(host, per_host_override)
    if override is not None:
        return override
    if is_local and detected_ram_gib is not None:
        return local_ceiling_gib(detected_ram_gib, reserve_gib)
    return None


# --- side-effecting probes (the injectable real-machine seam) ----------------
#
# These are the ONLY functions here that touch the OS / disk. They are thin so
# the pure core above is what gets exhaustively tested; a caller injects fixed
# numbers in tests and wires these in at runtime.

#: A probe returning a host's total RAM in GiB, or ``None`` when unreadable
#: (remote host / unsupported platform). Injectable for tests.
HostRamProbe = Callable[[], float | None]

#: A probe returning a model's on-disk weight size in GiB, or ``None`` when the
#: file cannot be sized (unknown id / path). Injectable for tests.
ModelFileSizeProbe = Callable[[str], float | None]


def detect_local_ram_gib() -> float | None:
    """Best-effort detect total physical RAM (GiB) of the LOCAL machine.

    macOS: ``sysctl -n hw.memsize`` (bytes). Linux: ``os.sysconf`` pages times
    page size. Any failure (unsupported platform, missing sysctl, parse error)
    returns ``None`` — the caller then skips the local-ceiling path and relies
    on an override / the load try+catch, never crashing. Never raises.
    """

    # POSIX (Linux + most Unix): pages * page size.
    with contextlib.suppress(Exception):
        page_size = os.sysconf("SC_PAGE_SIZE")
        phys_pages = os.sysconf("SC_PHYS_PAGES")
        if isinstance(page_size, int) and isinstance(phys_pages, int) and phys_pages > 0:
            return (page_size * phys_pages) / BYTES_PER_GIB
    # macOS: hw.memsize via sysctl (SC_PHYS_PAGES is absent on Darwin).
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        value = out.stdout.strip()
        if value.isdigit():
            return int(value) / BYTES_PER_GIB
    return None


def file_size_gib(path: str) -> float | None:
    """Best-effort on-disk size (GiB) of the file (or directory tree) at ``path``.

    A single GGUF file → its byte size; an MLX model directory → the summed
    size of its files. Any failure (missing path, permission) returns ``None``
    so the caller can fall back to a coarse default footprint rather than crash.
    Never raises.
    """

    with contextlib.suppress(Exception):
        if os.path.isdir(path):
            total = 0
            for root, _dirs, files in os.walk(path):
                for name in files:
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(root, name))
            return total / BYTES_PER_GIB
        return os.path.getsize(path) / BYTES_PER_GIB
    return None


# --- composition helpers (wire a HostBudget from the persisted budget block) --


def build_host_budget(
    host: str,
    *,
    ceiling_gib: float | None,
    reserve_gib: float,
    per_host_override: Mapping[str, float],
    is_local: bool,
    ram_probe: HostRamProbe = detect_local_ram_gib,
) -> HostBudget:
    """Build a per-host :class:`HostBudget` from the persisted budget block.

    Ceiling precedence (Annexe D + J step 3), highest first:

    1. ``per_host_override[host]`` — an explicit operator ceiling (local OR
       remote) always wins.
    2. ``ceiling_gib`` — a pinned global ceiling (the budget block's
       ``ceiling_gib`` when not ``null``).
    3. local host → detected RAM (``ram_probe``) minus ``reserve_gib``.
    4. remote host with no override / RAM unreadable → ``None`` (skip the
       check; the load try+catch is the only OOM net).

    Pure but for the injected ``ram_probe`` (defaults to the real
    :func:`detect_local_ram_gib`); tests inject a fixed RAM. The companion
    model-footprint probe is built separately by
    :func:`build_model_footprint_probe` and wired alongside on the manager.
    """

    override = remote_ceiling_gib(host, per_host_override)
    if override is not None:
        return HostBudget(ceiling_gib=override)
    if ceiling_gib is not None:
        return HostBudget(ceiling_gib=ceiling_gib)
    detected = ram_probe() if is_local else None
    ceiling = host_ceiling_gib(
        host,
        is_local=is_local,
        detected_ram_gib=detected,
        reserve_gib=reserve_gib,
        per_host_override=per_host_override,
    )
    return HostBudget(ceiling_gib=ceiling)


def build_model_footprint_probe(
    *,
    resolve_path: Callable[[str], str | None] | None = None,
    file_size_probe: ModelFileSizeProbe = file_size_gib,
    default_footprint_gib: float = 5.0,
    gib_per_1k_tokens: float = KV_GIB_PER_1K_TOKENS,
    default_margin_gib: float = DEFAULT_KV_MARGIN_GIB,
) -> Callable[[str, int | None], float]:
    """Build the ``(model_id, context_length) -> footprint_gib`` probe the manager wires.

    Resolves the model's on-disk path (``resolve_path`` — e.g. the LM Studio SDK
    catalogue path; ``None`` when the path is unknown), sizes it
    (``file_size_probe``), and adds the KV-cache margin (:func:`footprint_gib`).
    When the path is unknown or the file cannot be sized, falls back to
    ``default_footprint_gib`` + the KV margin — an un-sized model still books a
    realistic footprint rather than appearing free. Injectable end-to-end so the
    manager's budget check is exhaustively unit-testable.
    """

    def _footprint(model_id: str, context_length: int | None) -> float:
        size: float | None = None
        if resolve_path is not None:
            path = resolve_path(model_id)
            if path:
                size = file_size_probe(path)
        weights = size if size is not None else default_footprint_gib
        return footprint_gib(
            weights,
            context_length,
            gib_per_1k_tokens=gib_per_1k_tokens,
            default_margin_gib=default_margin_gib,
        )

    return _footprint
