"""Tests for the export/import feature (:mod:`mailing_list_ai_check.export_import`).

Written against ``docs/export-import.md``: a JSON Lines stream (``header`` first,
per-list ``list``/``pull_state`` then global ``person``/``address``/``message``
records with extraction + score embedded, ``trailer`` last), text stored as a
pointer into the message body (``full_body`` / ``span`` / ``inline``), and an
idempotent, collision-safe, all-or-nothing import.

Exports are zstd-compressed by default and the writer appends ``.zst`` to the
path it is given, so tests read back ``summary.path`` -- the path actually
written -- rather than the path they passed in. Imports classify the container
from its content, so the gzip and plain files of older exports are built here
directly (the exporter can no longer produce them) and imported under whatever
name they carry.

Fixtures are built through the public :class:`Store` API. The source database
covers: two lists; messages with and without a sender address; an address linked
to a person plus unlinked ones; an address with no messages (never exported); a
full-body / span / inline / no-extraction spread; a scored extraction carrying a
raw_response JSON blob, one carrying a null raw_response, and one ok-but-unscored;
and a pull_state cursor for one list.
"""

from __future__ import annotations

import gc
import gzip
import json
import logging
import tracemalloc
from pathlib import Path

import pytest

from mailing_list_ai_check import __version__, codec, export_import, staleness
from mailing_list_ai_check.cli import export_main, import_main
from email_reply_extractor.extraction import EXTRACTION_VERSION
from mailing_list_ai_check.store import Store, sha256_text

# --- fixture data -------------------------------------------------------------

ANNOUNCE = "Shared Folders/announce"
LAST_CALL = "Shared Folders/last-call"

# Message natural-key ids used throughout.
M1 = "<m1@example.org>"  # full_body, scored (raw_response JSON)
M2 = "<m2@example.org>"  # span, ok-but-unscored
M3 = "<m3@example.org>"  # inline, no sender, scored (raw_response null)
M4 = "<m4@example.org>"  # no extraction
M5 = "<m5@example.org>"  # last-call, too_short span, unscored
M6 = "<m6@example.org>"  # full_body, unscored, second Alice address

BODY1 = "This is the whole body of message one."
BODY2 = "Header line\nExtracted middle part\nFooter line"
SPAN2 = "Extracted middle part"
BODY3 = "Only quoted original content here."
INLINE3 = "Reconstructed text that is absent from the raw body"
BODY5 = "prefix Last call body suffix"
SPAN5 = "Last call body"
BODY6 = "Second alice address body."

RAW_RESPONSE_M1 = {"prediction_short": "AI", "fraction_ai": 0.95, "windows": []}


def _build_source(store: Store) -> None:
    """Populate ``store`` with the representative export fixture (see module docstring)."""
    ann = store.upsert_list("announce", ANNOUNCE)
    lc = store.upsert_list("last-call", LAST_CALL)

    p1 = store.create_person("Alice Smith")
    alice = store.upsert_address("alice@example.org", "Alice Smith")
    alice2 = store.upsert_address("alice@work.example", "Alice Smith")
    bob = store.upsert_address("bob@example.org", "Bob Jones")
    carol = store.upsert_address("carol@example.org", "Carol")
    store.upsert_address("dave@example.org", "Dave")  # no messages -> never exported
    store.assign_address_to_person(alice.id, p1.id)
    store.assign_address_to_person(alice2.id, p1.id)  # a1 + a2 form a person group

    # m1 -- extracted_text == raw_body -> full_body; scored with a raw_response blob.
    m1 = store.upsert_message(
        message_id=M1,
        list_id=ann.id,
        address_id=alice.id,
        subject="Full body",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY1,
        uid=101,
        fetched_at="2026-01-06T00:00:00+00:00",
        raw_html="<p>whole</p>",
        # Deliberately not the address's "Alice Smith": the per-message name is
        # carried by the file in its own right.
        from_name="Alice A. Smith",
        # Bytes, and deliberately non-UTF-8 (a raw 8-bit octet in a header), so
        # the round-trip proves base64 rather than a lossy text encoding.
        raw_headers=b"From: Alice A. Smith <alice@example.org>\r\nX-Odd: \xff\xfe\r\n",
    ).message
    e1 = store.insert_extraction(
        message_id=m1.id,
        extracted_text=BODY1,
        method="reply_parser",
        status="ok",
        created_at="2026-01-06T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e1.id,
        text_sha256=sha256_text("cleaned one"),
        fraction_ai=0.95,
        fraction_ai_assisted=0.03,
        fraction_human=0.02,
        label="AI",
        detector_version="3.3.2",
        raw_response=RAW_RESPONSE_M1,
        scored_at="2026-01-07T00:00:00+00:00",
    )

    # m2 -- extracted_text is a substring of raw_body -> span; ok but not scored.
    m2 = store.upsert_message(
        message_id=M2,
        list_id=ann.id,
        address_id=bob.id,
        subject="Span",
        date="2026-01-10T10:00:00+00:00",
        in_reply_to=M1,
        raw_body=BODY2,
        uid=102,
        fetched_at="2026-01-11T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m2.id,
        extracted_text=SPAN2,
        method="reply_parser",
        status="ok",
        created_at="2026-01-11T01:00:00+00:00",
    )

    # m3 -- no sender address; extracted_text not a substring -> inline; scored
    # with a null raw_response.
    m3 = store.upsert_message(
        message_id=M3,
        list_id=ann.id,
        address_id=None,
        subject="Inline",
        date="2026-01-15T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY3,
        uid=103,
        fetched_at="2026-01-16T00:00:00+00:00",
    ).message
    e3 = store.insert_extraction(
        message_id=m3.id,
        extracted_text=INLINE3,
        method="custom",
        status="ok",
        created_at="2026-01-16T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e3.id,
        text_sha256=sha256_text("cleaned three"),
        fraction_ai=0.10,
        fraction_ai_assisted=0.05,
        fraction_human=0.85,
        label="Human",
        detector_version="3.3.2",
        raw_response=None,
        scored_at="2026-01-17T00:00:00+00:00",
    )

    # m4 -- no extraction row at all.
    store.upsert_message(
        message_id=M4,
        list_id=ann.id,
        address_id=carol.id,
        subject="No extraction",
        date="2026-01-20T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body with no extraction row.",
        uid=104,
        fetched_at="2026-01-21T00:00:00+00:00",
    )

    # m6 -- full_body, unscored; second Alice address completes the person group.
    m6 = store.upsert_message(
        message_id=M6,
        list_id=ann.id,
        address_id=alice2.id,
        subject="Group",
        date="2026-01-25T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY6,
        uid=106,
        fetched_at="2026-01-26T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m6.id,
        extracted_text=BODY6,
        method="reply_parser",
        status="ok",
        created_at="2026-01-26T01:00:00+00:00",
    )

    # m5 -- last-call, too_short span, unscored.
    m5 = store.upsert_message(
        message_id=M5,
        list_id=lc.id,
        address_id=alice.id,
        subject="Last call",
        date="2026-02-01T10:00:00+00:00",
        in_reply_to=None,
        raw_body=BODY5,
        uid=201,
        fetched_at="2026-02-02T00:00:00+00:00",
    ).message
    store.insert_extraction(
        message_id=m5.id,
        extracted_text=SPAN5,
        method="reply_parser",
        status="too_short",
        created_at="2026-02-02T01:00:00+00:00",
    )

    store.set_pull_state(lc.id, uidvalidity=42, last_uid=99)


@pytest.fixture
def source():
    """An in-memory source Store populated with the export fixture."""
    with Store(":memory:") as s:
        _build_source(s)
        yield s


@pytest.fixture
def target():
    """A fresh, empty in-memory target Store."""
    with Store(":memory:") as s:
        yield s


# --- file helpers -------------------------------------------------------------


def _read_records(path: str | Path) -> list[dict]:
    """Read a JSONL export file into record dicts, whatever container it is in."""
    with codec.open_read_text(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _write_records(path: str | Path, records: list[dict]) -> None:
    """Write record dicts back out as plain, uncompressed JSONL.

    Deliberately uncompressed: the callers below build malformed files to prove
    the importer rejects them, and the importer must reach that validation on an
    uncompressed file just as it does on a zstd one.
    """
    with codec.open_write_text(path, compress=False) as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _write_gzip_records(path: str | Path, records: list[dict]) -> None:
    """Write record dicts as a gzip JSONL file, the shape of a pre-zstd export.

    The exporter cannot produce gzip any more, so the backward-compatibility
    tests build one here instead.
    """
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _write_zstd_records(path: str | Path, records: list[dict]) -> None:
    """Write record dicts as a zstd JSONL file under whatever name is given."""
    with codec.open_write_text(path, compress=True) as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _export_all(store: Store, tmp_path: Path, name: str = "all.jsonl") -> Path:
    """Export every list from ``store`` and return the path actually written.

    The exporter compresses by default and appends ``.zst``, so the path it
    reports -- not the one it was handed -- is the file that exists.
    """
    summary = export_import.export_lists(store, None, tmp_path / name, all_lists=True)
    return Path(summary.path)


def _messages_of_type(records: list[dict], type_: str) -> list[dict]:
    return [r for r in records if r.get("type") == type_]


def _message_by_id(records: list[dict]) -> dict[str, dict]:
    return {r["message_id"]: r for r in _messages_of_type(records, "message")}


# --- DB introspection helpers (natural keys, so ids need not match) -----------

_DATA_TABLES = ("lists", "pull_state", "persons", "addresses", "messages", "extractions", "scores")


def _counts(store: Store) -> dict[str, int]:
    return {
        t: store.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
        for t in _DATA_TABLES
    }


def _messages_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, m.subject AS subject, "
        "m.date AS date, m.in_reply_to AS in_reply_to, m.raw_body AS raw_body, "
        "m.raw_html AS raw_html, m.uid AS uid, m.fetched_at AS fetched_at, "
        "m.pipeline_version AS pipeline_version, m.from_name AS from_name, "
        "m.raw_headers AS raw_headers, a.email AS email "
        "FROM messages m JOIN lists l ON l.id = m.list_id "
        "LEFT JOIN addresses a ON a.id = m.address_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _extractions_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, e.extracted_text AS extracted_text, "
        "e.method AS method, e.char_count AS char_count, e.status AS status, "
        "e.created_at AS created_at "
        "FROM extractions e JOIN messages m ON m.id = e.message_id "
        "JOIN lists l ON l.id = m.list_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _extraction_versions_by_key(
    store: Store,
) -> dict[tuple[str, str], tuple[str | None, int | None]]:
    """Map each extraction to its ``(pipeline_version, extraction_version)`` stamps.

    Kept apart from :func:`_extractions_by_key` so the tests that compare whole
    extraction rows for equality are not also asserting on version stamps.
    """
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, "
        "e.pipeline_version AS pipeline_version, e.extraction_version AS extraction_version "
        "FROM extractions e JOIN messages m ON m.id = e.message_id "
        "JOIN lists l ON l.id = m.list_id"
    ).fetchall()
    return {
        (r["folder"], r["message_id"]): (r["pipeline_version"], r["extraction_version"])
        for r in rows
    }


def _scores_by_key(store: Store) -> dict[tuple[str, str], dict]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, m.message_id AS message_id, s.fraction_ai AS fraction_ai, "
        "s.fraction_ai_assisted AS fraction_ai_assisted, s.fraction_human AS fraction_human, "
        "s.label AS label, s.detector_version AS detector_version, "
        "s.raw_response AS raw_response, s.text_sha256 AS text_sha256, s.scored_at AS scored_at "
        "FROM scores s JOIN extractions e ON e.id = s.extraction_id "
        "JOIN messages m ON m.id = e.message_id JOIN lists l ON l.id = m.list_id"
    ).fetchall()
    return {(r["folder"], r["message_id"]): dict(r) for r in rows}


def _person_groups(store: Store) -> dict[str, frozenset[str]]:
    """Map each person's canonical name to the frozenset of its addresses' emails."""
    rows = store.conn.execute(
        "SELECT p.canonical_name AS name, a.email AS email "
        "FROM persons p JOIN addresses a ON a.person_id = p.id"
    ).fetchall()
    groups: dict[str, set[str]] = {}
    for r in rows:
        groups.setdefault(r["name"], set()).add(r["email"])
    return {k: frozenset(v) for k, v in groups.items()}


def _pull_states_by_folder(store: Store) -> dict[str, tuple[int, int]]:
    rows = store.conn.execute(
        "SELECT l.folder AS folder, ps.uidvalidity AS uidvalidity, ps.last_uid AS last_uid "
        "FROM pull_state ps JOIN lists l ON l.id = ps.list_id"
    ).fetchall()
    return {r["folder"]: (r["uidvalidity"], r["last_uid"]) for r in rows}


# ==============================================================================
# EXPORT
# ==============================================================================


def test_export_writes_valid_jsonl_with_header_and_trailer(source, tmp_path):
    """Header first, trailer last, records grouped in the spec order; summary matches."""
    out = tmp_path / "all.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)

    records = _read_records(summary.path)
    assert records[0]["type"] == "header"
    assert records[-1]["type"] == "trailer"

    header = records[0]
    assert header["format"] == export_import.FORMAT_NAME == "mlac-export"
    assert header["format_version"] == export_import.FORMAT_VERSION == 2
    assert header["app_version"] == __version__
    schema_version = source.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[
        "v"
    ]
    assert header["schema_version"] == schema_version
    assert set(header["folders"]) == {ANNOUNCE, LAST_CALL}

    # Record grouping: header, then lists (+pull_state), then persons, addresses,
    # then messages, then trailer -- a non-decreasing rank sequence.
    rank = {"header": 0, "list": 1, "pull_state": 1, "person": 2, "address": 3, "message": 4}
    body_ranks = [rank[r["type"]] for r in records[:-1]]
    assert body_ranks == sorted(body_ranks)

    trailer = records[-1]
    assert (trailer["lists"], trailer["messages"], trailer["extractions"], trailer["scores"]) == (
        2,
        6,
        5,
        2,
    )
    assert (summary.lists, summary.messages, summary.extractions, summary.scores) == (2, 6, 5, 2)
    # The summary reports the path actually written -- the compressed one.
    assert summary.path == str(codec.compressed_path(out))


def test_export_text_pointers_and_sha256(source, tmp_path):
    """full_body / span / inline are chosen per spec; sha256 is always the text hash."""
    out = tmp_path / "ptr.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))

    # full_body: extracted_text == raw_body.
    e1 = msgs[M1]["extraction"]
    assert e1["text"] == {"kind": "full_body"}
    assert e1["sha256"] == sha256_text(BODY1)

    # span: start/length resolve to the exact substring of the record's own body.
    e2 = msgs[M2]["extraction"]
    assert e2["text"]["kind"] == "span"
    start, length = e2["text"]["start"], e2["text"]["length"]
    assert msgs[M2]["raw_body"][start : start + length] == SPAN2
    assert length == len(SPAN2)
    assert e2["sha256"] == sha256_text(SPAN2)

    # inline: text not a contiguous substring -> carried verbatim.
    e3 = msgs[M3]["extraction"]
    assert e3["text"] == {"kind": "inline", "value": INLINE3}
    assert e3["sha256"] == sha256_text(INLINE3)


def test_export_message_without_extraction_and_unscored(source, tmp_path):
    """A message with no extraction and an ok-but-unscored extraction round-trip as nulls."""
    out = tmp_path / "misc.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))

    assert msgs[M4]["extraction"] is None  # no extraction row
    assert msgs[M2]["extraction"] is not None
    assert msgs[M2]["extraction"]["score"] is None  # ok but not scored


def test_export_message_without_sender(source, tmp_path):
    """A message with no sender address exports ``email: null``."""
    out = tmp_path / "nosender.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))
    assert msgs[M3]["email"] is None
    assert msgs[M1]["email"] == "alice@example.org"


def test_export_score_record_fields(source, tmp_path):
    """The embedded score carries fractions, label, version, raw_response verbatim, sha, time."""
    out = tmp_path / "score.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))

    score = msgs[M1]["extraction"]["score"]
    assert score["fraction_ai"] == 0.95
    assert score["fraction_ai_assisted"] == 0.03
    assert score["fraction_human"] == 0.02
    assert score["label"] == "AI"
    assert score["detector_version"] == "3.3.2"
    assert score["text_sha256"] == sha256_text("cleaned one")
    assert score["scored_at"] == "2026-01-07T00:00:00+00:00"
    # raw_response is the verbatim stored JSON string.
    assert json.loads(score["raw_response"]) == RAW_RESPONSE_M1

    # A score with a null raw_response stays null.
    assert msgs[M3]["extraction"]["score"]["raw_response"] is None


def test_export_message_records_carry_pipeline_version(source, tmp_path):
    """Every message record carries a ``pipeline_version`` key; the fixture rows
    were all written under the current package version."""
    out = tmp_path / "pv.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))
    assert set(msgs) == {M1, M2, M3, M4, M5, M6}
    for rec in msgs.values():
        assert "pipeline_version" in rec
        assert rec["pipeline_version"] == __version__


def test_export_extraction_records_carry_extraction_version(source, tmp_path):
    """Every embedded extraction carries the generation of the routine that derived
    it, and the header repeats the exporting build's generation for diagnostics."""
    out = tmp_path / "gen.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    records = _read_records(summary.path)

    assert records[0]["extraction_version"] == EXTRACTION_VERSION

    extractions = [
        r["extraction"] for r in _messages_of_type(records, "message") if r["extraction"]
    ]
    assert len(extractions) == 5
    # The fixture rows were all written by the running routine.
    for ext in extractions:
        assert ext["extraction_version"] == EXTRACTION_VERSION


def test_export_carries_the_generation_of_each_extraction(source, tmp_path):
    """The per-extraction value is the row's own stamp, not the running one: a file
    can hold several generations at once."""
    source.conn.execute(
        "UPDATE extractions SET extraction_version = 1 WHERE message_id = "
        "(SELECT id FROM messages WHERE message_id = ?)",
        (M1,),
    )
    out = tmp_path / "mixed.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    msgs = _message_by_id(_read_records(summary.path))

    assert msgs[M1]["extraction"]["extraction_version"] == 1
    assert msgs[M2]["extraction"]["extraction_version"] == EXTRACTION_VERSION


def test_export_single_list_only_that_lists_data(source, tmp_path):
    """Selecting one list exports only its messages and only referenced addresses/persons."""
    out = tmp_path / "announce.jsonl"
    summary = export_import.export_lists(source, ["announce"], out)
    records = _read_records(summary.path)

    assert set(_message_by_id(records)) == {M1, M2, M3, M4, M6}  # no M5 (last-call)
    emails = {r["email"] for r in _messages_of_type(records, "address")}
    assert emails == {
        "alice@example.org",
        "alice@work.example",
        "bob@example.org",
        "carol@example.org",
    }
    assert "dave@example.org" not in emails  # unreferenced address excluded
    persons = _messages_of_type(records, "person")
    assert [p["canonical_name"] for p in persons] == ["Alice Smith"]
    assert records[0]["folders"] == [ANNOUNCE]
    assert (summary.lists, summary.messages) == (1, 5)


def test_export_other_single_list_narrows_addresses(source, tmp_path):
    """last-call alone references only Alice; Bob/Carol are not exported."""
    out = tmp_path / "lc.jsonl"
    summary = export_import.export_lists(source, ["last-call"], out)
    records = _read_records(summary.path)
    assert set(_message_by_id(records)) == {M5}
    emails = {r["email"] for r in _messages_of_type(records, "address")}
    assert emails == {"alice@example.org"}
    assert (summary.lists, summary.messages, summary.extractions, summary.scores) == (1, 1, 1, 0)


def test_export_all_lists_covers_every_list_with_messages(source, tmp_path):
    out = tmp_path / "all2.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)
    folders = {r["folder"] for r in _messages_of_type(_read_records(summary.path), "list")}
    assert folders == {ANNOUNCE, LAST_CALL}


def test_export_several_named_lists_covers_all_of_them(source, tmp_path):
    """Naming both lists exports both, and every address either references."""
    out = tmp_path / "both.jsonl"
    summary = export_import.export_lists(source, ["announce", "last-call"], out)
    records = _read_records(summary.path)

    assert set(_message_by_id(records)) == {M1, M2, M3, M4, M5, M6}
    assert records[0]["folders"] == [ANNOUNCE, LAST_CALL]
    assert (summary.lists, summary.messages) == (2, 6)


# --- date-range exports -------------------------------------------------------
#
# Fixture message dates: M1 2026-01-05, M2 2026-01-10, M3 2026-01-15,
# M4 2026-01-20, M6 2026-01-25 (announce), M5 2026-02-01 (last-call). All carry
# a 10:00:00+00:00 time, which is what makes the bare-day bound test below sharp.


def test_export_date_from_keeps_only_later_messages(source, tmp_path):
    summary = export_import.export_lists(
        source, None, tmp_path / "from.jsonl", all_lists=True, date_from="2026-01-20"
    )
    records = _read_records(summary.path)
    assert set(_message_by_id(records)) == {M4, M6, M5}
    assert summary.messages == 3


def test_export_date_to_keeps_only_earlier_messages(source, tmp_path):
    summary = export_import.export_lists(
        source, None, tmp_path / "to.jsonl", all_lists=True, date_to="2026-01-15T23:59:59+00:00"
    )
    assert set(_message_by_id(_read_records(summary.path))) == {M1, M2, M3}
    assert summary.messages == 3


def test_export_both_bounds_are_inclusive(source, tmp_path):
    """A range spanning two message timestamps exactly includes both of them."""
    summary = export_import.export_lists(
        source,
        None,
        tmp_path / "span.jsonl",
        all_lists=True,
        date_from="2026-01-10T10:00:00+00:00",
        date_to="2026-01-20T10:00:00+00:00",
    )
    assert set(_message_by_id(_read_records(summary.path))) == {M2, M3, M4}


def test_export_bare_to_day_excludes_that_days_messages(source, tmp_path):
    """The documented sharp edge: the comparison is lexical over stored ISO text.

    ``2026-01-20`` sorts before ``2026-01-20T10:00:00+00:00``, so M4 falls
    outside a range ending on its own day. The dashboard's date filter behaves
    the same way, which is the point — a range means the same thing in both.
    """
    summary = export_import.export_lists(
        source, None, tmp_path / "bare.jsonl", all_lists=True, date_to="2026-01-20"
    )
    assert M4 not in _message_by_id(_read_records(summary.path))


def test_export_range_narrows_a_named_list(source, tmp_path):
    """The range applies to named lists as well as to --all-lists."""
    summary = export_import.export_lists(
        source, ["announce"], tmp_path / "named.jsonl", date_from="2026-01-15"
    )
    assert set(_message_by_id(_read_records(summary.path))) == {M3, M4, M6}
    assert (summary.lists, summary.messages) == (1, 3)


def test_export_range_drops_addresses_only_referenced_outside_it(source, tmp_path):
    """The address pre-pass sees the same messages as the streaming pass.

    Within announce from 2026-01-15 only M3 (no sender), M4 (Carol) and M6
    (Alice's second address) survive. Bob sends only M2, and Alice's first
    address only M1, so both must be absent from the file — an address record
    for a message that is not there is exactly what the two passes disagreeing
    about the range would produce.
    """
    summary = export_import.export_lists(
        source, ["announce"], tmp_path / "narrow.jsonl", date_from="2026-01-15"
    )
    records = _read_records(summary.path)
    emails = {r["email"] for r in _messages_of_type(records, "address")}
    assert emails == {"alice@work.example", "carol@example.org"}


def test_export_all_lists_skips_a_list_with_nothing_in_range(source, tmp_path):
    """last-call's only message is in February, so a January range omits the list."""
    summary = export_import.export_lists(
        source, None, tmp_path / "jan.jsonl", all_lists=True, date_to="2026-01-31"
    )
    records = _read_records(summary.path)
    assert {r["folder"] for r in _messages_of_type(records, "list")} == {ANNOUNCE}
    assert records[0]["folders"] == [ANNOUNCE]
    assert summary.lists == 1


def test_export_range_ships_no_pull_cursor(source, tmp_path):
    """A partial file cannot claim a list is pulled up to its cursor.

    The unranged export of the same store does carry last-call's cursor, so the
    absence here is the range's doing and not a missing fixture.
    """
    ranged = export_import.export_lists(
        source, None, tmp_path / "ranged.jsonl", all_lists=True, date_from="2026-01-01"
    )
    assert _messages_of_type(_read_records(ranged.path), "pull_state") == []

    whole = _read_records(_export_all(source, tmp_path, "whole.jsonl"))
    assert [r["folder"] for r in _messages_of_type(whole, "pull_state")] == [LAST_CALL]


def test_import_of_a_ranged_export_leaves_the_target_cursorless(source, target, tmp_path):
    """So the next pull starts where the target actually is, not above the gap."""
    summary = export_import.export_lists(
        source, None, tmp_path / "part2.jsonl", all_lists=True, date_from="2026-02-01"
    )
    export_import.import_file(target, summary.path)
    assert _pull_states_by_folder(target) == {}


def test_export_header_records_the_range_only_when_bounded(source, tmp_path):
    """The bounds are provenance in the header, and absent when not given."""
    bounded = export_import.export_lists(
        source,
        None,
        tmp_path / "bounded.jsonl",
        all_lists=True,
        date_from="2026-01-10",
        date_to="2026-01-20",
    )
    header = _read_records(bounded.path)[0]
    assert (header["date_from"], header["date_to"]) == ("2026-01-10", "2026-01-20")

    plain = _read_records(_export_all(source, tmp_path, "unbounded.jsonl"))[0]
    assert "date_from" not in plain and "date_to" not in plain


def test_export_range_roundtrips_as_a_normal_import(source, target, tmp_path):
    """A ranged file is an ordinary export: it imports, carrying just its subset."""
    summary = export_import.export_lists(
        source, None, tmp_path / "part.jsonl", all_lists=True, date_from="2026-01-20"
    )
    export_import.import_file(target, summary.path)

    assert set(_messages_by_key(target)) == {
        (ANNOUNCE, M4),
        (ANNOUNCE, M6),
        (LAST_CALL, M5),
    }
    # Only the addresses those three messages reference came across: Carol (M4),
    # Alice's second address (M6) and her first (M5).
    assert _counts(target)["addresses"] == 3


def test_export_rejects_names_and_all_lists_together(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, ["announce"], tmp_path / "x.jsonl", all_lists=True)


def test_export_rejects_neither_names_nor_all_lists(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, None, tmp_path / "x.jsonl")


def test_export_rejects_unknown_list_name(source, tmp_path):
    with pytest.raises(ValueError):
        export_import.export_lists(source, ["nope"], tmp_path / "x.jsonl")


def test_export_zstd_roundtrips(source, target, tmp_path):
    """The default output is a real zstd file and imports back into a fresh DB."""
    out = tmp_path / "all.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)

    written = Path(summary.path)
    with open(written, "rb") as fh:
        assert fh.read(4) == codec.ZSTD_MAGIC
    records = _read_records(written)  # transparently decompressed
    assert records[0]["type"] == "header"

    imported = export_import.import_file(target, written)
    assert imported.messages_inserted == 6
    assert _messages_by_key(target) == _messages_by_key(source)


# ==============================================================================
# EXPORT -- compression and output paths
# ==============================================================================


def test_export_default_compresses_and_appends_the_suffix(source, tmp_path):
    """The default run writes zstd to ``<path>.zst``; the bare path is not created."""
    out = tmp_path / "all.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True)

    written = Path(summary.path)
    assert written == tmp_path / "all.jsonl.zst"
    assert written.exists()
    assert not out.exists()
    assert codec.detect(written) == "zstd"


def test_export_no_compress_writes_plain_and_appends_nothing(source, tmp_path):
    """``compress=False`` writes uncompressed JSON Lines to the path as given."""
    out = tmp_path / "plain.jsonl"
    summary = export_import.export_lists(source, None, out, all_lists=True, compress=False)

    assert summary.path == str(out)
    assert out.exists()
    assert not (tmp_path / "plain.jsonl.zst").exists()
    assert codec.detect(out) == "plain"
    assert _read_records(out)[0]["type"] == "header"


def test_export_does_not_double_suffix_an_already_zst_path(source, tmp_path):
    """A path that already ends .zst is written in place, not as .zst.zst."""
    out = tmp_path / "all.jsonl.zst"
    summary = export_import.export_lists(source, None, out, all_lists=True)

    assert summary.path == str(out)
    assert out.exists()
    assert not (tmp_path / "all.jsonl.zst.zst").exists()
    assert codec.detect(out) == "zstd"


def test_export_compressed_and_plain_hold_the_same_records(source, tmp_path):
    """Compression is a container choice only: the emitted records are identical."""
    zstd_summary = export_import.export_lists(source, None, tmp_path / "z.jsonl", all_lists=True)
    plain_summary = export_import.export_lists(
        source, None, tmp_path / "p.jsonl", all_lists=True, compress=False
    )

    def _without_timestamp(records: list[dict]) -> list[dict]:
        return [{k: v for k, v in r.items() if k != "exported_at"} for r in records]

    assert _without_timestamp(_read_records(zstd_summary.path)) == _without_timestamp(
        _read_records(plain_summary.path)
    )


def test_export_is_smaller_compressed(source, tmp_path):
    """The zstd container really shrinks the JSON Lines it holds."""
    zstd_summary = export_import.export_lists(source, None, tmp_path / "z.jsonl", all_lists=True)
    plain_summary = export_import.export_lists(
        source, None, tmp_path / "p.jsonl", all_lists=True, compress=False
    )
    assert Path(zstd_summary.path).stat().st_size < Path(plain_summary.path).stat().st_size


# ==============================================================================
# IMPORT -- round trip
# ==============================================================================


def test_import_into_fresh_db_reproduces_everything(source, target, tmp_path):
    """A fresh import reproduces lists, pull_state, persons, addresses, messages, +children."""
    out = _export_all(source, tmp_path)
    summary = export_import.import_file(target, out)

    assert summary.lists_created == 2
    assert summary.lists_existing == 0
    assert summary.pull_states_created == 1
    assert summary.persons_created == 1
    assert summary.addresses_upserted == 4
    assert summary.messages_inserted == 6
    assert summary.messages_skipped == 0
    assert summary.body_mismatches == 0
    assert summary.extractions_inserted == 5
    assert summary.scores_inserted == 2
    assert summary.dry_run is False

    # Everything matches the source, compared by natural key.
    assert _messages_by_key(target) == _messages_by_key(source)
    assert _extractions_by_key(target) == _extractions_by_key(source)
    assert _scores_by_key(target) == _scores_by_key(source)
    assert (
        _person_groups(target)
        == _person_groups(source)
        == {
            "Alice Smith": frozenset(
                {
                    "alice@example.org",
                    "alice@work.example",
                }
            )
        }
    )
    assert _pull_states_by_folder(target) == {LAST_CALL: (42, 99)}

    # Address display names came across too.
    disp = dict(target.conn.execute("SELECT email, display_name FROM addresses").fetchall())
    assert disp["bob@example.org"] == "Bob Jones"
    assert disp["carol@example.org"] == "Carol"

    # Message columns incl. raw_html / uid / fetched_at.
    m1 = _messages_by_key(target)[(ANNOUNCE, M1)]
    assert m1["raw_html"] == "<p>whole</p>"
    assert m1["uid"] == 101
    assert m1["fetched_at"] == "2026-01-06T00:00:00+00:00"
    # The message's own From name survives, distinct from the address's.
    assert m1["from_name"] == "Alice A. Smith"
    # Headers survive byte-for-byte, raw 8-bit octets included.
    assert m1["raw_headers"] == (b"From: Alice A. Smith <alice@example.org>\r\nX-Odd: \xff\xfe\r\n")


def test_import_preserves_file_pipeline_version(source, target, tmp_path):
    """Each imported message carries the exact ``pipeline_version`` from the file."""
    out = _export_all(source, tmp_path)
    export_import.import_file(target, out)

    file_versions = {
        (rec["folder"], rec["message_id"]): rec["pipeline_version"]
        for rec in _messages_of_type(_read_records(out), "message")
    }
    stored = _messages_by_key(target)
    for key, version in file_versions.items():
        assert stored[key]["pipeline_version"] == version


def test_import_reconstructs_pointer_text_exactly(source, target, tmp_path):
    """full_body / span / inline all reconstruct the original extracted_text verbatim."""
    out = _export_all(source, tmp_path)
    export_import.import_file(target, out)

    ext = _extractions_by_key(target)
    assert ext[(ANNOUNCE, M1)]["extracted_text"] == BODY1  # full_body
    assert ext[(ANNOUNCE, M2)]["extracted_text"] == SPAN2  # span
    assert ext[(ANNOUNCE, M3)]["extracted_text"] == INLINE3  # inline
    assert ext[(LAST_CALL, M5)]["extracted_text"] == SPAN5
    # Method / status / char_count / created_at preserved.
    assert ext[(ANNOUNCE, M3)]["method"] == "custom"
    assert ext[(LAST_CALL, M5)]["status"] == "too_short"
    assert ext[(ANNOUNCE, M1)]["char_count"] == len(BODY1)
    assert ext[(ANNOUNCE, M1)]["created_at"] == "2026-01-06T01:00:00+00:00"


def test_import_is_idempotent(source, target, tmp_path):
    """Importing the same file twice inserts nothing new and skips every message."""
    out = _export_all(source, tmp_path)
    export_import.import_file(target, out)
    before = _counts(target)

    second = export_import.import_file(target, out)
    assert second.messages_skipped == 6
    assert second.messages_inserted == 0
    assert second.extractions_inserted == 0
    assert second.scores_inserted == 0
    assert _counts(target) == before  # nothing duplicated


def test_import_into_source_db_is_a_noop(source, tmp_path):
    """Importing an export back into the database it came from skips everything."""
    out = _export_all(source, tmp_path)
    before = _counts(source)

    summary = export_import.import_file(source, out)
    assert summary.messages_inserted == 0
    assert summary.messages_skipped == 6
    assert summary.extractions_inserted == 0
    assert summary.scores_inserted == 0
    assert _counts(source) == before


def test_import_partial_overlap_skips_existing_and_flags_body_mismatch(source, target, tmp_path):
    """A pre-existing message (same folder+id, different body) is skipped and its
    children are not imported; the mismatch is counted; other messages import."""
    out = _export_all(source, tmp_path)

    # Seed the target with m1 under a different body and no extraction.
    ann = target.upsert_list("announce", ANNOUNCE)
    addr = target.upsert_address("alice@example.org", "Alice Smith")
    target.upsert_message(
        message_id=M1,
        list_id=ann.id,
        address_id=addr.id,
        subject="Pre-existing",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body="DIFFERENT body already stored",
        uid=999,
        fetched_at="2026-01-01T00:00:00+00:00",
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.body_mismatches == 1
    assert summary.messages_inserted == 5  # m2, m3, m4, m5, m6

    # m1 kept the target's own body and gained no extraction/score.
    m1 = _messages_by_key(target)[(ANNOUNCE, M1)]
    assert m1["raw_body"] == "DIFFERENT body already stored"
    assert (ANNOUNCE, M1) not in _extractions_by_key(target)
    assert (ANNOUNCE, M1) not in _scores_by_key(target)
    # Only m3's score came across (m1's was skipped with the message).
    assert set(_scores_by_key(target)) == {(ANNOUNCE, M3)}


def test_import_does_not_overwrite_existing_pull_state(source, target, tmp_path):
    """An existing cursor for the list always wins; a fresh cursor is created."""
    out = _export_all(source, tmp_path)

    # Target already has a last-call list with its own cursor.
    lc = target.upsert_list("last-call", LAST_CALL)
    target.set_pull_state(lc.id, uidvalidity=7, last_uid=7000)

    summary = export_import.import_file(target, out)
    # last-call cursor untouched; announce got no cursor (source had none).
    assert _pull_states_by_folder(target) == {LAST_CALL: (7, 7000)}
    assert summary.pull_states_created == 0


def test_import_creates_pull_state_when_absent(source, target, tmp_path):
    out = _export_all(source, tmp_path)
    summary = export_import.import_file(target, out)
    assert summary.pull_states_created == 1
    assert _pull_states_by_folder(target) == {LAST_CALL: (42, 99)}


def test_import_existing_list_is_left_untouched(source, target, tmp_path):
    """A list already present is not re-created and its metadata is not overwritten."""
    out = _export_all(source, tmp_path)

    target.upsert_list("announce", ANNOUNCE)
    summary = export_import.import_file(target, out)
    assert summary.lists_created == 1  # only last-call is new
    assert summary.lists_existing == 1


def test_import_person_group_joins_existing_target_person(source, target, tmp_path):
    """When a target person already owns one of a group's addresses, the imported
    siblings join that person and no new person row is created."""
    out = _export_all(source, tmp_path)

    # Target already links alice@example.org to an existing person.
    existing = target.create_person("Existing Alice")
    a = target.upsert_address("alice@example.org", "Alice Smith")
    target.assign_address_to_person(a.id, existing.id)

    summary = export_import.import_file(target, out)
    assert summary.persons_created == 0  # no new person for the group

    # The sibling address joined the existing person.
    sibling = target.conn.execute(
        "SELECT person_id FROM addresses WHERE email = 'alice@work.example'"
    ).fetchone()
    assert sibling["person_id"] == existing.id
    assert target.conn.execute("SELECT COUNT(*) AS c FROM persons").fetchone()["c"] == 1


def test_import_dry_run_reports_but_writes_nothing(source, target, tmp_path):
    """dry_run mirrors a real run's counts, sets the flag, and leaves the DB untouched."""
    out = _export_all(source, tmp_path)

    before = _counts(target)
    dry = export_import.import_file(target, out, dry_run=True)
    assert dry.dry_run is True
    assert _counts(target) == before  # nothing written

    real = export_import.import_file(target, out)
    # The dry-run's would-be counts equal the real run's actual counts.
    assert (dry.lists_created, dry.pull_states_created, dry.persons_created) == (
        real.lists_created,
        real.pull_states_created,
        real.persons_created,
    )
    assert (dry.messages_inserted, dry.extractions_inserted, dry.scores_inserted) == (
        real.messages_inserted,
        real.extractions_inserted,
        real.scores_inserted,
    )
    assert dry.addresses_upserted == real.addresses_upserted


# ==============================================================================
# IMPORT -- version-aware refresh of existing messages
# ==============================================================================
#
# When an imported message already exists in the target, the message row is never
# modified, but its derived data (extraction + score) may be refreshed when the
# file carries a *later* ``pipeline_version`` and the derived data differs. These
# tests build a single-message file with a controlled version + derived data and
# import it over a target holding its own version of the same message.

VF = "Shared Folders/vtest"  # version-test folder
VMID = "<v1@example.org>"
# A body wide enough to contain both distinct span extractions used below.
VBODY = "The quick brown fox jumps over the lazy dog near the riverbank."
TEXT_A = "The quick brown fox"
TEXT_B = "jumps over the lazy dog"

V_SCORE_A = {
    "text_sha256": sha256_text("cleaned vA"),
    "fraction_ai": 0.90,
    "fraction_ai_assisted": 0.05,
    "fraction_human": 0.05,
    "label": "AI",
    "detector_version": "3.3.2",
    "raw_response": {"prediction_short": "AI", "variant": "A"},
    "scored_at": "2026-03-03T00:00:00+00:00",
}
V_SCORE_B = {
    "text_sha256": sha256_text("cleaned vB"),
    "fraction_ai": 0.10,
    "fraction_ai_assisted": 0.05,
    "fraction_human": 0.85,
    "label": "Human",
    "detector_version": "3.4.0",
    "raw_response": {"prediction_short": "Human", "variant": "B"},
    "scored_at": "2026-03-04T00:00:00+00:00",
}


def _seed_versioned(
    store,
    *,
    pipeline_version,
    subject="V subject",
    date="2026-03-01T00:00:00+00:00",
    raw_body=VBODY,
    extraction=True,
    extracted_text=TEXT_A,
    method="reply_parser",
    status="ok",
    ext_created_at="2026-03-02T00:00:00+00:00",
    score=None,
    extraction_version=None,
):
    """Put the single message (folder ``VF``, id ``VMID``) into ``store``.

    Every pipeline stage is stamped with ``pipeline_version`` so the message row
    ends up holding exactly that version regardless of which stage ran last.
    ``extraction_version`` sets the extraction's generation independently of it;
    the default leaves the store's own stamp (the running generation) in place.
    """
    lst = store.upsert_list("vtest", VF)
    m = store.upsert_message(
        message_id=VMID,
        list_id=lst.id,
        address_id=None,
        subject=subject,
        date=date,
        in_reply_to=None,
        raw_body=raw_body,
        uid=1,
        fetched_at="2026-03-01T12:00:00+00:00",
        pipeline_version=pipeline_version,
    ).message
    if extraction:
        ext = store.insert_extraction(
            message_id=m.id,
            extracted_text=extracted_text,
            method=method,
            status=status,
            created_at=ext_created_at,
            pipeline_version=pipeline_version,
            extraction_version=extraction_version,
        )
        if score is not None:
            store.insert_score(extraction_id=ext.id, pipeline_version=pipeline_version, **score)
    return m


def _versioned_file(tmp_path, name, **seed_kwargs):
    """Build a one-message export file whose version + derived data we control."""
    with Store(":memory:") as s:
        _seed_versioned(s, **seed_kwargs)
        return _export_all(s, tmp_path, name)


def test_import_later_version_replaces_differing_derived_data(target, tmp_path):
    """(a) File version later + derived data differs -> the message row is kept but
    its extraction + score are replaced with the file's and the version advances."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        subject="TARGET subject",
        date="2026-03-01T00:00:00+00:00",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "later.jsonl",
        pipeline_version="2.0.0",
        subject="FILE subject",
        date="2026-09-09T00:00:00+00:00",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.messages_inserted == 0
    assert summary.body_mismatches == 0
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    # The message row itself stays authoritative (subject/date/body unchanged)...
    m = _messages_by_key(target)[(VF, VMID)]
    assert m["subject"] == "TARGET subject"
    assert m["date"] == "2026-03-01T00:00:00+00:00"
    assert m["raw_body"] == VBODY
    # ...but its pipeline_version advanced to the file's.
    assert m["pipeline_version"] == "2.0.0"

    # Extraction replaced with the file's text/method/status.
    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_B
    assert ext["method"] == "custom"
    assert ext["status"] == "ok"

    # Old score gone; the file's score is the one present.
    sc = _scores_by_key(target)[(VF, VMID)]
    assert sc["label"] == "Human"
    assert sc["text_sha256"] == V_SCORE_B["text_sha256"]
    assert sc["detector_version"] == "3.4.0"


def test_import_later_version_identical_data_bumps_version_only(target, tmp_path):
    """(b) File version later, derived data identical -> only the version stamp advances.

    The later pipeline validated exactly what the target already holds, so the
    message adopts the file's ``pipeline_version``; extraction/score rows are
    untouched and no other message column changes.
    """
    common = dict(
        subject="Same",
        date="2026-03-01T00:00:00+00:00",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    _seed_versioned(target, pipeline_version="1.0.0", **common)
    out = _versioned_file(tmp_path, "identical.jsonl", pipeline_version="2.0.0", **common)

    ext_before = _extractions_by_key(target)
    sc_before = _scores_by_key(target)

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0
    assert summary.versions_bumped == 1

    # Only the stamp moved; derived rows and the rest of the message are intact.
    row = _messages_by_key(target)[(VF, VMID)]
    assert row["pipeline_version"] == "2.0.0"
    assert row["subject"] == "Same"
    assert _extractions_by_key(target) == ext_before
    assert _scores_by_key(target) == sc_before


def test_import_equal_version_different_data_is_pure_skip(target, tmp_path):
    """(c) File version equal to target's -> not later, so skip even when data differs."""
    _seed_versioned(
        target,
        pipeline_version="1.5.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "equal.jsonl",
        pipeline_version="1.5.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0

    # Target's own derived data is untouched.
    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_A
    assert ext["method"] == "reply_parser"
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "AI"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "1.5.0"


def test_import_earlier_version_is_pure_skip(target, tmp_path):
    """(d) File version earlier than target's -> skip, target data wins."""
    _seed_versioned(
        target,
        pipeline_version="2.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "earlier.jsonl",
        pipeline_version="1.0.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 0
    assert summary.scores_updated == 0

    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_A
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "AI"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "2.0.0"


def test_import_updates_when_target_version_is_null_legacy(target, tmp_path):
    """(e) Legacy target row (NULL version) + a real file version -> NULL sorts
    oldest, so the file is 'later' and its differing data replaces the target's."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    # Simulate a row written before the pipeline_version column existed.
    target.conn.execute("UPDATE messages SET pipeline_version = NULL")
    target.conn.commit()
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] is None

    out = _versioned_file(
        tmp_path,
        "legacy.jsonl",
        pipeline_version="1.0.0",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    ext = _extractions_by_key(target)[(VF, VMID)]
    assert ext["extracted_text"] == TEXT_B
    assert ext["method"] == "custom"
    assert _scores_by_key(target)[(VF, VMID)]["label"] == "Human"
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "1.0.0"


def test_import_later_version_null_extraction_clears_target_derived(target, tmp_path):
    """(f) Later file version whose extraction is null -> the later pipeline produced
    nothing, so the target's extraction + score are cleared and the version advances."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(tmp_path, "nullext.jsonl", pipeline_version="2.0.0", extraction=False)

    summary = export_import.import_file(target, out)
    assert summary.messages_skipped == 1
    assert summary.extractions_updated == 1
    assert summary.scores_updated == 1

    # Extraction and score both cleared; the message row survives with the new version.
    assert (VF, VMID) not in _extractions_by_key(target)
    assert (VF, VMID) not in _scores_by_key(target)
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "2.0.0"


def test_import_dry_run_over_update_reports_counts_without_writing(target, tmp_path):
    """(g) A dry-run over an update-triggering file reports the would-be counts but
    leaves the database completely unchanged."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        subject="TARGET subject",
        extracted_text=TEXT_A,
        method="reply_parser",
        score=V_SCORE_A,
    )
    out = _versioned_file(
        tmp_path,
        "dryupdate.jsonl",
        pipeline_version="2.0.0",
        subject="FILE subject",
        extracted_text=TEXT_B,
        method="custom",
        score=V_SCORE_B,
    )

    msgs_before = _messages_by_key(target)
    ext_before = _extractions_by_key(target)
    sc_before = _scores_by_key(target)
    counts_before = _counts(target)

    dry = export_import.import_file(target, out, dry_run=True)
    assert dry.dry_run is True
    assert dry.messages_skipped == 1
    assert dry.extractions_updated == 1
    assert dry.scores_updated == 1

    # The database is untouched by the dry run.
    assert _counts(target) == counts_before
    assert _messages_by_key(target) == msgs_before
    assert _extractions_by_key(target) == ext_before
    assert _scores_by_key(target) == sc_before


# ==============================================================================
# IMPORT -- extraction generation stamps
# ==============================================================================
#
# An imported extraction is stamped with the versions the *file* reports, never
# with the importing build's: ``pipeline_version`` from the message record, and
# ``extraction_version`` from the extraction record, falling back to what that
# release's routine must have been when the file predates the field.


def _without_extraction_version(path, tmp_path, name):
    """Rewrite an export as a pre-generation file: every ``extraction_version``
    key removed (header and extractions), everything else untouched."""
    records = _read_records(path)
    for rec in records:
        rec.pop("extraction_version", None)
        if rec.get("type") == "message" and rec.get("extraction") is not None:
            rec["extraction"].pop("extraction_version", None)
    out = tmp_path / name
    _write_records(out, records)
    return out


def test_import_stamps_version_columns_on_inserted_extractions(source, target, tmp_path):
    """A plain import into a fresh database leaves no NULL version stamp behind.

    The regression this guards: the insert omitted both columns, so every
    imported extraction landed with no provenance and no generation, and the
    staleness check reported the whole import as stale the moment it finished.
    """
    out = _export_all(source, tmp_path, "stamp.jsonl")
    export_import.import_file(target, out)

    stamps = _extraction_versions_by_key(target)
    assert len(stamps) == 5
    for pipeline_version, generation in stamps.values():
        assert pipeline_version == __version__
        assert generation == EXTRACTION_VERSION

    report = staleness.check(target)
    assert report.stale is False
    assert (report.stale_count, report.current_count) == (0, 5)


def test_import_infers_generation_2_from_a_recent_pre_generation_file(target, tmp_path):
    """Old file, no ``extraction_version``: a 1.2.x release derived generation-2 text."""
    versioned = _versioned_file(tmp_path, "old12.jsonl", pipeline_version="1.2.3")
    old = _without_extraction_version(versioned, tmp_path, "old12.plain.jsonl")

    export_import.import_file(target, old)
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("1.2.3", 2)


def test_import_infers_generation_1_from_an_old_pre_generation_file(target, tmp_path):
    """Old file from a pre-1.2 release: generation 1, and so stale on arrival."""
    versioned = _versioned_file(tmp_path, "old10.jsonl", pipeline_version="1.0.5")
    old = _without_extraction_version(versioned, tmp_path, "old10.plain.jsonl")

    export_import.import_file(target, old)
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("1.0.5", 1)
    assert staleness.check(target).stale is True


def test_import_of_pre_generation_file_without_a_version_stamps_null(target, tmp_path):
    """Nothing known about the file's routine -> NULL rather than a guess."""
    versioned = _versioned_file(tmp_path, "oldnull.jsonl", pipeline_version="1.2.3")
    records = _read_records(versioned)
    for rec in records:
        rec.pop("extraction_version", None)
        if rec.get("type") == "message":
            # A message row written before either version column existed.
            rec["pipeline_version"] = None
            rec["extraction"].pop("extraction_version", None)
    old = tmp_path / "oldnull.plain.jsonl"
    _write_records(old, records)

    export_import.import_file(target, old)
    assert _extraction_versions_by_key(target)[(VF, VMID)] == (None, None)


def test_import_roundtrips_the_files_generation_exactly(target, tmp_path):
    """The extraction's own value is authoritative — here it contradicts what the
    message's 1.2.x ``pipeline_version`` would have implied, and it wins."""
    out = _versioned_file(tmp_path, "gen1.jsonl", pipeline_version="1.2.3", extraction_version=1)

    export_import.import_file(target, out)
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("1.2.3", 1)

    # Generation 1 is older than the running routine, so it does not arrive current.
    report = staleness.check(target)
    assert report.stale is True
    assert report.stale_count == 1


def test_import_refresh_stamps_the_replacing_files_versions(target, tmp_path):
    """The refresh path stamps the file's generation on the row it writes."""
    _seed_versioned(
        target,
        pipeline_version="1.0.0",
        extracted_text=TEXT_A,
        method="reply_parser",
        extraction_version=1,
    )
    out = _versioned_file(
        tmp_path,
        "refreshgen.jsonl",
        pipeline_version="2.0.0",
        extracted_text=TEXT_B,
        method="custom",
        extraction_version=2,
    )

    summary = export_import.import_file(target, out)
    assert summary.extractions_updated == 1
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("2.0.0", 2)


def test_import_version_bump_advances_the_extraction_generation(target, tmp_path):
    """Identical derived data from a later, later-generation file: the file's routine
    has demonstrated it produces what the target holds, so the generation advances."""
    common = dict(extracted_text=TEXT_A, method="reply_parser", score=V_SCORE_A)
    _seed_versioned(target, pipeline_version="1.0.0", extraction_version=1, **common)
    out = _versioned_file(
        tmp_path, "bumpgen.jsonl", pipeline_version="2.0.0", extraction_version=2, **common
    )

    summary = export_import.import_file(target, out)
    assert summary.versions_bumped == 1
    assert summary.extractions_updated == 0
    # Only the generation moves; the row's own provenance stamp is left as written.
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("1.0.0", 2)


def test_import_version_bump_never_regresses_the_extraction_generation(target, tmp_path):
    """A later file of an *earlier* generation cannot pull the target's stamp back."""
    common = dict(extracted_text=TEXT_A, method="reply_parser", score=V_SCORE_A)
    _seed_versioned(target, pipeline_version="1.0.0", extraction_version=3, **common)
    out = _versioned_file(
        tmp_path, "bumpdown.jsonl", pipeline_version="2.0.0", extraction_version=1, **common
    )

    summary = export_import.import_file(target, out)
    assert summary.versions_bumped == 1
    assert _extraction_versions_by_key(target)[(VF, VMID)] == ("1.0.0", 3)


def test_import_version_bump_with_no_extraction_on_either_side(target, tmp_path):
    """The bump branch also runs when neither side has an extraction; nothing to stamp."""
    _seed_versioned(target, pipeline_version="1.0.0", extraction=False)
    out = _versioned_file(tmp_path, "bumpnoext.jsonl", pipeline_version="2.0.0", extraction=False)

    summary = export_import.import_file(target, out)
    assert summary.versions_bumped == 1
    assert _extraction_versions_by_key(target) == {}
    assert _messages_by_key(target)[(VF, VMID)]["pipeline_version"] == "2.0.0"


# ==============================================================================
# IMPORT -- error handling (all raise ExportImportError, all roll back)
# ==============================================================================


def _valid_records(source, tmp_path) -> list[dict]:
    return _read_records(_export_all(source, tmp_path, "valid.jsonl"))


def _assert_import_of_path_fails_and_rolls_back(target, path):
    """Assert importing ``path`` raises and leaves ``target`` exactly as it was.

    Returns the raised error, so a caller that cares which failure it got can
    assert on the message.
    """
    before = _counts(target)
    with pytest.raises(export_import.ExportImportError) as exc:
        export_import.import_file(target, path)
    assert _counts(target) == before  # rolled back -- no partial write
    return exc.value


def _assert_import_fails_and_rolls_back(target, records, tmp_path, name="bad.jsonl"):
    """Write ``records`` and assert importing them raises and leaves ``target`` empty."""
    bad = tmp_path / name
    _write_records(bad, records)
    _assert_import_of_path_fails_and_rolls_back(target, bad)


def test_import_rejects_first_record_not_header(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    _assert_import_fails_and_rolls_back(target, records[1:], tmp_path)  # header dropped


def test_import_rejects_wrong_format_name(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[0]["format"] = "not-mlac"
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_wrong_format_version(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[0]["format_version"] = 999
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_malformed_raw_headers_base64(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    message = next(r for r in records if r["type"] == "message" and "raw_headers_b64" in r)
    message["raw_headers_b64"] = "not base64!!"
    bad = tmp_path / "bad-b64.jsonl"
    _write_records(bad, records)
    # The path helper returns the error, so the failure can be pinned to this
    # field rather than to some unrelated validation tripping first.
    err = _assert_import_of_path_fails_and_rolls_back(target, bad)
    assert "raw_headers_b64" in str(err)


def test_import_accepts_a_message_record_without_raw_headers(source, target, tmp_path):
    # Files written before the field existed carry no raw_headers_b64; those
    # messages import with a NULL header block rather than failing.
    records = _valid_records(source, tmp_path)
    for record in records:
        record.pop("raw_headers_b64", None)
    path = tmp_path / "no-headers.jsonl"
    _write_records(path, records)
    export_import.import_file(target, path)
    assert all(m["raw_headers"] is None for m in _messages_by_key(target).values())


def test_import_rejects_missing_trailer(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    assert records[-1]["type"] == "trailer"
    _assert_import_fails_and_rolls_back(target, records[:-1], tmp_path)  # truncated


def test_import_rejects_trailer_count_mismatch(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records[-1]["messages"] = records[-1]["messages"] + 1
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_extraction_sha256_mismatch(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M1:
            rec["extraction"]["sha256"] = "0" * 64  # no longer matches the text
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_corrupted_span_pointer(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M2:
            rec["extraction"]["text"]["start"] += 3  # reconstructs the wrong text
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_unknown_record_type(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    records.insert(-1, {"type": "wat", "surprise": True})  # before the trailer
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_message_referencing_undeclared_folder(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "message" and rec["message_id"] == M1:
            rec["folder"] = "Shared Folders/ghost"  # no list record declares it
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


def test_import_rejects_address_referencing_unknown_person_key(source, target, tmp_path):
    records = _valid_records(source, tmp_path)
    for rec in records:
        if rec.get("type") == "address" and rec.get("person_key") is not None:
            rec["person_key"] = "p9999"  # no such person record
            break
    _assert_import_fails_and_rolls_back(target, records, tmp_path)


# ==============================================================================
# IMPORT -- containers, backward compatibility and corruption
# ==============================================================================
#
# The importer classifies a file by its leading bytes, never by its name, so
# every container these tests build is read the same way whatever it is called.
# gzip and plain are the shapes of exports written before zstd; the exporter can
# no longer produce them, so they are written here directly from a valid record
# list.

_CONTAINER_WRITERS = {
    "gzip": _write_gzip_records,
    "plain": _write_records,
    "zstd": _write_zstd_records,
}

#: Bytes of a file that is neither a container nor text: they carry neither the
#: zstd nor the gzip magic, so detection classifies them as "plain" and they are
#: handed straight to the UTF-8 decoder, which rejects the very first byte. This
#: is the shape of any binary file (the leading bytes here are a JPEG's) given to
#: the importer by mistake, and it exercises the decode failure rather than the
#: CodecError one -- the tests below assert on the message to keep them apart.
_BINARY_JUNK = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x03\x0a\xfe\xff\x80\x81" * 8


def _assert_matches_source(target, source):
    """Assert ``target`` holds exactly the source's messages, extractions and scores."""
    assert _messages_by_key(target) == _messages_by_key(source)
    assert _extractions_by_key(target) == _extractions_by_key(source)
    assert _scores_by_key(target) == _scores_by_key(source)


def test_import_reads_an_old_gzip_export(source, target, tmp_path):
    """Backward compatibility: a gzip export from before zstd still imports in full."""
    old = tmp_path / "old-export.jsonl.gz"
    _write_gzip_records(old, _valid_records(source, tmp_path))
    assert codec.detect(old) == "gzip"

    summary = export_import.import_file(target, old)
    assert summary.messages_inserted == 6
    assert summary.extractions_inserted == 5
    assert summary.scores_inserted == 2
    _assert_matches_source(target, source)


def test_import_reads_an_old_uncompressed_export(source, target, tmp_path):
    """Backward compatibility: an uncompressed export still imports in full."""
    old = tmp_path / "old-export.jsonl"
    _write_records(old, _valid_records(source, tmp_path))
    assert codec.detect(old) == "plain"

    summary = export_import.import_file(target, old)
    assert summary.messages_inserted == 6
    _assert_matches_source(target, source)


@pytest.mark.parametrize(
    ("container", "name"),
    [
        ("zstd", "zstd-under-a-plain-name.jsonl"),
        ("gzip", "gzip-under-a-plain-name.jsonl"),
        ("plain", "plain-under-a-compressed-name.zst"),
    ],
)
def test_import_ignores_the_file_suffix(source, target, tmp_path, container, name):
    """A misleading suffix changes nothing: the content decides how the file is read."""
    path = tmp_path / name
    _CONTAINER_WRITERS[container](path, _valid_records(source, tmp_path))
    assert codec.detect(path) == container

    summary = export_import.import_file(target, path)
    assert summary.messages_inserted == 6
    _assert_matches_source(target, source)


def test_zstd_roundtrip_reproduces_state_and_reimport_is_a_noop(source, target, tmp_path):
    """A default (zstd) export reproduces the source exactly, and importing it
    again changes nothing."""
    out = _export_all(source, tmp_path)
    assert codec.detect(out) == "zstd"

    first = export_import.import_file(target, out)
    assert first.messages_inserted == 6
    _assert_matches_source(target, source)
    assert _person_groups(target) == _person_groups(source)
    assert _pull_states_by_folder(target) == {LAST_CALL: (42, 99)}
    after_first = _counts(target)

    second = export_import.import_file(target, out)
    assert second.messages_inserted == 0
    assert second.messages_skipped == 6
    assert second.extractions_inserted == 0
    assert second.scores_inserted == 0
    assert _counts(target) == after_first
    _assert_matches_source(target, source)


def test_import_rejects_a_truncated_zstd_file(source, target, tmp_path):
    """A cut-short zstd frame is an ExportImportError, not a CodecError leak."""
    good = _export_all(source, tmp_path)
    truncated = tmp_path / "truncated.jsonl.zst"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    _assert_import_of_path_fails_and_rolls_back(target, truncated)


def test_import_rejects_zstd_magic_followed_by_garbage(target, tmp_path):
    """Content that only looks like zstd fails as a bad file, not a crash."""
    fake = tmp_path / "fake.jsonl.zst"
    fake.write_bytes(codec.ZSTD_MAGIC + b"this is not a zstd frame" * 8)

    _assert_import_of_path_fails_and_rolls_back(target, fake)


def test_import_rejects_a_corrupt_gzip_file(source, target, tmp_path):
    """The legacy gzip read path reports corruption as the same one exception type."""
    good = tmp_path / "old.jsonl.gz"
    _write_gzip_records(good, _valid_records(source, tmp_path))
    truncated = tmp_path / "truncated.jsonl.gz"
    truncated.write_bytes(good.read_bytes()[:60])

    _assert_import_of_path_fails_and_rolls_back(target, truncated)


def test_import_rejects_a_non_utf8_binary_file(target, tmp_path):
    """Binary without a container magic is read as plain text and fails cleanly.

    Nothing but zstd and gzip magic marks a file as compressed, so a binary file
    reaches the UTF-8 decoder instead of the codec layer; the decode failure must
    still be an ExportImportError with the target rolled back, not a
    UnicodeDecodeError escaping to the caller.
    """
    junk = tmp_path / "photo.jsonl"
    junk.write_bytes(_BINARY_JUNK)
    assert codec.detect(junk) == "plain"  # no magic: not the CodecError path

    err = _assert_import_of_path_fails_and_rolls_back(target, junk)
    assert "not valid UTF-8" in str(err)  # the decode branch, not a broken container


def test_import_of_a_corrupt_file_leaves_existing_rows_alone(source, target, tmp_path):
    """Rollback protects data already in the target, not just an empty database."""
    export_import.import_file(target, _export_all(source, tmp_path, "first.jsonl"))
    before_messages = _messages_by_key(target)
    before_counts = _counts(target)

    good = _export_all(source, tmp_path, "second.jsonl")
    truncated = tmp_path / "truncated.jsonl.zst"
    truncated.write_bytes(good.read_bytes()[: good.stat().st_size // 2])

    with pytest.raises(export_import.ExportImportError):
        export_import.import_file(target, truncated)
    assert _counts(target) == before_counts
    assert _messages_by_key(target) == before_messages


def test_import_missing_file_still_raises_file_not_found(target, tmp_path):
    """A missing file stays distinguishable from a corrupt one."""
    with pytest.raises(FileNotFoundError):
        export_import.import_file(target, tmp_path / "nope.jsonl.zst")


# ==============================================================================
# IMPORT -- resource limits and hostile input
# ==============================================================================


def _decompressed_lines(path: Path) -> list[str]:
    with codec.open_read_text(path) as fh:
        return fh.read().splitlines(keepends=True)


def test_default_limits_cover_the_records_a_real_store_holds():
    # Largest measured record components sum to about 2.3 million characters.
    assert export_import.MAX_LINE_CHARS >= 10 * 2_400_000
    assert export_import.MAX_TOTAL_CHARS >= 10 * 3_700_000_000


def test_a_normal_export_imports_at_exactly_its_own_sizes(source, target, tmp_path):
    """The limits are inclusive: a file at both limits imports unchanged."""
    path = _export_all(source, tmp_path)
    lines = _decompressed_lines(path)
    longest = max(len(line) - 1 for line in lines)  # characters before the newline
    total = sum(len(line) for line in lines)
    summary = export_import.import_file(target, path, max_line_chars=longest, max_total_chars=total)
    assert summary.messages_inserted == 6
    assert _messages_by_key(target) == _messages_by_key(source)


def test_import_rejects_a_line_over_the_line_limit(source, target, tmp_path):
    path = _export_all(source, tmp_path)
    longest = max(len(line) - 1 for line in _decompressed_lines(path))
    before = _counts(target)
    with pytest.raises(export_import.ExportImportError, match="longer than"):
        export_import.import_file(target, path, max_line_chars=longest - 1)
    assert _counts(target) == before


def test_import_rejects_a_file_over_the_total_limit(source, target, tmp_path):
    path = _export_all(source, tmp_path)
    total = sum(len(line) for line in _decompressed_lines(path))
    before = _counts(target)
    with pytest.raises(export_import.ExportImportError, match="decompressed characters"):
        export_import.import_file(target, path, max_total_chars=total - 1)
    assert _counts(target) == before


def test_a_zstd_bomb_line_is_never_held_in_memory(source, target, tmp_path):
    """A huge newline-free line is rejected after reading only the cap's worth."""
    header = _valid_records(source, tmp_path)[0]
    bomb = tmp_path / "bomb.jsonl.zst"
    line_chars = 32 * 1024 * 1024
    with codec.open_write_text(bomb, compress=True) as fh:
        fh.write(json.dumps(header) + "\n")
        chunk = "a" * (1024 * 1024)
        for _ in range(line_chars // len(chunk)):
            fh.write(chunk)
    assert bomb.stat().st_size < 64 * 1024  # small on disk, large decompressed

    cap = 256 * 1024
    gc.collect()
    tracemalloc.start()
    try:
        baseline = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        with pytest.raises(export_import.ExportImportError, match="longer than"):
            export_import.import_file(target, bomb, max_line_chars=cap)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak - baseline < 8 * 1024 * 1024
    assert _counts(target) == dict.fromkeys(_DATA_TABLES, 0)


def test_import_rejects_deeply_nested_json_as_an_export_error(target, tmp_path):
    nested = tmp_path / "nested.jsonl"
    nested.write_text("[" * 200_000 + "]" * 200_000 + "\n", encoding="utf-8")
    err = _assert_import_of_path_fails_and_rolls_back(target, nested)
    assert "nested too deeply" in str(err)


def test_import_rejects_an_oversized_integer_as_an_export_error(target, tmp_path):
    huge = tmp_path / "huge-int.jsonl"
    huge.write_text('{"type": "header", "format_version": ' + "9" * 10_000 + "}\n")
    _assert_import_of_path_fails_and_rolls_back(target, huge)


@pytest.mark.parametrize(
    "folder",
    [
        'Shared Folders/x" \r\nA1 DELETE "INBOX',
        "Shared Folders/x\nA1 LOGOUT",
        "Shared Folders/x\r",
        "Shared Folders/x\x00",
        "",
        None,
        ["Shared Folders/x"],
    ],
    ids=["crlf", "lf", "cr", "nul", "empty", "null", "array"],
)
def test_import_rejects_a_list_folder_imap_cannot_carry(source, target, tmp_path, folder):
    records = _valid_records(source, tmp_path)
    for record in records:
        if record.get("folder") == ANNOUNCE:
            record["folder"] = folder
    bad = tmp_path / "bad-folder.jsonl"
    _write_records(bad, records)
    err = _assert_import_of_path_fails_and_rolls_back(target, bad)
    assert "list folder" in str(err)


@pytest.mark.parametrize(
    "folder",
    ["Shared Folders/itu+ietf", "Shared Folders/some-list.name_2", 'Shared Folders/a"b\\'],
    ids=["plus", "dot-dash-underscore", "quote-backslash"],
)
def test_import_accepts_folders_quote_folder_can_carry(source, target, tmp_path, folder):
    records = _valid_records(source, tmp_path)
    for record in records:
        if record.get("folder") == ANNOUNCE:
            record["folder"] = folder
    path = tmp_path / "ok.jsonl"
    _write_records(path, records)
    export_import.import_file(target, path)
    folders = {row["folder"] for row in target.conn.execute("SELECT folder FROM lists")}
    assert folders == {folder, LAST_CALL}


# ==============================================================================
# CLI
# ==============================================================================


def _build_source_db(path: Path) -> None:
    with Store(path) as s:
        _build_source(s)


def test_export_main_happy_path(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    rc = export_main(["announce", "last-call", "-o", str(out), "--db", str(src)])
    assert rc == 0
    records = _read_records(codec.compressed_path(out))
    assert records[0]["type"] == "header"
    assert len(_message_by_id(records)) == 6


def test_export_main_all_lists(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-all.jsonl"
    rc = export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0
    records = _read_records(codec.compressed_path(out))
    assert {r["folder"] for r in _messages_of_type(records, "list")} == {
        ANNOUNCE,
        LAST_CALL,
    }


def test_export_main_compresses_by_default(tmp_path):
    """The default CLI run writes zstd to '<output>.zst'; the bare path is unused."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    rc = export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0

    written = codec.compressed_path(out)
    assert written.exists()
    assert not out.exists()
    with open(written, "rb") as fh:
        assert fh.read(4) == codec.ZSTD_MAGIC


def test_export_main_no_compress_writes_plain_to_the_given_path(tmp_path):
    """--no-compress writes uncompressed JSON Lines and appends no suffix."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    rc = export_main(["--all-lists", "-o", str(out), "--db", str(src), "--no-compress"])
    assert rc == 0

    assert out.exists()
    assert not codec.compressed_path(out).exists()
    assert codec.detect(out) == "plain"
    assert _read_records(out)[0]["type"] == "header"


def test_export_main_logs_the_path_actually_written(tmp_path, caplog):
    """The summary line names the compressed file on disk, not the -o argument."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    written = codec.compressed_path(out)

    with caplog.at_level(logging.INFO, logger="mailing_list_ai_check.export"):
        assert export_main(["--all-lists", "-o", str(out), "--db", str(src)]) == 0
    assert f"path={written}" in caplog.text


def test_export_main_no_compress_logs_the_given_path(tmp_path, caplog):
    """Without compression the logged path is the one the caller asked for."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"

    with caplog.at_level(logging.INFO, logger="mailing_list_ai_check.export"):
        assert export_main(["--all-lists", "-o", str(out), "--db", str(src), "--no-compress"]) == 0
    assert f"path={out}" in caplog.text
    assert f"path={out}{codec.COMPRESSED_SUFFIX}" not in caplog.text


def test_export_main_applies_a_date_range(tmp_path):
    """--date-from / --date-to reach the exporter, and the header records them."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-range.jsonl"
    rc = export_main(
        [
            "--all-lists",
            "-o",
            str(out),
            "--db",
            str(src),
            "--date-from",
            "2026-01-10",
            "--date-to",
            "2026-01-20T23:59:59+00:00",
        ]
    )
    assert rc == 0
    records = _read_records(codec.compressed_path(out))
    assert set(_message_by_id(records)) == {M2, M3, M4}
    assert (records[0]["date_from"], records[0]["date_to"]) == (
        "2026-01-10",
        "2026-01-20T23:59:59+00:00",
    )


def test_export_main_accepts_one_bound_alone(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-from.jsonl"
    assert (
        export_main(["--all-lists", "-o", str(out), "--db", str(src), "--date-from", "2026-02-01"])
        == 0
    )
    records = _read_records(codec.compressed_path(out))
    assert set(_message_by_id(records)) == {M5}
    assert "date_to" not in records[0]


@pytest.mark.parametrize("flag", ["--date-from", "--date-to"], ids=["from", "to"])
def test_export_main_rejects_an_unparseable_date(tmp_path, flag):
    """A typo is a usage error, not a silently wrong lexical range."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        export_main(
            ["--all-lists", "-o", str(tmp_path / "x.jsonl"), "--db", str(src), flag, "last week"]
        )
    assert exc.value.code == 2


def test_export_main_rejects_names_and_all_lists(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        export_main(["announce", "--all-lists", "-o", str(tmp_path / "x.jsonl"), "--db", str(src)])
    assert exc.value.code == 2


def test_export_main_rejects_neither(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        export_main(["-o", str(tmp_path / "x.jsonl"), "--db", str(src)])
    assert exc.value.code == 2


@pytest.mark.parametrize("extra_args", [[], ["--no-compress"]], ids=["zstd", "plain"])
def test_import_main_happy_path(tmp_path, extra_args):
    """The CLI reads back both the compressed and the uncompressed export."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    export_main(["--all-lists", "-o", str(out), "--db", str(src), *extra_args])
    written = out if extra_args else codec.compressed_path(out)

    target = tmp_path / "target.db"
    rc = import_main([str(written), "--db", str(target)])
    assert rc == 0
    with Store(target) as t, Store(src) as s:
        assert _messages_by_key(t) == _messages_by_key(s)


def test_import_main_dry_run_writes_nothing(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli.jsonl"
    export_main(["--all-lists", "-o", str(out), "--db", str(src)])

    target = tmp_path / "target.db"
    rc = import_main([str(codec.compressed_path(out)), "--db", str(target), "--dry-run"])
    assert rc == 0
    with Store(target) as t:
        assert _counts(t)["messages"] == 0


def test_import_main_returns_1_on_corrupt_file(tmp_path):
    bad = tmp_path / "corrupt.jsonl"
    bad.write_text('{"type": "list", "folder": "x"}\n', encoding="utf-8")  # no header
    target = tmp_path / "target.db"
    rc = import_main([str(bad), "--db", str(target)])
    assert rc == 1
    with Store(target) as t:
        assert _counts(t)["messages"] == 0


def test_import_main_returns_1_on_a_corrupt_compressed_file(tmp_path):
    """A broken container is as much a bad file as a broken record: rc 1, not a crash."""
    bad = tmp_path / "corrupt.jsonl.zst"
    bad.write_bytes(codec.ZSTD_MAGIC + b"not a zstd frame" * 8)
    target = tmp_path / "target.db"
    rc = import_main([str(bad), "--db", str(target)])
    assert rc == 1
    with Store(target) as t:
        assert _counts(t)["messages"] == 0


def test_import_main_returns_1_on_a_non_utf8_binary_file(tmp_path, caplog):
    """A binary file the CLI is pointed at is a logged error, not a traceback."""
    bad = tmp_path / "photo.jsonl"
    bad.write_bytes(_BINARY_JUNK)
    target = tmp_path / "target.db"

    with caplog.at_level(logging.ERROR, logger="mailing_list_ai_check.import"):
        rc = import_main([str(bad), "--db", str(target)])
    assert rc == 1
    assert "import failed" in caplog.text
    assert "not valid UTF-8" in caplog.text  # the decode branch, not a broken container
    with Store(target) as t:
        assert _counts(t)["messages"] == 0


# ==============================================================================
# EXPORT -- streaming regression guard
# ==============================================================================
#
# export_lists must write each message record as its row is read, so that peak
# memory is a function of the largest single message and not of how much mail is
# being exported. The guard below exports a synthetic database whose bodies
# dominate it and asserts that the exporter's own peak allocation stays far
# below the total body volume -- which it cannot do if the records are collected
# before being written.
#
# Why tracemalloc, and not the alternatives:
#
# - tracemalloc counts bytes this interpreter allocates for Python objects. It
#   is unaffected by wall-clock time, machine load, other processes, and the
#   test session's history (reset_peak gives a clean baseline), so the bound
#   below fails only on a real change in allocation behaviour. SQLite holds the
#   source database in C memory it does not trace, which keeps the baseline
#   small and the signal -- the row strings and record dicts crossing into
#   Python -- undiluted.
# - Peak RSS (resource.getrusage) is a process-lifetime high-water mark that
#   cannot be reset, includes every allocator arena the rest of the suite has
#   already touched, and is subject to the OS's paging decisions: on a loaded CI
#   machine it is exactly the kind of measurement that flakes.
# - "The output file grows before export_lists returns" needs a timing or
#   ordering hook, and would fail on an implementation that streams correctly
#   but buffers its writes a little more eagerly.
# - Instrumenting the store cursor to prove no fetchall() of message rows binds
#   the test to today's SQL calls rather than to the property that matters.
#
# Sizing: 120 bodies of 250,000 characters is ~30 MB of mail built in well under
# a second and never written to disk uncompressed. Streaming holds roughly one
# body at a time (measured ~1 MB peak); buffering would hold all of them. The
# bound is a quarter of the body volume, an order of magnitude above the
# streaming figure and well below the buffering one.

_STREAM_MESSAGES = 120
_STREAM_BODY_CHARS = 250_004  # a multiple of 4, the repeated unit's length
_STREAM_TOTAL_CHARS = _STREAM_MESSAGES * _STREAM_BODY_CHARS
_STREAM_PEAK_LIMIT = _STREAM_TOTAL_CHARS // 4


def _build_bulky_source(store: Store) -> None:
    """Populate ``store`` with many large-bodied messages and nothing else."""
    lst = store.upsert_list("bulk", "Shared Folders/bulk")
    for i in range(_STREAM_MESSAGES):
        store.upsert_message(
            message_id=f"<bulk{i}@example.org>",
            list_id=lst.id,
            address_id=None,
            subject=f"Bulk {i}",
            date="2026-04-01T00:00:00+00:00",
            in_reply_to=None,
            raw_body=f"{i:04d}" * (_STREAM_BODY_CHARS // 4),
            uid=i,
            fetched_at="2026-04-02T00:00:00+00:00",
        )


def test_export_streams_without_buffering_messages(tmp_path):
    """Peak allocation during an export is independent of how much mail it holds."""
    with Store(":memory:") as store:
        _build_bulky_source(store)
        gc.collect()

        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            summary = export_import.export_lists(
                store, None, tmp_path / "bulk.jsonl", all_lists=True
            )
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    assert summary.messages == _STREAM_MESSAGES
    assert Path(summary.path).exists()
    assert peak - baseline < _STREAM_PEAK_LIMIT, (
        f"export allocated {peak - baseline} bytes at peak for "
        f"{_STREAM_TOTAL_CHARS} bytes of message bodies; the exporter is "
        f"buffering records instead of streaming them"
    )
