"""Build a Gmail search-syntax string from structured arguments.

Gmail accepts a single ``q`` parameter on ``users.messages.list`` using its
search operator language (``from:``, ``subject:``, ``after:``, etc.). The
goal of this module is to be the **one** place in the codebase that knows
how to escape and quote those operators — the sub-agent tool handler
exposes a Pydantic-validated structured-arg surface and delegates the
serialisation here so LLM hallucinations of raw Gmail syntax stay out of
reach.

Reference: https://support.google.com/mail/answer/7190?hl=en
"""

from __future__ import annotations

from datetime import date


class QueryBuilderError(ValueError):
    """Raised when :func:`build_query` is called with no usable filters."""


def _quote(value: str) -> str:
    """Quote ``value`` for Gmail operators that take a free-text argument.

    Gmail uses double quotes to group a multi-word phrase; embedded double
    quotes are not escapable inside that syntax, so we replace them with a
    single quote (the closest visually-equivalent character) before wrapping
    the result. Backslashes are passed through — Gmail treats them as
    literal characters, not escape sequences.
    """

    cleaned = value.replace('"', "'")
    return f'"{cleaned}"'


def _format_date(value: date) -> str:
    """Render a :class:`datetime.date` as Gmail's ``YYYY/MM/DD`` form.

    Gmail accepts both ``YYYY/MM/DD`` and ``YYYY-MM-DD``; we use slashes
    because the docs prefer that form. (Test fixtures may pass either form
    as a string via the public API; we standardise to slashes here.)
    """

    return value.strftime("%Y/%m/%d")


def _coerce_date(value: date | str, *, arg_name: str) -> str:
    """Normalise ``value`` into the Gmail ``YYYY/MM/DD`` form.

    Accepts both :class:`datetime.date` instances and ISO-style strings
    (``YYYY-MM-DD``). Anything else is rejected with a clear message so the
    caller — and ultimately the LLM via Pydantic validation — gets
    actionable feedback instead of a silent malformed query.
    """

    if isinstance(value, date):
        return _format_date(value)
    if isinstance(value, str):
        # Tolerate both "2025-05-28" and "2025/05/28" — the operator works
        # with either, but we standardise to slashes for grep-ability of
        # the produced query string. Also tolerate a trailing ISO time/zone
        # component ("2025-05-28T00:00:00Z", "2025-05-28 09:30") — local
        # models routinely append one; Gmail's ``after:`` is date-only so we
        # drop everything past the date.
        date_part = value.strip().split("T", 1)[0].split(" ", 1)[0]
        normalised = date_part.replace("-", "/")
        try:
            parts = normalised.split("/")
            if len(parts) != 3:
                raise ValueError(parts)
            year, month, day = (int(p) for p in parts)
            return _format_date(date(year, month, day))
        except (ValueError, TypeError) as exc:
            raise QueryBuilderError(
                f"{arg_name} must be a YYYY-MM-DD string or datetime.date: got {value!r}"
            ) from exc
    raise QueryBuilderError(
        f"{arg_name} must be a YYYY-MM-DD string or datetime.date: got {value!r}"
    )


def build_query(
    *,
    from_name: str | None = None,
    from_email: str | None = None,
    subject_contains: str | None = None,
    after: date | str | None = None,
    before: date | str | None = None,
    has_attachment: bool | None = None,
    label: str | None = None,
) -> str:
    """Compose a Gmail search string from structured arguments.

    Every argument is optional but at least one must be set — otherwise the
    resulting query would be empty and the search would return the entire
    inbox, which is never what the caller wants. Raises
    :class:`QueryBuilderError` in that case.

    - ``from_name`` / ``from_email`` both emit ``from:"…"`` clauses; when
      both are set, both are emitted (Gmail ANDs them implicitly).
    - ``subject_contains`` emits ``subject:"…"`` so multi-word values are
      treated as a phrase.
    - ``after`` / ``before`` accept either :class:`datetime.date` or a
      ``YYYY-MM-DD`` string.
    - ``has_attachment=True`` emits ``has:attachment``; ``False`` and
      ``None`` are both treated as "no filter" (Gmail has no first-class
      operator for "no attachment").
    - ``label`` emits ``label:<value>`` raw (Gmail labels never carry
      whitespace; we trust the caller / Pydantic to have validated the
      shape).
    """

    parts: list[str] = []

    if from_name is not None:
        if not from_name.strip():
            raise QueryBuilderError("from_name must be non-empty when provided")
        parts.append(f"from:{_quote(from_name)}")
    if from_email is not None:
        if not from_email.strip():
            raise QueryBuilderError("from_email must be non-empty when provided")
        parts.append(f"from:{_quote(from_email)}")
    if subject_contains is not None:
        if not subject_contains.strip():
            raise QueryBuilderError("subject_contains must be non-empty when provided")
        parts.append(f"subject:{_quote(subject_contains)}")
    if after is not None:
        parts.append(f"after:{_coerce_date(after, arg_name='after')}")
    if before is not None:
        parts.append(f"before:{_coerce_date(before, arg_name='before')}")
    if has_attachment:
        parts.append("has:attachment")
    if label is not None:
        if not label.strip():
            raise QueryBuilderError("label must be non-empty when provided")
        parts.append(f"label:{label}")

    if not parts:
        raise QueryBuilderError("build_query requires at least one filter argument; got all-None")

    return " ".join(parts)


__all__ = ["QueryBuilderError", "build_query"]
