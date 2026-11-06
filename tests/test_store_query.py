"""Tests for the dashboard query layer added to :class:`Store` in Phase 5.

Covers :meth:`Store.query_messages`, :meth:`Store.summary`, the entity listings,
and the message-detail helpers. Aggregate expectations are hand-computed against
the fixture in :mod:`tests.seed` (read the two together).
"""

from __future__ import annotations

import math

import pytest

from mailing_list_ai_check.store import (
    MULTI_NAME_ADDRESS_THRESHOLD,
    MessageFilters,
    Store,
    ai_count,
    ai_share,
)

from seed import seed


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "query.db"
    with Store(db) as s:
        seed(s)
        yield s


def _keys(rows):
    """Map result rows back to their fixture message ids (via message_id)."""
    return {row["message_id"] for row in rows}


# --- query_messages: filters --------------------------------------------------


def test_no_filter_returns_all(store):
    rows, total = store.query_messages(MessageFilters(per_page=200))
    assert total == 15
    assert len(rows) == 15


def test_filter_by_list(store):
    rows, total = store.query_messages(MessageFilters(list_name="announce", per_page=200))
    assert total == 7
    assert _keys(rows) == {f"<m{n}@test>" for n in (1, 2, 3, 4, 5, 6, 7)}


def test_filter_by_address(store):
    rows, total = store.query_messages(MessageFilters(address="bob@example.org", per_page=200))
    assert total == 3
    assert _keys(rows) == {"<m3@test>", "<m8@test>", "<m13@test>"}


def test_filter_by_address_is_case_insensitive(store):
    _, total = store.query_messages(MessageFilters(address="BOB@EXAMPLE.ORG"))
    assert total == 3


def test_filter_by_person(store):
    # P1 = Alice (a1 + a2): m1, m2, m11 (a1) and m7, m15 (a2).
    p1 = store.get_person(1)
    assert p1 is not None
    rows, total = store.query_messages(MessageFilters(person_id=p1.id, per_page=200))
    assert total == 5
    assert _keys(rows) == {"<m1@test>", "<m2@test>", "<m7@test>", "<m11@test>", "<m15@test>"}


def test_filter_by_label(store):
    rows, total = store.query_messages(MessageFilters(label="AI", per_page=200))
    assert total == 3
    assert _keys(rows) == {"<m1@test>", "<m8@test>", "<m14@test>"}


def test_filter_by_date_range(store):
    rows, total = store.query_messages(
        MessageFilters(date_from="2026-02-01", date_to="2026-02-28", per_page=200)
    )
    assert total == 5
    assert _keys(rows) == {f"<m{n}@test>" for n in (4, 5, 6, 9, 10)}


def test_filter_by_min_likelihood(store):
    rows, total = store.query_messages(MessageFilters(min_likelihood=0.5, per_page=200))
    assert total == 5
    assert _keys(rows) == {"<m1@test>", "<m3@test>", "<m8@test>", "<m11@test>", "<m14@test>"}


def test_filter_by_max_likelihood(store):
    _, total = store.query_messages(MessageFilters(max_likelihood=0.1, per_page=200))
    # fraction_ai <= 0.1: m2 (0.02), m4 (0.10), m9 (0.05)
    assert total == 3


def test_filter_has_score(store):
    _, scored = store.query_messages(MessageFilters(has_score=True, per_page=200))
    _, unscored = store.query_messages(MessageFilters(has_score=False, per_page=200))
    assert scored == 9
    assert unscored == 6


def test_filter_q_over_subject(store):
    rows, total = store.query_messages(MessageFilters(q="QUIC", per_page=200))
    assert total == 3
    assert _keys(rows) == {"<m13@test>", "<m14@test>", "<m15@test>"}


def test_filter_q_matches_extracted_text(store):
    # Only ok extractions have body text "Body of <subject>"; "Body of" appears
    # in every extracted text, so q="Body of" selects exactly the 10 ok rows.
    _, total = store.query_messages(MessageFilters(q="Body of", per_page=200))
    assert total == 10


def test_filters_combine_with_and(store):
    rows, total = store.query_messages(
        MessageFilters(list_name="announce", label="AI", per_page=200)
    )
    assert total == 1
    assert _keys(rows) == {"<m1@test>"}


# --- query_messages: pagination + sort ----------------------------------------


def test_pagination_pages(store):
    page1, total = store.query_messages(MessageFilters(page=1, per_page=10))
    page2, _ = store.query_messages(MessageFilters(page=2, per_page=10))
    page3, _ = store.query_messages(MessageFilters(page=3, per_page=10))
    assert total == 15
    assert len(page1) == 10
    assert len(page2) == 5
    assert page3 == []  # beyond the end


def test_per_page_is_clamped(store):
    rows, total = store.query_messages(MessageFilters(per_page=10_000))
    assert total == 15
    assert len(rows) == 15  # clamp does not drop rows below MAX_PER_PAGE


def test_sort_by_date_asc_and_desc(store):
    asc, _ = store.query_messages(MessageFilters(sort="date", order="asc", per_page=200))
    desc, _ = store.query_messages(MessageFilters(sort="date", order="desc", per_page=200))
    assert asc[0]["message_id"] == "<m1@test>"  # 2026-01-05, earliest
    assert desc[0]["message_id"] == "<m15@test>"  # 2026-03-28, latest


def test_sort_by_fraction_ai_desc(store):
    rows, _ = store.query_messages(MessageFilters(sort="fraction_ai", order="desc", per_page=200))
    assert rows[0]["message_id"] == "<m14@test>"  # 0.97, highest


# --- summary ------------------------------------------------------------------


def test_summary_totals(store):
    summary = store.summary(MessageFilters())
    assert summary["total"] == 15
    assert summary["extracted"] == 10
    assert summary["scored"] == 9
    assert summary["too_short"] == 1
    assert summary["avg_fraction_ai"] == pytest.approx(4.52 / 9)


def test_summary_label_distribution(store):
    dist = store.summary(MessageFilters())["label_distribution"]
    assert dist == {"AI": 3, "Human": 3, "Mixed": 3}


def test_summary_by_list(store):
    by_list = store.summary(MessageFilters())["by_list"]
    # Ordered by volume desc.
    assert [row["list"] for row in by_list] == ["announce", "last-call", "quic"]
    counts = {row["list"]: row["count"] for row in by_list}
    assert counts == {"announce": 7, "last-call": 5, "quic": 3}
    avgs = {row["list"]: row["avg_fraction_ai"] for row in by_list}
    assert avgs["quic"] == pytest.approx(0.97)  # only m14 scored on quic


def test_summary_by_list_label_counts(store):
    by_list = {row["list"]: row for row in store.summary(MessageFilters())["by_list"]}
    # announce: m1(AI) m2(Human) m3(AI-Asst) m4(Human) m7(Mixed).
    assert by_list["announce"]["label_counts"] == {
        "AI": 1,
        "Human": 2,
        "Mixed": 2,
    }
    # last-call: m8(AI) m9(Human) m11(AI-Asst).
    assert by_list["last-call"]["label_counts"] == {"AI": 1, "Human": 1, "Mixed": 1}
    # quic: only m14 scored (m13 ok-unscored, m15 no extraction).
    assert by_list["quic"]["label_counts"] == {"AI": 1}


def test_summary_by_list_too_short_count(store):
    by_list = {row["list"]: row for row in store.summary(MessageFilters())["by_list"]}
    # m5 is the only too_short extraction, and it is on announce. m6 (empty) and
    # m10 (failed) are gated by nothing and must not count.
    assert by_list["announce"]["too_short_count"] == 1
    assert by_list["last-call"]["too_short_count"] == 0
    assert by_list["quic"]["too_short_count"] == 0


def test_summary_by_list_too_short_count_respects_filters(store):
    by_list = store.summary(MessageFilters(list_name="last-call"))["by_list"]
    assert [row["too_short_count"] for row in by_list] == [0]


def test_summary_by_list_label_counts_empty_when_unscored(store):
    # has_score=False selects only unscored messages, so every list's scored
    # subset is empty and label_counts collapses to {}.
    by_list = store.summary(MessageFilters(has_score=False))["by_list"]
    assert by_list  # lists with unscored messages are still present
    assert all(row["label_counts"] == {} for row in by_list)


def test_summary_by_address_flagged_share(store):
    by_address = store.summary(MessageFilters())["by_address"]
    by_email = {row["email"]: row for row in by_address}
    # Addresses with zero scored messages (dave, eve) are excluded.
    assert set(by_email) == {
        "alice@example.org",
        "alice@work.example",
        "bob@example.org",
        "carol@example.org",
    }
    assert by_email["bob@example.org"]["scored_count"] == 2
    # Only the fully AI verdict (m8) is flagged; m3 is a Mixed verdict.
    assert by_email["bob@example.org"]["flagged_count"] == 1
    assert by_email["bob@example.org"]["flagged_share"] == pytest.approx(0.5)
    assert by_email["carol@example.org"]["flagged_share"] == pytest.approx(1 / 3)


def test_summary_by_month(store):
    by_month = store.summary(MessageFilters())["by_month"]
    months = {row["month"]: row for row in by_month}
    assert set(months) == {"2026-01", "2026-02", "2026-03"}
    assert months["2026-01"]["count"] == 5
    assert months["2026-01"]["flagged_count"] == 2  # m1, m8 (m3 is Mixed)
    assert months["2026-02"]["flagged_count"] == 0
    assert months["2026-03"]["flagged_count"] == 1  # m14 (m11 is Mixed)
    assert months["2026-02"]["avg_fraction_ai"] == pytest.approx((0.10 + 0.05) / 2)


def test_summary_respects_filters(store):
    summary = store.summary(MessageFilters(list_name="quic"))
    assert summary["total"] == 3
    assert summary["scored"] == 1
    assert summary["label_distribution"] == {"AI": 1}


# --- entity listings + detail helpers -----------------------------------------


def test_list_rows(store):
    rows = store.list_rows()
    counts = {row["name"]: row["message_count"] for row in rows}
    assert counts == {"announce": 7, "last-call": 5, "quic": 3}
    assert all("folder" in row for row in rows)


def test_list_rows_scored_count_and_label_counts(store):
    rows = {row["name"]: row for row in store.list_rows()}
    # announce: scored m1(AI) m2(Human) m3(AI-Asst) m4(Human) m7(Mixed) = 5.
    assert rows["announce"]["scored_count"] == 5
    assert rows["announce"]["label_counts"] == {
        "AI": 1,
        "Human": 2,
        "Mixed": 2,
    }
    # last-call: scored m8(AI) m9(Human) m11(AI-Asst) = 3.
    assert rows["last-call"]["scored_count"] == 3
    assert rows["last-call"]["label_counts"] == {"AI": 1, "Human": 1, "Mixed": 1}
    # quic: only m14 scored (m13 ok-unscored, m15 no extraction).
    assert rows["quic"]["scored_count"] == 1
    assert rows["quic"]["label_counts"] == {"AI": 1}


def test_list_rows_too_short_count(store):
    rows = {row["name"]: row for row in store.list_rows()}
    # announce holds m5, the only too_short extraction; m6 (empty) and m10
    # (failed) are other statuses and must not count.
    assert rows["announce"]["too_short_count"] == 1
    assert rows["last-call"]["too_short_count"] == 0
    assert rows["quic"]["too_short_count"] == 0


def test_list_rows_earliest_message_at(store):
    rows = {row["name"]: row for row in store.list_rows()}
    # Oldest message date per list: m1 (announce), m8 (last-call), m13 (quic).
    assert rows["announce"]["earliest_message_at"] == "2026-01-05T10:00:00"
    assert rows["last-call"]["earliest_message_at"] == "2026-01-08T10:00:00"
    assert rows["quic"]["earliest_message_at"] == "2026-01-25T10:00:00"


def test_list_rows_earliest_message_at_none_without_messages(store):
    store.upsert_list("empty", "Shared Folders/empty")
    row = next(r for r in store.list_rows() if r["name"] == "empty")
    assert row["message_count"] == 0
    assert row["earliest_message_at"] is None


# --- sender_rows --------------------------------------------------------------


def _by_name(rows):
    return {row["name"]: row for row in rows}


def test_sender_rows_person_grouping(store):
    rows, total = store.sender_rows(per_page=200)
    # 2 persons (Alice, Bob) + 3 unlinked addresses (Carol, Dave, Eve).
    assert total == 5
    senders = _by_name(rows)

    alice = senders["Alice Smith"]
    assert alice["type"] == "person"
    assert alice["emails"] == ["alice@example.org", "alice@work.example"]
    assert len(alice["address_ids"]) == 2
    assert alice["message_count"] == 5  # m1, m2, m11 (a1) + m7, m15 (a2)
    assert alice["label_counts"] == {"AI": 1, "Human": 1, "Mixed": 2}

    bob = senders["Bob Jones"]
    assert bob["type"] == "person"
    assert bob["emails"] == ["bob@example.org"]
    assert bob["message_count"] == 3  # m3, m8, m13
    assert bob["label_counts"] == {"AI": 1, "Mixed": 1}  # m13 unscored


def _add_sender_message(store, email, key, *, auto_generated=None):
    """Attach one message from ``email`` to the seeded ``announce`` list."""
    list_id = next(
        r["id"] for r in store.conn.execute("SELECT id FROM lists WHERE name='announce'")
    )
    address = store.upsert_address(email, email.split("@")[0])
    return store.upsert_message(
        message_id=f"<{key}@test>",
        list_id=list_id,
        address_id=address.id,
        subject=key,
        date="2026-04-01T10:00:00",
        in_reply_to=None,
        raw_body="body",
        uid=None,
        auto_generated=auto_generated,
    ).message


def _add_named_message(store, email, key, from_name):
    """Attach one message from ``email`` carrying its own ``From`` display name."""
    list_id = next(
        r["id"] for r in store.conn.execute("SELECT id FROM lists WHERE name='announce'")
    )
    address = store.upsert_address(email, "First Seen Name")
    store.upsert_message(
        message_id=f"<{key}@test>",
        list_id=list_id,
        address_id=address.id,
        subject=key,
        date="2026-04-01T10:00:00",
        in_reply_to=None,
        raw_body="body",
        uid=None,
        from_name=from_name,
    )
    return address


def _add_distinct_names(store, email, count, prefix="mn"):
    """Give ``email`` ``count`` messages, each under a different From name."""
    for i in range(count):
        _add_named_message(store, email, f"{prefix}{i}", f"Person {i}")


def test_sender_rows_names_a_many_named_address_by_its_address(store):
    # At the threshold no single name represents the address, so the address is
    # the label rather than whichever name was stored first.
    _add_distinct_names(store, "noreply@example.org", MULTI_NAME_ADDRESS_THRESHOLD)

    entry = _by_name(store.sender_rows(per_page=200)[0])["noreply@example.org"]
    assert entry["distinct_from_names"] == MULTI_NAME_ADDRESS_THRESHOLD
    assert entry["named_by_address"] is True
    assert entry["emails"] == ["noreply@example.org"]
    # The stored display name is untouched; only the served label changes.
    assert store.get_address(entry["address_id"]).display_name == "First Seen Name"


def test_sender_rows_keeps_the_display_name_below_the_threshold(store):
    # One short of the threshold: an individual varying their own name a little
    # keeps their name.
    _add_distinct_names(store, "two@example.org", MULTI_NAME_ADDRESS_THRESHOLD - 1, prefix="tn")

    senders = _by_name(store.sender_rows(per_page=200)[0])
    assert "two@example.org" not in senders
    entry = senders["First Seen Name"]
    assert entry["distinct_from_names"] == MULTI_NAME_ADDRESS_THRESHOLD - 1
    assert entry["named_by_address"] is False


def test_sender_rows_counts_distinct_names_not_messages(store):
    # Ten messages under two names stays below the threshold.
    for i in range(10):
        _add_named_message(store, "repeat@example.org", f"rp{i}", "Alpha" if i % 2 else "Beta")

    entry = _by_name(store.sender_rows(per_page=200)[0])["First Seen Name"]
    assert (entry["distinct_from_names"], entry["message_count"]) == (2, 10)


def test_sender_rows_name_rule_ignores_the_list_filter(store):
    # The names are spread across two lists; a list-scoped view must not re-name
    # the sender just because that list saw fewer of them.
    _add_distinct_names(store, "spread@example.org", MULTI_NAME_ADDRESS_THRESHOLD, prefix="sp")
    quic = next(r["id"] for r in store.conn.execute("SELECT id FROM lists WHERE name='quic'"))
    store.conn.execute("UPDATE messages SET list_id = ? WHERE message_id = '<sp2@test>'", (quic,))
    store.conn.commit()

    scoped = _by_name(store.sender_rows(per_page=200, list_name="quic")[0])
    assert "spread@example.org" in scoped


def test_sender_rows_does_not_rename_a_person(store):
    # A person's canonical_name is set by hand and always wins.
    address = None
    for i in range(MULTI_NAME_ADDRESS_THRESHOLD):
        address = _add_named_message(store, "shared@example.org", f"pn{i}", f"Person {i}")
    person = store.create_person("Chosen Name")
    store.assign_address_to_person(address.id, person.id)

    assert "Chosen Name" in _by_name(store.sender_rows(per_page=200)[0])


def test_sender_rows_hides_senders_whose_mail_is_all_excluded(store):
    # A robot sender: every message auto-generated, so it can never be scored.
    _add_sender_message(store, "noreply@github.com", "bot1", auto_generated="robot-sender")
    _add_sender_message(store, "noreply@github.com", "bot2", auto_generated="robot-sender")

    hidden = _by_name(store.sender_rows(per_page=200)[0])
    assert "noreply" not in hidden

    shown, total = store.sender_rows(per_page=200, include_excluded=True)
    bot = _by_name(shown)["noreply"]
    assert bot["excluded_from_scoring"] is True
    assert (bot["excluded_count"], bot["message_count"]) == (2, 2)
    # total counts the visible set, so it grows by exactly the shown sender.
    assert total == store.sender_rows(per_page=200)[1] + 1


def test_sender_rows_keeps_a_sender_with_any_scoreable_mail(store):
    # One auto-generated message, one not: the sender still contributes verdicts.
    _add_sender_message(store, "mixed@example.org", "mix1", auto_generated="precedence-bulk")
    _add_sender_message(store, "mixed@example.org", "mix2")

    entry = _by_name(store.sender_rows(per_page=200)[0])["mixed"]
    assert entry["excluded_from_scoring"] is False
    assert (entry["excluded_count"], entry["message_count"]) == (1, 2)


def test_sender_rows_does_not_exclude_a_sender_with_no_messages(store):
    store.upsert_address("silent@example.org", "Silent")
    entry = _by_name(store.sender_rows(per_page=200)[0])["Silent"]
    assert entry["excluded_from_scoring"] is False
    assert (entry["excluded_count"], entry["message_count"]) == (0, 0)


def test_sender_rows_excludes_a_person_whose_addresses_are_all_excluded(store):
    # The rule applies to a linked person, not only a bare address.
    msg = _add_sender_message(store, "bot@example.org", "pbot", auto_generated="robot-sender")
    address_id = store.conn.execute(
        "SELECT address_id FROM messages WHERE id = ?", (msg.id,)
    ).fetchone()["address_id"]
    person = store.create_person("Bot Person")
    store.assign_address_to_person(address_id, person.id)

    assert "Bot Person" not in _by_name(store.sender_rows(per_page=200)[0])
    shown = _by_name(store.sender_rows(per_page=200, include_excluded=True)[0])
    assert shown["Bot Person"]["excluded_from_scoring"] is True
    assert shown["Bot Person"]["person_id"] == person.id


def test_sender_rows_hides_a_shared_address_with_scoreable_mail(store):
    # The datatracker address carries many people's mail; it is hidden from the
    # pane even though its messages are scoreable, and shown with "Show all".
    _add_sender_message(store, "noreply@ietf.org", "dt1")
    _add_sender_message(store, "NoReply@ietf.org", "dt2")

    assert "noreply" not in _by_name(store.sender_rows(per_page=200)[0])

    shown, total = store.sender_rows(per_page=200, include_excluded=True)
    entry = _by_name(shown)["noreply"]
    assert entry["shared_address"] is True
    assert entry["excluded_from_scoring"] is False
    assert total == store.sender_rows(per_page=200)[1] + 1
    # Every other sender is reported as not shared.
    assert not any(e["shared_address"] for e in shown if e["name"] != "noreply")


def test_sender_rows_does_not_flag_a_person_holding_a_shared_address(store):
    msg = _add_sender_message(store, "noreply@ietf.org", "dtp")
    address_id = store.conn.execute(
        "SELECT address_id FROM messages WHERE id = ?", (msg.id,)
    ).fetchone()["address_id"]
    person = store.create_person("Datatracker")
    store.assign_address_to_person(address_id, person.id)

    entry = _by_name(store.sender_rows(per_page=200)[0])["Datatracker"]
    assert entry["shared_address"] is False


def test_sender_rows_unlinked_addresses(store):
    senders = _by_name(store.sender_rows(per_page=200)[0])

    carol = senders["Carol"]
    assert carol["type"] == "address"
    assert carol["emails"] == ["carol@example.org"]
    assert carol["message_count"] == 3  # m4, m9, m14
    assert carol["label_counts"] == {"Human": 2, "AI": 1}

    # Dave has messages but none scored (too_short + failed).
    dave = senders["Dave"]
    assert dave["message_count"] == 2
    assert dave["label_counts"] == {}
    # Attached addresses never appear as their own entry.
    assert "alice@example.org" not in senders
    assert "bob@example.org" not in senders


def test_sender_rows_too_short_count(store):
    senders = _by_name(store.sender_rows(per_page=200)[0])
    # Dave sent m5 (too_short) and m10 (failed): only the gated one counts.
    assert senders["Dave"]["too_short_count"] == 1
    assert senders["Eve"]["too_short_count"] == 0  # m6 is empty, m12 unextracted
    assert senders["Alice Smith"]["too_short_count"] == 0


def test_sender_rows_too_short_count_scoped_to_list(store):
    # m5 is on announce, so Dave's gated count follows the list scope.
    announce = _by_name(store.sender_rows(per_page=200, list_name="announce")[0])
    assert announce["Dave"]["too_short_count"] == 1
    last_call = _by_name(store.sender_rows(per_page=200, list_name="last-call")[0])
    assert last_call["Dave"]["too_short_count"] == 0


def test_sender_rows_q_matches_name_and_email(store):
    by_name = store.sender_rows(q="alice", per_page=200)[0]
    assert {r["name"] for r in by_name} == {"Alice Smith"}
    # Match on a specific email that is not part of the display name.
    by_email = store.sender_rows(q="work.example", per_page=200)[0]
    assert {r["name"] for r in by_email} == {"Alice Smith"}
    by_bob = store.sender_rows(q="bob@example.org", per_page=200)[0]
    assert {r["name"] for r in by_bob} == {"Bob Jones"}
    assert store.sender_rows(q="nobody", per_page=200) == ([], 0)


def test_sender_rows_sort_count_desc_default(store):
    rows, _ = store.sender_rows(sort="count", per_page=200)
    # counts: Alice 5, Bob 3, Carol 3, Dave 2, Eve 2. Ties break on name asc.
    assert [r["name"] for r in rows] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_sender_rows_sort_count_asc(store):
    rows, _ = store.sender_rows(sort="count", order="asc", per_page=200)
    assert [r["name"] for r in rows] == [
        "Dave",
        "Eve",
        "Bob Jones",
        "Carol",
        "Alice Smith",
    ]


def test_sender_rows_sort_ai_desc_default(store):
    rows, _ = store.sender_rows(sort="ai", per_page=200)
    # AI + Mixed counts: Alice 3, Bob 2, Carol 1, Dave 0 (only a gated
    # message), Eve 0 (nothing in the mix). Dave and Eve, level on message
    # count too, break on name asc.
    assert [r["name"] for r in rows] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_sender_rows_sort_ai_asc(store):
    rows, _ = store.sender_rows(sort="ai", order="asc", per_page=200)
    assert [r["name"] for r in rows] == [
        "Dave",
        "Eve",
        "Carol",
        "Bob Jones",
        "Alice Smith",
    ]


def test_ai_share_counts_gated_messages_and_empty_mixes():
    assert ai_share({"AI": 1, "Human": 1}) == 0.5
    # A gated message is part of the denominator, as the mix bar draws it.
    assert ai_share({"AI": 1, "Human": 1}, 2) == 0.25
    assert ai_share({"Human": 3}) == 0.0
    assert ai_share({}, 0) == 0.0
    assert ai_share(None) == 0.0
    assert ai_share({"AI": 2}) == 1.0


def test_ai_count_sums_ai_and_mixed_only():
    assert ai_count({"AI": 2, "Mixed": 3, "Human": 7}) == 5
    assert ai_count({"Human": 3}) == 0
    assert ai_count({}) == 0
    assert ai_count(None) == 0


def test_sender_rows_sort_name(store):
    asc, _ = store.sender_rows(sort="name", order="asc", per_page=200)
    assert [r["name"] for r in asc] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]
    desc, _ = store.sender_rows(sort="name", order="desc", per_page=200)
    assert [r["name"] for r in desc] == list(reversed([r["name"] for r in asc]))


def test_sender_rows_pagination(store):
    page1, total = store.sender_rows(sort="name", order="asc", page=1, per_page=2)
    page2, _ = store.sender_rows(sort="name", order="asc", page=2, per_page=2)
    page3, _ = store.sender_rows(sort="name", order="asc", page=3, per_page=2)
    assert total == 5
    assert [r["name"] for r in page1] == ["Alice Smith", "Bob Jones"]
    assert [r["name"] for r in page2] == ["Carol", "Dave"]
    assert [r["name"] for r in page3] == ["Eve"]


def test_sender_rows_list_name_scopes_counts(store):
    rows, total = store.sender_rows(per_page=200, list_name="announce")
    senders = _by_name(rows)
    # announce messages: m1,m2(a1) m3(a3) m4(a4) m5(a5) m6(a6) m7(a2). Everyone posted.
    assert total == 5
    assert set(senders) == {"Alice Smith", "Bob Jones", "Carol", "Dave", "Eve"}
    # Alice = a1(m1,m2) + a2(m7), scoped to announce only.
    assert senders["Alice Smith"]["message_count"] == 3
    assert senders["Alice Smith"]["label_counts"] == {"AI": 1, "Human": 1, "Mixed": 1}
    # Bob = a3, only m3 is on announce.
    assert senders["Bob Jones"]["message_count"] == 1
    assert senders["Bob Jones"]["label_counts"] == {"Mixed": 1}
    # Dave/Eve posted (m5 too_short, m6 empty) but nothing scored.
    assert senders["Dave"]["message_count"] == 1
    assert senders["Dave"]["label_counts"] == {}


def test_sender_rows_list_name_drops_non_posters(store):
    rows, total = store.sender_rows(per_page=200, list_name="quic")
    senders = _by_name(rows)
    # quic messages: m13(a3) m14(a4) m15(a2). Only Alice(a2), Bob(a3), Carol(a4).
    assert total == 3
    assert set(senders) == {"Alice Smith", "Bob Jones", "Carol"}
    # Dave/Eve never posted to quic -> dropped entirely.
    assert "Dave" not in senders
    assert "Eve" not in senders
    # Carol = a4, m14 scored AI.
    assert senders["Carol"]["message_count"] == 1
    assert senders["Carol"]["label_counts"] == {"AI": 1}
    # Alice's only quic message is m15 (no extraction); Bob's m13 is unscored.
    assert senders["Alice Smith"]["message_count"] == 1
    assert senders["Alice Smith"]["label_counts"] == {}
    assert senders["Bob Jones"]["message_count"] == 1
    assert senders["Bob Jones"]["label_counts"] == {}


def test_sender_rows_list_name_unknown_returns_empty(store):
    assert store.sender_rows(per_page=200, list_name="does-not-exist") == ([], 0)


def test_sender_rows_list_name_none_is_unchanged(store):
    # None (the default) keeps every sender, including zero-scored ones.
    scoped, scoped_total = store.sender_rows(per_page=200, list_name=None)
    baseline, baseline_total = store.sender_rows(per_page=200)
    assert scoped_total == baseline_total == 5
    assert [r["name"] for r in scoped] == [r["name"] for r in baseline]


# --- db_size_bytes ------------------------------------------------------------


def test_db_size_bytes_file_backed(store):
    assert store.db_size_bytes() > 0


def test_db_size_bytes_in_memory():
    with Store(":memory:") as s:
        seed(s)
        assert s.db_size_bytes() == 0
        assert s.summary(MessageFilters())["db_size_bytes"] == 0


def test_address_rows_and_q(store):
    all_rows = store.address_rows()
    assert len(all_rows) == 6
    filtered = store.address_rows("alice")
    assert {r["email"] for r in filtered} == {"alice@example.org", "alice@work.example"}


def test_person_rows_include_addresses(store):
    persons = {p["canonical_name"]: p for p in store.person_rows()}
    alice = persons["Alice Smith"]
    assert {a["email"] for a in alice["addresses"]} == {
        "alice@example.org",
        "alice@work.example",
    }
    assert alice["message_count"] == 5  # m1, m2, m11 (a1) + m7, m15 (a2)


def test_find_message_by_message_id(store):
    m1 = store.find_message_by_message_id("<m1@test>")
    assert m1 is not None
    assert store.find_message_by_message_id("<nope@test>") is None


def test_extraction_and_score_helpers(store):
    m1_id = store.find_message_by_message_id("<m1@test>").id
    extraction = store.extraction_for_message(m1_id)
    assert extraction is not None and extraction.status == "ok"
    score = store.score_for_extraction(extraction.id)
    assert score is not None and score.label == "AI"

    # m13 is ok but unscored.
    m13_id = store.find_message_by_message_id("<m13@test>").id
    ext13 = store.extraction_for_message(m13_id)
    assert ext13 is not None
    assert store.score_for_extraction(ext13.id) is None


def test_update_and_delete_person(store):
    pid = store.create_person("Temp").id
    a4 = store.address_rows("carol")[0]["id"]
    store.assign_address_to_person(a4, pid)
    renamed = store.update_person_name(pid, "Renamed")
    assert renamed is not None and renamed.canonical_name == "Renamed"

    assert store.delete_person(pid) is True
    assert store.get_person(pid) is None
    # Address is detached, not deleted.
    assert store.get_address(a4).person_id is None
    assert store.delete_person(pid) is False  # already gone


def test_pages_math_helper_matches(store):
    # Sanity: our ceil-based page count matches the store's totals.
    _, total = store.query_messages(MessageFilters(per_page=10))
    assert math.ceil(total / 10) == 2


# --- get_parent_body ----------------------------------------------------------


def test_get_parent_body(tmp_path):
    with Store(tmp_path / "pb.db") as s:
        mlist = s.upsert_list("tls", "Shared Folders/tls")
        s.upsert_message(
            message_id="<parent@x>",
            list_id=mlist.id,
            address_id=None,
            subject="Original",
            date="2026-07-20T00:00:00+00:00",
            in_reply_to=None,
            raw_body="The parent body text.",
            uid=1,
        )
        # Exact angle-bracket match.
        assert s.get_parent_body("<parent@x>") == "The parent body text."
        # Trailing whitespace / extra ids / a CFWS comment: the first token wins.
        assert s.get_parent_body("  <parent@x> <other@y> (see thread)  ") == "The parent body text."
        # Unknown id.
        assert s.get_parent_body("<nope@x>") is None


def test_get_parent_body_excludes_self_reply(tmp_path):
    # A message whose In-Reply-To names its own Message-ID must not resolve to its
    # own body: otherwise the parent-diff assist would delete the whole message.
    with Store(tmp_path / "pb.db") as s:
        mlist = s.upsert_list("tls", "Shared Folders/tls")
        s.upsert_message(
            message_id="<self@x>",
            list_id=mlist.id,
            address_id=None,
            subject="Original",
            date="2026-07-20T00:00:00+00:00",
            in_reply_to="<self@x>",
            raw_body="The message body text.",
            uid=1,
        )
        # Without the guard the self-reference resolves to the message's own body.
        assert s.get_parent_body("<self@x>") == "The message body text."
        # With the guard it is treated as having no parent.
        assert s.get_parent_body("<self@x>", exclude_message_id="<self@x>") is None
        # A genuine different parent is unaffected by the guard.
        assert (
            s.get_parent_body("<self@x>", exclude_message_id="<child@x>")
            == "The message body text."
        )
