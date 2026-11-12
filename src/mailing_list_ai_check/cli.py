"""``mail-ai-pull`` — command-line entry point for the IMAP fetcher.

Selects lists, a depth mode and optional sender filters, then pulls matching
mail into the SQLite store. Progress and the end-of-run summary go through
:mod:`logging` (level from ``LOG_LEVEL``); message bodies are never logged above
DEBUG.

Testing runs must stay within the project's hard cap — pass ``--limit 10``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from collections.abc import Container, Sequence
from dataclasses import dataclass

from email_reply_extractor import clean_for_scoring
from .config import Config
from .export_import import ExportImportError, export_lists, import_file
from email_reply_extractor import extract_new_text
from .fetcher import (
    DepthMode,
    FetchRequest,
    FetchSummary,
    open_client,
    parse_message,
    resolve_folders,
    run_fetch,
)
from email_reply_extractor import split_html_parts
from .imap_client import ImapClient
from .pangram import (
    DEFAULT_MODEL,
    MODEL_GENERATIONS,
    PangramClient,
    PangramError,
    generation_for_model,
)
from .staleness import ParentRefreshSummary, refresh_for_new_parents
from .stats_export import export_stats
from .store import SETTING_PANGRAM_MODEL, Store, sha256_text

log = logging.getLogger("mailing_list_ai_check.pull")
extract_log = logging.getLogger("mailing_list_ai_check.extract")
score_log = logging.getLogger("mailing_list_ai_check.score")
export_log = logging.getLogger("mailing_list_ai_check.export")
stats_export_log = logging.getLogger("mailing_list_ai_check.export-stats")
import_log = logging.getLogger("mailing_list_ai_check.import")

#: Client-enforced reliability floor (words). Below it, extractions are marked
#: ``too_short`` and never sent to Pangram (see docs/findings/pangram.md).
SCORE_MIN_WORDS = 50
#: Default API-call cap per run. Deliberately small so an accidental run can't
#: spend; a production run must pass an explicit larger ``--limit``.
DEFAULT_SCORE_LIMIT = 10
#: Realtime Pangram price per 100 words by detector generation, for the
#: end-of-run spend estimate: Pangram 4 is $0.05 per 100 words, Pangram 3 was
#: $0.05 per 1,000. Unknown generations use the Pangram 4 price.
_PRICE_PER_100_WORDS = {"4": 0.05, "3": 0.005}
_DEFAULT_PRICE_PER_100_WORDS = _PRICE_PER_100_WORDS["4"]
#: Bulk API words are billed at a 20% discount off the realtime price.
_BULK_PRICE_FACTOR = 0.8


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-pull",
        description="Fetch mailing-list mail over IMAP into the local store.",
    )
    parser.add_argument(
        "lists",
        nargs="*",
        help="one or more list names (e.g. 'announce' 'general'); omit with --all-lists",
    )
    parser.add_argument(
        "--all-lists",
        action="store_true",
        help="pull every list folder on the server (touches ~1374 folders)",
    )
    parser.add_argument(
        "--include-excluded-lists",
        action="store_true",
        help=(
            "with --all-lists, also pull the lists that carry only auto-generated "
            "traffic (see docs/findings/auto-generated.md); they are skipped by default"
        ),
    )

    depth = parser.add_mutually_exclusive_group()
    depth.add_argument("--count", type=int, metavar="N", help="most recent N messages per list")
    depth.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help=(
            "messages on/after this date; a fetched message whose own Date header "
            "is earlier is discarded, since the server-side search matches on "
            "arrival time and can return re-imported older mail"
        ),
    )
    depth.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="messages from the last N days (same early-date discard as --since)",
    )
    depth.add_argument(
        "--incremental",
        action="store_true",
        help=(
            "resume from the stored per-list cursor (UIDVALIDITY-aware); with "
            "--all-lists, a list that has no cursor yet is skipped rather than "
            "pulled in full, so use a date-based pull to take one on"
        ),
    )

    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help=(
            "with --since, end the period before this date (exclusive): the server "
            "search adds SENTBEFORE and a fetched message dated on or after it is "
            "discarded; such a pull never writes a list's incremental cursor"
        ),
    )
    parser.add_argument(
        "--from",
        dest="from_filters",
        action="append",
        default=[],
        metavar="ADDR",
        help="server-side FROM substring filter; repeatable (values are OR-ed)",
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="hard cap on messages fetched this run (use --limit 10 for testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="search and report match counts without fetching or storing",
    )
    parser.add_argument(
        "--backfill-html",
        action="store_true",
        help=(
            "re-fetch already-stored messages that lack a raw_html part and fill "
            "it in (no normal pull); respects --limit as a per-run message cap"
        ),
    )
    parser.add_argument(
        "--backfill-headers",
        action="store_true",
        help=(
            "re-fetch the header block of already-stored messages that lack one "
            "and fill it in, re-deriving any missing From display name (no normal "
            "pull); respects --limit as a per-run message cap"
        ),
    )
    parser.add_argument("--batch-size", type=int, default=200, help=argparse.SUPPRESS)
    return parser


def _resolve_depth(args: argparse.Namespace) -> DepthMode:
    """Turn parsed depth args into a :class:`DepthMode` (validated by caller)."""
    if args.incremental:
        # With --all-lists the folder set comes from the server, so a list with
        # no cursor is one never asked for rather than one to bootstrap: require
        # a cursor there, and leave named lists free to take a full first pull.
        return DepthMode(incremental=True, require_cursor=args.all_lists)
    if args.since is not None:
        return DepthMode(since=args.since, until=args.until)
    if args.days is not None:
        from datetime import UTC, datetime, timedelta

        since = (datetime.now(UTC) - timedelta(days=args.days)).date().isoformat()
        return DepthMode(since=since)
    return DepthMode(count=args.count)


def _validate(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.all_lists and args.lists:
        parser.error("give either list names or --all-lists, not both")
    if not args.all_lists and not args.lists:
        parser.error("specify at least one list name, or --all-lists")

    depth_flags = [
        args.count is not None,
        args.since is not None,
        args.days is not None,
        args.incremental,
    ]
    if args.backfill_html and args.backfill_headers:
        parser.error("give either --backfill-html or --backfill-headers, not both")
    # Both backfills re-fetch stored messages rather than doing a depth-based
    # pull, so neither takes a depth mode (and both reject one, to avoid confusion).
    if args.backfill_html or args.backfill_headers:
        flag = "--backfill-html" if args.backfill_html else "--backfill-headers"
        if sum(depth_flags) > 0 or args.until is not None:
            parser.error(f"{flag} does not take a depth mode (--count/--since/…)")
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be a positive integer")
        return
    if sum(depth_flags) == 0:
        parser.error("choose a depth: --count, --since, --days, or --incremental")
    # --count/--days must be positive if given.
    if args.count is not None and args.count <= 0:
        parser.error("--count must be a positive integer")
    if args.days is not None and args.days <= 0:
        parser.error("--days must be a positive integer")
    if args.since is not None:
        from datetime import datetime

        try:
            datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            parser.error("--since must be an ISO date (YYYY-MM-DD)")
    if args.until is not None:
        from datetime import datetime

        if args.since is None:
            parser.error("--until requires --since")
        try:
            datetime.strptime(args.until, "%Y-%m-%d")
        except ValueError:
            parser.error("--until must be an ISO date (YYYY-MM-DD)")
        if args.until <= args.since:
            parser.error("--until must be later than --since")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")


def _log_summary(summary: FetchSummary, dry_run: bool) -> None:
    if dry_run:
        log.info("dry-run summary: %d message(s) would be fetched", summary.matched)
    else:
        log.info("summary: %s", summary.as_line())
    for name, count in sorted(summary.per_list.items()):
        log.info("  %s: %d", name, count)


#: Default per-run message cap for ``--backfill-html`` (the CLAUDE.md testing
#: cap). Production backfills pass a larger explicit ``--limit``.
DEFAULT_BACKFILL_LIMIT = 10


@dataclass
class BackfillSummary:
    """Tally of one ``--backfill-html`` run."""

    lists_processed: int = 0
    lists_skipped: int = 0
    fetched: int = 0
    html_found: int = 0
    html_missing: int = 0

    def as_line(self) -> str:
        return (
            f"lists_processed={self.lists_processed} lists_skipped={self.lists_skipped} "
            f"fetched={self.fetched} html_found={self.html_found} html_missing={self.html_missing}"
        )


def run_backfill_html(
    client: ImapClient,
    store: Store,
    folders: Sequence[str],
    *,
    limit: int = DEFAULT_BACKFILL_LIMIT,
    batch_size: int = 200,
) -> BackfillSummary:
    """Re-fetch stored messages missing ``raw_html`` and fill it in.

    For each folder: select it read-only and verify its UIDVALIDITY matches the
    stored ``pull_state`` (skip the list with a warning otherwise — a changed
    UIDVALIDITY means the stored UIDs no longer address the same messages). Then
    iterate :meth:`Store.iter_messages_missing_html`, UID-FETCH those messages in
    the usual batch size, parse each, and :meth:`Store.set_message_raw_html` with
    the HTML part when present or the empty-string tombstone when the message has
    none. Stamping the tombstone is what lets a capped run make forward progress:
    without it, HTML-less messages stay ``raw_html IS NULL`` and every run
    re-fetches the same ones forever, never reaching messages further up the UID
    order. ``raw_body`` and every other field are left untouched — history is
    never rewritten, only ``raw_html`` is added.

    ``limit`` is a hard cap on messages fetched across all folders this run
    (default the CLAUDE.md testing cap); production runs pass a larger value.
    """
    from .fetcher import list_name_for_folder

    summary = BackfillSummary()
    remaining = limit

    for folder in folders:
        name = list_name_for_folder(folder)
        if remaining <= 0:
            log.info("backfill message cap reached; skipping %s", name)
            break

        mlist = store.upsert_list(name, folder)
        status = client.examine(folder)
        cursor = store.get_pull_state(mlist.id)
        if cursor is None or cursor.uidvalidity != status.uidvalidity:
            log.warning(
                "skipping %s: UIDVALIDITY %s does not match stored cursor %s",
                name,
                status.uidvalidity,
                cursor.uidvalidity if cursor else "<none>",
            )
            summary.lists_skipped += 1
            continue

        # Messages needing a backfill, in UID order, capped to what's left.
        pending = list(store.iter_messages_missing_html(mlist.id))[:remaining]
        by_uid = {m.uid: m.id for m in pending if m.uid is not None}
        if not by_uid:
            summary.lists_processed += 1
            continue

        for uid, raw in client.fetch_bodies(sorted(by_uid), batch_size=batch_size):
            message_pk = by_uid.get(uid)
            if message_pk is None:
                continue
            try:
                parsed = parse_message(raw, uid=uid, folder=folder)
            except Exception:
                log.warning("parse error backfilling %s uid=%s", name, uid)
                continue
            summary.fetched += 1
            remaining -= 1
            if parsed.html_body:
                store.set_message_raw_html(message_pk, parsed.html_body)
                summary.html_found += 1
            else:
                # Tombstone: the message genuinely has no HTML part. Stamp an
                # empty string ("checked, none present") so it is not re-fetched
                # every run — iter_messages_missing_html only returns raw_html
                # IS NULL rows, so '' drops out of the backfill queue and the
                # run makes forward progress. Downstream, '' is falsy exactly
                # like NULL (no HTML oracle / signature hint), so the sentinel
                # changes only the backfill queue, never extraction/scoring.
                store.set_message_raw_html(message_pk, "")
                summary.html_missing += 1

        summary.lists_processed += 1

    log.info("backfill summary: %s", summary.as_line())
    return summary


@dataclass
class HeaderBackfillSummary:
    """Tally of one ``--backfill-headers`` run."""

    lists_processed: int = 0
    lists_skipped: int = 0
    fetched: int = 0
    headers_stored: int = 0
    headers_missing: int = 0
    names_recovered: int = 0

    def as_line(self) -> str:
        return (
            f"lists_processed={self.lists_processed} lists_skipped={self.lists_skipped} "
            f"fetched={self.fetched} headers_stored={self.headers_stored} "
            f"headers_missing={self.headers_missing} names_recovered={self.names_recovered}"
        )


def run_backfill_headers(
    client: ImapClient,
    store: Store,
    folders: Sequence[str],
    *,
    limit: int = DEFAULT_BACKFILL_LIMIT,
    batch_size: int = 200,
) -> HeaderBackfillSummary:
    """Re-fetch the header block of stored messages missing one and fill it in.

    The same shape as :func:`run_backfill_html` — per folder, select read-only
    and verify UIDVALIDITY against the stored ``pull_state`` (skip the list with
    a warning otherwise, since a changed UIDVALIDITY means the stored UIDs no
    longer address the same messages), then walk
    :meth:`Store.iter_messages_missing_headers` in UID order — but it fetches
    ``BODY.PEEK[HEADER]`` rather than whole bodies, so it moves a fraction of the
    bytes a body backfill does.

    Each fetched blob is stored verbatim and re-parsed for the ``From`` display
    name, which is written only where ``from_name`` is still NULL (see
    :meth:`Store.set_message_headers`). This is what recovers the names of
    messages pulled before migration 015, whose senders were until now shown
    under whatever name their address happened to be first seen with. A message
    the server returns nothing for is tombstoned with ``b""`` so a capped run
    keeps making forward progress instead of re-fetching it every time.

    ``limit`` is a hard cap on messages fetched across all folders this run
    (default the CLAUDE.md testing cap); production runs pass a larger value.
    """
    from .fetcher import list_name_for_folder, parse_header, split_headers

    summary = HeaderBackfillSummary()
    remaining = limit

    for folder in folders:
        name = list_name_for_folder(folder)
        if remaining <= 0:
            log.info("header backfill message cap reached; skipping %s", name)
            break

        mlist = store.upsert_list(name, folder)
        status = client.examine(folder)
        cursor = store.get_pull_state(mlist.id)
        if cursor is None or cursor.uidvalidity != status.uidvalidity:
            log.warning(
                "skipping %s: UIDVALIDITY %s does not match stored cursor %s",
                name,
                status.uidvalidity,
                cursor.uidvalidity if cursor else "<none>",
            )
            summary.lists_skipped += 1
            continue

        pending = list(store.iter_messages_missing_headers(mlist.id))[:remaining]
        by_uid = {m.uid: m for m in pending if m.uid is not None}
        if not by_uid:
            summary.lists_processed += 1
            continue

        for uid, raw in client.fetch_full_headers(sorted(by_uid), batch_size=batch_size):
            message = by_uid.get(uid)
            if message is None:
                continue
            summary.fetched += 1
            remaining -= 1

            headers = split_headers(raw)
            if not headers:
                store.set_message_headers(message.id, b"")
                summary.headers_missing += 1
                continue

            # Re-derive the display name from the bytes just stored. A name
            # captured at fetch time wins; set_message_headers only fills NULLs.
            from_name: str | None = None
            try:
                from_name = parse_header(headers).from_name
            except Exception:
                log.warning("parse error backfilling headers for %s uid=%s", name, uid)
            store.set_message_headers(message.id, headers, from_name=from_name)
            summary.headers_stored += 1
            if from_name and message.from_name is None:
                summary.names_recovered += 1

        summary.lists_processed += 1

    log.info("header backfill summary: %s", summary.as_line())
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(args, parser)

    config = Config.load()
    if args.db:
        db_path = args.db
    else:
        db_path = config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.backfill_html or args.backfill_headers:
        limit = args.limit if args.limit is not None else DEFAULT_BACKFILL_LIMIT
        run = run_backfill_html if args.backfill_html else run_backfill_headers
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
        try:
            folders = resolve_folders(
                client,
                args.lists,
                all_lists=args.all_lists,
                include_excluded=args.include_excluded_lists,
            )
            with Store(db_path) as store:
                run(client, store, folders, limit=limit, batch_size=args.batch_size)
        finally:
            client.close()
            client.logout()
        return 0

    depth = _resolve_depth(args)

    client = open_client(
        config.imap_host, config.imap_port, config.imap_username, config.imap_password
    )
    try:
        folders = resolve_folders(
            client,
            args.lists,
            all_lists=args.all_lists,
            include_excluded=args.include_excluded_lists,
        )
        request = FetchRequest(
            folders=tuple(folders),
            depth=depth,
            from_filters=tuple(args.from_filters),
            limit=args.limit,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
        )
        with Store(db_path) as store:
            summary = run_fetch(client, store, request)
    finally:
        client.close()
        client.logout()

    _log_summary(summary, args.dry_run)
    return 0


# --- extraction command -------------------------------------------------------


def build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-extract",
        description=(
            "Extract each author's newly written text from stored messages "
            "(the email-reply-extractor library). Idempotent: only messages "
            "without an extraction row are processed."
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="stop after processing N messages this run",
    )
    return parser


def refresh_parents(store: Store, logger: logging.Logger) -> ParentRefreshSummary:
    """Re-derive extracted replies whose parent has arrived; log what changed.

    A wrapper over :func:`staleness.refresh_for_new_parents` for the pipeline
    stages. The summary line is logged at INFO only when at least one extracted
    reply was rechecked, so a run that adds no parents logs nothing.
    """
    summary = refresh_for_new_parents(store)
    if summary.reextract.processed:
        logger.info("parent arrival: %s", summary.as_line())
    return summary


def run_extract(store: Store, limit: int | None = None) -> tuple[Counter[str], Counter[str]]:
    """Extract text for every message lacking an extraction row.

    Returns ``(status_counts, method_counts)``. Idempotent — a second run over an
    already-extracted store processes nothing. ``limit`` caps messages per run.

    Before extracting, the stage re-derives the already-extracted replies whose
    thread parent has been stored since the previous run
    (:func:`refresh_parents`). That step is not subject to ``limit`` and is not
    counted in the returned tallies, which describe new extractions only.
    """
    refresh_parents(store, extract_log)
    status_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    processed = 0
    for message in store.iter_messages_without_extraction():
        if limit is not None and processed >= limit:
            break
        parent_body = (
            store.get_parent_body(message.in_reply_to, exclude_message_id=message.message_id)
            if message.in_reply_to
            else None
        )
        result = extract_new_text(message.raw_body, parent_body, html_body=message.raw_html)
        store.insert_extraction(
            message_id=message.id,
            extracted_text=result.text,
            method=result.method,
            status=result.status,
        )
        status_counts[result.status] += 1
        method_counts[result.method] += 1
        processed += 1
        extract_log.debug(
            "extracted message id=%s status=%s method=%s chars=%d",
            message.id,
            result.status,
            result.method,
            len(result.text),
        )
    # New extractions give replies a char_count to time against their parent.
    if processed:
        store.recompute_timing()
    return status_counts, method_counts


def extract_main(argv: Sequence[str] | None = None) -> int:
    parser = build_extract_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer")

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    with Store(db_path) as store:
        status_counts, method_counts = run_extract(store, limit=args.limit)

    total = sum(status_counts.values())
    extract_log.info(
        "summary: processed=%d extracted=%d empty=%d failed=%d",
        total,
        status_counts.get("ok", 0),
        status_counts.get("empty", 0),
        status_counts.get("failed", 0),
    )
    for method, count in sorted(method_counts.items()):
        extract_log.info("  method %s: %d", method, count)
    return 0


# --- scoring command ----------------------------------------------------------


@dataclass
class ScoreSummary:
    """Tally of one ``mail-ai-score`` run."""

    scored: int = 0
    cache_hits: int = 0
    too_short: int = 0
    failed: int = 0
    api_calls: int = 0
    words_sent: int = 0
    #: Set by :func:`run_score` from the active detector generation.
    price_per_100_words: float = _DEFAULT_PRICE_PER_100_WORDS

    @property
    def estimated_spend(self) -> float:
        """Rough realtime cost of the words actually sent this run (USD)."""
        return self.words_sent / 100 * self.price_per_100_words

    def as_line(self) -> str:
        return (
            f"scored={self.scored} cache_hits={self.cache_hits} "
            f"too_short={self.too_short} failed={self.failed} "
            f"api_calls={self.api_calls} words_sent={self.words_sent} "
            f"est_spend=${self.estimated_spend:.4f}"
        )


@dataclass
class _BulkGroup:
    """One unique cleaned text queued for a bulk job.

    ``extraction_ids`` lists every extraction sharing that text this run; the
    first is the one whose submission is billed, the rest are fanned out from
    its result like cache hits.
    """

    text: str
    extraction_ids: list[int]


def build_score_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-score",
        description=(
            "Score each unscored, long-enough extraction for AI-generated content "
            "with Pangram. Each extraction is first cleaned for scoring (greetings, "
            "sign-offs and signatures removed); extractions whose cleaned text is "
            "under the 50-word reliability floor are marked 'too_short' and never "
            "sent; identical cleaned text is served from the score cache without an "
            "API call. Idempotent."
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_SCORE_LIMIT,
        metavar="N",
        help=(
            f"cap on Pangram API calls this run (cache hits are free and uncapped); "
            f"default {DEFAULT_SCORE_LIMIT}. Pass a larger value for production runs."
        ),
    )
    parser.add_argument(
        "--model",
        choices=sorted(MODEL_GENERATIONS),
        help=(
            "detector generation to score with, overriding the stored dashboard "
            "setting for this run: 'pangram-4' is Pangram 4, 'default' is the "
            "API's default selector, which resolves to Pangram 3 until that "
            "generation is deprecated. Without this flag the stored setting "
            f"applies, or {DEFAULT_MODEL} when none is stored."
        ),
    )
    parser.add_argument(
        "--bulk",
        action="store_true",
        help=(
            "submit this run's texts as a single Pangram Bulk API job instead of "
            "one realtime call per text; bulk words are billed at a 20%% discount, "
            "which suits large catch-up runs. --limit still caps the texts submitted."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be scored / gated / cache-hit without calling the API",
    )
    return parser


def _html_signature_hint(store: Store, message_pk: int) -> str | None:
    """Return the HTML signature-container text for a message, or ``None``.

    Resolves the message for an extraction and, when it has a stored ``raw_html``,
    returns :func:`~html_text.split_html_parts`'s ``signature_text`` (a stage-2
    cleaning hint). ``None`` when the message is gone or has no HTML part.
    """
    msg = store.get_message(message_pk)
    if msg is None or not msg.raw_html:
        return None
    return split_html_parts(msg.raw_html).signature_text or None


def run_score(
    store: Store,
    client: PangramClient | None,
    *,
    limit: int = DEFAULT_SCORE_LIMIT,
    min_words: int = SCORE_MIN_WORDS,
    dry_run: bool = False,
    message_ids: Container[int] | None = None,
    bulk: bool = False,
) -> ScoreSummary:
    """Score unscored extractions, capping real API calls at ``limit``.

    Each ok extraction is first run through :func:`cleaning.clean_for_scoring`
    (with the message's HTML signature hint when it has stored ``raw_html``);
    the ``min_words`` floor, the score-cache key and the text sent to Pangram all
    use that **cleaned** text (not the raw stage-1 extraction). Marks
    sub-``min_words`` (cleaned) extractions ``too_short``; serves identical
    cleaned text from the score cache (uncapped); scores the rest via ``client``
    up to ``limit`` calls. Idempotent — a re-run over a fully scored/gated store
    does nothing. ``client`` may be ``None`` only in ``dry_run`` mode.

    ``message_ids``, when given, restricts the run to the extractions of those
    messages; the rest of the scoring queue is left untouched. The default
    ``None`` scores the whole queue.

    The score cache is consulted per detector generation: only a verdict from
    the generation ``client`` selects (``DEFAULT_MODEL``'s in a dry run, where
    there is no client) can be reused, so a stored Pangram 3 verdict never
    stands in for a Pangram 4 run or the reverse.

    With ``bulk``, the texts that would each get a realtime call are instead
    submitted as one Bulk API job (billed at a 20% discount): ``limit`` caps
    the texts submitted, ``api_calls`` counts them (the billed unit), and
    identical cleaned text is submitted once with the verdict fanned out to
    every extraction sharing it, counted as cache hits past the first. Items
    the job fails (or never reports back) are left unscored for a later run.

    A run that is not a dry run first re-derives the extracted replies whose
    thread parent has been stored since the last check (:func:`refresh_parents`),
    so text made stale by a pull is never sent; a reply whose score that deletes
    joins this run's queue.
    """
    # A reply whose parent arrived after it was extracted must be re-derived
    # before its text is sent; the extract stage normally has done this, and
    # the repeat costs two lookups. A dry run writes nothing, so it skips it.
    if not dry_run:
        refresh_parents(store, score_log)

    summary = ScoreSummary()
    generation = generation_for_model(client.model if client is not None else DEFAULT_MODEL)
    price = _PRICE_PER_100_WORDS.get(generation, _DEFAULT_PRICE_PER_100_WORDS)
    summary.price_per_100_words = price * _BULK_PRICE_FACTOR if bulk else price

    #: text_sha256 → the texts queued for the one bulk job (bulk mode only).
    pending: dict[str, _BulkGroup] = {}

    # One pass over every unscored ok extraction (min_words=0 yields all); the
    # short-text gate is applied here on the *cleaned* word count so it never
    # depends on the raw extraction length. Cache first, API second (capped).
    for extraction in store.iter_extractions_needing_score(min_words=0):
        if message_ids is not None and extraction.message_id not in message_ids:
            continue
        # Compute the HTML signature hint once per extraction and clean with it,
        # so the reliability floor, cache key and scored text all reflect it.
        hint = _html_signature_hint(store, extraction.message_id)
        text = clean_for_scoring(extraction.extracted_text, hint).text
        word_count = len(text.split())

        # Gate short cleaned text (never sent). In dry-run, only count.
        if word_count < min_words:
            summary.too_short += 1
            if not dry_run:
                store.update_extraction_status(extraction.id, "too_short")
            continue

        text_sha = sha256_text(text)
        cached = store.find_score_by_text_sha256(text_sha, generation=generation)
        if cached is not None:
            summary.cache_hits += 1
            if not dry_run:
                store.insert_score(
                    extraction_id=extraction.id,
                    text_sha256=cached.text_sha256,
                    fraction_ai=cached.fraction_ai,
                    fraction_ai_assisted=cached.fraction_ai_assisted,
                    fraction_human=cached.fraction_human,
                    label=cached.label,
                    detector_version=cached.detector_version,
                    raw_response=cached.raw_response,
                )
            continue

        if bulk:
            # Queue for the single bulk job. A within-run duplicate joins the
            # existing item free of charge (over the limit included) and is
            # tallied as a cache hit — here in a dry run, at fan-out otherwise.
            group = pending.get(text_sha)
            if group is not None:
                group.extraction_ids.append(extraction.id)
                if dry_run:
                    summary.cache_hits += 1
                continue
            if len(pending) >= limit:
                continue
            pending[text_sha] = _BulkGroup(text=text, extraction_ids=[extraction.id])
            summary.words_sent += word_count
            if dry_run:
                summary.scored += 1
            continue

        # Needs a real API call — respect the per-run cap (leaves the rest for
        # a later run so scoring resumes cleanly).
        attempts = summary.scored + summary.failed
        if attempts >= limit:
            continue
        summary.words_sent += word_count

        if dry_run:
            summary.scored += 1
            continue

        assert client is not None  # guaranteed by score_main outside dry-run
        summary.api_calls += 1
        try:
            result = client.predict(text)
        except PangramError as exc:
            summary.failed += 1
            score_log.warning("scoring failed for extraction id=%s: %s", extraction.id, exc)
            continue
        store.insert_score(
            extraction_id=extraction.id,
            text_sha256=text_sha,
            fraction_ai=result.fraction_ai,
            fraction_ai_assisted=result.fraction_ai_assisted,
            fraction_human=result.fraction_human,
            label=result.prediction_short,
            detector_version=result.version,
            raw_response=result.raw,
        )
        summary.scored += 1
        score_log.debug(
            "scored extraction id=%s label=%s fraction_ai=%s",
            extraction.id,
            result.prediction_short,
            result.fraction_ai,
        )

    if pending and not dry_run:
        _score_bulk(store, client, pending, summary)
    return summary


def _score_bulk(
    store: Store,
    client: PangramClient | None,
    pending: dict[str, _BulkGroup],
    summary: ScoreSummary,
) -> None:
    """Submit ``pending`` as one bulk job and store each verdict that comes back.

    Item ids are the texts' sha256 hashes (the score-cache key), so results map
    straight back to their groups. A group whose item fails is only logged — its
    extractions stay unscored and the next run retries them.
    """
    assert client is not None  # guaranteed by score_main outside dry-run
    summary.api_calls += len(pending)
    try:
        outcome = client.predict_bulk({sha: group.text for sha, group in pending.items()})
    except PangramError as exc:
        summary.failed += len(pending)
        score_log.warning("bulk scoring failed for all %d texts: %s", len(pending), exc)
        return
    for text_sha, group in pending.items():
        result = outcome.results.get(text_sha)
        if result is None:
            summary.failed += 1
            score_log.warning(
                "bulk scoring failed for extraction id=%s: %s",
                group.extraction_ids[0],
                outcome.errors.get(text_sha, "missing from bulk results"),
            )
            continue
        for position, extraction_id in enumerate(group.extraction_ids):
            store.insert_score(
                extraction_id=extraction_id,
                text_sha256=text_sha,
                fraction_ai=result.fraction_ai,
                fraction_ai_assisted=result.fraction_ai_assisted,
                fraction_human=result.fraction_human,
                label=result.prediction_short,
                detector_version=result.version,
                raw_response=result.raw,
            )
            if position == 0:
                summary.scored += 1
            else:
                summary.cache_hits += 1
        score_log.debug(
            "scored extraction id=%s label=%s fraction_ai=%s",
            group.extraction_ids[0],
            result.prediction_short,
            result.fraction_ai,
        )


def score_main(argv: Sequence[str] | None = None) -> int:
    parser = build_score_parser()
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be a positive integer")

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.dry_run and not config.pangram_api_key:
        parser.error(
            "PANGRAM_API_KEY is not set. Add it to .env (see .env.example) to score messages;"
            " pulling and extraction work without it."
        )
    with Store(db_path) as store:
        # --model wins for this run; otherwise the dashboard's stored choice,
        # falling back to the client default when nothing has been chosen.
        model = args.model or store.get_setting(SETTING_PANGRAM_MODEL) or DEFAULT_MODEL
        client = None if args.dry_run else PangramClient(config.pangram_api_key, model=model)
        summary = run_score(
            store,
            client,
            limit=args.limit,
            min_words=SCORE_MIN_WORDS,
            dry_run=args.dry_run,
            bulk=args.bulk,
        )

    prefix = "dry-run summary" if args.dry_run else "summary"
    score_log.info("%s: %s", prefix, summary.as_line())
    return 0


# --- export command -----------------------------------------------------------


def build_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-export",
        description=(
            "Export one or more lists' messages, extractions and scores to a "
            "portable JSON Lines file, optionally limited to a range of message "
            "dates. The file is zstd-compressed by default and "
            "'.zst' is appended to the output path unless it is already there; "
            "--no-compress writes plain JSON Lines to the path as given. "
            "A local database read only — no IMAP or Pangram calls."
        ),
    )
    parser.add_argument(
        "lists",
        nargs="*",
        help="one or more list names (e.g. 'announce' 'general'); omit with --all-lists",
    )
    parser.add_argument(
        "--all-lists",
        action="store_true",
        help="export every list that has at least one message",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="output file path ('.zst' is appended unless --no-compress); required",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="write plain uncompressed JSON Lines to the output path as given",
    )
    parser.add_argument(
        "--date-from",
        metavar="ISO",
        help="export only messages dated on or after this ISO-8601 date/datetime",
    )
    parser.add_argument(
        "--date-to",
        metavar="ISO",
        help=(
            "export only messages dated on or before this ISO-8601 date/datetime "
            "(a bare date excludes that day's messages, which carry a time)"
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    return parser


def _validated_date(parser: argparse.ArgumentParser, flag: str, value: str | None) -> str | None:
    """``value`` if it parses as ISO-8601, else exit with a usage error.

    Checked here rather than in the exporter because an unparseable bound is a
    typo that would otherwise export a silently wrong range: the comparison is
    lexical, so any string is a valid bound as far as SQLite is concerned.
    """
    if not value:
        return None
    from datetime import datetime

    try:
        datetime.fromisoformat(value)
    except ValueError:
        parser.error(f"{flag} must be an ISO-8601 date or datetime")
    return value


def export_main(argv: Sequence[str] | None = None) -> int:
    parser = build_export_parser()
    args = parser.parse_args(argv)
    if args.all_lists and args.lists:
        parser.error("give either list names or --all-lists, not both")
    if not args.all_lists and not args.lists:
        parser.error("specify at least one list name, or --all-lists")
    if not args.output:
        parser.error("-o/--output is required")
    date_from = _validated_date(parser, "--date-from", args.date_from)
    date_to = _validated_date(parser, "--date-to", args.date_to)

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    list_names = args.lists or None
    try:
        with Store(db_path) as store:
            summary = export_lists(
                store,
                list_names,
                args.output,
                all_lists=args.all_lists,
                compress=not args.no_compress,
                date_from=date_from,
                date_to=date_to,
            )
    except ValueError as exc:
        parser.error(str(exc))

    export_log.info("summary: %s", summary.as_line())
    return 0


# --- stats export command -------------------------------------------------------


def build_stats_export_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-export-stats",
        description=(
            "Export the scores and message metadata of one or more lists as a zip "
            "archive of CSV files for analysis outside the app, optionally limited "
            "to a range of message dates. The archive carries no message text and "
            "cannot be imported; '.zip' is appended to the output path unless it is "
            "already there. A local database read only — no IMAP or Pangram calls."
        ),
    )
    parser.add_argument(
        "lists",
        nargs="*",
        help="one or more list names (e.g. 'announce' 'general'); omit with --all-lists",
    )
    parser.add_argument(
        "--all-lists",
        action="store_true",
        help="export every list that has at least one message",
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="FILE",
        help="output file path ('.zip' is appended unless already present); required",
    )
    parser.add_argument(
        "--date-from",
        metavar="ISO",
        help="export only messages dated on or after this ISO-8601 date/datetime",
    )
    parser.add_argument(
        "--date-to",
        metavar="ISO",
        help=(
            "export only messages dated on or before this ISO-8601 date/datetime "
            "(a bare date excludes that day's messages, which carry a time)"
        ),
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    return parser


def stats_export_main(argv: Sequence[str] | None = None) -> int:
    parser = build_stats_export_parser()
    args = parser.parse_args(argv)
    if args.all_lists and args.lists:
        parser.error("give either list names or --all-lists, not both")
    if not args.all_lists and not args.lists:
        parser.error("specify at least one list name, or --all-lists")
    if not args.output:
        parser.error("-o/--output is required")
    date_from = _validated_date(parser, "--date-from", args.date_from)
    date_to = _validated_date(parser, "--date-to", args.date_to)

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    list_names = args.lists or None
    try:
        with Store(db_path) as store:
            summary = export_stats(
                store,
                list_names,
                args.output,
                all_lists=args.all_lists,
                date_from=date_from,
                date_to=date_to,
            )
    except ValueError as exc:
        parser.error(str(exc))

    stats_export_log.info("summary: %s", summary.as_line())
    return 0


# --- import command -----------------------------------------------------------


def build_import_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-import",
        description=(
            "Import a JSON Lines export produced by mail-ai-export. Idempotent and "
            "collision-safe (messages already present are skipped) and all-or-nothing "
            "(one transaction, rolled back on any error). The container is detected "
            "from the file's content, not its name: zstd, gzip and uncompressed input "
            "are all accepted under any suffix."
        ),
    )
    parser.add_argument(
        "file",
        help="the export file to import (zstd, gzip or uncompressed; detected from content)",
    )
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and report what would be imported without writing",
    )
    return parser


def import_main(argv: Sequence[str] | None = None) -> int:
    parser = build_import_parser()
    args = parser.parse_args(argv)

    config = Config.load()
    db_path = args.db or config.database_path

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        with Store(db_path) as store:
            summary = import_file(store, args.file, dry_run=args.dry_run)
    except (ExportImportError, FileNotFoundError) as exc:
        import_log.error("import failed: %s", exc)
        return 1

    prefix = "dry-run summary" if args.dry_run else "summary"
    import_log.info("%s: %s", prefix, summary.as_line())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
