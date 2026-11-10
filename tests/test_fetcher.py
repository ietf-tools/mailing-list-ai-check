"""Unit tests for fetch orchestration and RFC 5322 parsing (no network)."""

from __future__ import annotations

from datetime import datetime

from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check.fetcher import (
    DepthMode,
    FetchRequest,
    compute_uids,
    folder_for_list,
    iso_to_imap_date,
    list_name_for_folder,
    parse_header,
    parse_message,
    refresh_lists_index,
    resolve_folders,
    run_fetch,
    run_fetch_uids,
    split_headers,
)
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.store import Store


# --- date helper --------------------------------------------------------------


def test_iso_to_imap_date():
    assert iso_to_imap_date("2025-01-06") == "06-Jan-2025"
    assert iso_to_imap_date("2025-12-31") == "31-Dec-2025"


# --- folder mapping -----------------------------------------------------------


def test_folder_and_list_name_roundtrip():
    assert folder_for_list("announce") == "Shared Folders/announce"
    assert folder_for_list("Shared Folders/announce") == "Shared Folders/announce"
    assert list_name_for_folder("Shared Folders/last-call") == "last-call"


def test_resolve_folders_named():
    client = ImapClient(FakeImapConn())
    assert resolve_folders(client, ["announce", "quic"]) == [
        "Shared Folders/announce",
        "Shared Folders/quic",
    ]


def test_resolve_folders_all_lists_enumerates():
    lines = [
        rb'(\Noselect) "/" "Shared Folders"',
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
    ]
    client = ImapClient(FakeImapConn(list_lines=lines))
    assert resolve_folders(client, [], all_lists=True) == ["Shared Folders/announce"]


def test_refresh_lists_index_maps_folders_to_names(tmp_path):
    lines = [
        rb'(\Noselect) "/" "Shared Folders"',
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
        rb'(\HasNoChildren) "/" "Shared Folders/last-call"',
    ]
    client = ImapClient(FakeImapConn(list_lines=lines))
    with Store(tmp_path / "t.db") as store:
        counts = refresh_lists_index(client, store)
        assert counts["added"] == 2
        assert counts["total"] == 2
        rows = {row["name"]: row["folder"] for row in store.list_rows()}
    assert rows == {
        "announce": "Shared Folders/announce",
        "last-call": "Shared Folders/last-call",
    }


def test_refresh_lists_index_checks_activity_only_for_message_lists(tmp_path):
    lines = [
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
        rb'(\HasNoChildren) "/" "Shared Folders/last-call"',
    ]
    announce_fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    announce_fd.messages[1] = make_raw(message_id="<1@x>")
    announce_fd.dates[1] = datetime(2025, 3, 1, 12, 0, 0)
    lastcall_fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    lastcall_fd.messages[1] = make_raw(message_id="<2@x>")
    conn = FakeImapConn(
        folders={"Shared Folders/announce": announce_fd, "Shared Folders/last-call": lastcall_fd},
        list_lines=lines,
    )
    client = ImapClient(conn)
    with Store(tmp_path / "t.db") as store:
        # Only announce holds a local message, so only it is tracked/checked.
        lst = store.upsert_list("announce", "Shared Folders/announce")
        store.upsert_message(
            message_id="<1@x>",
            list_id=lst.id,
            address_id=None,
            subject="s",
            date=None,
            in_reply_to=None,
            raw_body="body",
            uid=1,
        )
        counts = refresh_lists_index(client, store)
        assert counts["activity_checked"] == 1
        assert counts["activity_failed"] == 0
        # last-call is index-only, so it is never EXAMINEd/fetched.
        assert conn.internaldate_calls == ["Shared Folders/announce"]
        assert store.get_list(lst.id).last_message_at == "2025-03-01T12:00:00+00:00"


def test_refresh_lists_index_counts_activity_failures(tmp_path):
    lines = [rb'(\HasNoChildren) "/" "Shared Folders/announce"']

    class Boom(FakeImapConn):
        def fetch(self, message_set, message_parts):
            raise RuntimeError("fetch blew up")

    fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<1@x>")
    conn = Boom(folders={"Shared Folders/announce": fd}, list_lines=lines)
    client = ImapClient(conn)
    with Store(tmp_path / "t.db") as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        store.upsert_message(
            message_id="<1@x>",
            list_id=lst.id,
            address_id=None,
            subject="s",
            date=None,
            in_reply_to=None,
            raw_body="body",
            uid=1,
        )
        counts = refresh_lists_index(client, store)
        # A failed check is counted, never fatal, and leaves the stamp untouched.
        assert counts["activity_checked"] == 0
        assert counts["activity_failed"] == 1
        assert store.get_list(lst.id).last_message_at is None


# --- parsing ------------------------------------------------------------------


def test_parse_message_decodes_rfc2047_headers_and_normalizes_address():
    raw = (
        b"Message-ID: <r1@example.org>\r\n"
        b"From: =?UTF-8?Q?Andr=C3=A9?= <Andre@Example.ORG>\r\n"
        b"Subject: =?UTF-8?Q?Caf=C3=A9_meeting?=\r\n"
        b"Date: Mon, 06 Jan 2025 10:00:00 +0000\r\n"
        b"\r\n"
        b"body text here\r\n"
    )
    parsed = parse_message(raw, uid=1, folder="Shared Folders/announce")
    assert parsed.from_name == "André"
    assert parsed.from_email == "andre@example.org"  # lowercased
    assert parsed.subject == "Café meeting"
    assert parsed.message_id == "<r1@example.org>"


def test_parse_message_prefers_text_plain_over_html():
    raw = make_raw(plain="the plain new text", html="<p>the html version</p>")
    parsed = parse_message(raw)
    assert parsed.html_only is False
    assert "the plain new text" in parsed.body
    assert "html version" not in parsed.body


def test_parse_message_captures_html_body_alongside_plain():
    # A multipart/alternative message keeps the plain body AND the HTML part.
    raw = make_raw(plain="the plain new text", html="<p>the html version</p>")
    parsed = parse_message(raw)
    assert "the plain new text" in parsed.body
    assert parsed.html_body is not None
    assert "the html version" in parsed.html_body


def test_parse_message_html_only_leaves_empty_body_but_captures_html():
    raw = make_raw(plain=None, html="<p>only html here</p>")
    parsed = parse_message(raw)
    assert parsed.html_only is True
    assert parsed.body is None
    assert parsed.html_body is not None
    assert "only html here" in parsed.html_body


def test_parse_message_plain_only_has_no_html_body():
    raw = make_raw(plain="just plain text", html=None)
    parsed = parse_message(raw)
    assert parsed.html_only is False
    assert parsed.html_body is None


def test_parse_message_date_normalized_to_utc():
    raw = make_raw(date="Mon, 06 Jan 2025 10:00:00 +0200")
    parsed = parse_message(raw)
    assert parsed.date == "2025-01-06T08:00:00+00:00"


def test_parse_message_synthesizes_missing_message_id():
    raw = make_raw(message_id="")
    parsed = parse_message(raw, uid=42, folder="Shared Folders/quic")
    assert "42" in parsed.message_id
    assert parsed.message_id.startswith("<no-message-id-")


def test_parse_message_captures_in_reply_to():
    raw = make_raw(in_reply_to="<parent@example.org>")
    parsed = parse_message(raw)
    assert parsed.in_reply_to == "<parent@example.org>"


# --- header-only parse (preview) ----------------------------------------------


def test_parse_header_extracts_from_subject_and_utc_date():
    raw = (
        b"From: =?UTF-8?Q?Andr=C3=A9?= <Andre@Example.ORG>\r\n"
        b"Subject: =?UTF-8?Q?Caf=C3=A9_meeting?=\r\n"
        b"Date: Mon, 06 Jan 2025 10:00:00 +0200\r\n"
        b"\r\n"
    )
    header = parse_header(raw)
    assert header.from_name == "André"
    assert header.from_email == "andre@example.org"  # lowercased/stripped
    assert header.subject == "Café meeting"
    assert header.date == "2025-01-06T08:00:00+00:00"  # normalized to UTC


def test_parse_header_matches_parse_message_on_full_message():
    raw = make_raw(
        from_header="Bob <bob@example.org>",
        subject="Hello there",
        date="Tue, 07 Jan 2025 09:00:00 +0000",
    )
    header = parse_header(raw)
    message = parse_message(raw)
    assert (header.from_email, header.from_name, header.subject, header.date) == (
        message.from_email,
        message.from_name,
        message.subject,
        message.date,
    )


def test_parse_header_missing_date_is_none():
    raw = b"From: a@x.org\r\nSubject: no date\r\n\r\n"
    header = parse_header(raw)
    assert header.date is None
    assert header.subject == "no date"


# --- UID computation ----------------------------------------------------------


def _folder(uids, uidvalidity=1000, uidnext=999):
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=uidnext, exists=len(uids))
    for uid, (date, frm) in uids.items():
        fd.messages[uid] = make_raw(
            message_id=f"<{uid}@example.org>",
            from_header=f"X <{frm}>",
            date=date.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        )
        fd.dates[uid] = date
        fd.froms[uid] = frm
    return fd


def _client_store(fd, folder="Shared Folders/t"):
    client = ImapClient(FakeImapConn(folders={folder: fd}))
    store = Store(":memory:")
    mlist = store.upsert_list(list_name_for_folder(folder), folder)
    return client, store, mlist


def test_compute_uids_count_slices_from_top():
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 6)})
    client, store, mlist = _client_store(fd)
    uids, status = compute_uids(client, store, "Shared Folders/t", mlist.id, DepthMode(count=2), ())
    assert uids == [4, 5]
    assert status.uidvalidity == 1000
    store.close()


def test_compute_uids_since_filters_server_side():
    fd = _folder(
        {
            1: (datetime(2024, 12, 1), "a@x"),
            2: (datetime(2025, 2, 1), "b@x"),
            3: (datetime(2025, 3, 1), "c@x"),
        }
    )
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(since="2025-01-01"), ()
    )
    assert uids == [2, 3]
    store.close()


def test_compute_uids_incremental_fresh_takes_all():
    # A cursorless folder still selects every UID here; whether such a folder
    # should be visited at all is run_fetch's call (see require_cursor tests).
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 4)})
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    assert uids == [1, 2, 3]
    store.close()


def test_compute_uids_incremental_advances_past_cursor():
    fd = _folder({u: (datetime(2025, 1, u), "a@x") for u in range(1, 6)})
    client, store, mlist = _client_store(fd)
    store.set_pull_state(mlist.id, 1000, 3)  # same uidvalidity, last_uid=3
    uids, _ = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    assert uids == [4, 5]
    store.close()


def test_compute_uids_incremental_uidvalidity_change_resyncs_by_date():
    fd = _folder(
        {
            1: (datetime(2024, 6, 1), "a@x"),
            2: (datetime(2025, 3, 1), "b@x"),
        },
        uidvalidity=2000,
    )
    client, store, mlist = _client_store(fd)
    # Stored cursor has a DIFFERENT uidvalidity → forces resync.
    store.set_pull_state(mlist.id, 1111, 99)
    store.set_list_synced(mlist.id, "2025-01-01T00:00:00+00:00")
    uids, status = compute_uids(
        client, store, "Shared Folders/t", mlist.id, DepthMode(incremental=True), ()
    )
    # Resync re-searches SINCE 2025-01-01, so only uid 2 (Mar) matches.
    assert uids == [2]
    assert status.uidvalidity == 2000
    store.close()


def test_compute_uids_union_of_multiple_from_filters_deduped():
    fd = _folder(
        {
            1: (datetime(2025, 1, 1), "alice@example.org"),
            2: (datetime(2025, 1, 2), "bob@example.com"),
            3: (datetime(2025, 1, 3), "carol@example.org"),
        }
    )
    client, store, mlist = _client_store(fd)
    uids, _ = compute_uids(
        client,
        store,
        "Shared Folders/t",
        mlist.id,
        DepthMode(count=None),
        ("example.org", "example.com"),
    )
    assert uids == [1, 2, 3]
    store.close()


# --- run_fetch ----------------------------------------------------------------


def _request(**kw):
    defaults = dict(
        folders=("Shared Folders/t",),
        depth=DepthMode(count=100),
        from_filters=(),
        limit=None,
        dry_run=False,
        batch_size=200,
    )
    defaults.update(kw)
    return FetchRequest(**defaults)


def test_run_fetch_stores_messages_addresses_and_cursor():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request())
    assert summary.fetched == 3
    assert summary.per_list["t"] == 3
    # cursor advanced to max UID, list stamped
    mlist = store.upsert_list("t", "Shared Folders/t")
    cursor = store.get_pull_state(mlist.id)
    assert cursor is not None and cursor.last_uid == 3 and cursor.uidvalidity == 1000
    assert store.get_list(mlist.id).last_synced_at is not None
    # address landed, normalized
    addr = store.upsert_address("user1@example.org")
    assert addr.email == "user1@example.org"
    store.close()


def test_split_headers_slices_at_the_blank_line():
    assert split_headers(b"From: a\r\nTo: b\r\n\r\nbody\r\n") == b"From: a\r\nTo: b\r\n"
    assert split_headers(b"From: a\nTo: b\n\nbody\n") == b"From: a\nTo: b\n"
    # A header-only blob (no body at all) comes back whole.
    assert split_headers(b"From: a\r\nTo: b\r\n") == b"From: a\r\nTo: b\r\n"


def test_parse_message_keeps_headers_verbatim_and_reparseable():
    raw = make_raw(
        message_id="<hdr@example.org>",
        from_header="=?utf-8?q?Andr=C3=A9?= <andre@example.org>",
        subject="Hello",
    )
    parsed = parse_message(raw)
    # Sliced out of the input, not re-serialized: a prefix of the raw bytes,
    # with the encoded word still encoded.
    assert raw.startswith(parsed.raw_headers)
    assert b"=?utf-8?q?" in parsed.raw_headers
    assert b"plain body" not in parsed.raw_headers
    # Re-parsing the stored bytes reproduces what the fetch derived.
    assert parse_header(parsed.raw_headers).from_name == parsed.from_name == "André"


# --- cursor rules: require_cursor and empty-folder seeding ---------------------


def test_since_pull_does_not_advance_an_existing_cursor():
    # A date search omits UIDs its criteria excluded without naming them, so a
    # tracked list's cursor must not move — advancing it would claim messages
    # as stored that never were (the late-arrival loss).
    fd = _folder({u: (datetime(2026, 5, u), f"u{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 1)
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert summary.fetched == 3  # uid 1 refetched: dates match, it isn't stored
    assert store.get_pull_state(mlist.id).last_uid == 1
    store.close()


def test_incremental_self_heals_the_span_a_date_pull_left_behind():
    # The recovery contract the no-advance rule rests on: a --since catch-up
    # discards a late-arriving message (Date before the period) and leaves the
    # cursor alone; the next incremental run fetches it by UID and stores it.
    fd = _folder(
        {
            1: (datetime(2026, 5, 1), "a@example.org"),
            2: (datetime(2026, 4, 1), "b@example.org"),  # held in moderation
            3: (datetime(2026, 5, 3), "c@example.org"),
        }
    )
    fd.dates = {u: datetime(2026, 5, u) for u in (1, 2, 3)}  # all ARRIVED in May
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 0)
    first = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert first.fetched == 2 and first.discarded_early == 1
    assert store.get_pull_state(mlist.id).last_uid == 0  # not advanced past uid 2
    second = run_fetch(client, store, _request(depth=DepthMode(incremental=True)))
    assert second.fetched == 1  # uid 2, recovered; 1 and 3 dedupe as stored
    assert second.duplicates == 2
    assert store.get_pull_state(mlist.id).last_uid == 3
    store.close()


def test_count_pull_does_not_advance_an_existing_cursor():
    # A count slice takes the top N, skipping anything in between; the webapp's
    # per-list pull uses this mode against tracked lists.
    fd = _folder({u: (datetime(2026, 5, u), f"u{u}@example.org") for u in range(1, 6)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 2)
    summary = run_fetch(client, store, _request(depth=DepthMode(count=2)))
    assert summary.fetched == 2  # uids 4 and 5; uid 3 skipped by the slice
    assert store.get_pull_state(mlist.id).last_uid == 2
    store.close()


def test_from_filtered_pull_never_writes_a_cursor():
    # A --from pull is a query, not a sync: it omits every other sender, so it
    # may neither adopt an untracked list nor advance a tracked one.
    fd = _folder(
        {
            1: (datetime(2026, 5, 1), "alice@example.org"),
            2: (datetime(2026, 5, 2), "bob@example.org"),
        }
    )
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(
        client,
        store,
        _request(depth=DepthMode(since="2026-05-01"), from_filters=("alice@example.org",)),
    )
    assert summary.fetched == 1
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_from_filtered_incremental_does_not_advance_the_cursor():
    fd = _folder(
        {
            1: (datetime(2026, 5, 1), "alice@example.org"),
            2: (datetime(2026, 5, 2), "bob@example.org"),
            3: (datetime(2026, 5, 3), "alice@example.org"),
        }
    )
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 1)
    summary = run_fetch(
        client,
        store,
        _request(depth=DepthMode(incremental=True), from_filters=("alice@example.org",)),
    )
    assert summary.fetched == 1  # uid 3 only
    # Advancing to 3 would bury bob's uid 2 forever; the cursor must hold.
    assert store.get_pull_state(mlist.id).last_uid == 1
    store.close()


def test_date_pull_still_adopts_an_untracked_list():
    # The first unfiltered pull of a list defines its scope — the claim every
    # existing cursor was created under, and how a discovery sweep takes on a
    # list that is new on the server.
    fd = _folder({u: (datetime(2026, 5, u), f"u{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-02")))
    assert summary.fetched == 2  # uids 2 and 3
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id).last_uid == 3
    store.close()


def test_run_fetch_incremental_requires_cursor_skips_untracked_folder():
    # The --all-lists --incremental case: no cursor means a list never asked
    # for, so its history must NOT be backfilled.
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(
        client, store, _request(depth=DepthMode(incremental=True, require_cursor=True))
    )
    assert summary.fetched == 0
    assert summary.matched == 0
    assert summary.untracked_skipped == 1
    assert store.conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"] == 0
    # skipped without examining the folder, so no cursor was invented for it
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_incremental_requires_cursor_still_pulls_a_tracked_folder():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 1)  # tracked through uid 1
    summary = run_fetch(
        client, store, _request(depth=DepthMode(incremental=True, require_cursor=True))
    )
    assert summary.untracked_skipped == 0
    assert summary.fetched == 2  # uids 2 and 3 only
    assert store.get_pull_state(mlist.id).last_uid == 3
    store.close()


def test_run_fetch_incremental_without_require_cursor_takes_a_full_first_pull():
    # A named list keeps the bootstrap behaviour: `mail-ai-pull t --incremental`.
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(incremental=True)))
    assert summary.fetched == 3
    assert summary.untracked_skipped == 0
    store.close()


def test_run_fetch_seeds_a_cursor_for_an_empty_folder():
    # A discovery pull registers an empty list at UIDNEXT - 1, so a later
    # --incremental run tracks it and catches its first ever message.
    fd = FakeFolder(uidvalidity=1000, uidnext=42, exists=0)
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2025-01-01")))
    assert summary.fetched == 0
    assert summary.cursors_seeded == 1
    mlist = store.upsert_list("t", "Shared Folders/t")
    cursor = store.get_pull_state(mlist.id)
    assert cursor is not None and cursor.last_uid == 41 and cursor.uidvalidity == 1000
    store.close()


def test_run_fetch_does_not_seed_a_cursor_for_a_non_empty_folder():
    # Only `exists == 0` justifies the completeness claim. A folder that matched
    # nothing this run may still hold messages, so it must stay untracked.
    fd = _folder({1: (datetime(2024, 1, 1), "a@example.org")}, uidnext=99)
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2025-06-01")))
    assert summary.fetched == 0  # the 2024 message is out of the period
    assert summary.cursors_seeded == 0
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_seeding_leaves_an_existing_cursor_alone():
    fd = FakeFolder(uidvalidity=1000, uidnext=42, exists=0)
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    mlist = store.upsert_list("t", "Shared Folders/t")
    store.set_pull_state(mlist.id, 1000, 7)
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2025-01-01")))
    assert summary.cursors_seeded == 0
    assert store.get_pull_state(mlist.id).last_uid == 7
    store.close()


def test_run_fetch_dry_run_seeds_no_cursor():
    fd = FakeFolder(uidvalidity=1000, uidnext=42, exists=0)
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2025-01-01"), dry_run=True))
    assert summary.cursors_seeded == 0
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_stores_raw_headers():
    fd = _folder({1: (datetime(2025, 1, 1), "a@example.org")})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    row = store.conn.execute("SELECT raw_headers FROM messages").fetchone()
    assert isinstance(row["raw_headers"], bytes)
    assert b"Message-ID:" in row["raw_headers"]
    store.close()


def test_run_fetch_stores_each_messages_own_from_name():
    # A notification sender puts a different person's name in From on each
    # message. The address row keeps the first name it was seen with; every
    # message keeps the name its own header carried.
    fd = FakeFolder(uidvalidity=1000, uidnext=999, exists=2)
    for uid, name in ((1, "First Person"), (2, "Second Person")):
        date = datetime(2025, 1, uid)
        fd.messages[uid] = make_raw(
            message_id=f"<{uid}@example.org>",
            from_header=f"{name} <noreply@example.org>",
            date=date.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        )
        fd.dates[uid] = date
        fd.froms[uid] = "noreply@example.org"

    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())

    rows = store.conn.execute("SELECT from_name FROM messages ORDER BY uid").fetchall()
    assert [r["from_name"] for r in rows] == ["First Person", "Second Person"]
    assert store.upsert_address("noreply@example.org").display_name == "First Person"
    store.close()


def test_run_fetch_links_dmarc_rewrites():
    # The pull reconciles the pairing, so a rewrite and its original arriving on
    # different runs (or different lists) still end up as one sender.
    fd = _folder({1: (datetime(2025, 1, 1), "maarten.simon@sidn.nl")})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    assert store.upsert_address("maarten.simon@sidn.nl").person_id is None

    fd2 = _folder({2: (datetime(2025, 1, 2), "maarten.simon=40sidn.nl@dmarc.ietf.org")})
    client2 = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd2}))
    run_fetch(client2, store, _request())

    person_id = store.upsert_address("maarten.simon@sidn.nl").person_id
    assert person_id is not None
    assert store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org").person_id == person_id
    store.close()


def test_run_fetch_dry_run_does_not_link_dmarc_rewrites():
    fd = _folder({1: (datetime(2025, 1, 1), "a@example.org")})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    store.upsert_address("maarten.simon@sidn.nl", "Maarten Simon")
    store.upsert_address("maarten.simon=40sidn.nl@dmarc.ietf.org", "Maarten Simon")
    run_fetch(client, store, _request(dry_run=True))
    assert store.upsert_address("maarten.simon@sidn.nl").person_id is None
    store.close()


def test_run_fetch_sets_last_message_at():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    mlist = store.upsert_list("t", "Shared Folders/t")
    # Newest message is uid 3 (dated 2025-01-03), recorded as UTC ISO-8601.
    assert store.get_list(mlist.id).last_message_at == "2025-01-03T00:00:00+00:00"
    store.close()


def test_run_fetch_is_idempotent_on_repull():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    second = run_fetch(client, store, _request())
    assert second.fetched == 0
    assert second.duplicates == 3
    store.close()


def test_run_fetch_limit_caps_messages():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 6)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(limit=3))
    assert summary.fetched == 3
    store.close()


def test_run_fetch_dry_run_stores_nothing():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(dry_run=True))
    assert summary.matched == 3
    assert summary.fetched == 0
    assert conn.fetch_calls == []  # never fetched bodies
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_counts_html_only():
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<h@x>", plain=None, html="<p>hi</p>")
    fd.dates[1] = datetime(2025, 1, 1)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request())
    assert summary.fetched == 1
    assert summary.html_only == 1
    store.close()


def test_run_fetch_stores_raw_html():
    # A multipart/alternative message stores both raw_body and raw_html.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(
        message_id="<mix@x>", plain="the plain body", html="<p>the html body</p>"
    )
    fd.dates[1] = datetime(2025, 1, 1)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request())
    row = store.conn.execute(
        "SELECT raw_body, raw_html FROM messages WHERE message_id = ?", ("<mix@x>",)
    ).fetchone()
    assert "the plain body" in row["raw_body"]
    assert "the html body" in row["raw_html"]
    store.close()


def test_run_fetch_since_discards_messages_dated_before_period():
    # SINCE matches on INTERNALDATE (arrival), so re-imported history arrives
    # with much older Date headers: uid 2 arrived in-period but is dated 2003.
    # uid 1 is dated exactly midnight of the since day — the boundary is kept.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=2)
    fd.messages[1] = make_raw(message_id="<new@x>", date="Fri, 01 May 2026 00:00:00 +0000")
    fd.messages[2] = make_raw(message_id="<old@x>", date="Tue, 03 Jun 2003 09:00:00 +0000")
    fd.dates[1] = datetime(2026, 5, 2)
    fd.dates[2] = datetime(2026, 5, 15)
    fd.froms[1] = fd.froms[2] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert summary.fetched == 1
    assert summary.discarded_early == 1
    assert "discarded_early=1" in summary.as_line()
    rows = store.conn.execute("SELECT message_id FROM messages").fetchall()
    assert [r["message_id"] for r in rows] == ["<new@x>"]
    store.close()


def test_run_fetch_since_keeps_messages_without_a_date():
    # An unparsable/absent Date header cannot be compared, so the message is kept.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<undated@x>", date="")
    fd.dates[1] = datetime(2026, 5, 2)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert summary.fetched == 1
    assert summary.discarded_early == 0
    store.close()


def test_since_pull_prefilters_on_sent_date_server_side():
    # uid 2 arrived in-period (INTERNALDATE May 15) but its Date header is
    # 2003: the SENTSINCE prefilter excludes it server-side, so its body is
    # never downloaded. The search carries a one-day margin (SENTSINCE
    # 30-Apr) because SENTSINCE disregards the header's time zone.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=2)
    fd.messages[1] = make_raw(message_id="<new@x>", date="Sat, 02 May 2026 10:00:00 +0000")
    fd.messages[2] = make_raw(message_id="<old@x>", date="Tue, 03 Jun 2003 09:00:00 +0000")
    fd.dates[1] = datetime(2026, 5, 2)
    fd.dates[2] = datetime(2026, 5, 15)
    fd.sent_dates[1] = datetime(2026, 5, 2)
    fd.sent_dates[2] = datetime(2003, 6, 3)
    fd.froms[1] = fd.froms[2] = "a@example.org"
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert summary.fetched == 1
    assert summary.matched == 1  # uid 2 never even matched the search
    assert conn.search_calls == [("SINCE", "01-May-2026", "SENTSINCE", "30-Apr-2026")]
    assert conn.fetch_calls == ["1"]  # uid 2's body was never downloaded
    store.close()


def test_until_pull_bounds_the_period_server_side_and_discards_late_mail():
    # uid 1 is in July; uid 2 is sent 2 Aug (excluded server-side by
    # SENTBEFORE); uid 3 is sent 1 Aug 00:30 +0000, which passes the one-day
    # SENTBEFORE margin but is discarded client-side as on/after --until.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=3)
    fd.messages[1] = make_raw(message_id="<jul@x>", date="Mon, 15 Jul 2024 10:00:00 +0000")
    fd.messages[2] = make_raw(message_id="<aug@x>", date="Fri, 02 Aug 2024 10:00:00 +0000")
    fd.messages[3] = make_raw(message_id="<edge@x>", date="Thu, 01 Aug 2024 00:30:00 +0000")
    fd.dates = {1: datetime(2024, 7, 15), 2: datetime(2024, 8, 2), 3: datetime(2024, 8, 1)}
    fd.sent_dates = dict(fd.dates)
    fd.froms = {1: "a@example.org", 2: "a@example.org", 3: "a@example.org"}
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    depth = DepthMode(since="2024-07-01", until="2024-08-01")
    summary = run_fetch(client, store, _request(depth=depth))
    assert conn.search_calls == [
        ("SINCE", "01-Jul-2024", "SENTSINCE", "30-Jun-2024", "SENTBEFORE", "02-Aug-2024")
    ]
    assert summary.matched == 2
    assert summary.fetched == 1
    assert summary.discarded_late == 1
    assert "discarded_late=1" in summary.as_line()
    rows = store.conn.execute("SELECT message_id FROM messages").fetchall()
    assert [r["message_id"] for r in rows] == ["<jul@x>"]
    store.close()


def test_until_pull_never_writes_a_cursor_for_an_untracked_list():
    # An unfiltered first --since pull adopts its period as the list's scope;
    # with --until the period omits everything after it, so no cursor is written.
    fd = _folder({u: (datetime(2024, 7, u), f"u{u}@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    depth = DepthMode(since="2024-07-01", until="2024-08-01")
    summary = run_fetch(client, store, _request(depth=depth))
    assert summary.fetched == 3
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id) is None
    store.close()


def test_run_fetch_repull_skips_stored_bodies():
    # A second identical pull downloads nothing: the stored UIDs are
    # subtracted from the search result (UIDVALIDITY unchanged), counted as
    # duplicates, and the cursor still covers them.
    fd = _folder({u: (datetime(2026, 5, 2 + u), "a@example.org") for u in range(1, 4)})
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    calls_after_first = list(conn.fetch_calls)
    second = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert second.fetched == 0
    assert second.duplicates == 3
    assert conn.fetch_calls == calls_after_first  # no body re-downloaded
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id).last_uid == 3
    store.close()


def test_run_fetch_refetches_stored_uids_after_uidvalidity_change():
    # A changed UIDVALIDITY invalidates stored UIDs, so nothing is subtracted;
    # the bodies are re-downloaded and the message-id dedupe catches them.
    fd = _folder({u: (datetime(2026, 5, 2 + u), "a@example.org") for u in range(1, 3)})
    conn = FakeImapConn(folders={"Shared Folders/t": fd})
    client = ImapClient(conn)
    store = Store(":memory:")
    run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    fd.uidvalidity = 2000
    second = run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01")))
    assert second.fetched == 0
    assert second.duplicates == 2
    assert len(conn.fetch_calls) == 2  # one batched fetch per run
    store.close()


def test_run_fetch_limit_never_advances_cursor_past_unfetched_uids():
    # The adopting run stores uid 1 (limit 1) and must leave the cursor at 1 —
    # not at any higher matched-but-unfetched uid. The incremental run that
    # follows skips stored uid 1, fetches uid 2 under the same cap, and
    # advances the cursor to exactly 2.
    fd = _folder({u: (datetime(2026, 5, 2 + u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch(client, store, _request(depth=DepthMode(since="2026-05-01"), limit=1))
    mlist = store.upsert_list("t", "Shared Folders/t")
    assert store.get_pull_state(mlist.id).last_uid == 1
    second = run_fetch(client, store, _request(depth=DepthMode(incremental=True), limit=1))
    assert second.fetched == 1
    assert store.get_pull_state(mlist.id).last_uid == 2
    store.close()


def test_run_fetch_count_mode_never_discards():
    # Only date-based pulls have a period; --count stores old-dated mail as-is.
    fd = FakeFolder(uidvalidity=1000, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<old@x>", date="Tue, 03 Jun 2003 09:00:00 +0000")
    fd.dates[1] = datetime(2026, 5, 15)
    fd.froms[1] = "a@example.org"
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    summary = run_fetch(client, store, _request(depth=DepthMode(count=10)))
    assert summary.fetched == 1
    assert summary.discarded_early == 0
    store.close()


# --- run_fetch_uids -----------------------------------------------------------


def test_run_fetch_uids_fetches_exact_set_and_upserts():
    fd = _folder({u: (datetime(2025, 1, u), f"user{u}@example.org") for u in range(1, 6)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    # Pull only an explicit subset (uids 2 and 4), not the whole folder.
    summary = run_fetch_uids(client, store, "Shared Folders/t", [2, 4])
    assert summary.fetched == 2
    assert summary.matched == 2
    assert summary.per_list["t"] == 2
    mlist = store.get_list_by_name("t")
    stored = {
        row["uid"]
        for row in store.conn.execute(
            "SELECT uid FROM messages WHERE list_id = ?", (mlist.id,)
        ).fetchall()
    }
    assert stored == {2, 4}
    store.close()


def test_run_fetch_uids_leaves_cursor_and_sync_to_caller():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    mlist = store.get_list_by_name("t")
    # The wrapper deliberately does not touch pull_state or last_synced_at.
    assert store.get_pull_state(mlist.id) is None
    assert store.get_list(mlist.id).last_synced_at is None
    store.close()


def test_run_fetch_uids_is_idempotent_on_repull():
    fd = _folder({u: (datetime(2025, 1, u), "a@example.org") for u in range(1, 4)})
    client = ImapClient(FakeImapConn(folders={"Shared Folders/t": fd}))
    store = Store(":memory:")
    run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    second = run_fetch_uids(client, store, "Shared Folders/t", [1, 2, 3])
    assert second.fetched == 0
    assert second.duplicates == 3
    store.close()
