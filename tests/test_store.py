"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import json
import sqlite3

import pytest

from mailing_list_ai_check import __version__
from email_reply_extractor.extraction import EXTRACTION_VERSION
from mailing_list_ai_check.store import (
    EXTRACTION_STATUSES,
    Store,
    apply_migrations,
    dmarc_rewrite_original,
    extraction_version_for_app_version,
    sha256_text,
    version_key,
)


@pytest.fixture
def store(tmp_path):
    """A Store backed by a fresh temp-file database."""
    db = tmp_path / "test.db"
    with Store(db) as s:
        yield s


# --- migrations ---------------------------------------------------------------


def test_migrations_create_schema(store):
    tables = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "schema_version",
        "lists",
        "pull_state",
        "persons",
        "addresses",
        "messages",
        "extractions",
        "scores",
    } <= tables


def test_migrations_are_idempotent(tmp_path):
    db = tmp_path / "idempotent.db"
    with Store(db) as s:
        version_before = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[
            "v"
        ]
        row_count_before = s.conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()[
            "c"
        ]
        # Running again must be a no-op: no new version rows, no errors.
        apply_migrations(s.conn)
        apply_migrations(s.conn)
        version_after = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()[
            "v"
        ]
        row_count_after = s.conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert version_before == version_after == 19
    assert row_count_before == row_count_after == 19


def test_reopening_database_is_a_noop(tmp_path):
    db = tmp_path / "reopen.db"
    with Store(db) as s:
        s.upsert_list("announce", "Shared Folders/announce")
    # Reopen: migrations must not re-run or duplicate data.
    with Store(db) as s:
        rows = s.conn.execute("SELECT COUNT(*) AS c FROM lists").fetchone()["c"]
        version = s.conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    assert rows == 1
    assert version == 19


def test_migration_013_undoes_the_003_rebadge(store):
    """Replaying 003 and 013 together leaves every label as the API returned it."""
    list_id = store.upsert_list("tls", "Shared Folders/tls").id
    addr_id = store.upsert_address("a@example.org", "A").id
    for i, (fractions, expected) in enumerate(
        [
            ((0.0, 1.0, 0.0), "AI-Assisted"),  # fully assisted, mislabeled Mixed
            ((0.63, 0.0, 0.37), "Mixed"),  # genuine AI/human mix stays
            ((0.28, 0.34, 0.38), "Mixed"),  # human-dominant mix stays
        ]
    ):
        msg = store.upsert_message(
            message_id=f"<m{i}@test>",
            list_id=list_id,
            address_id=addr_id,
            subject=f"s{i}",
            date="2026-07-14T00:00:00+00:00",
            in_reply_to=None,
            raw_body="body",
            uid=None,
        ).message
        extraction = store.insert_extraction(
            message_id=msg.id, extracted_text=f"text {i}", method="m", status="ok"
        )
        ai, assisted, human = fractions
        store.insert_score(
            extraction_id=extraction.id,
            text_sha256=sha256_text(f"text {i}"),
            fraction_ai=ai,
            fraction_ai_assisted=assisted,
            fraction_human=human,
            label="Mixed",
            detector_version="v3",
            raw_response={"prediction_short": "Mixed"},
        )
    # Rewind to pre-003 and re-apply so both backfills run over the rows: 003
    # rebadges assisted-dominant Mixed, then 013 restores the verbatim label.
    # Drop the columns/indexes added by 004..011 and 014 and the table added by
    # 012 so those migrations re-apply cleanly alongside 003 and 013.
    store.conn.execute("DELETE FROM schema_version WHERE version >= 3")
    store.conn.execute("ALTER TABLE messages DROP COLUMN raw_html")
    store.conn.execute("ALTER TABLE lists DROP COLUMN last_message_at")
    store.conn.execute("DROP INDEX idx_messages_message_id")
    store.conn.execute("ALTER TABLE messages DROP COLUMN pipeline_version")
    store.conn.execute("ALTER TABLE extractions DROP COLUMN pipeline_version")
    store.conn.execute("ALTER TABLE extractions DROP COLUMN extraction_version")
    store.conn.execute("DROP INDEX idx_messages_timing")
    store.conn.execute("ALTER TABLE messages DROP COLUMN timing")
    store.conn.execute("DROP INDEX idx_messages_timing_cpm")
    store.conn.execute("ALTER TABLE messages DROP COLUMN timing_cpm")
    store.conn.execute("DROP TABLE app_settings")
    store.conn.execute("DROP INDEX IF EXISTS idx_messages_address_list_autogen")
    store.conn.execute("DROP INDEX IF EXISTS idx_messages_address_from_name")
    store.conn.execute("ALTER TABLE messages DROP COLUMN auto_generated")
    store.conn.execute("ALTER TABLE messages DROP COLUMN from_name")
    store.conn.execute("ALTER TABLE messages DROP COLUMN raw_headers")
    store.conn.execute("ALTER TABLE extractions DROP COLUMN part_starts")
    store.conn.execute("ALTER TABLE scores DROP COLUMN part_check_status")
    store.conn.execute("ALTER TABLE scores DROP COLUMN part_check")
    apply_migrations(store.conn)
    labels = [
        row["label"]
        for row in store.conn.execute("SELECT label FROM scores ORDER BY extraction_id")
    ]
    assert labels == ["Mixed", "Mixed", "Mixed"]


def test_migration_004_adds_raw_html_column(store):
    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "raw_html" in cols


def test_migration_004_present_on_migrated_db(tmp_path):
    # A database rewound to pre-004 gains the raw_html column on re-open/migrate.
    # last_message_at (005), the message_id index (006), the two
    # pipeline_version columns (007/008), the two timing columns (009/010),
    # extraction_version (011) and the app_settings table (012) are dropped too
    # so they re-apply cleanly.
    db = tmp_path / "migrated.db"
    with Store(db) as s:
        s.conn.execute("DELETE FROM schema_version WHERE version >= 4")
        s.conn.execute("ALTER TABLE messages DROP COLUMN raw_html")
        s.conn.execute("ALTER TABLE lists DROP COLUMN last_message_at")
        s.conn.execute("DROP INDEX idx_messages_message_id")
        s.conn.execute("ALTER TABLE messages DROP COLUMN pipeline_version")
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
        cols = {row["name"] for row in s.conn.execute("PRAGMA table_info(messages)").fetchall()}
        version = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert "raw_html" in cols
    assert version == 19


def test_migration_005_adds_last_message_at_column(store):
    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(lists)").fetchall()}
    assert "last_message_at" in cols


def test_migration_005_present_on_migrated_db(tmp_path):
    # A database rewound to pre-005 gains the last_message_at column on migrate.
    # The message_id index (006), the two pipeline_version columns (007/008),
    # the two timing columns (009/010), extraction_version (011) and the
    # app_settings table (012) are dropped too so they re-apply cleanly.
    db = tmp_path / "migrated005.db"
    with Store(db) as s:
        s.conn.execute("DELETE FROM schema_version WHERE version >= 5")
        s.conn.execute("ALTER TABLE lists DROP COLUMN last_message_at")
        s.conn.execute("DROP INDEX idx_messages_message_id")
        s.conn.execute("ALTER TABLE messages DROP COLUMN pipeline_version")
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
        cols = {row["name"] for row in s.conn.execute("PRAGMA table_info(lists)").fetchall()}
        version = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert "last_message_at" in cols
    assert version == 19


def test_migration_006_present_on_migrated_db(tmp_path):
    # A database rewound to pre-006 gains the message_id index on migrate. The
    # two pipeline_version columns (007/008), the two timing columns (009/010),
    # extraction_version (011) and the app_settings table (012) are dropped too
    # so they re-apply cleanly.
    db = tmp_path / "migrated006.db"
    with Store(db) as s:
        s.conn.execute("DELETE FROM schema_version WHERE version >= 6")
        s.conn.execute("DROP INDEX idx_messages_message_id")
        s.conn.execute("ALTER TABLE messages DROP COLUMN pipeline_version")
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
        indexes = {
            row["name"]
            for row in s.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        version = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert "idx_messages_message_id" in indexes
    assert version == 19


def test_expected_indexes_exist(store):
    indexes = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
    }
    for expected in (
        "idx_messages_date",
        "idx_messages_address_id",
        "idx_messages_list_id",
        "idx_messages_message_id",
        "idx_extractions_status",
        "idx_scores_text_sha256",
        "idx_scores_label",
    ):
        assert expected in indexes, f"missing index {expected}"


def test_pragmas_enabled(store):
    fk = store.conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert fk == 1
    assert journal.lower() == "wal"


# --- lists --------------------------------------------------------------------


def test_upsert_list_is_idempotent(store):
    a = store.upsert_list("announce", "Shared Folders/announce")
    b = store.upsert_list("announce", "Shared Folders/announce")
    assert a == b
    assert store.conn.execute("SELECT COUNT(*) AS c FROM lists").fetchone()["c"] == 1


def test_set_list_synced(store):
    lst = store.upsert_list("quic", "Shared Folders/quic")
    assert store.get_list(lst.id).last_synced_at is None
    store.set_list_synced(lst.id, "2026-07-21T00:00:00+00:00")
    assert store.get_list(lst.id).last_synced_at == "2026-07-21T00:00:00+00:00"


def test_set_list_last_message_roundtrip(store):
    lst = store.upsert_list("quic", "Shared Folders/quic")
    assert store.get_list(lst.id).last_message_at is None
    store.set_list_last_message(lst.id, "2026-07-20T09:30:00+00:00")
    assert store.get_list(lst.id).last_message_at == "2026-07-20T09:30:00+00:00"
    # None clears it back (folder empty / never checked).
    store.set_list_last_message(lst.id, None)
    assert store.get_list(lst.id).last_message_at is None


def test_list_rows_includes_last_message_at(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    store.set_list_last_message(lst.id, "2026-07-19T00:00:00+00:00")
    row = next(r for r in store.list_rows() if r["name"] == "announce")
    assert row["last_message_at"] == "2026-07-19T00:00:00+00:00"


def test_list_rows_earliest_message_at_ignores_undated_messages(store):
    """The earliest stamp is the oldest usable ``messages.date``, blanks excluded."""
    lst = store.upsert_list("announce", "Shared Folders/announce")
    for message_id, date in (
        ("<m1@x>", "2026-03-01T09:00:00+00:00"),
        ("<m2@x>", None),  # no Date header at all
        ("<m3@x>", ""),  # stored blank must not sort ahead of a real date
        ("<m4@x>", "2026-02-01T09:00:00+00:00"),
    ):
        store.upsert_message(
            message_id=message_id,
            list_id=lst.id,
            address_id=None,
            subject="s",
            date=date,
            in_reply_to=None,
            raw_body="body",
            uid=None,
        )
    row = next(r for r in store.list_rows() if r["name"] == "announce")
    assert row["message_count"] == 4
    assert row["earliest_message_at"] == "2026-02-01T09:00:00+00:00"


def test_tracked_list_folders_only_lists_with_messages(store):
    with_msg = store.upsert_list("announce", "Shared Folders/announce")
    store.upsert_list("empty", "Shared Folders/empty")  # no messages -> excluded
    store.upsert_message(
        message_id="<m1@x>",
        list_id=with_msg.id,
        address_id=None,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body="body",
        uid=1,
    )
    assert store.tracked_list_folders() == [(with_msg.id, "Shared Folders/announce")]


def test_tracked_list_folders_excludes_server_removed(store):
    gone = store.upsert_list("gone", "Shared Folders/gone")
    store.upsert_message(
        message_id="<m2@x>",
        list_id=gone.id,
        address_id=None,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body="body",
        uid=1,
    )
    # Has messages but the server no longer carries it -> excluded.
    store.refresh_lists_index(_entries("announce"))
    assert store.get_list(gone.id).removed_from_server_at is not None
    assert store.tracked_list_folders() == []


def _entries(*names):
    return [(name, f"Shared Folders/{name}") for name in names]


def test_refresh_lists_index_populates_fresh_store(store):
    counts = store.refresh_lists_index(_entries("announce", "quic", "last-call"))
    assert counts == {"added": 3, "restored": 0, "deleted": 0, "kept_missing": 0, "total": 3}
    names = [row["name"] for row in store.list_rows()]
    assert names == ["announce", "last-call", "quic"]
    assert all(row["removed_from_server_at"] is None for row in store.list_rows())


def test_refresh_lists_index_is_idempotent(store):
    store.refresh_lists_index(_entries("announce", "quic"))
    counts = store.refresh_lists_index(_entries("announce", "quic"))
    assert counts == {"added": 0, "restored": 0, "deleted": 0, "kept_missing": 0, "total": 2}


def test_refresh_lists_index_deletes_missing_list_without_messages(store):
    store.upsert_list("gone", "Shared Folders/gone")
    counts = store.refresh_lists_index(_entries("announce"))
    assert counts["deleted"] == 1
    assert counts["total"] == 1
    assert [row["name"] for row in store.list_rows()] == ["announce"]


def test_refresh_lists_index_keeps_and_stamps_missing_list_with_messages(store):
    lst = store.upsert_list("gone", "Shared Folders/gone")
    store.upsert_message(
        message_id="<m1@example.org>",
        list_id=lst.id,
        address_id=None,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body="body",
        uid=1,
    )
    counts = store.refresh_lists_index(_entries("announce"))
    assert counts == {"added": 1, "restored": 0, "deleted": 0, "kept_missing": 1, "total": 2}
    kept = store.get_list(lst.id)
    assert kept is not None
    assert kept.removed_from_server_at is not None
    # A repeat refresh must not re-stamp (the timestamp is stable) or delete it.
    stamp = kept.removed_from_server_at
    counts = store.refresh_lists_index(_entries("announce"))
    assert counts["kept_missing"] == 0
    assert store.get_list(lst.id).removed_from_server_at == stamp


def test_refresh_lists_index_restores_reappearing_list(store):
    lst = store.upsert_list("back", "Shared Folders/back")
    store.upsert_message(
        message_id="<m2@example.org>",
        list_id=lst.id,
        address_id=None,
        subject="s",
        date=None,
        in_reply_to=None,
        raw_body="body",
        uid=1,
    )
    store.refresh_lists_index(_entries("announce"))
    assert store.get_list(lst.id).removed_from_server_at is not None

    counts = store.refresh_lists_index(_entries("announce", "back"))
    assert counts["restored"] == 1
    assert store.get_list(lst.id).removed_from_server_at is None


# --- pull_state ---------------------------------------------------------------


def test_pull_state_roundtrip_and_uidvalidity_change(store):
    lst = store.upsert_list("last-call", "Shared Folders/last-call")
    assert store.get_pull_state(lst.id) is None

    store.set_pull_state(lst.id, uidvalidity=1571671002, last_uid=100)
    ps = store.get_pull_state(lst.id)
    assert (ps.uidvalidity, ps.last_uid) == (1571671002, 100)

    # Advance last_uid within the same UIDVALIDITY.
    store.set_pull_state(lst.id, uidvalidity=1571671002, last_uid=250)
    assert store.get_pull_state(lst.id).last_uid == 250

    # UIDVALIDITY change -> caller resyncs and overwrites the cursor.
    store.set_pull_state(lst.id, uidvalidity=9999999999, last_uid=5)
    ps = store.get_pull_state(lst.id)
    assert (ps.uidvalidity, ps.last_uid) == (9999999999, 5)
    # Still exactly one cursor row for the list.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM pull_state").fetchone()["c"] == 1


# --- list lookup / uid bounds -------------------------------------------------


def test_get_list_by_name(store):
    created = store.upsert_list("announce", "Shared Folders/announce")
    assert store.get_list_by_name("announce").id == created.id
    assert store.get_list_by_name("nope") is None


def test_min_and_max_uid_for_list_ignore_null_uids(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    # No messages yet -> both None.
    assert store.min_uid_for_list(lst.id) is None
    assert store.max_uid_for_list(lst.id) is None

    for mid, uid in (("<a@x>", 10), ("<b@x>", 4), ("<c@x>", 7), ("<d@x>", None)):
        _add_message(store, lst.id, addr.id, message_id=mid, uid=uid)

    assert store.min_uid_for_list(lst.id) == 4
    assert store.max_uid_for_list(lst.id) == 10


def test_uid_bounds_are_scoped_per_list(store):
    a = store.upsert_list("announce", "Shared Folders/announce")
    b = store.upsert_list("quic", "Shared Folders/quic")
    addr = store.upsert_address("a@x.org")
    _add_message(store, a.id, addr.id, message_id="<a@x>", uid=5)
    _add_message(store, b.id, addr.id, message_id="<b@x>", uid=50)
    assert store.max_uid_for_list(a.id) == 5
    assert store.min_uid_for_list(b.id) == 50


# --- addresses ----------------------------------------------------------------


def test_upsert_address_normalizes_and_dedupes(store):
    a = store.upsert_address("Alice@Example.ORG ", "Alice")
    b = store.upsert_address("alice@example.org")
    assert a.email == "alice@example.org"
    assert a.id == b.id
    assert store.conn.execute("SELECT COUNT(*) AS c FROM addresses").fetchone()["c"] == 1


def test_upsert_address_backfills_display_name(store):
    a = store.upsert_address("bob@example.org", None)
    assert a.display_name is None
    b = store.upsert_address("bob@example.org", "Bob Builder")
    assert b.display_name == "Bob Builder"


# --- messages -----------------------------------------------------------------


def _add_message(store, list_id, address_id, message_id="<m1@x>", **kw):
    defaults = dict(
        message_id=message_id,
        list_id=list_id,
        address_id=address_id,
        subject="Subject",
        date="2026-07-01T12:00:00+00:00",
        in_reply_to=None,
        raw_body="body",
        uid=1,
    )
    defaults.update(kw)
    return store.upsert_message(**defaults)


def test_upsert_message_dedupes_on_list_and_message_id(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")

    first = _add_message(store, lst.id, addr.id)
    assert first.inserted is True

    second = _add_message(store, lst.id, addr.id, subject="changed")
    assert second.inserted is False
    assert second.message.id == first.message.id
    # Existing row is unchanged (upsert is a no-op on duplicate).
    assert second.message.subject == "Subject"
    assert store.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"] == 1


def test_same_message_id_allowed_across_lists(store):
    a = store.upsert_list("announce", "Shared Folders/announce")
    b = store.upsert_list("quic", "Shared Folders/quic")
    addr = store.upsert_address("a@x.org")
    r1 = _add_message(store, a.id, addr.id, message_id="<dup@x>")
    r2 = _add_message(store, b.id, addr.id, message_id="<dup@x>")
    assert r1.inserted and r2.inserted
    assert r1.message.id != r2.message.id


def test_upsert_message_stores_raw_html_on_insert(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<h@x>", raw_html="<p>hi</p>")
    assert up.inserted is True
    assert store.get_message(up.message.id).raw_html == "<p>hi</p>"


def test_upsert_message_conflict_leaves_raw_html_untouched(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    _add_message(store, lst.id, addr.id, message_id="<h@x>", raw_html="<p>first</p>")
    # A re-pull with different raw_html is a no-op; the stored value is kept.
    second = _add_message(store, lst.id, addr.id, message_id="<h@x>", raw_html="<p>second</p>")
    assert second.inserted is False
    assert store.get_message(second.message.id).raw_html == "<p>first</p>"


def test_upsert_message_stores_its_own_from_name(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org", "Alice")
    up = _add_message(store, lst.id, addr.id, message_id="<n@x>", from_name="Alice Smith")
    assert store.get_message(up.message.id).from_name == "Alice Smith"


def test_from_name_is_per_message_not_per_address(store):
    # The regression this column exists for: one address presenting a different
    # name on each message (a notification sender such as noreply@github.com).
    # The address keeps the first name it was seen with; each message keeps its own.
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("noreply@github.com", "First Person")
    store.upsert_address("noreply@github.com", "Second Person")

    _add_message(store, lst.id, addr.id, message_id="<g1@x>", uid=1, from_name="First Person")
    _add_message(store, lst.id, addr.id, message_id="<g2@x>", uid=2, from_name="Second Person")

    assert store.get_address(addr.id).display_name == "First Person"
    rows = store.conn.execute("SELECT from_name FROM messages ORDER BY uid").fetchall()
    assert [r["from_name"] for r in rows] == ["First Person", "Second Person"]


def test_upsert_message_conflict_leaves_from_name_untouched(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    _add_message(store, lst.id, addr.id, message_id="<n@x>", from_name="Original")
    second = _add_message(store, lst.id, addr.id, message_id="<n@x>", from_name="Replacement")
    assert second.inserted is False
    assert store.get_message(second.message.id).from_name == "Original"


def test_upsert_message_from_name_defaults_to_null(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<n@x>")
    assert store.get_message(up.message.id).from_name is None


def test_upsert_message_stores_raw_headers_as_bytes(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<h@x>", raw_headers=b"From: a@x.org\r\n")
    assert store.get_message(up.message.id).raw_headers == b"From: a@x.org\r\n"


def test_set_message_headers_fills_only_a_null_from_name(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    blank = _add_message(store, lst.id, addr.id, message_id="<b@x>", uid=1)
    named = _add_message(store, lst.id, addr.id, message_id="<n@x>", uid=2, from_name="Kept")

    store.set_message_headers(blank.message.id, b"From: X <a@x.org>\r\n", from_name="Recovered")
    store.set_message_headers(named.message.id, b"From: Y <a@x.org>\r\n", from_name="Ignored")

    assert store.get_message(blank.message.id).from_name == "Recovered"
    assert store.get_message(named.message.id).from_name == "Kept"
    # Headers are written in both cases; raw_body is never touched.
    assert store.get_message(named.message.id).raw_headers == b"From: Y <a@x.org>\r\n"
    assert store.get_message(named.message.id).raw_body == "body"


def test_iter_messages_missing_headers_skips_the_tombstone(store):
    # A message the server returned no headers for is stamped b"" so a capped
    # backfill run drains its queue instead of re-fetching it forever.
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    pending = _add_message(store, lst.id, addr.id, message_id="<p@x>", uid=1)
    tombstoned = _add_message(store, lst.id, addr.id, message_id="<t@x>", uid=2)
    store.set_message_headers(tombstoned.message.id, b"")

    assert [m.id for m in store.iter_messages_missing_headers(lst.id)] == [pending.message.id]


def test_iter_messages_missing_headers_needs_a_uid(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    _add_message(store, lst.id, addr.id, message_id="<u@x>", uid=None)
    assert list(store.iter_messages_missing_headers(lst.id)) == []


def test_set_message_raw_html_backfills_without_touching_body(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<b@x>", raw_body="the body").message
    assert store.get_message(m.id).raw_html is None
    store.set_message_raw_html(m.id, "<p>backfilled</p>")
    refreshed = store.get_message(m.id)
    assert refreshed.raw_html == "<p>backfilled</p>"
    assert refreshed.raw_body == "the body"  # body untouched


def test_iter_messages_missing_html_orders_by_uid_and_excludes_present(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    # uid 3 missing html, uid 1 missing html, uid 2 already has html, uid None skipped.
    _add_message(store, lst.id, addr.id, message_id="<u3@x>", uid=3)
    _add_message(store, lst.id, addr.id, message_id="<u1@x>", uid=1)
    _add_message(store, lst.id, addr.id, message_id="<u2@x>", uid=2, raw_html="<p>has</p>")
    _add_message(store, lst.id, addr.id, message_id="<none@x>", uid=None)

    missing = list(store.iter_messages_missing_html(lst.id))
    assert [m.uid for m in missing] == [1, 3]  # uid order, present + null excluded


def test_iter_messages_missing_html_excludes_empty_tombstone(store):
    # The backfill stamps "" for messages with no HTML part; that tombstone must
    # be treated as "checked, absent" and excluded, not re-queued like NULL.
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    _add_message(store, lst.id, addr.id, message_id="<null@x>", uid=1)
    m2 = _add_message(store, lst.id, addr.id, message_id="<tomb@x>", uid=2).message
    store.set_message_raw_html(m2.id, "")  # tombstone

    missing = list(store.iter_messages_missing_html(lst.id))
    assert [m.uid for m in missing] == [1]  # tombstoned row excluded


def test_iter_messages_missing_html_scoped_to_list(store):
    a = store.upsert_list("announce", "Shared Folders/announce")
    b = store.upsert_list("quic", "Shared Folders/quic")
    addr = store.upsert_address("a@x.org")
    _add_message(store, a.id, addr.id, message_id="<a@x>", uid=1)
    _add_message(store, b.id, addr.id, message_id="<b@x>", uid=1)
    assert [m.message_id for m in store.iter_messages_missing_html(a.id)] == ["<a@x>"]


def test_iter_messages_without_extraction(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m1 = _add_message(store, lst.id, addr.id, message_id="<m1@x>").message
    m2 = _add_message(store, lst.id, addr.id, message_id="<m2@x>").message

    assert [m.id for m in store.iter_messages_without_extraction()] == [m1.id, m2.id]

    store.insert_extraction(
        message_id=m1.id, extracted_text="hello world", method="test", status="ok"
    )
    assert [m.id for m in store.iter_messages_without_extraction()] == [m2.id]


# --- extractions --------------------------------------------------------------


def test_insert_extraction_computes_char_count(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="hello", method="reply-parser", status="ok"
    )
    assert ext.char_count == 5
    assert ext.status == "ok"


@pytest.mark.parametrize("status", EXTRACTION_STATUSES)
def test_extraction_accepts_valid_statuses(store, status):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id=f"<{status}@x>").message
    ext = store.insert_extraction(message_id=m.id, extracted_text="x", method="m", status=status)
    assert ext.status == status


def test_extraction_rejects_bad_status(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    with pytest.raises(ValueError):
        store.insert_extraction(message_id=m.id, extracted_text="x", method="m", status="bogus")


def test_one_extraction_per_message(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    store.insert_extraction(message_id=m.id, extracted_text="a", method="m", status="ok")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_extraction(message_id=m.id, extracted_text="b", method="m", status="ok")


def test_iter_extractions_needing_score(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")

    long_text = " ".join(f"word{i}" for i in range(60))  # 60 words -> scorable
    short_text = "just three words"  # 3 words -> below floor

    m_ok = _add_message(store, lst.id, addr.id, message_id="<ok@x>").message
    m_short = _add_message(store, lst.id, addr.id, message_id="<short@x>").message
    m_bad = _add_message(store, lst.id, addr.id, message_id="<bad@x>").message

    ext_ok = store.insert_extraction(
        message_id=m_ok.id, extracted_text=long_text, method="m", status="ok"
    )
    store.insert_extraction(
        message_id=m_short.id, extracted_text=short_text, method="m", status="ok"
    )
    store.insert_extraction(
        message_id=m_bad.id, extracted_text=long_text, method="m", status="too_short"
    )

    # Only the long, ok, unscored extraction qualifies at the 50-word floor.
    queue = list(store.iter_extractions_needing_score(min_words=50))
    assert [e.id for e in queue] == [ext_ok.id]

    # A lower floor lets the short one through too.
    assert len(list(store.iter_extractions_needing_score(min_words=1))) == 2

    # Once scored, it drops out of the queue.
    store.insert_score(extraction_id=ext_ok.id, text_sha256=sha256_text(long_text), fraction_ai=1.0)
    assert list(store.iter_extractions_needing_score(min_words=50)) == []


# --- scores / cache -----------------------------------------------------------


def test_score_sha256_cache_hit_and_miss(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="some text", method="m", status="ok"
    )
    digest = sha256_text("some text")

    assert store.find_score_by_text_sha256(digest) is None  # miss

    store.insert_score(
        extraction_id=ext.id,
        text_sha256=digest,
        fraction_ai=0.1,
        fraction_ai_assisted=0.2,
        fraction_human=0.7,
        label="Human",
        detector_version="3.3.2",
        raw_response={"stage": "STAGE_SUCCESS", "windows": []},
    )

    hit = store.find_score_by_text_sha256(digest)  # cache hit
    assert hit is not None
    assert hit.label == "Human"
    assert hit.detector_version == "3.3.2"
    # raw_response round-trips as JSON text.
    assert json.loads(hit.raw_response)["stage"] == "STAGE_SUCCESS"

    assert store.find_score_by_text_sha256(sha256_text("different")) is None  # miss


def test_one_score_per_extraction(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    ext = store.insert_extraction(message_id=m.id, extracted_text="text", method="m", status="ok")
    store.insert_score(extraction_id=ext.id, text_sha256="deadbeef")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_score(extraction_id=ext.id, text_sha256="deadbeef")


def test_insert_score_accepts_raw_json_string(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id).message
    ext = store.insert_extraction(message_id=m.id, extracted_text="text", method="m", status="ok")
    score = store.insert_score(
        extraction_id=ext.id, text_sha256="abc", raw_response='{"already":"json"}'
    )
    assert score.raw_response == '{"already":"json"}'


def _scored(store, *, text, message_id, detector_version):
    """Seed a message + extraction + score for ``text`` and return the score."""
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id=message_id).message
    ext = store.insert_extraction(message_id=m.id, extracted_text=text, method="m", status="ok")
    return store.insert_score(
        extraction_id=ext.id,
        text_sha256=sha256_text(text),
        label="AI",
        detector_version=detector_version,
    )


def test_score_cache_filters_on_the_detector_generation(store):
    _scored(store, text="some text", message_id="<gen@x>", detector_version="3.3.2")
    digest = sha256_text("some text")

    # A Pangram 3 verdict serves a generation-3 lookup and no other.
    assert store.find_score_by_text_sha256(digest, generation="3") is not None
    assert store.find_score_by_text_sha256(digest, generation="4") is None
    # No generation given: any verdict matches (the pre-existing behaviour).
    assert store.find_score_by_text_sha256(digest) is not None


def test_score_cache_generation_ignores_rows_without_a_version(store):
    _scored(store, text="unversioned", message_id="<nover@x>", detector_version=None)
    digest = sha256_text("unversioned")
    assert store.find_score_by_text_sha256(digest) is not None
    assert store.find_score_by_text_sha256(digest, generation="3") is None
    assert store.find_score_by_text_sha256(digest, generation="4") is None


def test_scores_outside_generation_lists_messages_and_word_counts(store):
    old = _scored(store, text="one two three", message_id="<old@x>", detector_version="3.3.2")
    new = _scored(store, text="four five", message_id="<new@x>", detector_version="4.0")
    none = _scored(store, text="six", message_id="<none@x>", detector_version=None)

    old_message = store.conn.execute(
        "SELECT message_id AS m FROM extractions WHERE id = ?", (old.extraction_id,)
    ).fetchone()["m"]
    none_message = store.conn.execute(
        "SELECT message_id AS m FROM extractions WHERE id = ?", (none.extraction_id,)
    ).fetchone()["m"]
    new_message = store.conn.execute(
        "SELECT message_id AS m FROM extractions WHERE id = ?", (new.extraction_id,)
    ).fetchone()["m"]

    # Against generation 4: the 3.3.2 row and the unversioned row are outside it.
    rows = store.scores_outside_generation("4")
    assert rows == sorted([(old_message, 3), (none_message, 1)])
    # Against generation 3: the 4.0 row and the unversioned row are.
    rows = store.scores_outside_generation("3")
    assert rows == sorted([(new_message, 2), (none_message, 1)])


# --- settings -----------------------------------------------------------------


def test_migration_012_creates_app_settings(store):
    tables = {
        row["name"]
        for row in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "app_settings" in tables
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(app_settings)")}
    assert columns == {"key", "value"}


def test_settings_round_trip(store):
    assert store.get_setting("pangram_model") is None  # unset reads as None
    store.set_setting("pangram_model", "default")
    assert store.get_setting("pangram_model") == "default"
    # A second write replaces the value rather than failing on the primary key.
    store.set_setting("pangram_model", "pangram-4")
    assert store.get_setting("pangram_model") == "pangram-4"
    # Keys are independent.
    store.set_setting("pangram_upgrade_notice", "later")
    assert store.get_setting("pangram_upgrade_notice") == "later"
    assert store.get_setting("pangram_model") == "pangram-4"


def test_settings_persist_across_reopen(tmp_path):
    db = tmp_path / "settings.db"
    with Store(db) as s:
        s.set_setting("pangram_model", "default")
    with Store(db) as s:
        assert s.get_setting("pangram_model") == "default"


# --- persons ------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,expected",
    [
        ("maarten.simon=40sidn.nl@dmarc.ietf.org", "maarten.simon@sidn.nl"),
        ("xueyuan=40w3.org@dmarc.ietf.org", "xueyuan@w3.org"),
        # Case is normalized, as everywhere else an address is handled.
        ("Xueyuan=40W3.ORG@DMARC.IETF.ORG", "xueyuan@w3.org"),
        # Escapes other than =40 unescape the same way (=2E is '.').
        ("a=2Eb=40x.org@dmarc.ietf.org", "a.b@x.org"),
        # Not the rewriting domain.
        ("someone=40x.org@example.org", None),
        ("plain@sidn.nl", None),
        # Nothing to unescape, so no address inside the local part.
        ("nobody@dmarc.ietf.org", None),
        # Unescapes to something that is not a single address.
        ("=40x.org@dmarc.ietf.org", None),
        ("a=40b=40c@dmarc.ietf.org", None),
    ],
)
def test_dmarc_rewrite_original(email, expected):
    assert dmarc_rewrite_original(email) == expected


def test_link_dmarc_rewrites_pairs_an_unlinked_rewrite(store):
    original = store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    rewrite = store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")

    assert store.link_dmarc_rewrites() == 1

    person_id = store.get_address(original.id).person_id
    assert person_id is not None
    assert store.get_address(rewrite.id).person_id == person_id
    # Named after the original, whose display name the sender chose.
    assert store.get_person(person_id).canonical_name == "Maarten Simon"
    assert {a.email for a in store.addresses_for_person(person_id)} == {
        "maarten.simon@sidn.nl",
        "maarten.simon=40sidn.nl@dmarc.ietf.org",
    }


def test_link_dmarc_rewrites_is_idempotent(store):
    store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    assert store.link_dmarc_rewrites() == 1
    assert store.link_dmarc_rewrites() == 0  # already paired
    assert store.conn.execute("SELECT COUNT(*) AS c FROM persons").fetchone()["c"] == 1


def test_link_dmarc_rewrites_joins_an_existing_person(store):
    original = store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    rewrite = store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    person = store.create_person("Maarten Simon (chosen)")
    store.assign_address_to_person(original.id, person.id)

    assert store.link_dmarc_rewrites() == 1
    assert store.get_address(rewrite.id).person_id == person.id
    # No second person invented, and the hand-set name is kept.
    assert store.conn.execute("SELECT COUNT(*) AS c FROM persons").fetchone()["c"] == 1
    assert store.get_person(person.id).canonical_name == "Maarten Simon (chosen)"


def test_link_dmarc_rewrites_joins_from_the_rewrite_side(store):
    original = store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    rewrite = store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    person = store.create_person("Maarten Simon")
    store.assign_address_to_person(rewrite.id, person.id)

    assert store.link_dmarc_rewrites() == 1
    assert store.get_address(original.id).person_id == person.id


def test_link_dmarc_rewrites_leaves_two_different_persons_alone(store):
    # Both sides already grouped by hand: merging two people on an
    # address-shaped guess is not a repair this makes on its own.
    original = store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    rewrite = store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    one = store.create_person("One")
    two = store.create_person("Two")
    store.assign_address_to_person(original.id, one.id)
    store.assign_address_to_person(rewrite.id, two.id)

    assert store.link_dmarc_rewrites() == 0
    assert store.get_address(original.id).person_id == one.id
    assert store.get_address(rewrite.id).person_id == two.id


def test_link_dmarc_rewrites_skips_a_rewrite_with_no_original(store):
    rewrite = store.upsert_address("nobody=40elsewhere.org@dmarc.ietf.org", "Nobody")
    assert store.link_dmarc_rewrites() == 0
    assert store.get_address(rewrite.id).person_id is None
    assert store.conn.execute("SELECT COUNT(*) AS c FROM persons").fetchone()["c"] == 0


def test_person_assignment_and_detach(store):
    a1 = store.upsert_address("jane@work.example", "Jane Doe")
    a2 = store.upsert_address("jane@home.example", "Jane Doe")
    person = store.create_person("Jane Doe")

    store.assign_address_to_person(a1.id, person.id)
    store.assign_address_to_person(a2.id, person.id)
    linked = store.addresses_for_person(person.id)
    assert {a.email for a in linked} == {"jane@work.example", "jane@home.example"}
    assert store.get_address(a1.id).person_id == person.id

    # Detach.
    store.assign_address_to_person(a1.id, None)
    assert store.get_address(a1.id).person_id is None
    assert len(store.addresses_for_person(person.id)) == 1


def test_deleting_person_nulls_address_link(store):
    a = store.upsert_address("x@example.org", "X")
    person = store.create_person("X")
    store.assign_address_to_person(a.id, person.id)
    store.conn.execute("DELETE FROM persons WHERE id = ?", (person.id,))
    store.conn.commit()
    # ON DELETE SET NULL keeps the address but clears the link.
    assert store.get_address(a.id).person_id is None


def test_suggest_person_merges(store):
    # Same display name, two distinct emails -> a suggestion.
    store.upsert_address("jane@work.example", "Jane Doe")
    store.upsert_address("jane@home.example", "Jane Doe")
    # Unique display name -> no suggestion.
    store.upsert_address("solo@example.org", "Solo Person")
    # Blank display name -> ignored.
    store.upsert_address("anon@example.org", "")

    suggestions = store.suggest_person_merges()
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.display_name == "Jane Doe"
    assert set(s.emails) == {"jane@work.example", "jane@home.example"}
    assert len(s.address_ids) == 2


# --- pipeline_version (migration 007) -----------------------------------------


def test_migration_007_adds_pipeline_version_column(store):
    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "pipeline_version" in cols


def test_migration_007_present_on_migrated_db(tmp_path):
    # A database rewound to pre-007 gains the messages.pipeline_version column on
    # migrate; extractions.pipeline_version (008), the two timing columns
    # (009/010), extraction_version (011) and the app_settings table (012) are
    # dropped too so they re-apply.
    db = tmp_path / "migrated007.db"
    with Store(db) as s:
        s.conn.execute("DELETE FROM schema_version WHERE version >= 7")
        s.conn.execute("ALTER TABLE messages DROP COLUMN pipeline_version")
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
        cols = {row["name"] for row in s.conn.execute("PRAGMA table_info(messages)").fetchall()}
        version = s.conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
    assert "pipeline_version" in cols
    assert version == 19


def test_message_pipeline_version_roundtrips(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<pv@x>", pipeline_version="1.2.3")
    assert up.message.pipeline_version == "1.2.3"
    assert store.get_message(up.message.id).pipeline_version == "1.2.3"


def test_upsert_message_default_stamps_package_version(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<def@x>")
    assert up.message.pipeline_version == __version__


def test_upsert_message_honors_explicit_pipeline_version(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    up = _add_message(store, lst.id, addr.id, message_id="<exp@x>", pipeline_version="0.9.0")
    assert up.message.pipeline_version == "0.9.0"


def test_upsert_message_conflict_leaves_pipeline_version_untouched(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    _add_message(store, lst.id, addr.id, message_id="<c@x>", pipeline_version="1.0.0")
    # A re-pull carrying a different version is a no-op; the stored value is kept.
    second = _add_message(store, lst.id, addr.id, message_id="<c@x>", pipeline_version="2.0.0")
    assert second.inserted is False
    assert store.get_message(second.message.id).pipeline_version == "1.0.0"


def test_insert_extraction_restamps_pipeline_version_default(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<e@x>", pipeline_version="0.1.0").message
    assert store.get_message(m.id).pipeline_version == "0.1.0"
    store.insert_extraction(message_id=m.id, extracted_text="x", method="m", status="ok")
    assert store.get_message(m.id).pipeline_version == __version__


def test_insert_extraction_restamps_pipeline_version_explicit(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<e@x>", pipeline_version="0.1.0").message
    store.insert_extraction(
        message_id=m.id, extracted_text="x", method="m", status="ok", pipeline_version="3.2.1"
    )
    assert store.get_message(m.id).pipeline_version == "3.2.1"


def test_insert_score_restamps_pipeline_version_default(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<s@x>").message
    # Pin the extraction stage to a distinct version so the score restamp shows.
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="text", method="m", status="ok", pipeline_version="2.0.0"
    )
    assert store.get_message(m.id).pipeline_version == "2.0.0"
    store.insert_score(extraction_id=ext.id, text_sha256="abc")
    assert store.get_message(m.id).pipeline_version == __version__


def test_insert_score_restamps_pipeline_version_explicit(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<s@x>").message
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="text", method="m", status="ok", pipeline_version="2.0.0"
    )
    store.insert_score(extraction_id=ext.id, text_sha256="abc", pipeline_version="4.5.6")
    assert store.get_message(m.id).pipeline_version == "4.5.6"


# --- version_key --------------------------------------------------------------


def test_version_key_parses_semver_tuple():
    assert version_key("1.2.3") == (1, 2, 3)


def test_version_key_orders_numerically_not_lexically():
    # Lexical string comparison would rank "1.10.0" < "1.9.9"; tuple order must not.
    assert version_key("1.10.0") > version_key("1.9.9")


def test_version_key_none_and_unparsable_sort_oldest():
    assert version_key(None) == (0, 0, 0)
    assert version_key("garbage") == (0, 0, 0)
    assert version_key("") == (0, 0, 0)
    assert version_key("1.2") == (0, 0, 0)
    assert version_key("1.2.3.4") == (0, 0, 0)
    # Every real version outranks the (0, 0, 0) floor.
    assert version_key("1.0.0") > version_key(None)


# --- extraction_version (migration 011) ---------------------------------------


def test_migration_011_adds_extraction_version_column(store):
    cols = {row["name"] for row in store.conn.execute("PRAGMA table_info(extractions)").fetchall()}
    assert "extraction_version" in cols
    # pipeline_version stays alongside it as provenance.
    assert "pipeline_version" in cols


def test_insert_extraction_stamps_the_running_extraction_version(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<gen@x>").message
    ext = store.insert_extraction(message_id=m.id, extracted_text="x", method="m", status="ok")
    assert ext.extraction_version == EXTRACTION_VERSION
    assert store.get_extraction(ext.id).extraction_version == EXTRACTION_VERSION


def test_insert_extraction_honors_an_explicit_extraction_version(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<gen2@x>").message
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="x", method="m", status="ok", extraction_version=1
    )
    assert ext.extraction_version == 1


def test_replace_extraction_restamps_the_extraction_version(store):
    lst = store.upsert_list("announce", "Shared Folders/announce")
    addr = store.upsert_address("a@x.org")
    m = _add_message(store, lst.id, addr.id, message_id="<gen3@x>").message
    ext = store.insert_extraction(
        message_id=m.id, extracted_text="x", method="m", status="ok", extraction_version=1
    )
    after = store.replace_extraction(ext.id, extracted_text="y", method="m", status="ok")
    assert after.extraction_version == EXTRACTION_VERSION


def _rewound_to_pre_011(db):
    """Drop the extraction_version column and rewind the schema version past it.

    The app_settings table (012) and the auto_generated column (014) go with it
    so those migrations re-apply too.
    """
    with Store(db) as s:
        s.conn.execute("DELETE FROM schema_version WHERE version >= 11")
        s.conn.execute("ALTER TABLE extractions DROP COLUMN extraction_version")
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


@pytest.mark.parametrize(
    "pipeline_version, expected",
    [
        ("1.2.0", 2),
        ("1.2.9", 2),
        ("1.2.10", 2),  # the two-digit patch a lexical comparison would misplace
        ("1.1.0", 1),
        ("1.0.5", 1),
        ("0.9.0", 1),
        ("garbage", 1),
        (None, None),
    ],
)
def test_migration_011_backfills_from_the_pipeline_version(tmp_path, pipeline_version, expected):
    """Each pre-011 stamp maps onto the generation that produced its text.

    1.2.0 is the release that changed extraction, so 1.2.x is generation 2 and
    everything older generation 1. A row with no stamp at all stays NULL, which
    the staleness check reads as older than every generation.
    """
    db = tmp_path / "backfill011.db"
    with Store(db) as s:
        lst = s.upsert_list("announce", "Shared Folders/announce")
        addr = s.upsert_address("a@x.org")
        m = _add_message(s, lst.id, addr.id, message_id="<bf@x>").message
        ext = s.insert_extraction(message_id=m.id, extracted_text="x", method="m", status="ok")
        s.conn.execute(
            "UPDATE extractions SET pipeline_version = ? WHERE id = ?",
            (pipeline_version, ext.id),
        )
        s.conn.commit()
    _rewound_to_pre_011(db)
    with Store(db) as s:
        assert s.get_extraction(ext.id).extraction_version == expected
        # The provenance stamp is left exactly as it was.
        assert s.get_extraction(ext.id).pipeline_version == pipeline_version


def test_extraction_version_for_app_version_matches_the_migration():
    assert extraction_version_for_app_version("1.2.0") == 2
    assert extraction_version_for_app_version("1.2.10") == 2
    assert extraction_version_for_app_version("1.10.0") == 2
    assert extraction_version_for_app_version("2.0.0") == 2
    assert extraction_version_for_app_version("1.1.0") == 1
    assert extraction_version_for_app_version("1.0.5") == 1
    assert extraction_version_for_app_version("garbage") == 1
    assert extraction_version_for_app_version(None) is None
