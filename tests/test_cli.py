"""Tests for the mail-ai-pull CLI: arg validation and an end-to-end run."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check import cli
from mailing_list_ai_check.fetcher import parse_header
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.store import Store


# --- argument validation ------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["announce", "--all-lists", "--count", "5"],  # lists + --all-lists
        ["--count", "5"],  # no lists, no --all-lists
        ["announce"],  # no depth mode
        ["announce", "--count", "0"],  # non-positive count
        ["announce", "--days", "-1"],  # non-positive days
        ["announce", "--since", "not-a-date"],  # bad date
        ["announce", "--count", "5", "--limit", "0"],  # non-positive limit
        ["announce", "--count", "5", "--until", "2025-02-01"],  # --until without --since
        ["announce", "--since", "2025-01-01", "--until", "nope"],  # bad --until date
        ["announce", "--since", "2025-01-01", "--until", "2025-01-01"],  # empty period
        ["announce", "--backfill-headers", "--until", "2025-01-01"],  # backfill + --until
    ],
)
def test_cli_rejects_bad_args(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2  # argparse error exit code


def test_cli_mutually_exclusive_depth():
    with pytest.raises(SystemExit) as exc:
        cli.main(["announce", "--count", "5", "--since", "2025-01-01"])
    assert exc.value.code == 2


def test_days_resolves_to_since_iso():
    ns = cli.build_parser().parse_args(["announce", "--days", "7"])
    depth = cli._resolve_depth(ns)
    assert depth.since is not None
    # a plausible ISO date string
    datetime.strptime(depth.since, "%Y-%m-%d")


def test_until_resolves_onto_the_since_depth():
    ns = cli.build_parser().parse_args(
        ["announce", "--since", "2024-07-01", "--until", "2024-08-01"]
    )
    depth = cli._resolve_depth(ns)
    assert (depth.since, depth.until) == ("2024-07-01", "2024-08-01")


def test_all_lists_incremental_requires_a_cursor():
    # --all-lists takes the folder set from the server, so an untracked list is
    # one never asked for rather than one to bootstrap.
    ns = cli.build_parser().parse_args(["--all-lists", "--incremental"])
    assert cli._resolve_depth(ns).require_cursor is True


def test_named_list_incremental_does_not_require_a_cursor():
    # `mail-ai-pull announce --incremental` still takes a full first pull.
    ns = cli.build_parser().parse_args(["announce", "--incremental"])
    assert cli._resolve_depth(ns).require_cursor is False


# --- end-to-end main() with a fake client -------------------------------------


def _install_fake(monkeypatch, folders):
    conn = FakeImapConn(folders=folders)
    client = ImapClient(conn)
    monkeypatch.setattr(cli, "open_client", lambda *a, **k: client)
    monkeypatch.setenv("PANGRAM_API_KEY", "test-key")  # Config.load requires it
    return client, conn


def test_main_pulls_into_store(monkeypatch, tmp_path):
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=2)
    for uid in (1, 2):
        fd.messages[uid] = make_raw(
            message_id=f"<{uid}@example.org>",
            from_header=f"User {uid} <user{uid}@example.org>",
            date=datetime(2025, 1, uid).strftime("%a, %d %b %Y %H:%M:%S +0000"),
        )
        fd.dates[uid] = datetime(2025, 1, uid)
        fd.froms[uid] = f"user{uid}@example.org"
    _install_fake(monkeypatch, {"Shared Folders/last-call": fd})

    db = tmp_path / "cli.db"
    rc = cli.main(["last-call", "--count", "5", "--db", str(db)])
    assert rc == 0

    with Store(db) as store:
        mlist = store.upsert_list("last-call", "Shared Folders/last-call")
        rows = store.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        assert rows == 2
        cursor = store.get_pull_state(mlist.id)
        assert cursor is not None and cursor.last_uid == 2


def test_main_dry_run_stores_nothing(monkeypatch, tmp_path):
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<1@x>")
    fd.dates[1] = datetime(2025, 1, 1)
    fd.froms[1] = "a@example.org"
    _install_fake(monkeypatch, {"Shared Folders/announce": fd})

    db = tmp_path / "dry.db"
    rc = cli.main(["announce", "--count", "5", "--dry-run", "--db", str(db)])
    assert rc == 0
    with Store(db) as store:
        rows = store.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        assert rows == 0


# --- --backfill-html ----------------------------------------------------------


def _seed_missing_html(store, list_id, addr_id, uids, uidvalidity=1000):
    """Seed messages (raw_html NULL) with the given UIDs and a matching cursor."""
    for uid in uids:
        store.upsert_message(
            message_id=f"<bf{uid}@x>",
            list_id=list_id,
            address_id=addr_id,
            subject="s",
            date="2026-07-01T00:00:00+00:00",
            in_reply_to=None,
            raw_body="the plain body",
            uid=uid,
        )
    store.set_pull_state(list_id, uidvalidity, max(uids))


def _html_folder(uids, uidvalidity=1000):
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=max(uids) + 1, exists=len(uids))
    for uid in uids:
        fd.messages[uid] = make_raw(
            message_id=f"<bf{uid}@x>",
            plain="the plain body",
            html=f"<p>html body for {uid}</p>",
        )
        fd.dates[uid] = datetime(2025, 1, 1)
        fd.froms[uid] = "a@example.org"
    return fd


def test_backfill_html_fills_missing_html(monkeypatch, tmp_path):
    db = tmp_path / "backfill.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("a@example.org", "A")
        _seed_missing_html(store, lst.id, addr.id, [1, 2, 3])

    _install_fake(monkeypatch, {"Shared Folders/announce": _html_folder([1, 2, 3])})
    rc = cli.main(["announce", "--backfill-html", "--db", str(db)])
    assert rc == 0

    with Store(db) as store:
        rows = store.conn.execute(
            "SELECT message_id, raw_html FROM messages ORDER BY uid"
        ).fetchall()
        assert all("html body" in r["raw_html"] for r in rows)
        # raw_body was left untouched.
        assert all(
            "the plain body" in r["raw_body"]
            for r in store.conn.execute("SELECT raw_body FROM messages").fetchall()
        )


def test_backfill_html_default_cap_is_ten(monkeypatch, tmp_path):
    db = tmp_path / "cap.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("a@example.org", "A")
        _seed_missing_html(store, lst.id, addr.id, list(range(1, 13)))  # 12 pending

    _install_fake(monkeypatch, {"Shared Folders/announce": _html_folder(list(range(1, 13)))})
    rc = cli.main(["announce", "--backfill-html", "--db", str(db)])  # no --limit
    assert rc == 0

    with Store(db) as store:
        filled = store.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE raw_html IS NOT NULL"
        ).fetchone()["c"]
        assert filled == 10  # the CLAUDE.md testing cap
        # The lowest 10 UIDs were fetched; two remain for a later run.
        assert len(list(store.iter_messages_missing_html(1))) == 2


def test_backfill_html_skips_on_uidvalidity_mismatch(monkeypatch, tmp_path):
    db = tmp_path / "mismatch.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("a@example.org", "A")
        # Cursor UIDVALIDITY (999) differs from the folder's (1000) -> skip.
        _seed_missing_html(store, lst.id, addr.id, [1, 2], uidvalidity=999)

    _install_fake(monkeypatch, {"Shared Folders/announce": _html_folder([1, 2], uidvalidity=1000)})
    rc = cli.main(["announce", "--backfill-html", "--db", str(db)])
    assert rc == 0

    with Store(db) as store:
        filled = store.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE raw_html IS NOT NULL"
        ).fetchone()["c"]
        assert filled == 0  # nothing backfilled for the skipped list


def test_backfill_html_rejects_depth_mode():
    with pytest.raises(SystemExit) as exc:
        cli.main(["announce", "--backfill-html", "--count", "5"])
    assert exc.value.code == 2


def _plain_only_folder(uids, uidvalidity=1000):
    """A folder whose messages carry a plain part but NO text/html part."""
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=max(uids) + 1, exists=len(uids))
    for uid in uids:
        fd.messages[uid] = make_raw(
            message_id=f"<bf{uid}@x>", plain="plain only, no html", html=None
        )
        fd.dates[uid] = datetime(2025, 1, 1)
        fd.froms[uid] = "a@example.org"
    return fd


def test_backfill_html_tombstones_html_less_messages(monkeypatch, tmp_path):
    # Regression: a message that genuinely has no HTML part must not be
    # re-fetched every run. It is stamped with an empty-string tombstone so the
    # backfill queue drains and a capped run makes forward progress rather than
    # burning its budget on the same HTML-less messages forever.
    db = tmp_path / "tombstone.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("a@example.org", "A")
        _seed_missing_html(store, lst.id, addr.id, [1, 2, 3])

    _install_fake(monkeypatch, {"Shared Folders/announce": _plain_only_folder([1, 2, 3])})
    assert cli.main(["announce", "--backfill-html", "--db", str(db)]) == 0

    with Store(db) as store:
        # Queue is drained: nothing re-fetched on a subsequent run.
        assert list(store.iter_messages_missing_html(1)) == []
        # Tombstone is the empty string, not NULL, and raw_body is untouched.
        rows = store.conn.execute("SELECT raw_html, raw_body FROM messages").fetchall()
        assert all(r["raw_html"] == "" for r in rows)
        assert all(r["raw_body"] == "the plain body" for r in rows)  # seeded body untouched


# --- --backfill-headers -------------------------------------------------------


def _named_folder(names, uidvalidity=1000):
    """A folder whose messages all share one address but differ in From name."""
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=len(names) + 1, exists=len(names))
    for uid, display in names.items():
        fd.messages[uid] = make_raw(
            message_id=f"<bf{uid}@x>",
            from_header=f"{display} <noreply@example.org>",
            plain="the plain body",
        )
        fd.dates[uid] = datetime(2025, 1, 1)
        fd.froms[uid] = "noreply@example.org"
    return fd


def test_backfill_headers_stores_headers_and_recovers_names(monkeypatch, tmp_path):
    # The whole point of the mode: rows pulled before the columns existed get
    # their verbatim headers, and each recovers the name its own header carried
    # rather than inheriting the address's first-seen one.
    db = tmp_path / "hdr.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("noreply@example.org", "First Person")
        _seed_missing_html(store, lst.id, addr.id, [1, 2, 3])

    names = {1: "First Person", 2: "Second Person", 3: "Third Person"}
    _install_fake(monkeypatch, {"Shared Folders/announce": _named_folder(names)})
    assert cli.main(["announce", "--backfill-headers", "--db", str(db)]) == 0

    with Store(db) as store:
        rows = store.conn.execute(
            "SELECT uid, from_name, raw_headers FROM messages ORDER BY uid"
        ).fetchall()
        assert [r["from_name"] for r in rows] == list(names.values())
        # Stored verbatim as bytes, and re-parseable back to the same name.
        for row in rows:
            assert isinstance(row["raw_headers"], bytes)
            assert b"From:" in row["raw_headers"]
            assert parse_header(row["raw_headers"]).from_name == row["from_name"]
        # The address keeps the name it was first seen with; queue is drained.
        assert store.get_address(1).display_name == "First Person"
        assert list(store.iter_messages_missing_headers(1)) == []


def test_backfill_headers_keeps_a_name_captured_at_fetch_time(monkeypatch, tmp_path):
    # from_name already set (pulled after migration 015) is authoritative: the
    # backfill adds headers but must not overwrite it.
    db = tmp_path / "keep.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("noreply@example.org", "A")
        store.upsert_message(
            message_id="<bf1@x>",
            list_id=lst.id,
            address_id=addr.id,
            subject="s",
            date="2026-07-01T00:00:00+00:00",
            in_reply_to=None,
            raw_body="the plain body",
            uid=1,
            from_name="Captured At Fetch",
        )
        store.set_pull_state(lst.id, 1000, 1)

    _install_fake(monkeypatch, {"Shared Folders/announce": _named_folder({1: "From The Header"})})
    assert cli.main(["announce", "--backfill-headers", "--db", str(db)]) == 0

    with Store(db) as store:
        row = store.conn.execute("SELECT from_name, raw_headers FROM messages").fetchone()
        assert row["from_name"] == "Captured At Fetch"
        assert b"From The Header" in row["raw_headers"]


def test_backfill_headers_default_cap_is_ten(monkeypatch, tmp_path):
    db = tmp_path / "hdrcap.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("noreply@example.org", "A")
        _seed_missing_html(store, lst.id, addr.id, list(range(1, 13)))  # 12 pending

    names = {uid: f"Person {uid}" for uid in range(1, 13)}
    _install_fake(monkeypatch, {"Shared Folders/announce": _named_folder(names)})
    assert cli.main(["announce", "--backfill-headers", "--db", str(db)]) == 0

    with Store(db) as store:
        filled = store.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE raw_headers IS NOT NULL"
        ).fetchone()["c"]
        assert filled == 10  # the CLAUDE.md testing cap
        assert len(list(store.iter_messages_missing_headers(1))) == 2


def test_backfill_headers_skips_on_uidvalidity_mismatch(monkeypatch, tmp_path):
    db = tmp_path / "hdrmismatch.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("noreply@example.org", "A")
        _seed_missing_html(store, lst.id, addr.id, [1, 2], uidvalidity=999)

    _install_fake(
        monkeypatch,
        {"Shared Folders/announce": _named_folder({1: "A", 2: "B"}, uidvalidity=1000)},
    )
    assert cli.main(["announce", "--backfill-headers", "--db", str(db)]) == 0

    with Store(db) as store:
        filled = store.conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE raw_headers IS NOT NULL"
        ).fetchone()["c"]
        assert filled == 0


def test_backfill_headers_rejects_depth_mode():
    with pytest.raises(SystemExit) as exc:
        cli.main(["announce", "--backfill-headers", "--count", "5"])
    assert exc.value.code == 2


def test_backfill_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit) as exc:
        cli.main(["announce", "--backfill-html", "--backfill-headers"])
    assert exc.value.code == 2


def test_main_limit_caps_fetch(monkeypatch, tmp_path):
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=5)
    for uid in range(1, 6):
        fd.messages[uid] = make_raw(message_id=f"<{uid}@x>")
        fd.dates[uid] = datetime(2025, 1, uid)
        fd.froms[uid] = "a@example.org"
    _install_fake(monkeypatch, {"Shared Folders/announce": fd})

    db = tmp_path / "limit.db"
    rc = cli.main(["announce", "--count", "5", "--limit", "2", "--db", str(db)])
    assert rc == 0
    with Store(db) as store:
        rows = store.conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        assert rows == 2
