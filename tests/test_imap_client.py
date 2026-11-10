"""Unit tests for the read-only IMAP client wrapper (no network)."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check.imap_client import (
    ImapError,
    ImapClient,
    build_search_criteria,
    quote_folder,
    quote_imap_string,
    unquote_imap_string,
)


# --- pure helpers -------------------------------------------------------------


def test_quote_folder_wraps_in_quotes():
    assert quote_folder("Shared Folders/last-call") == '"Shared Folders/last-call"'


def test_quote_folder_leaves_real_folder_names_unchanged():
    # Every character class seen in real folder names (letters, digits, ".",
    # "-", "_", "+", space and "/") passes through unescaped.
    for name in ("Shared Folders/itu+ietf", "Shared Folders/some-list.name_2"):
        assert quote_folder(name) == f'"{name}"'


def test_quote_folder_escapes_backslash_before_quote():
    # A trailing backslash must not escape the closing quote, and an embedded
    # quote must not end the string early.
    assert quote_folder("evil\\") == '"evil\\\\"'
    assert quote_folder('a"b') == '"a\\"b"'
    assert quote_folder('a\\"b') == '"a\\\\\\"b"'


@pytest.mark.parametrize("bad", ["x\r\nA1 DELETE INBOX", "x\nA1 LOGOUT", "x\rA", "x\x00"])
def test_quote_folder_rejects_cr_lf_nul(bad):
    with pytest.raises(ValueError):
        quote_folder(bad)


def test_examine_refuses_injected_folder_before_any_command():
    conn = FakeImapConn(folders={})
    client = ImapClient(conn)
    with pytest.raises(ValueError):
        client.examine('Shared Folders/x" \r\nA2 DELETE "INBOX')
    assert conn.selected is None


def test_unquote_imap_string_reverses_quote():
    for value in ("plain", 'a"b', "trail\\", 'mix\\"ed'):
        assert unquote_imap_string(quote_imap_string(value)[1:-1]) == value


def test_build_search_criteria_escapes_from_addr():
    crit = build_search_criteria(from_addr='Jane "JD" Doe\\')
    assert crit == ["FROM", '"Jane \\"JD\\" Doe\\\\"']


def test_build_search_criteria_rejects_crlf_in_from_addr():
    with pytest.raises(ValueError):
        build_search_criteria(from_addr="a@example.org\r\nA9 LOGOUT")


def test_build_search_criteria_defaults_to_all():
    assert build_search_criteria() == ["ALL"]


def test_build_search_criteria_combines_since_uid_and_from():
    crit = build_search_criteria(since="01-Jan-2025", uid_range="42:*", from_addr="example.org")
    assert crit == ["UID", "42:*", "SINCE", "01-Jan-2025", "FROM", '"example.org"']


def test_build_search_criteria_since_only():
    assert build_search_criteria(since="06-Jan-2025") == ["SINCE", "06-Jan-2025"]


def test_build_search_criteria_sent_since_follows_since():
    crit = build_search_criteria(since="01-May-2026", sent_since="30-Apr-2026")
    assert crit == ["SINCE", "01-May-2026", "SENTSINCE", "30-Apr-2026"]


def test_build_search_criteria_sent_before_follows_sent_since():
    crit = build_search_criteria(
        since="01-Jul-2024", sent_since="30-Jun-2024", sent_before="02-Aug-2024"
    )
    assert crit == ["SINCE", "01-Jul-2024", "SENTSINCE", "30-Jun-2024", "SENTBEFORE", "02-Aug-2024"]


# --- folder listing -----------------------------------------------------------


def test_list_folders_parses_and_quotes_and_drops_noselect():
    lines = [
        rb'(\Noselect \HasChildren) "/" "Shared Folders"',
        rb'(\HasNoChildren) "/" "Shared Folders/announce"',
        rb'(\HasNoChildren) "/" "Shared Folders/last-call"',
    ]
    client = ImapClient(FakeImapConn(list_lines=lines))
    folders = client.list_folders()
    assert folders == ["Shared Folders/announce", "Shared Folders/last-call"]


def test_list_folders_handles_unquoted_names():
    lines = [rb'(\HasNoChildren) "/" Shared_Folders_quic']
    client = ImapClient(FakeImapConn(list_lines=lines))
    assert client.list_folders() == ["Shared_Folders_quic"]


def test_list_folders_unescapes_quoted_names():
    lines = [rb'(\HasNoChildren) "/" "Shared Folders/a\"b\\c"']
    client = ImapClient(FakeImapConn(list_lines=lines))
    folders = client.list_folders()
    assert folders == ['Shared Folders/a"b\\c']
    assert quote_folder(folders[0]) == '"Shared Folders/a\\"b\\\\c"'


# --- examine ------------------------------------------------------------------


def test_examine_returns_status():
    folder = FakeFolder(uidvalidity=1455297825, uidnext=146390, exists=146312)
    conn = FakeImapConn(folders={"Shared Folders/announce": folder})
    client = ImapClient(conn)
    status = client.examine("Shared Folders/announce")
    assert status.uidvalidity == 1455297825
    assert status.uidnext == 146390
    assert status.exists == 146312
    # read-only EXAMINE, i.e. select(..., readonly=True)
    assert conn.selected == "Shared Folders/announce"


# --- search -------------------------------------------------------------------


def _folder_with(uids_from_dates):
    fd = FakeFolder(uidvalidity=1, uidnext=100)
    for uid, (d, frm) in uids_from_dates.items():
        fd.messages[uid] = make_raw(message_id=f"<{uid}@x>")
        fd.dates[uid] = d
        fd.froms[uid] = frm
    return fd


def test_uid_search_all_returns_sorted_ints():
    fd = _folder_with(
        {
            3: (datetime(2025, 1, 3), "a@x"),
            1: (datetime(2025, 1, 1), "b@x"),
            2: (datetime(2025, 1, 2), "c@x"),
        }
    )
    client = ImapClient(FakeImapConn(folders={"f": fd}))
    client.examine("f")
    assert client.uid_search(["ALL"]) == [1, 2, 3]


def test_uid_search_empty_result():
    fd = FakeFolder(uidvalidity=1, uidnext=1)
    client = ImapClient(FakeImapConn(folders={"f": fd}))
    client.examine("f")
    assert client.uid_search(["ALL"]) == []


def test_uid_search_since_and_from_filter():
    fd = _folder_with(
        {
            1: (datetime(2024, 12, 1), "old@x"),
            2: (datetime(2025, 2, 1), "keep@example.org"),
            3: (datetime(2025, 3, 1), "other@x"),
        }
    )
    client = ImapClient(FakeImapConn(folders={"f": fd}))
    client.examine("f")
    assert client.uid_search(build_search_criteria(since="01-Jan-2025")) == [2, 3]
    assert client.uid_search(build_search_criteria(from_addr="example.org")) == [2]


# --- fetch --------------------------------------------------------------------


def test_fetch_bodies_maps_uid_and_batches():
    fd = FakeFolder(uidvalidity=1, uidnext=10)
    for uid in (5, 6, 7):
        fd.messages[uid] = make_raw(message_id=f"<{uid}@x>")
    conn = FakeImapConn(folders={"f": fd})
    client = ImapClient(conn)
    client.examine("f")
    got = list(client.fetch_bodies([5, 6, 7], batch_size=2))
    uids = [u for u, _ in got]
    assert uids == [5, 6, 7]
    # batch_size=2 → two FETCH round-trips
    assert len(conn.fetch_calls) == 2
    assert conn.fetch_calls[0] == "5,6"
    assert all(raw.startswith(b"Message-ID") or b"Message-ID" in raw for _, raw in got)


def test_fetch_headers_maps_uid_and_batches():
    fd = FakeFolder(uidvalidity=1, uidnext=10)
    for uid in (5, 6, 7):
        fd.messages[uid] = make_raw(message_id=f"<{uid}@x>", subject=f"S{uid}")
    conn = FakeImapConn(folders={"f": fd})
    client = ImapClient(conn)
    client.examine("f")
    got = list(client.fetch_headers([5, 6, 7], batch_size=2))
    uids = [u for u, _ in got]
    assert uids == [5, 6, 7]
    # batch_size=2 → two FETCH round-trips, header-field FETCH item on the wire.
    assert len(conn.fetch_calls) == 2
    assert conn.fetch_calls[0] == "5,6"
    # The returned bytes carry the requested headers (the fake serves full raw).
    assert all(b"Subject" in raw for _, raw in got)


def test_fetch_headers_failure_raises():
    class Boom(FakeImapConn):
        def uid(self, command, *args):
            if command == "FETCH":
                return ("NO", [b"denied"])
            return super().uid(command, *args)

    fd = FakeFolder(uidvalidity=1, uidnext=10)
    fd.messages[1] = make_raw(message_id="<1@x>")
    client = ImapClient(Boom(folders={"f": fd}))
    client.examine("f")
    with pytest.raises(ImapError):
        list(client.fetch_headers([1]))


def test_last_message_internaldate_parses_to_utc_iso():
    fd = FakeFolder(uidvalidity=1, uidnext=10, exists=2)
    fd.messages[1] = make_raw(message_id="<1@x>")
    fd.messages[2] = make_raw(message_id="<2@x>")
    fd.dates[2] = datetime(2025, 1, 6, 10, 0, 0)  # newest (highest seq)
    conn = FakeImapConn(folders={"f": fd})
    client = ImapClient(conn)
    assert client.last_message_internaldate("f") == "2025-01-06T10:00:00+00:00"
    # Only the last message was fetched, and the folder was EXAMINEd.
    assert conn.internaldate_calls == ["f"]


def test_last_message_internaldate_empty_folder_returns_none():
    fd = FakeFolder(uidvalidity=1, uidnext=1, exists=0)
    conn = FakeImapConn(folders={"f": fd})
    client = ImapClient(conn)
    assert client.last_message_internaldate("f") is None
    # An empty folder is never fetched.
    assert conn.internaldate_calls == []


def test_last_message_internaldate_garbage_response_returns_none():
    class Garbage(FakeImapConn):
        def fetch(self, message_set, message_parts):
            return ("OK", [b"not an internaldate at all"])

    fd = FakeFolder(uidvalidity=1, uidnext=10, exists=1)
    fd.messages[1] = make_raw(message_id="<1@x>")
    client = ImapClient(Garbage(folders={"f": fd}))
    assert client.last_message_internaldate("f") is None


def test_search_failure_raises():
    class Boom(FakeImapConn):
        def uid(self, command, *args):
            return ("NO", [b"denied"])

    fd = FakeFolder(uidvalidity=1, uidnext=1)
    client = ImapClient(Boom(folders={"f": fd}))
    client.examine("f")
    with pytest.raises(ImapError):
        client.uid_search(["ALL"])
