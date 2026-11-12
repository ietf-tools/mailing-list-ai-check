"""Tests for parent-arrival invalidation (``staleness.refresh_for_new_parents``).

A reply extracted while its thread parent was absent from the store holds text
derived without the parent-diff step. These tests cover the pipeline hook that
re-derives such replies once the parent is stored: the start of
``cli.run_extract`` and the guard at the start of a non-dry ``cli.run_score``.

No test here touches IMAP or Pangram; the scoring tests drive a stub client.
"""

from __future__ import annotations

import pytest

from email_reply_extractor import clean_for_scoring
from mailing_list_ai_check import cli, staleness
from mailing_list_ai_check.staleness import refresh_for_new_parents
from mailing_list_ai_check.store import SETTING_PARENT_CHECK_MESSAGE_ID, Store, sha256_text

#: A parent body and a reply that quotes it without ``>`` markers, so only the
#: parent-diff step can tell the quoted lines from the reply's own.
PARENT_BODY = (
    "Thanks for the detailed proposal about the new key rotation scheme.\n"
    "I think the possession side needs a clearer failure mode when the status\n"
    "cannot be established within the negotiated window between the two peers.\n"
)
REPLY_OWN = (
    "I have revised the entire draft to incorporate all of your suggestions.\n"
    "Every reviewer concern is now tracked in the updated issue list online.\n"
)
REPLY_BODY = REPLY_OWN + "\n" + PARENT_BODY
#: A reply that shares no line with the parent: parent-diff removes nothing.
UNRELATED_REPLY_BODY = (
    "The interim meeting is confirmed for the second week of next month.\n"
    "An agenda will be circulated to the list once the chairs have agreed it.\n"
)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "parents.db") as s:
        yield s


def add(
    store,
    *,
    message_id,
    body,
    in_reply_to=None,
    list_name="tls",
    auto_generated=None,
):
    """Insert one message (no extraction) and return its primary key."""
    mlist = store.upsert_list(list_name, f"Shared Folders/{list_name}")
    addr = store.upsert_address("author@example.org", "Author")
    return store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="Re: key rotation",
        date="2026-07-21T00:00:00+00:00",
        in_reply_to=in_reply_to,
        raw_body=body,
        uid=None,
        auto_generated=auto_generated,
    ).message.id


def score(store, message_pk):
    """Attach a verdict keyed on the extraction's current cleaned text."""
    extraction = store.extraction_for_message(message_pk)
    return store.insert_score(
        extraction_id=extraction.id,
        text_sha256=sha256_text(clean_for_scoring(extraction.extracted_text).text),
        fraction_ai=0.9,
        fraction_ai_assisted=0.0,
        fraction_human=0.1,
        label="AI",
        detector_version="v3",
        raw_response={"prediction_short": "AI"},
    )


def extraction_row(store, message_pk):
    return dict(
        store.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
    )


def checked_mark(store):
    return store.get_setting(SETTING_PARENT_CHECK_MESSAGE_ID)


class StubPangram:
    """A Pangram client that records the texts it was asked to score."""

    def __init__(self, model="pangram-4"):
        self.model = model
        self.texts = []

    def predict(self, text):
        self.texts.append(text)

        class Result:
            fraction_ai = 0.5
            fraction_ai_assisted = 0.0
            fraction_human = 0.5
            prediction_short = "Mixed"
            version = "stub"
            raw = {"prediction_short": "Mixed"}

        return Result()


# --- reply first, parent later --------------------------------------------------


def test_reply_is_rederived_and_its_score_deleted_when_the_parent_arrives(store, caplog):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    before = store.extraction_for_message(reply)
    assert "possession side" in before.extracted_text
    assert "+parent-diff" not in before.method
    score(store, reply)

    add(store, message_id="<parent@x>", body=PARENT_BODY)
    with caplog.at_level("INFO", logger="mailing_list_ai_check.extract"):
        status_counts, _ = cli.run_extract(store)

    # The returned tallies describe new extractions only: the parent's.
    assert sum(status_counts.values()) == 1
    after = store.extraction_for_message(reply)
    assert after.id == before.id
    assert "I have revised the entire draft" in after.extracted_text
    assert "possession side" not in after.extracted_text
    assert after.method.endswith("+parent-diff")
    assert store.score_for_extraction(after.id) is None
    # An unscored ok extraction is the scoring queue: the reply is back in it.
    assert after.id in {e.id for e in store.iter_extractions_needing_score(min_words=0)}
    assert (
        "parent arrival: new_parents=1 replies_rechecked=1 rewritten=1 unchanged=0 "
        "scores_invalidated=1 scores_carried=0 to_rescore=1"
    ) in caplog.text


def test_refresh_reports_the_messages_needing_a_rescore(store):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    score(store, reply)
    add(store, message_id="<parent@x>", body=PARENT_BODY)

    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert summary.reextract.rewritten == 1
    assert summary.reextract.scores_invalidated == 1
    assert summary.rescore_message_ids == [reply]


def test_in_reply_to_is_parsed_the_way_extraction_parses_it(store):
    # Surrounding whitespace and a trailing comment: the first <...> token is
    # the parent, exactly as Store.get_parent_body reads it.
    reply = add(
        store, message_id="<reply@x>", in_reply_to="  <parent@x> (earlier mail)", body=REPLY_BODY
    )
    cli.run_extract(store)
    add(store, message_id="<parent@x>", body=PARENT_BODY)

    cli.run_extract(store)

    assert store.extraction_for_message(reply).method.endswith("+parent-diff")


def test_an_auto_generated_parent_counts(store):
    # An auto-generated message never gets an extraction row, but a reply to it
    # still resolves it as a parent.
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<parent@x>", body=PARENT_BODY, auto_generated="auto-submitted")

    cli.run_extract(store)

    assert store.extraction_for_message(reply).method.endswith("+parent-diff")
    # The parent itself stays out of the extraction queue.
    assert store.conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1


# --- whitespace-only change -----------------------------------------------------


def test_a_whitespace_only_change_keeps_the_score(store, monkeypatch):
    # The library decides what parent-diff removes; this test fixes its output
    # so that the parent's arrival moves only blank lines, and checks the
    # pipeline keeps the verdict, re-keyed to the new cleaned text.
    spaced = "First paragraph about the retransmission section.\n\n\nSecond paragraph, decided."
    tight = spaced.replace("\n\n\n", "\n")

    class Result:
        status = "ok"

        def __init__(self, text, method):
            self.text = text
            self.method = method

    def fake_extract(raw_body, parent_body=None, *, html_body=None):
        if parent_body is None:
            return Result(spaced, "fake")
        return Result(tight, "fake+parent-diff")

    monkeypatch.setattr(cli, "extract_new_text", fake_extract)
    monkeypatch.setattr(staleness, "extract_new_text", fake_extract)

    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body="reply body")
    cli.run_extract(store)
    kept = score(store, reply)
    assert clean_for_scoring(spaced).text != clean_for_scoring(tight).text

    add(store, message_id="<parent@x>", body="parent body")
    summary = refresh_for_new_parents(store)

    assert summary.reextract.rewritten == 1
    assert summary.reextract.scores_invalidated == 0
    assert summary.reextract.scores_carried == 1
    assert summary.rescore_message_ids == []
    after = store.extraction_for_message(reply)
    assert after.extracted_text == tight
    carried = store.score_for_extraction(after.id)
    assert carried is not None
    assert carried.id == kept.id
    assert carried.label == "AI"
    assert carried.text_sha256 == sha256_text(clean_for_scoring(tight).text)


# --- no change ------------------------------------------------------------------


def test_an_unchanged_reply_is_left_untouched(store):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=UNRELATED_REPLY_BODY)
    cli.run_extract(store)
    kept = score(store, reply)
    row_before = extraction_row(store, reply)

    add(store, message_id="<parent@x>", body=PARENT_BODY)
    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert (summary.reextract.processed, summary.reextract.unchanged) == (1, 1)
    assert summary.reextract.rewritten == 0
    assert summary.rescore_message_ids == []
    assert extraction_row(store, reply) == row_before
    assert store.score_for_extraction(row_before["id"]).text_sha256 == kept.text_sha256


def test_a_message_that_is_not_a_reply_to_the_new_parent_is_not_rechecked(store):
    add(store, message_id="<other@x>", in_reply_to="<elsewhere@x>", body=REPLY_BODY)
    add(store, message_id="<plain@x>", body=UNRELATED_REPLY_BODY)
    cli.run_extract(store)

    add(store, message_id="<parent@x>", body=PARENT_BODY)
    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert summary.reextract.processed == 0


# --- idempotence and cost -------------------------------------------------------


def test_a_second_run_does_nothing_and_scans_nothing(store, monkeypatch):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<parent@x>", body=PARENT_BODY)
    cli.run_extract(store)
    row_before = extraction_row(store, reply)

    def must_not_scan(*args, **kwargs):
        raise AssertionError("no message was stored since the last check")

    monkeypatch.setattr(store, "message_ids_first_stored_after", must_not_scan)
    monkeypatch.setattr(store, "extracted_replies_to", must_not_scan)

    summary = refresh_for_new_parents(store)
    cli.run_extract(store)

    assert summary.new_parents == 0
    assert summary.reextract.processed == 0
    assert extraction_row(store, reply) == row_before


def test_new_messages_that_nothing_replies_to_cost_no_rederivation(store, monkeypatch):
    add(store, message_id="<a@x>", body=UNRELATED_REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<b@x>", body=PARENT_BODY)

    def must_not_reextract(*args, **kwargs):
        raise AssertionError("no extracted reply names the new message")

    monkeypatch.setattr(staleness, "reextract", must_not_reextract)

    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert checked_mark(store) == str(store.max_message_pk())


def test_the_mark_advances_only_after_the_rederivation_completes(store, monkeypatch):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    mark = checked_mark(store)
    add(store, message_id="<parent@x>", body=PARENT_BODY)

    def interrupted(*args, **kwargs):
        raise RuntimeError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(staleness, "reextract", interrupted)
        with pytest.raises(RuntimeError):
            refresh_for_new_parents(store)
    assert checked_mark(store) == mark

    # The next run repeats the check in full.
    summary = refresh_for_new_parents(store)
    assert summary.reextract.rewritten == 1
    assert store.extraction_for_message(reply).method.endswith("+parent-diff")
    assert checked_mark(store) == str(store.max_message_pk())


def test_a_never_checked_store_starts_from_the_last_extracted_message(store):
    # Two replies were extracted without their parents. One parent was stored
    # before the last extraction ran, one after; the store has no mark (as when
    # an existing database is first opened by this release).
    early = add(store, message_id="<early@x>", in_reply_to="<p1@x>", body=REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<p1@x>", body=PARENT_BODY, auto_generated="auto-submitted")
    late = add(store, message_id="<late@x>", in_reply_to="<p2@x>", body=REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<p2@x>", body=PARENT_BODY)
    store.conn.execute("DELETE FROM app_settings WHERE key = ?", (SETTING_PARENT_CHECK_MESSAGE_ID,))
    store.conn.execute(
        "UPDATE extractions SET extracted_text = ?, method = 'stale' WHERE message_id = ?",
        (REPLY_BODY, early),
    )
    store.conn.commit()

    summary = refresh_for_new_parents(store)

    # Only the parent stored after the last extracted message is treated as new.
    assert summary.new_parents == 1
    assert store.extraction_for_message(late).method.endswith("+parent-diff")
    assert store.extraction_for_message(early).method == "stale"


def test_a_mark_above_the_newest_message_is_pulled_back(store):
    add(store, message_id="<a@x>", body=UNRELATED_REPLY_BODY)
    cli.run_extract(store)
    store.set_setting(SETTING_PARENT_CHECK_MESSAGE_ID, "999")

    refresh_for_new_parents(store)

    assert checked_mark(store) == str(store.max_message_pk())


# --- parent present on several lists --------------------------------------------


def test_a_second_copy_of_an_already_stored_parent_is_not_a_new_parent(store):
    # The parent was on one list when the reply was extracted, so the reply was
    # derived with it. The same message arriving on another list changes nothing:
    # the lowest-id copy is the one extraction reads.
    add(store, message_id="<parent@x>", body=PARENT_BODY, list_name="tls")
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    row_before = extraction_row(store, reply)
    assert row_before["method"].endswith("+parent-diff")

    add(store, message_id="<parent@x>", body="A different body on this list.\n", list_name="quic")
    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 0
    assert summary.reextract.processed == 0
    assert extraction_row(store, reply) == row_before


def test_a_parent_arriving_on_several_lists_rechecks_each_reply_once(store):
    on_tls = add(store, message_id="<r1@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    on_quic = add(
        store, message_id="<r1@x>", in_reply_to="<parent@x>", body=REPLY_BODY, list_name="quic"
    )
    cli.run_extract(store)

    add(store, message_id="<parent@x>", body=PARENT_BODY, list_name="tls")
    add(store, message_id="<parent@x>", body=PARENT_BODY, list_name="quic")
    add(store, message_id="<parent@x>", body=PARENT_BODY, list_name="httpbis")
    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert summary.reextract.processed == 2
    assert summary.reextract.rewritten == 2
    for pk in (on_tls, on_quic):
        assert store.extraction_for_message(pk).method.endswith("+parent-diff")


# --- self-reply -----------------------------------------------------------------


def test_a_self_reply_is_never_rechecked(store):
    # A message whose In-Reply-To names its own Message-ID has no parent. With
    # the mark reset, the message is itself "newly stored" and extracted, and it
    # must still not be selected as a reply to itself.
    own = add(store, message_id="<self@x>", in_reply_to="<self@x>", body=REPLY_BODY)
    cli.run_extract(store)
    kept = score(store, own)
    row_before = extraction_row(store, own)
    store.set_setting(SETTING_PARENT_CHECK_MESSAGE_ID, "0")

    summary = refresh_for_new_parents(store)

    assert summary.new_parents == 1
    assert summary.reextract.processed == 0
    assert extraction_row(store, own) == row_before
    assert store.score_for_extraction(row_before["id"]).id == kept.id


def test_a_self_reply_copy_on_another_list_is_not_wiped(store):
    own = add(store, message_id="<self@x>", in_reply_to="<self@x>", body=REPLY_BODY)
    cli.run_extract(store)
    row_before = extraction_row(store, own)

    add(store, message_id="<self@x>", in_reply_to="<self@x>", body=REPLY_BODY, list_name="quic")
    cli.run_extract(store)

    assert extraction_row(store, own) == row_before
    assert row_before["status"] == "ok"


# --- the score stage ------------------------------------------------------------


def test_run_score_rederives_before_scoring_when_extract_was_skipped(store):
    # pull -> score with no extract in between: the reply's stored text is stale
    # and unscored, and must not be sent as it stands.
    padding = " ".join(["consensus"] * 60) + "\n"
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=padding + REPLY_BODY)
    cli.run_extract(store)
    add(store, message_id="<parent@x>", body=PARENT_BODY)

    client = StubPangram()
    summary = cli.run_score(store, client, limit=10)

    assert summary.scored == 1
    assert len(client.texts) == 1
    assert "possession side" not in client.texts[0]
    assert "I have revised the entire draft" in client.texts[0]
    extraction = store.extraction_for_message(reply)
    assert extraction.method.endswith("+parent-diff")
    assert store.score_for_extraction(extraction.id) is not None


def test_a_rescored_reply_reaches_the_next_scoring_run(store):
    padding = " ".join(["consensus"] * 60) + "\n"
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=padding + REPLY_BODY)
    cli.run_extract(store)
    first = StubPangram()
    cli.run_score(store, first, limit=10)
    assert "possession side" in first.texts[0]

    add(store, message_id="<parent@x>", body=PARENT_BODY)
    cli.run_extract(store)
    assert store.score_for_extraction(store.extraction_for_message(reply).id) is None

    second = StubPangram()
    cli.run_score(store, second, limit=10)

    rescored = [text for text in second.texts if "I have revised the entire draft" in text]
    assert len(rescored) == 1
    assert "possession side" not in rescored[0]
    assert store.score_for_extraction(store.extraction_for_message(reply).id) is not None


def test_a_dry_run_score_writes_nothing(store):
    reply = add(store, message_id="<reply@x>", in_reply_to="<parent@x>", body=REPLY_BODY)
    cli.run_extract(store)
    mark = checked_mark(store)
    row_before = extraction_row(store, reply)
    add(store, message_id="<parent@x>", body=PARENT_BODY)

    cli.run_score(store, None, limit=10, dry_run=True)

    assert extraction_row(store, reply) == row_before
    assert checked_mark(store) == mark
