"""Tests for :mod:`bob.connectors.gmail.query_builder`."""

from __future__ import annotations

from datetime import date

import pytest

from bob.connectors.gmail.query_builder import QueryBuilderError, build_query


def test_all_none_raises() -> None:
    with pytest.raises(QueryBuilderError, match="at least one"):
        build_query()


def test_single_from_name() -> None:
    assert build_query(from_name="Holyana Callejon") == 'from:"Holyana Callejon"'


def test_single_from_email() -> None:
    assert build_query(from_email="hcallejon@example.com") == 'from:"hcallejon@example.com"'


def test_subject_contains_quotes_phrase() -> None:
    assert build_query(subject_contains="Q3 forecast") == 'subject:"Q3 forecast"'


def test_after_accepts_date_object() -> None:
    assert build_query(after=date(2025, 5, 1)) == "after:2025/05/01"


def test_after_accepts_iso_string() -> None:
    assert build_query(after="2025-05-01") == "after:2025/05/01"


def test_after_accepts_slash_string() -> None:
    assert build_query(after="2025/05/01") == "after:2025/05/01"


def test_before_accepts_date_object() -> None:
    assert build_query(before=date(2025, 6, 1)) == "before:2025/06/01"


def test_after_rejects_garbage_string() -> None:
    with pytest.raises(QueryBuilderError, match="after"):
        build_query(after="not-a-date")


def test_after_rejects_non_date_type() -> None:
    with pytest.raises(QueryBuilderError, match="after"):
        build_query(after=12345)  # type: ignore[arg-type]


def test_has_attachment_true_emits_clause() -> None:
    assert build_query(has_attachment=True, from_name="A") == 'from:"A" has:attachment'


def test_has_attachment_false_omits_clause() -> None:
    # Gmail has no "no attachment" operator — False is treated as no filter.
    assert build_query(has_attachment=False, from_name="A") == 'from:"A"'


def test_label_emits_raw() -> None:
    assert build_query(label="STARRED") == "label:STARRED"


def test_full_combination_emits_all_clauses_in_order() -> None:
    out = build_query(
        from_name="Marie Lefèvre",
        from_email="marie@lunabee.com",
        subject_contains="Q3 forecast",
        after=date(2025, 5, 1),
        before=date(2025, 6, 1),
        has_attachment=True,
        label="IMPORTANT",
    )
    assert out == (
        'from:"Marie Lefèvre" '
        'from:"marie@lunabee.com" '
        'subject:"Q3 forecast" '
        "after:2025/05/01 "
        "before:2025/06/01 "
        "has:attachment "
        "label:IMPORTANT"
    )


def test_name_with_double_quotes_is_escaped() -> None:
    # Gmail's quoting syntax cannot escape internal double quotes, so we
    # downgrade them to single quotes — visually equivalent and avoids
    # the operator silently breaking.
    out = build_query(from_name='Bob "the" Builder')
    assert out == "from:\"Bob 'the' Builder\""


def test_subject_with_double_quotes_is_escaped() -> None:
    assert build_query(subject_contains='say "hi"') == "subject:\"say 'hi'\""


def test_from_name_empty_string_raises() -> None:
    with pytest.raises(QueryBuilderError, match="from_name"):
        build_query(from_name="   ")


def test_from_email_empty_string_raises() -> None:
    with pytest.raises(QueryBuilderError, match="from_email"):
        build_query(from_email="")


def test_subject_empty_string_raises() -> None:
    with pytest.raises(QueryBuilderError, match="subject_contains"):
        build_query(subject_contains="")


def test_label_empty_string_raises() -> None:
    with pytest.raises(QueryBuilderError, match="label"):
        build_query(label="   ")
