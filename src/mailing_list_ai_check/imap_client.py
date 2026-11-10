"""Thin read-only wrapper over stdlib :mod:`imaplib` for the archive IMAP server.

Everything here is strictly **read-only** — folders are opened with ``EXAMINE``
(``select(..., readonly=True)``) and bodies are fetched with ``BODY.PEEK[]`` so
the ``\\Seen`` flag is never set. No write, flag, copy, move or delete command
is ever issued.

The archive server this was developed against (Isode M-Box, verified in
``docs/findings/imap.md``) exposes every mailing-list archive as a flat
``Shared Folders/<listname>`` mailbox and
supports server-side ``UID SEARCH SINCE``/``FROM``, so both date and sender
filtering are pushed down to the server rather than done client-side.

The public surface is intentionally small so the fetcher (and its tests) can
drive a fake connection: :class:`ImapClient` wraps any object implementing the
handful of :mod:`imaplib` methods used here (``login``, ``list``, ``select``,
``response``, ``uid``, ``close``, ``logout``).
"""

from __future__ import annotations

import imaplib
import re
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

#: The flat namespace prefix every list folder lives under.
FOLDER_PREFIX = "Shared Folders/"

#: imaplib's default line cap (10 KB) overflows on the archive server's large
#: ``LIST`` and ``FETCH`` responses (findings §8). Raise it generously.
MAXLINE = 10_000_000

#: Default number of UIDs per ``UID FETCH`` round-trip. Throughput improves with
#: bigger batches (findings §6); a few hundred is comfortable.
DEFAULT_BATCH_SIZE = 200

# LIST response line, e.g.  (\HasNoChildren) "/" "Shared Folders/announce"
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+"(?P<sep>[^"]*)"\s+(?P<name>.+)$')
# UID token inside a FETCH response's descriptor line.
_UID_RE = re.compile(rb"UID (\d+)")


@dataclass(frozen=True)
class FolderStatus:
    """Read-only status of an ``EXAMINE``-selected folder."""

    folder: str
    uidvalidity: int
    uidnext: int | None
    exists: int | None


#: Characters an RFC 3501 ``quoted`` string cannot carry at all (``QUOTED-CHAR``
#: excludes CR and LF, and NUL is not a valid ``CHAR``). A value containing one
#: would need a literal, which this client never sends; it is refused instead.
_UNQUOTABLE = frozenset("\r\n\x00")


def quote_imap_string(value: str) -> str:
    """Return ``value`` as an RFC 3501 quoted string.

    Backslash is escaped before double quote, so every ``\\`` and ``"`` in the
    value reaches the server as data and the string cannot end early. Raises
    :class:`ValueError` for a value containing CR, LF or NUL: those characters
    cannot appear in a quoted string, and passing them through would end the
    command line and let the remainder be read as a further command.
    """
    bad = _UNQUOTABLE.intersection(value)
    if bad:
        raise ValueError(
            f"IMAP string contains a character that cannot be quoted "
            f"({', '.join(sorted(repr(c) for c in bad))}): {value!r}"
        )
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unquote_imap_string(value: str) -> str:
    """Reverse :func:`quote_imap_string` for a quoted string's contents.

    ``value`` is the text between the surrounding double quotes; each ``\\x``
    escape is replaced by ``x``.
    """
    return re.sub(r"\\(.)", r"\1", value)


def quote_folder(folder: str) -> str:
    """Wrap a mailbox name in double quotes for ``SELECT``/``EXAMINE``.

    These folder names contain ``/`` and hyphens and must always be quoted
    (findings §8). Quoting follows :func:`quote_imap_string`, which also raises
    :class:`ValueError` for a name containing CR, LF or NUL.
    """
    return quote_imap_string(folder)


def build_search_criteria(
    *,
    since: str | None = None,
    sent_since: str | None = None,
    sent_before: str | None = None,
    uid_range: str | None = None,
    from_addr: str | None = None,
) -> list[str]:
    """Build a server-side ``UID SEARCH`` criteria token list.

    Any combination of a ``SINCE`` date (``DD-Mon-YYYY``), a ``SENTSINCE``
    date, a ``SENTBEFORE`` date, a ``UID`` range (e.g. ``"42:*"`` for incremental pulls) and a single
    ``FROM`` substring is AND-ed together. ``SINCE`` matches on INTERNALDATE
    (arrival in the folder); ``SENTSINCE`` matches on the message's own
    ``Date`` header (RFC 3501 base search key, live-verified on the archive
    server 2026-08-14), which lets a date-based pull exclude re-imported old
    history before any body is downloaded; ``SENTBEFORE`` (also a Date-header
    key, exclusive) bounds the period's end the same way. When nothing is supplied the
    criteria default to ``ALL``.

    ``from_addr`` is quoted (see :func:`quote_imap_string`) so multi-word
    display-name terms survive; a value containing CR, LF or NUL raises
    :class:`ValueError`.
    """
    criteria: list[str] = []
    if uid_range is not None:
        criteria += ["UID", uid_range]
    if since is not None:
        criteria += ["SINCE", since]
    if sent_since is not None:
        criteria += ["SENTSINCE", sent_since]
    if sent_before is not None:
        criteria += ["SENTBEFORE", sent_before]
    if from_addr is not None:
        criteria += ["FROM", quote_imap_string(from_addr)]
    return criteria or ["ALL"]


class ImapClient:
    """Minimal read-only IMAP client over an :mod:`imaplib`-style connection."""

    def __init__(self, conn: object) -> None:
        self._conn = conn

    @classmethod
    def connect(cls, host: str, port: int, username: str, password: str) -> "ImapClient":
        """Open a TLS connection, raise the line cap, and log in.

        Defaults from :class:`Config` are the public anonymous login, which
        grants read access to every public list archive.
        """
        imaplib._MAXLINE = MAXLINE  # type: ignore[attr-defined]
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(username, password)
        return cls(conn)

    # -- folder enumeration ---------------------------------------------------

    def list_folders(self) -> list[str]:
        """Return every selectable list folder (full ``Shared Folders/...`` name).

        Parses the ``LIST`` response, drops ``\\Noselect`` nodes (the namespace
        root) and unquotes the mailbox names, removing quoted-string escapes so
        that :func:`quote_folder` reproduces the server's form.
        """
        typ, data = self._conn.list()  # type: ignore[attr-defined]
        if typ != "OK":
            raise ImapError(f"LIST failed: {typ}")
        folders: list[str] = []
        for line in data:
            if line is None:
                continue
            raw = line if isinstance(line, bytes) else str(line).encode()
            match = _LIST_RE.match(raw.strip())
            if not match:
                continue
            flags = match.group("flags").decode(errors="replace").lower()
            if "\\noselect" in flags:
                continue
            name = match.group("name").decode(errors="replace").strip()
            if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
                name = unquote_imap_string(name[1:-1])
            folders.append(name)
        return folders

    # -- read-only selection --------------------------------------------------

    def examine(self, folder: str) -> FolderStatus:
        """``EXAMINE`` (read-only select) ``folder`` and return its status."""
        typ, data = self._conn.select(quote_folder(folder), readonly=True)  # type: ignore[attr-defined]
        if typ != "OK":
            raise ImapError(f"EXAMINE {folder!r} failed: {typ} {data!r}")
        exists: int | None = None
        if data and data[0] is not None:
            try:
                exists = int(data[0])
            except TypeError, ValueError:
                exists = None
        return FolderStatus(
            folder=folder,
            uidvalidity=self._response_int("UIDVALIDITY"),
            uidnext=self._response_int("UIDNEXT", required=False),
            exists=exists,
        )

    def _response_int(self, key: str, *, required: bool = True) -> int:
        typ, values = self._conn.response(key)  # type: ignore[attr-defined]
        if values and values[0] is not None:
            return int(values[0])
        if required:
            raise ImapError(f"no {key} in server response")
        return 0

    def last_message_internaldate(self, folder: str) -> str | None:
        """Return the newest message's ``INTERNALDATE`` for ``folder`` as UTC ISO-8601.

        ``EXAMINE``s the folder read-only and, when it holds any messages,
        fetches only the last one by sequence number (``*``) for its
        ``INTERNALDATE`` — a single cheap round-trip that records when the server
        last saw traffic on the list. Returns ``None`` for an empty folder or a
        missing/malformed response; a bad reply is swallowed rather than raised,
        so an activity check never fails a pull.
        """
        status = self.examine(folder)
        if not status.exists:
            return None
        typ, data = self._conn.fetch("*", "(INTERNALDATE)")  # type: ignore[attr-defined]
        if typ != "OK" or not data:
            return None
        for item in data:
            raw = item[0] if isinstance(item, tuple) else item
            if not isinstance(raw, (bytes, bytearray)):
                continue
            # Internaldate2tuple yields a local-time struct_time; round-trip it
            # back through mktime so the stored value is unambiguously UTC.
            parsed = imaplib.Internaldate2tuple(bytes(raw))
            if parsed is not None:
                epoch = time.mktime(parsed)
                return datetime.fromtimestamp(epoch, tz=UTC).isoformat()
        return None

    # -- search ---------------------------------------------------------------

    def uid_search(self, criteria: Sequence[str]) -> list[int]:
        """Run ``UID SEARCH`` and return matching UIDs as ints (ascending)."""
        typ, data = self._conn.uid("SEARCH", None, *criteria)  # type: ignore[attr-defined]
        if typ != "OK":
            raise ImapError(f"UID SEARCH {list(criteria)!r} failed: {typ}")
        if not data or data[0] in (None, b"", ""):
            return []
        blob = data[0]
        if isinstance(blob, str):
            blob = blob.encode()
        return sorted(int(tok) for tok in blob.split())

    # -- fetch ----------------------------------------------------------------

    def _fetch(
        self, uids: Sequence[int], fetch_item: str, batch_size: int
    ) -> Iterator[tuple[int | None, bytes]]:
        """Yield ``(uid, raw_bytes)`` for a batched ``UID FETCH`` of ``fetch_item``.

        Shared by :meth:`fetch_bodies` and :meth:`fetch_headers`. ``fetch_item`` is
        the parenthesised FETCH data item (e.g. ``(UID BODY.PEEK[])``). Each
        response tuple's descriptor line carries the ``UID`` token that maps the
        returned literal back to its UID regardless of server ordering; malformed
        or non-literal items are skipped rather than raising.
        """
        for start in range(0, len(uids), batch_size):
            chunk = uids[start : start + batch_size]
            id_list = ",".join(str(u) for u in chunk)
            typ, data = self._conn.uid("FETCH", id_list, fetch_item)  # type: ignore[attr-defined]
            if typ != "OK":
                raise ImapError(f"UID FETCH failed: {typ}")
            for item in data:
                if not isinstance(item, tuple) or len(item) < 2:
                    continue
                descriptor, raw = item[0], item[1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                uid: int | None = None
                if isinstance(descriptor, (bytes, bytearray)):
                    match = _UID_RE.search(descriptor)
                    if match:
                        uid = int(match.group(1))
                yield uid, bytes(raw)

    def fetch_bodies(
        self, uids: Sequence[int], *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[tuple[int | None, bytes]]:
        """Yield ``(uid, raw_rfc822_bytes)`` for ``uids``, batched.

        Uses ``(UID BODY.PEEK[])`` — ``PEEK`` leaves ``\\Seen`` untouched, and the
        explicit ``UID`` item lets us map each body back to its UID regardless of
        server response ordering.
        """
        yield from self._fetch(uids, "(UID BODY.PEEK[])", batch_size)

    def fetch_headers(
        self, uids: Sequence[int], *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[tuple[int | None, bytes]]:
        """Yield ``(uid, raw_header_bytes)`` for ``uids`` — header fields only, batched.

        Uses ``(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])``: ``PEEK`` leaves
        ``\\Seen`` untouched and only the three named header fields are transferred,
        so a message-list preview is far cheaper than a full-body fetch. The
        returned bytes are just those headers (parse with the stdlib
        :mod:`email` package). Response tuples are matched with the same lenient
        shape as :meth:`fetch_bodies` — servers echo the requested item as e.g.
        ``BODY[HEADER.FIELDS (...)]`` — and the ``UID`` item maps each blob to its
        UID regardless of ordering.
        """
        yield from self._fetch(
            uids, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])", batch_size
        )

    def fetch_full_headers(
        self, uids: Sequence[int], *, batch_size: int = DEFAULT_BATCH_SIZE
    ) -> Iterator[tuple[int | None, bytes]]:
        """Yield ``(uid, raw_header_block)`` for ``uids`` — every header, batched.

        Uses ``(UID BODY.PEEK[HEADER])``, which transfers the whole header block
        rather than the three fields :meth:`fetch_headers` names. Still far
        cheaper than :meth:`fetch_bodies`, and it is what the
        ``--backfill-headers`` pull mode stores, since the point of that column
        is to hold every header, not a chosen few. ``PEEK`` leaves ``\\Seen``
        untouched and responses are matched exactly as in :meth:`fetch_bodies`.
        """
        yield from self._fetch(uids, "(UID BODY.PEEK[HEADER])", batch_size)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the selected folder (ignored if none selected)."""
        try:
            self._conn.close()  # type: ignore[attr-defined]
        except Exception:
            pass

    def logout(self) -> None:
        """Log out from the server."""
        try:
            self._conn.logout()  # type: ignore[attr-defined]
        except Exception:
            pass


class ImapError(RuntimeError):
    """Raised when the IMAP server returns a non-``OK`` status."""
