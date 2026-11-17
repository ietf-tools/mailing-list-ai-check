"""Tests for the part check: AI verdicts re-checked by scoring each part.

A :class:`PartClient` stands in for :class:`PangramClient`. The whole text of a
test message is the parts joined by a blank line; the client rates any text
that contains a part boundary marker as the whole and each part by the verdict
the test assigns it, so a test controls the whole-text and per-part verdicts
independently. No network or key is used.
"""

from __future__ import annotations

import json

import pytest

from mailing_list_ai_check import cli, staleness
from mailing_list_ai_check.export_import import export_lists, import_file
from mailing_list_ai_check.pangram import PangramBulkOutcome, PangramError, PangramResult
from email_reply_extractor import clean_for_scoring, extract_new_text
from mailing_list_ai_check.store import Store, sha256_text

#: Distinct words, so each generated part is its own text.
_counter = iter(range(1_000_000))


def _words(n: int, tag: str) -> str:
    return " ".join(f"{tag}{next(_counter)}" for _ in range(n))


def _verdict(fraction_ai, fraction_human, label, version="4.1"):
    return PangramResult(
        fraction_ai=fraction_ai,
        fraction_ai_assisted=round(1 - fraction_ai - fraction_human, 6),
        fraction_human=fraction_human,
        prediction_short=label,
        version=version,
        raw={"prediction_short": label, "headline": f"{label} headline"},
    )


AI = _verdict(0.95, 0.0, "AI")
HUMAN = _verdict(0.0, 1.0, "Human")


class PartClient:
    """Rates a registered text by its verdict; anything else by ``default``."""

    def __init__(self, *, model="pangram-4", fail_on=None):
        self.model = model
        self.verdicts: dict[str, PangramResult] = {}
        self.default = AI
        self.texts: list[str] = []
        self.bulk_jobs: list[dict[str, str]] = []
        self.fail_on = fail_on or set()

    def _rate(self, text):
        if text in self.fail_on:
            raise PangramError("simulated failure")
        return self.verdicts.get(text, self.default)

    def predict(self, text):
        self.texts.append(text)
        return self._rate(text)

    def predict_bulk(self, items):
        self.bulk_jobs.append(dict(items))
        results, errors = {}, {}
        for item_id, text in items.items():
            try:
                results[item_id] = self._rate(text)
            except PangramError as exc:
                errors[item_id] = str(exc)
        return PangramBulkOutcome(bulk_id="b", status="succeeded", results=results, errors=errors)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "parts.db") as s:
        yield s


def _seed(store, parts, *, message_id, part_starts="auto", list_name="l"):
    """One ok extraction whose text is ``parts`` joined by blank lines.

    ``part_starts="auto"`` records a boundary at the first line of every part
    after the first; ``None`` leaves the boundaries unrecorded.
    """
    text = "\n\n".join(parts)
    if part_starts == "auto":
        starts, line = [], 0
        for i, part in enumerate(parts):
            if i:
                starts.append(line)
            line += part.count("\n") + 2
        part_starts = starts
    mlist = store.upsert_list(list_name, f"Shared Folders/{list_name}")
    addr = store.upsert_address("a@example.org", "A")
    message = store.upsert_message(
        message_id=message_id,
        list_id=mlist.id,
        address_id=addr.id,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body=text,
        uid=None,
    ).message
    return store.insert_extraction(
        message_id=message.id,
        extracted_text=text,
        method="erp",
        status="ok",
        part_starts=part_starts,
    )


def _score_of(store, extraction):
    return store.score_for_extraction(extraction.id)


# --- helpers ------------------------------------------------------------------


def test_label_bands_match_pangram():
    assert cli.label_for_fractions(0.8, 0.0) == "AI"
    assert cli.label_for_fractions(0.79, 0.21) == "Mixed"
    assert cli.label_for_fractions(0.0, 0.9) == "Mixed"
    assert cli.label_for_fractions(0.0, 0.91) == "Human"


def test_short_parts_merge_into_the_next():
    a, b, c = _words(3, "a"), _words(60, "b"), _words(10, "c")
    assert cli.merge_short_parts([a, b, c], 50) == [f"{a}\n\n{b}\n\n{c}"]
    d, e = _words(55, "d"), _words(52, "e")
    assert cli.merge_short_parts([a, d, e], 50) == [f"{a}\n\n{d}", e]
    assert cli.merge_short_parts([], 50) == []


# --- the check ----------------------------------------------------------------


def test_parts_that_disagree_replace_the_verdict(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()
    client.verdicts = {p1: HUMAN, p2: HUMAN}

    summary = cli.run_score(store, client, limit=10)

    assert client.texts[1:] == [p1, p2]
    score = _score_of(store, extraction)
    assert score.label == "Human"
    assert score.fraction_ai == 0.0
    assert score.part_check_status == "checked"
    detail = json.loads(score.part_check)
    assert detail["replaced"] is True
    assert detail["whole"]["label"] == "AI"
    assert detail["whole"]["fraction_ai"] == 0.95
    assert [p["words"] for p in detail["parts"]] == [60, 60]
    assert json.loads(score.raw_response)["prediction_short"] == "AI"
    assert (summary.scored, summary.part_checked, summary.part_replaced) == (1, 1, 1)
    assert summary.api_calls == 3


def test_parts_that_agree_keep_the_verdict(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()  # everything AI

    summary = cli.run_score(store, client, limit=10)

    score = _score_of(store, extraction)
    assert (score.label, score.fraction_ai) == ("AI", 0.95)
    assert score.part_check_status == "checked"
    assert json.loads(score.part_check)["replaced"] is False
    assert (summary.part_checked, summary.part_replaced) == (1, 0)


def test_combined_verdict_is_weighted_by_words(store):
    p1, p2 = _words(150, "p"), _words(50, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()
    client.verdicts = {p1: _verdict(0.6, 0.4, "Mixed"), p2: AI}

    cli.run_score(store, client, limit=10)

    score = _score_of(store, extraction)
    assert score.fraction_ai == pytest.approx((150 * 0.6 + 50 * 0.95) / 200)
    assert score.label == "Mixed"


def test_a_single_part_is_recorded_without_a_call(store):
    extraction = _seed(store, [_words(80, "p")], message_id="<m1@x>")
    client = PartClient()

    summary = cli.run_score(store, client, limit=10)

    assert len(client.texts) == 1
    score = _score_of(store, extraction)
    assert score.part_check_status == "single"
    assert score.part_check is None
    assert summary.part_single == 1


def test_short_parts_merge_before_counting(store):
    # A two-word line between two quotes joins the answer that follows it.
    extraction = _seed(store, [_words(2, "a"), _words(60, "b")], message_id="<m1@x>")
    client = PartClient()

    cli.run_score(store, client, limit=10)

    assert _score_of(store, extraction).part_check_status == "single"
    assert len(client.texts) == 1


def test_verdicts_below_the_trigger_are_not_checked(store):
    extraction = _seed(store, [_words(60, "p"), _words(60, "q")], message_id="<m1@x>")
    client = PartClient()
    client.default = _verdict(0.79, 0.21, "Mixed")

    summary = cli.run_score(store, client, limit=10)

    assert len(client.texts) == 1
    assert _score_of(store, extraction).part_check_status is None
    assert summary.part_checked == summary.part_single == 0


def test_unrecorded_boundaries_wait_for_re_derivation(store):
    extraction = _seed(
        store, [_words(60, "p"), _words(60, "q")], message_id="<m1@x>", part_starts=None
    )
    client = PartClient()

    cli.run_score(store, client, limit=10)

    assert len(client.texts) == 1
    assert _score_of(store, extraction).part_check_status is None


def test_the_check_shares_the_run_limit(store):
    extraction = _seed(store, [_words(60, "p"), _words(60, "q")], message_id="<m1@x>")
    client = PartClient()

    cli.run_score(store, client, limit=2)  # whole text + one part: not enough

    assert len(client.texts) == 1
    assert _score_of(store, extraction).part_check_status is None

    cli.run_score(store, client, limit=2)  # a later run has room

    assert len(client.texts) == 3
    assert _score_of(store, extraction).part_check_status == "checked"


def test_a_failed_part_leaves_the_verdict_unchecked(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient(fail_on={p2})

    summary = cli.run_score(store, client, limit=10)

    score = _score_of(store, extraction)
    assert (score.label, score.part_check_status) == ("AI", None)
    assert summary.part_failed == 1


def test_a_cross_post_is_checked_once(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    first = _seed(store, [p1, p2], message_id="<m1@x>", list_name="a")
    second = _seed(store, [p1, p2], message_id="<m1@x>", list_name="b")
    client = PartClient()
    client.verdicts = {p1: HUMAN, p2: HUMAN}

    cli.run_score(store, client, limit=10)

    # One whole-text call (the second is a cache hit) and two part calls.
    assert len(client.texts) == 3
    for extraction in (first, second):
        score = _score_of(store, extraction)
        assert (score.label, score.part_check_status) == ("Human", "checked")


def test_a_later_cross_post_copies_the_check(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    _seed(store, [p1, p2], message_id="<m1@x>", list_name="a")
    client = PartClient()
    client.verdicts = {p1: HUMAN, p2: HUMAN}
    cli.run_score(store, client, limit=10)

    later = _seed(store, [p1, p2], message_id="<m1@x>", list_name="b")
    cli.run_score(store, client, limit=10)

    assert len(client.texts) == 3  # nothing new was sent
    score = _score_of(store, later)
    assert (score.label, score.part_check_status) == ("Human", "checked")


def test_bulk_scores_the_parts_in_a_second_job(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()
    client.verdicts = {p1: HUMAN, p2: HUMAN}

    summary = cli.run_score(store, client, limit=10, bulk=True)

    assert len(client.bulk_jobs) == 2
    assert sorted(client.bulk_jobs[1].values()) == sorted([p1, p2])
    score = _score_of(store, extraction)
    assert (score.label, score.part_check_status) == ("Human", "checked")
    assert summary.api_calls == 3


def test_dry_run_writes_and_sends_nothing(store):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()
    cli.run_score(store, client, limit=10)
    store.conn.execute("UPDATE scores SET part_check_status = NULL, part_check = NULL")
    store.conn.commit()

    summary = cli.run_score(store, None, limit=10, dry_run=True)

    assert summary.part_checked == 1
    assert _score_of(store, extraction).part_check_status is None


# --- re-derivation ------------------------------------------------------------


def _quoted_reply(store):
    """A stored inline reply whose extraction predates recorded boundaries."""
    a, b = _words(60, "a"), _words(60, "b")
    body = f"{a}\n\n> quoted question\n\n{b}\n"
    mlist = store.upsert_list("l", "Shared Folders/l")
    addr = store.upsert_address("a@example.org", "A")
    message = store.upsert_message(
        message_id="<r@x>",
        list_id=mlist.id,
        address_id=addr.id,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body=body,
        uid=None,
    ).message
    # The text the running routine derives, so the row is unchanged on
    # re-derivation whatever the generation's blank-line layout.
    extraction = store.insert_extraction(
        message_id=message.id,
        extracted_text=extract_new_text(body, None).text,
        method="erp",
        status="ok",
        extraction_version=7,
    )
    return message, extraction


def test_re_derivation_records_boundaries_of_unchanged_text(store):
    message, extraction = _quoted_reply(store)
    assert extraction.part_starts is None

    summary = staleness.reextract(store, [message.id])

    assert summary.unchanged == 1
    assert store.get_extraction(extraction.id).part_starts == (
        extract_new_text(message.raw_body, None).part_starts
    )
    assert len(store.get_extraction(extraction.id).part_starts) == 1


def test_new_boundaries_return_a_single_part_finding_to_the_queue(store):
    message, extraction = _quoted_reply(store)
    scored = clean_for_scoring(extraction.extracted_text).text
    store.insert_score(
        extraction_id=extraction.id, text_sha256=sha256_text(scored), fraction_ai=0.95, label="AI"
    )
    score = store.score_for_extraction(extraction.id)
    store.record_part_check(score.id, "single")

    staleness.reextract(store, [message.id])

    assert store.score_for_extraction(extraction.id).part_check_status is None


# --- export / import ----------------------------------------------------------


def test_export_import_round_trips_the_part_data(store, tmp_path):
    p1, p2 = _words(60, "p"), _words(60, "q")
    extraction = _seed(store, [p1, p2], message_id="<m1@x>")
    client = PartClient()
    client.verdicts = {p1: HUMAN, p2: HUMAN}
    cli.run_score(store, client, limit=10)
    path = tmp_path / "out.jsonl"
    export_lists(store, ["l"], path, compress=False)

    with Store(tmp_path / "target.db") as target:
        import_file(target, path)
        message = target.find_message_by_message_id("<m1@x>")
        copy = target.extraction_for_message(message.id)
        score = target.score_for_extraction(copy.id)

    original = _score_of(store, extraction)
    assert copy.part_starts == extraction.part_starts
    assert (score.label, score.part_check_status) == ("Human", "checked")
    assert score.part_check == original.part_check


def test_an_old_export_without_part_data_imports(store, tmp_path):
    extraction = _seed(store, [_words(60, "p")], message_id="<m1@x>", part_starts=None)
    store.insert_score(extraction_id=extraction.id, text_sha256="x", fraction_ai=0.1, label="Human")
    path = tmp_path / "out.jsonl"
    export_lists(store, ["l"], path, compress=False)
    line = next(
        rec for rec in map(json.loads, path.read_text().splitlines()) if rec["type"] == "message"
    )
    assert "part_starts" not in line["extraction"]
    assert "part_check_status" not in line["extraction"]["score"]

    with Store(tmp_path / "target.db") as target:
        import_file(target, path)
        message = target.find_message_by_message_id("<m1@x>")
        copy = target.extraction_for_message(message.id)
        assert copy.part_starts is None
        assert target.score_for_extraction(copy.id).part_check_status is None


# --- dashboard API ------------------------------------------------------------


def test_the_api_reports_the_check_and_the_replacing_label(tmp_path):
    from mailing_list_ai_check.config import Config
    from mailing_list_ai_check.webapp import create_app

    db = tmp_path / "web.db"
    with Store(db) as store:
        p1, p2 = _words(60, "p"), _words(60, "q")
        extraction = _seed(store, [p1, p2], message_id="<m1@x>")
        client = PartClient()
        client.verdicts = {p1: HUMAN, p2: HUMAN}
        cli.run_score(store, client, limit=10)
        message_pk = extraction.message_id

    config = Config(
        imap_host="",
        imap_port=993,
        imap_username="",
        imap_password="",
        pangram_api_key="",
        database_path=str(db),
        log_level="INFO",
        flask_host="127.0.0.1",
        flask_port=8050,
    )
    app = create_app(config, frontend_dist=None)
    app.testing = True
    web = app.test_client()

    score = web.get(f"/api/messages/{message_pk}").get_json()["score"]
    assert score["label"] == score["prediction_short"] == "Human"
    assert score["part_check"]["replaced"] is True
    assert score["part_check"]["whole"]["label"] == "AI"
    assert [p["label"] for p in score["part_check"]["parts"]] == ["Human", "Human"]
    assert score["headline"] == "Verdict from scoring the parts separately"

    row = web.get("/api/messages").get_json()["messages"][0]
    assert row["score"]["prediction_short"] == "Human"
    assert row["score"]["part_check"]["replaced"] is True
