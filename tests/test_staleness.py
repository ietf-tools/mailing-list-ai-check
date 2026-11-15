"""Tests for stale-extraction detection and repair.

Three layers, in order: the ``extractions.extraction_version`` column and the
store methods that write it (migration 011), the
:mod:`mailing_list_ai_check.staleness` operations over it (check / diff /
reextract), and the four ``/api/staleness*`` endpoints the dashboard drives.

No test here touches IMAP or Pangram: the diff and re-extraction paths are local
work by construction, and the one scoring test drives a stub client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailing_list_ai_check import __version__
from mailing_list_ai_check.cli import ScoreSummary, run_extract, run_score
from mailing_list_ai_check.config import Config
from email_reply_extractor import clean_for_scoring
from email_reply_extractor.extraction import EXTRACTION_VERSION
from mailing_list_ai_check.staleness import check, diff, reextract
from mailing_list_ai_check.store import Store, sha256_text
from mailing_list_ai_check.webapp import create_app

from seed import seed

#: An extraction generation older than the running one, and one newer than it
#: (what a store written by a later build carries — never stale).
OLD_GENERATION = EXTRACTION_VERSION - 1
NEWER_GENERATION = EXTRACTION_VERSION + 1

#: An app version unrelated to the running one, for the rows whose provenance
#: stamp a test pins. The app version no longer bears on staleness at all.
OLD_VERSION = "0.9.0"

#: A body long enough to read like prose, with no quoting or furniture, so that
#: extraction returns it unchanged and cleaning leaves it alone.
BODY = (
    "The working group discussed the transport draft at some length today and "
    "reached rough consensus on the retransmission section."
)


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "staleness.db"
    with Store(db) as s:
        yield s


def add_message(store, *, message_id="<m1@test>", body=BODY, subject="Transport draft"):
    """Insert one message (no extraction) and return it."""
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("alice@example.org", "Alice Smith")
    return store.upsert_message(
        message_id=message_id,
        list_id=lst.id,
        address_id=addr.id,
        subject=subject,
        date="2026-07-01T10:00:00",
        in_reply_to=None,
        raw_body=body,
        uid=1,
    ).message


def extracted(store, **kw):
    """Insert a message and run the real extractor over it; return the message."""
    message = add_message(store, **kw)
    run_extract(store)
    return message


def score(store, extraction_id, text):
    """Attach a verdict to an extraction, keyed on the text that was scored."""
    return store.insert_score(
        extraction_id=extraction_id,
        text_sha256=sha256_text(text),
        fraction_ai=0.9,
        fraction_ai_assisted=0.0,
        fraction_human=0.1,
        label="AI",
        detector_version="v3",
        raw_response={"prediction_short": "AI"},
    )


# --- migrations 008/011 and the store methods ---------------------------------


def test_migration_adds_the_extraction_version_columns(store):
    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(extractions)").fetchall()}
    assert "pipeline_version" in cols
    assert "extraction_version" in cols


def test_migration_backfills_extraction_version_from_message(tmp_path):
    db = tmp_path / "backfill.db"
    with Store(db) as s:
        message = add_message(s)
        s.insert_extraction(
            message_id=message.id,
            extracted_text=BODY,
            method="m",
            status="ok",
            pipeline_version="1.1.0",
        )
        # Rewind to pre-008 with the column gone, leaving only the message
        # stamp. The timing columns (009/010), extraction_version (011), the
        # app_settings table (012) and auto_generated (014) are dropped too so
        # they re-apply.
        s.conn.execute("DELETE FROM schema_version WHERE version >= 8")
        s.conn.execute("ALTER TABLE extractions DROP COLUMN pipeline_version")
        s.conn.execute("ALTER TABLE extractions DROP COLUMN extraction_version")
        s.conn.execute("DROP INDEX idx_messages_timing")
        s.conn.execute("ALTER TABLE messages DROP COLUMN timing")
        s.conn.execute("DROP INDEX idx_messages_timing_cpm")
        s.conn.execute("ALTER TABLE messages DROP COLUMN timing_cpm")
        s.conn.execute("DROP TABLE app_settings")
        s.conn.execute("DROP INDEX IF EXISTS idx_messages_address_list_autogen")
        s.conn.execute("DROP INDEX IF EXISTS idx_messages_address_from_name")
        s.conn.execute("ALTER TABLE messages DROP COLUMN auto_generated")
        s.conn.execute("ALTER TABLE messages DROP COLUMN from_name")
        s.conn.execute("ALTER TABLE messages DROP COLUMN raw_headers")
        s.conn.execute("ALTER TABLE extractions DROP COLUMN part_starts")
        s.conn.execute("ALTER TABLE scores DROP COLUMN part_check_status")
        s.conn.execute("ALTER TABLE scores DROP COLUMN part_check")
        s.conn.commit()
    with Store(db) as s:
        after = s.extraction_for_message(message.id)
    # 008 recovers the app version from the message, and 011 maps it onto the
    # generation that app version derived text with.
    assert after.pipeline_version == "1.1.0"
    assert after.extraction_version == 1


def test_insert_extraction_stamps_the_running_versions(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id, extracted_text=BODY, method="m", status="ok"
    )
    assert extraction.pipeline_version == __version__
    assert extraction.extraction_version == EXTRACTION_VERSION


def test_extraction_version_counts_orders_oldest_generation_first(store):
    for i, generation in enumerate((EXTRACTION_VERSION, OLD_GENERATION, OLD_GENERATION)):
        message = add_message(store, message_id=f"<v{i}@test>")
        extraction = store.insert_extraction(
            message_id=message.id,
            extracted_text=f"text {i}",
            method="m",
            status="ok",
            extraction_version=generation,
        )
        # A NULL stamp only reaches the column through the migration backfill
        # (insert_extraction's None means "use the running generation"), so write
        # it directly to get the unrecorded case into the tally.
        if i == 1:
            store.conn.execute(
                "UPDATE extractions SET extraction_version = NULL WHERE id = ?", (extraction.id,)
            )
            store.conn.commit()
    assert store.extraction_version_counts() == [
        (None, 1),
        (OLD_GENERATION, 1),
        (EXTRACTION_VERSION, 1),
    ]


def test_set_extraction_version_writes_both_stamps_and_leaves_the_text_alone(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id,
        extracted_text=BODY,
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
        extraction_version=OLD_GENERATION,
    )
    store.set_extraction_version(extraction.id)
    after = store.get_extraction(extraction.id)
    assert after.pipeline_version == __version__
    assert after.extraction_version == EXTRACTION_VERSION
    assert after.extracted_text == BODY


def test_set_extraction_version_honors_explicit_stamps(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id, extracted_text=BODY, method="m", status="ok"
    )
    store.set_extraction_version(extraction.id, OLD_GENERATION, OLD_VERSION)
    after = store.get_extraction(extraction.id)
    assert (after.extraction_version, after.pipeline_version) == (OLD_GENERATION, OLD_VERSION)


def test_replace_extraction_rewrites_the_row_and_restamps_the_message(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id,
        extracted_text="old text",
        method="old",
        status="ok",
        pipeline_version=OLD_VERSION,
    )
    store.conn.execute(
        "UPDATE messages SET pipeline_version = ? WHERE id = ?", (OLD_VERSION, message.id)
    )
    store.conn.commit()

    after = store.replace_extraction(
        extraction.id, extracted_text="new text here", method="new", status="ok"
    )
    assert after.id == extraction.id  # same row, so any score survives
    assert after.extracted_text == "new text here"
    assert after.method == "new"
    assert after.char_count == len("new text here")
    assert after.pipeline_version == __version__
    assert after.extraction_version == EXTRACTION_VERSION
    assert after.created_at == extraction.created_at  # first-extraction time is kept
    assert store.get_message(message.id).pipeline_version == __version__


def test_replace_extraction_unknown_id_returns_none(store):
    assert store.replace_extraction(999, extracted_text="t", method="m", status="ok") is None


def test_replace_extraction_rejects_an_invalid_status(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id, extracted_text=BODY, method="m", status="ok"
    )
    with pytest.raises(ValueError):
        store.replace_extraction(extraction.id, extracted_text="t", method="m", status="nonsense")


def test_delete_score_for_extraction(store):
    message = add_message(store)
    extraction = store.insert_extraction(
        message_id=message.id, extracted_text=BODY, method="m", status="ok"
    )
    score(store, extraction.id, BODY)
    assert store.delete_score_for_extraction(extraction.id) is True
    assert store.score_for_extraction(extraction.id) is None
    assert store.delete_score_for_extraction(extraction.id) is False


# --- check() ------------------------------------------------------------------


def test_rekey_score_for_extraction(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    score(store, extraction.id, "old cleaned text")

    assert store.rekey_score_for_extraction(extraction.id, sha256_text("new cleaned text"))
    after = store.score_for_extraction(extraction.id)
    assert after.text_sha256 == sha256_text("new cleaned text")
    assert after.label == "AI"

    assert not store.rekey_score_for_extraction(4242, sha256_text("x"))


def test_check_on_an_empty_store_is_not_stale(store):
    report = check(store)
    assert report.stale is False
    assert (report.total, report.stale_count, report.current_count) == (0, 0, 0)
    assert report.app_version == __version__
    assert report.extraction_version == EXTRACTION_VERSION


def test_check_is_not_stale_after_a_normal_extraction_run(store):
    extracted(store)
    report = check(store)
    assert report.stale is False
    assert report.stale_count == 0
    assert report.current_count == 1


def test_check_reports_an_older_generation_as_stale(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.set_extraction_version(extraction.id, OLD_GENERATION)

    report = check(store)
    assert report.stale is True
    assert report.stale_count == 1
    assert report.total == 1
    assert [(v.extraction_version, v.count, v.stale) for v in report.versions] == [
        (OLD_GENERATION, 1, True)
    ]


def test_check_treats_a_missing_generation_as_stale(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.conn.execute(
        "UPDATE extractions SET extraction_version = NULL WHERE id = ?", (extraction.id,)
    )
    store.conn.commit()
    assert check(store).stale is True


def test_check_ignores_the_app_version(store):
    """Only the generation stamp counts: an old app version is not staleness."""
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.set_extraction_version(extraction.id, EXTRACTION_VERSION, OLD_VERSION)
    report = check(store)
    assert report.stale is False
    assert report.current_count == 1


def test_check_does_not_offer_to_downgrade_a_newer_generation(store):
    """A store written by a later build reads as current, never stale.

    The comparison is ``<``, not ``!=``: text derived by a newer routine than
    this build has is better than anything re-extraction here could produce, so
    the check must not offer to overwrite it.
    """
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.set_extraction_version(extraction.id, NEWER_GENERATION)
    report = check(store)
    assert report.stale is False
    assert report.stale_count == 0
    assert report.current_count == 1
    assert [(v.extraction_version, v.stale) for v in report.versions] == [(NEWER_GENERATION, False)]


# --- diff() -------------------------------------------------------------------


def test_diff_stamps_unchanged_extractions_and_reports_nothing(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.set_extraction_version(extraction.id, OLD_GENERATION)

    report = diff(store)
    assert report.differing == []
    assert (report.checked, report.unchanged, report.stamped) == (1, 1, 1)
    # The stamp is what stops a re-derivation that found nothing from prompting
    # again on the next start-up.
    after = store.extraction_for_message(message.id)
    assert (after.extraction_version, after.pipeline_version) == (EXTRACTION_VERSION, __version__)
    assert check(store).stale is False


def test_diff_stamps_nothing_when_the_generation_is_already_current(store):
    extracted(store)
    report = diff(store)
    assert (report.checked, report.unchanged, report.stamped) == (1, 1, 0)


def test_diff_reports_a_changed_extraction_with_its_metadata(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.replace_extraction(
        extraction.id,
        extracted_text="something else entirely",
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
        extraction_version=OLD_GENERATION,
    )
    score(store, extraction.id, "something else entirely")

    report = diff(store)
    assert report.checked == 1
    assert report.unchanged == 0
    assert len(report.differing) == 1
    row = report.differing[0]
    assert row.message_id == message.id
    assert row.extraction_id == extraction.id
    assert row.list_name == "announce"
    assert row.subject == "Transport draft"
    assert row.from_address == "alice@example.org"
    assert row.from_display_name == "Alice Smith"
    assert row.pipeline_version == OLD_VERSION
    assert row.extraction_version == OLD_GENERATION
    assert row.old_chars == len("something else entirely")
    assert row.new_chars == len(BODY)
    assert row.old_status == "ok"
    assert row.new_status == "ok"
    assert row.text_changed is True
    assert row.scored_text_changed is True
    assert row.scored is True


def test_diff_rewrites_no_text_and_touches_no_score(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.replace_extraction(extraction.id, extracted_text="stale text", method="m", status="ok")
    score(store, extraction.id, "stale text")

    diff(store)
    assert store.extraction_for_message(message.id).extracted_text == "stale text"
    assert store.score_for_extraction(extraction.id) is not None


def test_diff_ignores_messages_without_an_extraction(store):
    add_message(store, message_id="<none@test>")
    report = diff(store)
    assert report.checked == 0
    assert report.differing == []


# --- reextract() --------------------------------------------------------------


def test_reextract_rewrites_a_changed_extraction_and_invalidates_its_score(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.replace_extraction(
        extraction.id,
        extracted_text="stale text",
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
    )
    score(store, extraction.id, "stale text")

    summary = reextract(store, [message.id])
    assert (summary.processed, summary.rewritten, summary.unchanged) == (1, 1, 0)
    assert summary.scores_invalidated == 1
    assert summary.rescore_message_ids == [message.id]

    after = store.extraction_for_message(message.id)
    assert after.extracted_text == BODY
    assert after.pipeline_version == __version__
    assert after.extraction_version == EXTRACTION_VERSION
    assert store.score_for_extraction(extraction.id) is None


def test_reextract_keeps_the_score_when_only_the_extracted_text_changed(store):
    # The stored text carries a greeting the current routine no longer keeps.
    # Cleaning strips greetings, so the *scored* text is identical and the stored
    # verdict is still a verdict on the text that would be sent.
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.replace_extraction(
        extraction.id,
        extracted_text=f"Hi all,\n\n{BODY}",
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
    )
    score(store, extraction.id, BODY)

    summary = reextract(store, [message.id])
    assert summary.rewritten == 1
    assert summary.scores_invalidated == 0
    assert summary.rescore_message_ids == []
    assert store.extraction_for_message(message.id).extracted_text == BODY
    assert store.score_for_extraction(extraction.id) is not None


def test_reextract_carries_the_score_when_the_cleaned_text_moved_only_in_whitespace(store):
    # The stored text differs from a fresh derivation only in blank-line
    # placement (the usual effect of a whitespace-heavy generation change).
    # The verdict is a verdict on the same substance, so it is kept — re-keyed
    # to the new cleaned text's hash so the score cache serves the new bytes.
    body = (
        "First paragraph about the retransmission section.\n\nSecond paragraph with the decision."
    )
    message = extracted(store, body=body)
    extraction = store.extraction_for_message(message.id)
    stored_text = body.replace("\n\n", "\n")
    store.replace_extraction(
        extraction.id,
        extracted_text=stored_text,
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
    )
    score(store, extraction.id, clean_for_scoring(stored_text).text)

    summary = reextract(store, [message.id])
    assert summary.rewritten == 1
    assert summary.scores_invalidated == 0
    assert summary.scores_carried == 1
    assert summary.rescore_message_ids == []

    after = store.extraction_for_message(message.id)
    assert after.extracted_text == body
    kept = store.score_for_extraction(extraction.id)
    assert kept is not None
    assert kept.text_sha256 == sha256_text(clean_for_scoring(body).text)


def test_reextract_only_stamps_an_unchanged_extraction(store):
    message = extracted(store)
    extraction = store.extraction_for_message(message.id)
    store.set_extraction_version(extraction.id, OLD_GENERATION)

    summary = reextract(store, [message.id])
    assert (summary.processed, summary.rewritten, summary.unchanged) == (1, 0, 1)
    after = store.extraction_for_message(message.id)
    assert (after.extraction_version, after.pipeline_version) == (EXTRACTION_VERSION, __version__)


def test_reextract_reports_a_no_longer_ok_extraction(store):
    # An empty body re-derives to an 'empty' extraction, which is never scored.
    message = add_message(store, message_id="<blank@test>", body="")
    extraction = store.insert_extraction(
        message_id=message.id,
        extracted_text="text that is no longer there",
        method="m",
        status="ok",
        pipeline_version=OLD_VERSION,
    )
    score(store, extraction.id, "text that is no longer there")

    summary = reextract(store, [message.id])
    assert (summary.rewritten, summary.not_ok, summary.scores_invalidated) == (1, 1, 1)
    assert summary.rescore_message_ids == []
    assert store.extraction_for_message(message.id).status == "empty"


def test_reextract_skips_ids_without_an_extraction(store):
    message = add_message(store, message_id="<none@test>")
    summary = reextract(store, [message.id, 4242])
    assert summary.processed == 0


# --- run_score's message filter -----------------------------------------------


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


def test_run_score_message_ids_restricts_the_queue(store):
    long_body = " ".join(["consensus"] * 80)
    wanted = extracted(store, message_id="<a@test>", body=long_body)
    other = add_message(store, message_id="<b@test>", body=long_body)
    run_extract(store)

    client = StubPangram()
    summary = run_score(store, client, limit=10, message_ids={wanted.id})

    assert summary.scored == 1
    assert len(client.texts) == 1
    assert store.score_for_extraction(store.extraction_for_message(other.id).id) is None


# --- the API endpoints --------------------------------------------------------


def _config(db_path: Path, *, pangram_key: str = "test-key") -> Config:
    return Config(
        imap_host="imap.example.org",
        imap_port=993,
        imap_username="anonymous",
        imap_password="anonymous@example.com",
        pangram_api_key=pangram_key,
        database_path=str(db_path),
        log_level="INFO",
        flask_host="127.0.0.1",
        flask_port=8050,
    )


@pytest.fixture
def seeded_db(tmp_path):
    """A seeded database whose stored extractions do not match their bodies.

    :func:`seed` writes hand-written extracted text unrelated to each message's
    ``raw_body``, so every seeded extraction re-derives differently — exactly the
    shape the affected-messages table is for.
    """
    path = tmp_path / "api.db"
    with Store(path) as store:
        seed(store)
        for message_pk in store.extracted_message_ids():
            extraction = store.extraction_for_message(message_pk)
            store.set_extraction_version(extraction.id, OLD_GENERATION, OLD_VERSION)
    return path


@pytest.fixture
def client(seeded_db):
    app = create_app(_config(seeded_db), frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_staleness_endpoint_reports_the_generation_breakdown(client):
    data = client.get("/api/staleness").get_json()
    assert data["stale"] is True
    assert data["app_version"] == __version__
    assert data["extraction_version"] == EXTRACTION_VERSION
    assert data["stale_count"] == data["total"] > 0
    assert data["current_count"] == 0
    assert data["versions"] == [
        {"extraction_version": OLD_GENERATION, "count": data["total"], "stale": True}
    ]


def test_staleness_check_lists_the_affected_messages(client):
    data = client.post("/api/staleness/check").get_json()
    assert data["checked"] == 13  # the seeded messages that have an extraction
    assert data["differing"] == len(data["messages"]) == 13
    row = next(m for m in data["messages"] if m["subject"] == "Intro to draft")
    assert row["list"] == "announce"
    assert row["from"]["address"] == "alice@example.org"
    assert row["pipeline_version"] == OLD_VERSION
    assert row["text_changed"] is True
    assert row["scored"] is True
    assert isinstance(row["id"], int)


def test_staleness_check_stamps_extractions_it_finds_unchanged(tmp_path):
    path = tmp_path / "unchanged.db"
    with Store(path) as store:
        message = extracted(store)
        store.set_extraction_version(store.extraction_for_message(message.id).id, OLD_GENERATION)
    app = create_app(_config(path), frontend_dist=None)
    app.testing = True
    web = app.test_client()

    data = web.post("/api/staleness/check").get_json()
    assert (data["checked"], data["unchanged"], data["stamped"], data["differing"]) == (1, 1, 1, 0)
    assert data["messages"] == []
    assert web.get("/api/staleness").get_json()["stale"] is False


def test_staleness_reextract_rewrites_only_the_given_messages(client, seeded_db):
    ids = [m["id"] for m in client.post("/api/staleness/check").get_json()["messages"]]
    chosen = ids[:2]

    data = client.post("/api/staleness/reextract", json={"ids": chosen}).get_json()
    assert data["processed"] == 2
    assert data["rewritten"] == 2
    assert set(data["rescore_ids"]) <= set(chosen)

    with Store(seeded_db) as store:
        for message_pk in chosen:
            after = store.extraction_for_message(message_pk)
            assert (after.extraction_version, after.pipeline_version) == (
                EXTRACTION_VERSION,
                __version__,
            )
        untouched = store.extraction_for_message(next(i for i in ids if i not in chosen))
        assert (untouched.extraction_version, untouched.pipeline_version) == (
            OLD_GENERATION,
            OLD_VERSION,
        )


@pytest.mark.parametrize(
    "body, message",
    [
        ({}, "ids must be a non-empty list of message ids"),
        ({"ids": []}, "ids must be a non-empty list of message ids"),
        ({"ids": "5"}, "ids must be a non-empty list of message ids"),
        ({"ids": [1, "2"]}, "ids must contain integer message ids"),
        ({"ids": [True]}, "ids must contain integer message ids"),
        ({"ids": list(range(1001))}, "ids must contain at most 1000 message ids"),
    ],
)
def test_staleness_id_validation(client, body, message):
    for path in ("/api/staleness/reextract", "/api/staleness/rescore"):
        res = client.post(path, json=body)
        assert res.status_code == 400
        assert res.get_json()["error"] == message


def test_staleness_rescore_passes_only_the_given_ids_to_the_scorer(client, monkeypatch):
    calls = {}

    def fake_run_score(store, pangram_client, **kwargs):
        calls.update(kwargs)
        return ScoreSummary(scored=2, cache_hits=1, api_calls=2, too_short=0)

    monkeypatch.setattr("mailing_list_ai_check.webapp.api.run_score", fake_run_score)
    monkeypatch.setattr(
        "mailing_list_ai_check.webapp.api.PangramClient",
        lambda key, model="pangram-4": StubPangram(model),
    )

    data = client.post("/api/staleness/rescore", json={"ids": [3, 4, 5]}).get_json()
    assert data == {
        "scored": 2,
        "cache_hits": 1,
        "api_calls": 2,
        "too_short": 0,
        "scoring_skipped": False,
    }
    assert calls["message_ids"] == {3, 4, 5}
    assert calls["limit"] == 3


def test_staleness_rescore_is_skipped_without_an_api_key(seeded_db, monkeypatch):
    app = create_app(_config(seeded_db, pangram_key=""), frontend_dist=None)
    app.testing = True

    def fail(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("Pangram must not be called without an API key")

    monkeypatch.setattr("mailing_list_ai_check.webapp.api.PangramClient", fail)

    data = app.test_client().post("/api/staleness/rescore", json={"ids": [1]}).get_json()
    assert data["scoring_skipped"] is True
    assert data["api_calls"] == 0
