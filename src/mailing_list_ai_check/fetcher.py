"""Fetch orchestration: IMAP selection → parsed messages → the SQLite store.

Resolves a user's list/depth/sender selection into per-folder UID sets, fetches
them in batches over :class:`~mailing_list_ai_check.imap_client.ImapClient`, parses
each RFC 5322 message with the stdlib :mod:`email` package (``policy=default``,
so RFC 2047 headers decode automatically), and upserts the result through
:class:`~mailing_list_ai_check.store.Store`.

Every stage is idempotent: re-pulling a message is a no-op (dedupe on
``(list_id, message_id)``), and the per-folder ``(uidvalidity, last_uid)`` cursor
lets ``--incremental`` resume.

HTML-only messages
------------------
When a message has no ``text/plain`` part (HTML only), we **store the row with an
empty ``raw_body``** (``raw_body = None``) — but we now also capture the decoded
``text/html`` part into ``raw_html`` whenever one is present (HTML-only or
alongside a plain part). The HTML gives the extraction pipeline a structural
oracle (see :mod:`email_reply_extractor.html_text`): it can recover novel text from
HTML-only messages and use ``<blockquote>``/Gmail/Outlook quote containers as
evidence for what is quoted. HTML-only rows are still counted separately
(``html_only``) in the run summary so they are visible, not silently dropped.
"""

from __future__ import annotations

import email
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime

from .autogen import classify_message, is_excluded_list
from .imap_client import (
    DEFAULT_BATCH_SIZE,
    FOLDER_PREFIX,
    FolderStatus,
    ImapClient,
    build_search_criteria,
)
from .store import Store

log = logging.getLogger(__name__)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# --- selection / depth --------------------------------------------------------


@dataclass(frozen=True)
class DepthMode:
    """How much of each folder to pull. Exactly one field is meaningful.

    - ``count``: the most recent N messages (UID slice from the top).
    - ``since``: server-side ``SINCE`` from an ISO ``YYYY-MM-DD`` date.
      ``until`` (ISO, exclusive) optionally bounds that period's end; it is
      meaningful only with ``since``.
    - ``incremental``: resume from the stored ``pull_state`` cursor.

    ``require_cursor`` refines ``incremental``: a folder with no cursor is
    skipped rather than pulled from UID 0. It is set when the folder set came
    from the server (``--all-lists``) rather than from named lists, because
    "no cursor" then means "a list never asked for", not "a list to bootstrap".
    See :func:`run_fetch`.
    """

    count: int | None = None
    since: str | None = None
    until: str | None = None
    incremental: bool = False
    require_cursor: bool = False


@dataclass(frozen=True)
class FetchRequest:
    """A fully-resolved fetch request."""

    folders: tuple[str, ...]
    depth: DepthMode
    from_filters: tuple[str, ...] = ()
    limit: int | None = None
    dry_run: bool = False
    batch_size: int = 200


@dataclass
class FetchSummary:
    """Counts collected across a run.

    ``discarded_early`` counts messages fetched by a date-based pull whose own
    ``Date`` header predates the pull period, discarded instead of stored (see
    :func:`run_fetch`). ``untracked_skipped`` and ``cursors_seeded`` count the
    two cursor-driven behaviours also described there.
    """

    fetched: int = 0
    duplicates: int = 0
    parse_errors: int = 0
    html_only: int = 0
    matched: int = 0
    auto_generated: int = 0
    discarded_early: int = 0
    #: Messages a ``--until``-bounded pull fetched whose own ``Date`` header is
    #: on or after the period's end, discarded instead of stored.
    discarded_late: int = 0
    #: Folders an ``--all-lists --incremental`` run skipped for want of a
    #: cursor (see :func:`run_fetch`). Nothing was fetched or examined for them.
    untracked_skipped: int = 0
    #: Empty folders given a bootstrap cursor at ``UIDNEXT - 1`` by a
    #: discovery pull, so a later ``--incremental`` run tracks them.
    cursors_seeded: int = 0
    per_list: dict[str, int] = field(default_factory=dict)

    def as_line(self) -> str:
        return (
            f"fetched={self.fetched} duplicates={self.duplicates} "
            f"parse_errors={self.parse_errors} html_only={self.html_only} "
            f"matched={self.matched} auto_generated={self.auto_generated} "
            f"discarded_early={self.discarded_early} "
            f"discarded_late={self.discarded_late} "
            f"untracked_skipped={self.untracked_skipped} "
            f"cursors_seeded={self.cursors_seeded}"
        )


# --- parsing ------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMessage:
    """The fields we persist from one RFC 5322 message.

    ``body`` is the decoded ``text/plain`` part (``None`` when HTML-only) and
    ``html_body`` the decoded ``text/html`` part (``None`` when absent). Both are
    captured with the same charset-fallback handling; ``html_only`` stays true
    only when there is no plain part.

    ``auto_generated`` is the classification reason when the message's headers
    mark it machine-generated (see
    :func:`~mailing_list_ai_check.autogen.classify_message`), ``None`` for
    human mail.

    ``raw_headers`` is the message's verbatim header block, sliced out of the
    same bytes every other field was parsed from (see :func:`split_headers`).
    """

    message_id: str
    from_email: str
    from_name: str | None
    subject: str | None
    date: str | None
    in_reply_to: str | None
    body: str | None
    html_only: bool
    html_body: str | None = None
    auto_generated: str | None = None
    raw_headers: bytes | None = None


@dataclass(frozen=True)
class ParsedHeader:
    """The header-only fields a message-list preview shows for a candidate message.

    The read-only preview path (see the dashboard's "Add messages" popover) fetches
    only ``From``/``Subject``/``Date`` and never a body, so this is the subset of
    :class:`ParsedMessage` derivable from those headers alone.
    """

    from_email: str
    from_name: str | None
    subject: str | None
    date: str | None


def iso_to_imap_date(iso_date: str) -> str:
    """Convert ``YYYY-MM-DD`` to IMAP's ``DD-Mon-YYYY`` (for ``SINCE``)."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def _header_str(msg: EmailMessage, name: str) -> str | None:
    value = msg[name]
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decode_part(part: EmailMessage) -> str | None:
    """Decode one MIME part to text, falling back on a lenient UTF-8 decode.

    Mirrors the charset handling used for the plain part: an unknown or broken
    charset never raises — the raw payload is decoded with ``errors="replace"``.
    Returns ``None`` for an empty part.
    """
    try:
        content = part.get_content()
    except LookupError, ValueError, UnicodeDecodeError:
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    return content if content else None


def _extract_body(msg: EmailMessage) -> tuple[str | None, bool, str | None]:
    """Return ``(text_plain_body, html_only, text_html_body)``.

    Prefers a ``text/plain`` part for ``body``. The ``text/html`` part, if any,
    is decoded into the third element regardless of whether a plain part exists.
    ``html_only`` stays true only when there is no plain part (see the module
    docstring); it does not change just because the HTML is now captured.
    """
    html_part = msg.get_body(preferencelist=("html",))
    html_body = _decode_part(html_part) if html_part is not None else None

    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        return _decode_part(plain), False, html_body

    return None, (html_part is not None), html_body


def _date_header_to_iso(msg: EmailMessage) -> str | None:
    """Parse a message's ``Date`` header to a UTC ISO-8601 string, or ``None``.

    Shared by :func:`parse_message` and :func:`parse_header` so a preview's date
    matches exactly what a full pull would store. Prefers the ``policy=default``
    header's parsed ``datetime`` and falls back to :func:`parsedate_to_datetime`;
    a naive (offset-less) datetime is assumed to be UTC. Returns ``None`` for a
    missing or unparsable header.
    """
    date_hdr = msg["Date"]
    if date_hdr is None:
        return None
    dt = None
    try:
        dt = date_hdr.datetime  # type: ignore[attr-defined]
    except AttributeError, ValueError:
        dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(str(date_hdr))
        except TypeError, ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def split_headers(raw: bytes) -> bytes:
    """Return the verbatim header block of ``raw``, without the blank separator.

    The bytes are sliced, never re-serialized, so what is stored is exactly what
    the server sent — folding, RFC 2047 encoded words and any raw 8-bit octets
    intact — and re-parsing it reproduces the same headers this module parsed.
    Accepts a full message or a header-only FETCH blob (which the server ends
    with the same blank line). Bodyless input is returned whole.
    """
    crlf = raw.find(b"\r\n\r\n")
    lf = raw.find(b"\n\n")
    if crlf != -1 and (lf == -1 or crlf <= lf):
        return raw[: crlf + 2]
    if lf != -1:
        return raw[: lf + 1]
    return raw


def parse_message(raw: bytes, *, uid: int | None = None, folder: str = "") -> ParsedMessage:
    """Parse raw RFC 5322 bytes into a :class:`ParsedMessage`.

    ``policy=default`` decodes RFC 2047 words in headers. The ``From`` address is
    lowercased and stripped; a missing ``Message-ID`` is synthesized from the UID
    so the row still has a stable dedupe key. The header block is carried through
    verbatim so every header-derived field can be recomputed without a re-fetch.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)

    message_id = _header_str(msg, "Message-ID") or ""
    if not message_id:
        message_id = f"<no-message-id-{folder}-{uid}@mailing-list-ai-check>"

    display_name, addr = parseaddr(str(msg["From"] or ""))
    from_email = addr.strip().lower()
    from_name = display_name.strip() or None

    body, html_only, html_body = _extract_body(msg)

    return ParsedMessage(
        message_id=message_id,
        from_email=from_email,
        from_name=from_name,
        subject=_header_str(msg, "Subject"),
        date=_date_header_to_iso(msg),
        in_reply_to=_header_str(msg, "In-Reply-To"),
        body=body,
        html_only=html_only,
        html_body=html_body,
        auto_generated=classify_message(msg),
        raw_headers=split_headers(raw),
    )


def parse_header(raw: bytes) -> ParsedHeader:
    """Parse a header-only FETCH blob (``FROM``/``SUBJECT``/``DATE``) into a header.

    Uses the same stdlib :mod:`email` parser and ``policy=default`` as
    :func:`parse_message`, so the sender, subject and normalized UTC date a
    preview shows match exactly what a full pull would persist. The ``From``
    address is lowercased and stripped identically. No body is present or parsed.
    """
    msg = email.message_from_bytes(raw, policy=policy.default)
    display_name, addr = parseaddr(str(msg["From"] or ""))
    return ParsedHeader(
        from_email=addr.strip().lower(),
        from_name=display_name.strip() or None,
        subject=_header_str(msg, "Subject"),
        date=_date_header_to_iso(msg),
    )


# --- folder resolution --------------------------------------------------------


def folder_for_list(list_name: str) -> str:
    """Map a bare list slug (or an already-prefixed name) to its IMAP folder."""
    return list_name if list_name.startswith(FOLDER_PREFIX) else FOLDER_PREFIX + list_name


def list_name_for_folder(folder: str) -> str:
    """Inverse of :func:`folder_for_list` — strip the namespace prefix."""
    return folder[len(FOLDER_PREFIX) :] if folder.startswith(FOLDER_PREFIX) else folder


def resolve_folders(
    client: ImapClient,
    list_names: Sequence[str],
    *,
    all_lists: bool = False,
    include_excluded: bool = False,
) -> list[str]:
    """Resolve a selection into concrete folder names.

    ``all_lists`` enumerates the server; otherwise each name is mapped through
    :func:`folder_for_list`. An enumeration skips the lists that carry only
    auto-generated traffic (see
    :func:`~mailing_list_ai_check.autogen.is_excluded_list`) unless
    ``include_excluded`` is set; explicitly named lists are always honoured.
    """
    if not all_lists:
        return [folder_for_list(name) for name in list_names]
    folders = client.list_folders()
    if include_excluded:
        return folders
    kept = [f for f in folders if not is_excluded_list(list_name_for_folder(f))]
    skipped = len(folders) - len(kept)
    if skipped:
        log.info("skipping %d auto-generated list(s); --include-excluded-lists overrides", skipped)
    return kept


def refresh_lists_index(client: ImapClient, store: Store) -> dict[str, int]:
    """Populate/refresh the stored lists index from the server's ``LIST`` output.

    The ``LIST`` enumeration is one IMAP round-trip; reconciliation semantics
    (what is added, deleted, or kept-but-stamped) live in
    :meth:`~mailing_list_ai_check.store.Store.refresh_lists_index`.

    After reconciling, the newest-message timestamp is refreshed for **tracked**
    lists only — those with local messages and still on the server (see
    :meth:`~mailing_list_ai_check.store.Store.tracked_list_folders`), so the
    ~1,400 index-only folders are never EXAMINEd. Each check is one more
    round-trip; an empty folder (``None``) still counts as ``activity_checked``,
    while an exception is logged, counted as ``activity_failed`` and never aborts
    the sweep.
    """
    folders = client.list_folders()
    entries = [(list_name_for_folder(folder), folder) for folder in folders]
    counts = store.refresh_lists_index(entries)

    activity_checked = 0
    activity_failed = 0
    for list_id, folder in store.tracked_list_folders():
        try:
            when = client.last_message_internaldate(folder)
        except Exception:
            activity_failed += 1
            log.warning("activity check failed for %s", folder)
            continue
        store.set_list_last_message(list_id, when)
        activity_checked += 1
    counts["activity_checked"] = activity_checked
    counts["activity_failed"] = activity_failed

    log.info(
        "lists index refreshed: total=%d added=%d restored=%d deleted=%d kept_missing=%d "
        "activity_checked=%d activity_failed=%d",
        counts["total"],
        counts["added"],
        counts["restored"],
        counts["deleted"],
        counts["kept_missing"],
        counts["activity_checked"],
        counts["activity_failed"],
    )
    return counts


# --- UID computation ----------------------------------------------------------


def _union_search(
    client: ImapClient,
    *,
    since: str | None,
    uid_range: str | None,
    from_filters: Sequence[str],
    sent_since: str | None = None,
    sent_before: str | None = None,
) -> list[int]:
    """Run one search per ``FROM`` filter and return the deduped, sorted union.

    With no filters a single search runs. Multiple ``--from`` values are a union
    of independent server-side searches (findings: ``FROM`` is a substring match).
    """
    if not from_filters:
        return client.uid_search(
            build_search_criteria(
                since=since, sent_since=sent_since, sent_before=sent_before, uid_range=uid_range
            )
        )
    seen: set[int] = set()
    for term in from_filters:
        criteria = build_search_criteria(
            since=since,
            sent_since=sent_since,
            sent_before=sent_before,
            uid_range=uid_range,
            from_addr=term,
        )
        seen.update(client.uid_search(criteria))
    return sorted(seen)


def compute_uids(
    client: ImapClient,
    store: Store,
    folder: str,
    list_id: int,
    depth: DepthMode,
    from_filters: Sequence[str],
) -> tuple[list[int], FolderStatus]:
    """Compute the UID set to fetch for ``folder``, and the folder's status.

    Handles the three depth modes, including the documented UIDVALIDITY-change
    resync for ``--incremental``. The returned :class:`FolderStatus` carries the
    UIDVALIDITY the caller records with the cursor, plus the ``exists`` and
    ``uidnext`` values :func:`run_fetch` needs to seed one for an empty folder.

    ``depth.require_cursor`` is not consulted here: this function answers what a
    depth mode selects within a folder, and from UID 0 stays right for the first
    pull of a *named* list. Whether an untracked folder should be visited at all
    depends on how the folder set was chosen, which is :func:`run_fetch`'s call.
    """
    status = client.examine(folder)
    uidvalidity = status.uidvalidity

    if depth.incremental:
        cursor = store.get_pull_state(list_id)
        if cursor is not None and cursor.uidvalidity != uidvalidity:
            # Documented resync path: the folder was reset. Re-search from the
            # last successful sync date and rewrite the cursor afterwards.
            mlist = store.get_list(list_id)
            since_iso = (mlist.last_synced_at or "")[:10] if mlist else ""
            log.warning(
                "UIDVALIDITY changed for %s (stored=%s server=%s); resyncing via SINCE %s",
                folder,
                cursor.uidvalidity,
                uidvalidity,
                since_iso or "<none>",
            )
            since = iso_to_imap_date(since_iso) if since_iso else None
            uids = _union_search(client, since=since, uid_range=None, from_filters=from_filters)
        else:
            last_uid = cursor.last_uid if cursor else 0
            uid_range = f"{last_uid + 1}:*"
            uids = _union_search(client, since=None, uid_range=uid_range, from_filters=from_filters)
            # `n:*` can echo the highest UID when n exceeds it; drop stale ones.
            uids = [u for u in uids if u > last_uid]
        return uids, status

    if depth.since is not None:
        since = iso_to_imap_date(depth.since)
        # Also pre-filter on the Date header (SENTSINCE) so re-imported old
        # history is excluded before any body is downloaded. One day of margin
        # because SENTSINCE compares the header's date part disregarding its
        # time zone; the UTC-normalized client-side discard in run_fetch stays
        # the precise gate.
        margin = datetime.strptime(depth.since, "%Y-%m-%d") - timedelta(days=1)
        sent_since = iso_to_imap_date(margin.date().isoformat())
        # --until bounds the period's end on the Date header only (SENTBEFORE,
        # exclusive). INTERNALDATE is left unbounded above, since mail sent in
        # the period can arrive in the folder later. One day of margin for the
        # same time-zone reason as SENTSINCE; the client-side discard in
        # _fetch_folder is the precise gate.
        sent_before = None
        if depth.until is not None:
            end = datetime.strptime(depth.until, "%Y-%m-%d") + timedelta(days=1)
            sent_before = iso_to_imap_date(end.date().isoformat())
        uids = _union_search(
            client,
            since=since,
            uid_range=None,
            from_filters=from_filters,
            sent_since=sent_since,
            sent_before=sent_before,
        )
        return uids, status

    # --count N: most recent N via a UID slice from the top.
    uids = _union_search(client, since=None, uid_range=None, from_filters=from_filters)
    if depth.count is not None:
        uids = uids[-depth.count :] if depth.count > 0 else []
    return uids, status


# --- run ----------------------------------------------------------------------


def run_fetch(client: ImapClient, store: Store, request: FetchRequest) -> FetchSummary:
    """Execute a fetch request, returning a :class:`FetchSummary`.

    Respects ``request.limit`` as a hard global message cap across all folders
    (the safety valve for testing) and ``request.dry_run`` (search + count only).

    A date-based pull (``--since``, or ``--days`` resolved to it) discards any
    fetched message whose own ``Date`` header predates the pull period instead
    of storing it, counting it in ``discarded_early``. The server-side
    ``SINCE`` search matches on INTERNALDATE (arrival in the archive folder),
    so re-imported or late-delivered history arrives carrying much older
    ``Date`` headers; without the discard, a pull for one period silently
    accretes messages from far outside it. Messages with no parsable ``Date``
    are kept. Count-based, incremental and explicit-UID pulls have no period
    and never discard.

    Three cursor rules apply, all resting on what a ``pull_state`` row asserts:
    that the list is stored complete through ``last_uid``.

    - Only a run whose completeness claim is true writes the cursor: an
      unfiltered ``--incremental`` run (its ``UID n:*`` search enumerates every
      UID above the cursor, and on a UIDVALIDITY change the documented resync
      rewrite re-establishes tracking from the last sync date), or the first
      unfiltered pull of a list, whose period defines the list's scope on
      adoption. A date- or count-based pull over a list that already has a
      cursor, or any ``--from``-filtered pull, leaves the cursor untouched —
      their matched sets under-enumerate by construction, so advancing would
      claim unfetched messages as stored. The next unfiltered incremental run
      covers the span such a pull left behind.

    - With ``depth.require_cursor`` (an ``--all-lists --incremental`` run), a
      folder with no cursor is skipped and counted in ``untracked_skipped``,
      not pulled from UID 0. The server supplies the folder set there, so a
      missing cursor means a list never asked for, and pulling from 0 would
      backfill its whole history. Named lists do not set the flag, so the first
      pull of one still takes everything. The skip happens before the
      ``EXAMINE``, so such a folder costs no round trip.
    - A discovery pull (``--count``/``--since``/``--days``, which visit every
      folder) seeds a cursor at ``UIDNEXT - 1`` for a folder the server reports
      empty, counting it in ``cursors_seeded``. The completeness claim holds
      trivially for an empty folder, and it lets a later ``--incremental`` run
      catch that list's first ever message rather than skip it.
    """
    summary = FetchSummary()
    remaining = request.limit

    for folder in request.folders:
        name = list_name_for_folder(folder)
        if remaining is not None and remaining <= 0:
            log.info("global limit reached; skipping %s", name)
            break

        mlist = store.upsert_list(name, folder)
        cursor = store.get_pull_state(mlist.id)

        # An --all-lists --incremental run pulls only lists it already tracks.
        # Skipping before the EXAMINE keeps the round trip off the wire too, so
        # the run costs one search per tracked list rather than per folder.
        if request.depth.incremental and request.depth.require_cursor and cursor is None:
            summary.untracked_skipped += 1
            log.debug("%s: no cursor; not tracked, skipped", name)
            continue

        try:
            uids, status = compute_uids(
                client, store, folder, mlist.id, request.depth, request.from_filters
            )
        except Exception:
            log.exception("failed to compute UID set for %s", name)
            continue
        uidvalidity = status.uidvalidity

        summary.matched += len(uids)
        if request.dry_run:
            log.info("[dry-run] %s: %d message(s) match", name, len(uids))
            summary.per_list[name] = len(uids)
            continue

        # Skip re-downloading bodies the store already holds: a stored UID
        # names the same message while the folder's UIDVALIDITY is unchanged
        # (the cursor records the value stored rows were pulled under). The
        # skipped messages are counted as duplicates, exactly as if their
        # bodies had been fetched and the upsert had deduped them.
        matched_uids = uids
        stored_matched: set[int] = set()
        if cursor is not None and cursor.uidvalidity == uidvalidity:
            stored_uids = store.uids_for_list(mlist.id)
            stored_matched = {u for u in matched_uids if u in stored_uids}
            if stored_matched:
                summary.duplicates += len(stored_matched)
                log.debug("%s: %d already-stored uid(s) not re-fetched", name, len(stored_matched))
        uids = [u for u in matched_uids if u not in stored_matched]

        if remaining is not None:
            uids = uids[:remaining] if remaining < len(uids) else uids

        list_count = _fetch_folder(
            client,
            store,
            mlist.id,
            folder,
            uids,
            request.batch_size,
            summary,
            min_date=request.depth.since,
            max_date=request.depth.until,
        )
        summary.per_list[name] = list_count

        # Advance the cursor over the contiguous processed prefix of the
        # search result — UIDs fetched this run or skipped as already stored.
        # Never past a UID the --limit cap left unfetched, or a later
        # --incremental pull would miss it.
        #
        # Only a run whose claim is true may write the cursor at all. An
        # unfiltered --incremental search (`UID n:*`) enumerates every UID
        # above the cursor, so its processed prefix is the literal truth; and
        # the first unfiltered pull of a list adopts it, the run's period
        # defining the list's scope — the claim every existing cursor was
        # created under. Every other run under-enumerates by construction: a
        # date or count search omits UIDs its criteria excluded without naming
        # them, and a --from filter omits other senders, so advancing an
        # existing cursor would mark messages stored that never were, hiding
        # them from every later incremental run (late-arriving mail held in a
        # moderation queue was lost exactly this way). Such runs leave the
        # cursor alone; the next unfiltered --incremental run re-searches the
        # span above it, skips what is stored as duplicates, and fetches what
        # the filtered run passed by.
        # A --until-bounded pull omits everything after its period, so it can
        # never claim completeness through its last UID either.
        may_write_cursor = (
            not request.from_filters
            and request.depth.until is None
            and (request.depth.incremental or cursor is None)
        )
        processed = stored_matched.union(uids)
        last_processed = None
        for uid in matched_uids:
            if uid not in processed:
                break
            last_processed = uid
        if may_write_cursor and last_processed is not None:
            store.set_pull_state(mlist.id, uidvalidity, last_processed)
        elif cursor is None and status.exists == 0 and status.uidnext:
            # A folder the server reports empty. "Complete through UIDNEXT - 1"
            # is then true whatever --from filters were in play, since there is
            # no message to have missed, so the cursor may be seeded from the
            # EXAMINE alone. That makes the list tracked, and a later
            # --incremental run picks up its first ever message instead of
            # skipping it for want of a cursor. Only a discovery pull reaches
            # here: --incremental never examines an untracked folder.
            store.set_pull_state(mlist.id, uidvalidity, status.uidnext - 1)
            summary.cursors_seeded += 1
        store.set_list_synced(mlist.id)

        # Record when the server last saw traffic on this list. A failure here
        # must never fail the pull, so it is logged and swallowed.
        try:
            when = client.last_message_internaldate(folder)
            if when is not None:
                store.set_list_last_message(mlist.id, when)
        except Exception:
            log.warning("activity check failed for %s", name)

        if remaining is not None:
            remaining -= list_count

    # A pulled message may be a reply whose parent just arrived (or vice
    # versa), so the reply-timing classification is refreshed once per run.
    if not request.dry_run:
        store.recompute_timing()
        # A DMARC rewrite and the address it stands for can arrive in either
        # order and on different lists, so the pairing is reconciled once per
        # run rather than when either address is first seen.
        store.link_dmarc_rewrites()

    return summary


def _fetch_folder(
    client: ImapClient,
    store: Store,
    list_id: int,
    folder: str,
    uids: Sequence[int],
    batch_size: int,
    summary: FetchSummary,
    *,
    min_date: str | None = None,
    max_date: str | None = None,
) -> int:
    """Fetch, parse and upsert ``uids`` from ``folder``. Returns rows fetched.

    ``min_date`` (ISO ``YYYY-MM-DD``) is the start of a date-based pull's
    period: a parsed message dated before it is discarded, not stored (see
    :func:`run_fetch`). ``None`` disables the check. The comparison is
    lexicographic — a stored date is a full UTC ISO-8601 timestamp, so any
    timestamp on ``min_date``'s own day sorts after the bare date and is kept.
    ``max_date`` is the exclusive end of the period: a parsed message dated on or
    after it is discarded (any timestamp on ``max_date``'s day sorts after the
    bare date, so that whole day is excluded). ``None`` disables the check.
    """
    name = list_name_for_folder(folder)
    fetched_here = 0
    for uid, raw in client.fetch_bodies(uids, batch_size=batch_size):
        try:
            parsed = parse_message(raw, uid=uid, folder=folder)
        except Exception:
            summary.parse_errors += 1
            log.warning("parse error for %s uid=%s", name, uid)
            continue

        if min_date is not None and parsed.date is not None and parsed.date < min_date:
            summary.discarded_early += 1
            log.debug("discarding %s uid=%s dated %s (before %s)", name, uid, parsed.date, min_date)
            continue
        if max_date is not None and parsed.date is not None and parsed.date >= max_date:
            summary.discarded_late += 1
            log.debug("discarding %s uid=%s dated %s (from %s)", name, uid, parsed.date, max_date)
            continue

        address_id: int | None = None
        if parsed.from_email:
            address_id = store.upsert_address(parsed.from_email, parsed.from_name).id

        result = store.upsert_message(
            message_id=parsed.message_id,
            list_id=list_id,
            address_id=address_id,
            subject=parsed.subject,
            date=parsed.date,
            in_reply_to=parsed.in_reply_to,
            raw_body=parsed.body,
            uid=uid,
            raw_html=parsed.html_body,
            auto_generated=parsed.auto_generated,
            from_name=parsed.from_name,
            raw_headers=parsed.raw_headers,
        )
        if result.inserted:
            summary.fetched += 1
            fetched_here += 1
            if parsed.html_only:
                summary.html_only += 1
            if parsed.auto_generated:
                summary.auto_generated += 1
            # Never log body content; a length is safe only at DEBUG.
            log.debug("stored %s uid=%s body_chars=%s", name, uid, len(parsed.body or ""))
        else:
            summary.duplicates += 1
    log.info("%s: %d fetched", name, fetched_here)
    return fetched_here


def run_fetch_uids(
    client: ImapClient,
    store: Store,
    folder: str,
    uids: Sequence[int],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> FetchSummary:
    """Fetch, parse and upsert an explicit, pre-computed UID set for one folder.

    A thin wrapper over :func:`_fetch_folder` for callers that have already
    resolved the exact UIDs to pull (the dashboard's ranged "new"/"before" pull),
    rather than going through :func:`compute_uids`' depth modes. The list row is
    upserted from ``folder`` (created if new, like :func:`run_fetch`), the bodies
    are fetched with ``BODY.PEEK[]`` and upserted idempotently, and a
    :class:`FetchSummary` is returned with ``matched`` set to ``len(uids)``.

    Cursor (``pull_state``), ``last_synced_at`` and ``last_message_at``
    bookkeeping are intentionally left to the caller, because whether the
    incremental cursor may advance depends on the pull direction (a "before" pull
    must never move it) — see the webapp's ``/api/pull/range`` endpoint.
    """
    name = list_name_for_folder(folder)
    mlist = store.upsert_list(name, folder)
    # A UID FETCH requires the mailbox to be selected; EXAMINE keeps it read-only
    # and makes this wrapper self-contained regardless of what the caller selected.
    client.examine(folder)
    summary = FetchSummary()
    summary.matched = len(uids)
    fetched = _fetch_folder(client, store, mlist.id, folder, uids, batch_size, summary)
    summary.per_list[name] = fetched
    store.recompute_timing()
    return summary


def open_client(host: str, port: int, username: str, password: str) -> ImapClient:
    """Convenience: open and log in an :class:`ImapClient` from config values."""
    if not host:
        raise RuntimeError(
            "IMAP_HOST is not set. Copy .env.example to .env and fill in the "
            "IMAP settings for the mail archive you want to check."
        )
    return ImapClient.connect(host, port, username, password)
