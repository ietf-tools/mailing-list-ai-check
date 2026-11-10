"""Shared test helpers: a network-free fake IMAP connection.

``FakeImapConn`` implements exactly the small :mod:`imaplib` surface that
:class:`mailing_list_ai_check.imap_client.ImapClient` calls (``login``, ``list``,
``select``, ``response``, ``uid``, ``close``, ``logout``) and interprets the
``UID SEARCH`` criteria the fetcher builds (``ALL`` / ``UID n:*`` / ``SINCE`` /
``SENTSINCE`` / ``SENTBEFORE`` / ``FROM``), so tests never touch the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage


def make_raw(
    *,
    message_id: str = "<m@example.org>",
    from_header: str = "Alice <alice@example.org>",
    subject: str = "Hello",
    date: str = "Mon, 06 Jan 2025 10:00:00 +0000",
    in_reply_to: str | None = None,
    plain: str | None = "plain body",
    html: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    """Build raw RFC 5322 bytes, optionally multipart/alternative or HTML-only."""
    msg = EmailMessage()
    if message_id:
        msg["Message-ID"] = message_id
    msg["From"] = from_header
    msg["Subject"] = subject
    msg["Date"] = date
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    for name, value in (extra_headers or {}).items():
        msg[name] = value

    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif html is not None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(plain if plain is not None else "")
    return msg.as_bytes()


@dataclass
class FakeFolder:
    """A fake mailbox: UID→raw bytes plus per-UID date/from for search.

    ``dates`` plays INTERNALDATE (matched by ``SINCE``); ``sent_dates`` plays
    the ``Date`` header's date (matched by ``SENTSINCE``), falling back to
    ``dates`` per UID so tests that never set it behave as before.
    """

    uidvalidity: int
    uidnext: int
    exists: int = 0
    messages: dict[int, bytes] = field(default_factory=dict)
    dates: dict[int, datetime] = field(default_factory=dict)
    sent_dates: dict[int, datetime] = field(default_factory=dict)
    froms: dict[int, str] = field(default_factory=dict)

    def match(self, criteria: tuple[str, ...]) -> list[int]:
        uids = set(self.messages)
        result = set(uids)
        crit = list(criteria)
        j = 0
        while j < len(crit):
            tok = crit[j]
            if tok == "ALL":
                j += 1
            elif tok == "UID":
                lo_s, _, hi_s = crit[j + 1].partition(":")
                lo = int(lo_s)
                result &= {u for u in uids if u >= lo}
                if hi_s and hi_s != "*":
                    hi = int(hi_s)
                    result &= {u for u in uids if u <= hi}
                j += 2
            elif tok == "SINCE":
                since = datetime.strptime(crit[j + 1], "%d-%b-%Y")
                result &= {u for u in uids if self.dates.get(u, datetime.min) >= since}
                j += 2
            elif tok == "SENTSINCE":
                since = datetime.strptime(crit[j + 1], "%d-%b-%Y")
                result &= {
                    u
                    for u in uids
                    if self.sent_dates.get(u, self.dates.get(u, datetime.min)) >= since
                }
                j += 2
            elif tok == "SENTBEFORE":
                before = datetime.strptime(crit[j + 1], "%d-%b-%Y")
                result &= {
                    u
                    for u in uids
                    if self.sent_dates.get(u, self.dates.get(u, datetime.max)) < before
                }
                j += 2
            elif tok == "FROM":
                term = crit[j + 1].strip('"').lower()
                result &= {u for u in uids if term in self.froms.get(u, "").lower()}
                j += 2
            else:
                j += 1
        return sorted(result)


class FakeImapConn:
    """A stand-in for an :class:`imaplib.IMAP4_SSL` connection."""

    def __init__(
        self,
        folders: dict[str, FakeFolder] | None = None,
        list_lines: list[bytes] | None = None,
    ) -> None:
        self.folders = folders or {}
        self.list_lines = list_lines or []
        self.selected: str | None = None
        self._responses: dict[str, list] = {}
        self.logged_in = False
        self.closed = False
        self.logged_out = False
        self.search_calls: list[tuple[str, ...]] = []
        self.fetch_calls: list[str] = []
        # Folders whose last-message INTERNALDATE was fetched (via fetch("*", ...)),
        # in call order — lets activity-check tests assert what got EXAMINEd.
        self.internaldate_calls: list[str] = []

    def login(self, user, password):
        self.logged_in = True
        return ("OK", [b"ok"])

    def list(self, *args, **kwargs):
        return ("OK", list(self.list_lines))

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        self.selected = name
        fd = self.folders[name]
        self._responses = {
            "UIDVALIDITY": [str(fd.uidvalidity).encode()],
            "UIDNEXT": [str(fd.uidnext).encode()],
        }
        return ("OK", [str(fd.exists).encode()])

    def response(self, key):
        return (key, self._responses.get(key, [None]))

    def uid(self, command, *args):
        fd = self.folders[self.selected]
        if command == "SEARCH":
            criteria = tuple(str(a) for a in args[1:])
            self.search_calls.append(criteria)
            uids = fd.match(criteria)
            return ("OK", [" ".join(str(u) for u in uids).encode()])
        if command == "FETCH":
            id_list = args[0]
            self.fetch_calls.append(id_list)
            wanted = [int(x) for x in id_list.split(",") if x]
            out: list = []
            for u in wanted:
                raw = fd.messages.get(u)
                if raw is None:
                    continue
                descriptor = f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode()
                out.append((descriptor, raw))
                out.append(b")")
            return ("OK", out)
        raise AssertionError(f"unexpected uid command {command}")

    def fetch(self, message_set, message_parts):
        """Serve a sequence ``FETCH`` of ``(INTERNALDATE)`` for the last message.

        Only the shape :meth:`ImapClient.last_message_internaldate` issues is
        supported: ``fetch("*", "(INTERNALDATE)")``. ``*`` resolves to the
        highest sequence number (max UID here); the INTERNALDATE is formatted
        from the folder's stored date for that message.
        """
        fd = self.folders[self.selected]
        self.internaldate_calls.append(self.selected)
        if not fd.messages:
            return ("OK", [])
        seq = max(fd.messages) if message_set == "*" else int(message_set)
        date = fd.dates.get(seq, datetime(2025, 1, 1))
        stamp = date.strftime("%d-%b-%Y %H:%M:%S +0000")
        line = f'{seq} (INTERNALDATE "{stamp}")'.encode()
        return ("OK", [line])

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"bye"])
