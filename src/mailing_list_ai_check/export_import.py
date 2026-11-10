"""Portable export / import of a list's messages and their pipeline state.

Moves everything related to one or more mailing lists — the ``lists`` row, its
pull cursor, the sender ``addresses`` and ``persons`` groupings, the ``messages``
themselves, and the ``extractions`` / ``scores`` derived from them — between
SQLite databases as a single JSON Lines file, without ever corrupting the target
on re-import. The full format and semantics live in ``docs/export-import.md``;
this module is the authoritative implementation of that spec.

Four design choices keep the file small, the import safe, and both directions
usable on a database of any size:

- **Text pointers.** A message body is static, so an extraction's text is stored
  as a pointer into ``raw_body`` (whole body, or a character span) rather than a
  duplicated copy, falling back to an inline literal only when the text is not a
  contiguous substring of the body. Every extraction also carries the SHA-256 of
  its text; the importer reconstructs the text from the pointer and aborts on a
  hash mismatch, so a pointer that no longer resolves is caught as corruption.
- **One transaction.** Import runs as a single explicit transaction on the
  store's connection using raw SQL (not the per-call-committing :class:`Store`
  mutators), committed once at the end and rolled back on any error, so a
  truncated or malformed file can never leave a half-imported database.
  ``dry_run`` takes the identical code path and rolls back instead of committing.
- **Both directions stream.** Neither export nor import ever holds more than one
  message in memory. Message bodies dominate an export — a JSON Lines file runs
  about 2.3x the size of the rows it came from — so buffering them would put
  peak memory in the gigabytes at realistic list sizes. The exporter therefore
  iterates its message cursor and writes each record as it is read (a cheap
  pre-pass over ``address_id`` alone supplies the addresses and persons that the
  format requires ahead of the messages), and the importer consumes one line at
  a time. Only the small per-file structures — the selected lists, the referenced
  addresses and persons — are collected up front.
- **Compression is a content question, not a naming one.** Exports are written
  zstd-compressed by default (see :mod:`.codec`); imports sniff the file's magic
  bytes and transparently accept zstd, the gzip of older exports, or plain text,
  whatever the file is called.

Import is idempotent and collision-safe: a message already present in the target
(same Message-ID on the same list) is skipped along with its extraction/score,
so importing the same file twice — or into the database it came from — is a
no-op.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .codec import CodecError, compressed_path, open_read_text, open_write_text
from email_reply_extractor import EXTRACTION_VERSION
from .store import (
    EXTRACTION_STATUSES,
    Store,
    extraction_version_for_app_version,
    sha256_text,
    version_key,
)

log = logging.getLogger("mailing_list_ai_check.export_import")

#: On-disk format identifiers, written into the ``header`` record and checked on
#: import. Bump :data:`FORMAT_VERSION` only on an incompatible format change.
#: Version 2 added the header ``app_version`` and per-message ``pipeline_version``
#: fields; version-1 files are rejected (the format shipped unreleased, so none
#: exist in the wild).
#:
#: Version 2 is extended additively rather than bumped: the extraction generation
#: (``extraction_version``, in the header and on each embedded extraction) was
#: added to it in place, and later the per-message ``from_name`` and
#: ``raw_headers_b64`` (base64, since the header block is bytes) alongside it,
#: then the header's optional ``date_from`` / ``date_to`` range.
#: A bump would have rejected every file already written,
#: and nothing needs rejecting in either direction. New code reading an old file
#: finds the key absent and falls back to inference from ``pipeline_version``
#: (see :func:`~.store.extraction_version_for_app_version`); old code reading a
#: new file accepts the unchanged ``format_version`` at the header and then
#: ignores the key, because every handler reads only the keys it names —
#: ``type`` is the sole field checked against a fixed set (:data:`_RECORD_RANK`),
#: and no handler validates a record against a closed key list. Any *removal* or
#: change of meaning in an existing key is still a bump.
FORMAT_NAME = "mlac-export"
FORMAT_VERSION = 2

#: Longest line, in characters, :func:`import_file` accepts. One line is one
#: record, and the largest record is a message carrying its raw body, raw HTML,
#: base64 raw headers and an embedded extraction and score. Measured on a
#: 174,029-message store (2026-11-11), the largest values were: raw body 328,040
#: characters, raw HTML 1,417,233, raw headers 24,672 bytes (about 33,000 in
#: base64), a score's raw response 550,794. Their sum, about 2.3 million
#: characters, bounds a real record before JSON escaping, which at most doubles
#: it for this content (``ensure_ascii=False``, so only quotes, backslashes and
#: control characters expand). 64 Mi characters leaves more than ten times that
#: headroom while keeping one line's memory bounded. The limit is enforced while
#: reading, so a longer line is never held in memory.
MAX_LINE_CHARS = 64 * 1024 * 1024

#: Total decompressed characters :func:`import_file` reads from one file before
#: it stops. The full export of the same 174,029-message store is about 220 MB
#: zstd-compressed and about 3.7 G characters decompressed; 64 Gi characters is
#: about seventeen times that. The cap bounds the work and the transaction size
#: a small compressed file can demand (a decompression bomb) at a fixed figure,
#: independent of the compression ratio, which varies with the share of HTML and
#: so gives no tight bound of its own. Every line is also parsed and validated
#: as it is read, so a stream of invalid records stops at its first line.
MAX_TOTAL_CHARS = 64 * 1024 * 1024 * 1024

#: Characters an IMAP mailbox name cannot carry in the quoted form
#: :func:`~.imap_client.quote_folder` sends (CR, LF and NUL). A ``list`` record
#: whose ``folder`` holds one is rejected, since no pull could open it.
_FOLDER_FORBIDDEN = frozenset("\r\n\x00")

#: Record ``type`` values in their fixed file order, mapped to a monotonic rank.
#: The importer requires records to appear in non-decreasing rank (``header``
#: first, ``trailer`` last), which guarantees every cross-reference is a
#: backward reference (a message's folder/email, an address's person) already
#: seen by the time it is needed.
_RECORD_RANK = {
    "header": 0,
    "list": 1,
    "pull_state": 1,
    "person": 2,
    "address": 3,
    "message": 4,
    "trailer": 5,
}


class ExportImportError(Exception):
    """Raised for any export/import validation failure (bad header, out-of-order
    record, forward reference, unknown type, invalid extraction status, hash
    mismatch, truncated file, or JSON parse error)."""


@dataclass(frozen=True)
class ExportSummary:
    """Tally of one :func:`export_lists` run."""

    lists: int
    messages: int
    extractions: int
    scores: int
    path: str

    def as_line(self) -> str:
        return (
            f"lists={self.lists} messages={self.messages} "
            f"extractions={self.extractions} scores={self.scores} path={self.path}"
        )


@dataclass(frozen=True)
class ImportSummary:
    """Tally of one :func:`import_file` run.

    ``*_created`` / ``*_inserted`` count rows actually written; ``messages_skipped``
    counts collision-guarded messages already present in the target (their
    embedded extraction/score are not imported); ``body_mismatches`` counts
    skipped messages whose stored ``raw_body`` differed from the file's (logged,
    never overwritten). ``extractions_updated`` / ``scores_updated`` count
    skipped messages whose derived state was refreshed from a later
    ``pipeline_version``; ``versions_bumped`` counts skipped messages whose
    derived state already matched a later-version file, so only version stamps
    moved — the message's ``pipeline_version`` and, when the file's generation is
    higher, the extraction's ``extraction_version`` (no other message column is
    ever modified). ``dry_run`` is true when the run rolled back instead of
    committing.
    """

    lists_created: int = 0
    lists_existing: int = 0
    pull_states_created: int = 0
    persons_created: int = 0
    addresses_upserted: int = 0
    messages_inserted: int = 0
    messages_skipped: int = 0
    body_mismatches: int = 0
    extractions_inserted: int = 0
    scores_inserted: int = 0
    extractions_updated: int = 0
    scores_updated: int = 0
    versions_bumped: int = 0
    dry_run: bool = False

    def as_line(self) -> str:
        return (
            f"lists_created={self.lists_created} lists_existing={self.lists_existing} "
            f"pull_states_created={self.pull_states_created} persons_created={self.persons_created} "
            f"addresses_upserted={self.addresses_upserted} "
            f"messages_inserted={self.messages_inserted} messages_skipped={self.messages_skipped} "
            f"body_mismatches={self.body_mismatches} "
            f"extractions_inserted={self.extractions_inserted} scores_inserted={self.scores_inserted} "
            f"extractions_updated={self.extractions_updated} scores_updated={self.scores_updated} "
            f"versions_bumped={self.versions_bumped} "
            f"dry_run={self.dry_run}"
        )


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def _decode_raw_headers(record: dict[str, Any], lineno: int) -> bytes | None:
    """Decode a message record's ``raw_headers_b64`` field, or ``None`` if absent.

    Absent in every file written before the field existed, and in new files for
    a message whose headers were never captured. Raises
    :class:`ExportImportError` rather than importing a corrupt header block.
    """
    encoded = record.get("raw_headers_b64")
    if encoded is None:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ExportImportError(f"line {lineno}: raw_headers_b64 is not valid base64") from exc


def _text_pointer(extracted_text: str, raw_body: str | None) -> dict[str, Any]:
    """Choose the smallest faithful pointer for ``extracted_text`` in ``raw_body``.

    In priority order: ``full_body`` when the text is the whole body, ``span``
    when it occurs verbatim (first ``str.find`` offset), else an ``inline``
    literal (also the case when ``raw_body`` is null).
    """
    if raw_body is not None and extracted_text == raw_body:
        return {"kind": "full_body"}
    if raw_body is not None:
        start = raw_body.find(extracted_text)
        if start != -1:
            return {"kind": "span", "start": start, "length": len(extracted_text)}
    return {"kind": "inline", "value": extracted_text}


def _resolve_pointer(pointer: Any, raw_body: str | None) -> str:
    """Reconstruct extracted text from its ``pointer`` and the message ``raw_body``.

    Inverse of :func:`_text_pointer`. Raises :class:`ExportImportError` on a
    malformed pointer or one that cannot resolve against ``raw_body``.
    """
    if not isinstance(pointer, dict):
        raise ExportImportError(f"malformed extraction text pointer: {pointer!r}")
    kind = pointer.get("kind")
    if kind == "full_body":
        if raw_body is None:
            raise ExportImportError("full_body text pointer but message has no raw_body")
        return raw_body
    if kind == "span":
        start, length = pointer.get("start"), pointer.get("length")
        if not isinstance(start, int) or not isinstance(length, int):
            raise ExportImportError(f"malformed span pointer: {pointer!r}")
        if raw_body is None:
            raise ExportImportError("span text pointer but message has no raw_body")
        return raw_body[start : start + length]
    if kind == "inline":
        value = pointer.get("value")
        if not isinstance(value, str):
            raise ExportImportError(f"malformed inline pointer: {pointer!r}")
        return value
    raise ExportImportError(f"unknown extraction text pointer kind: {kind!r}")


def _range_clause(column: str, date_from: str | None, date_to: str | None) -> str:
    """A date-range SQL fragment for ``column``, appended to a message query.

    ``column`` is the qualified date column of the query being extended, never
    user input; the bounds themselves are passed separately (see
    :func:`_range_params`) so that every query in one export selects the
    identical set of messages. Empty when neither bound is given, which leaves
    those queries byte-for-byte the ones an unranged export has always run.
    """
    clause = f" AND {column} >= ?" if date_from else ""
    return clause + (f" AND {column} <= ?" if date_to else "")


def _range_params(date_from: str | None, date_to: str | None) -> list[str]:
    """The bound parameters :func:`_range_clause` leaves placeholders for."""
    return [bound for bound in (date_from, date_to) if bound]


def _select_lists(
    conn: Any,
    list_names: Sequence[str] | None,
    *,
    all_lists: bool,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[Any]:
    """The ``lists`` rows an export selects, deduplicated by folder in file order.

    Lists are chosen by ``lists.name`` (a name may match several rows — every
    match is selected) or, with ``all_lists=True``, every list with at least one
    message in the date range, ordered by folder. Passing both ``list_names`` and
    ``all_lists`` — or neither — is a :class:`ValueError`, as is an unknown name.
    A named list is selected whether or not the range leaves it any message.

    Shared by :func:`export_lists` and
    :func:`~.stats_export.export_stats` so the two exports, their CLIs and their
    endpoints select the same lists from the same arguments.
    """
    has_names = list_names is not None and len(list_names) > 0
    if all_lists and has_names:
        raise ValueError("give either list names or all_lists=True, not both")
    if not all_lists and not has_names:
        raise ValueError("give one or more list names, or all_lists=True")

    selected: list[Any] = []
    seen_folders: set[str] = set()
    if all_lists:
        rows = conn.execute(
            "SELECT * FROM lists l "
            "WHERE EXISTS (SELECT 1 FROM messages m WHERE m.list_id = l.id"
            f"{_range_clause('m.date', date_from, date_to)}) "
            "ORDER BY l.folder",
            _range_params(date_from, date_to),
        ).fetchall()
        for row in rows:
            selected.append(row)
            seen_folders.add(row["folder"])
    else:
        assert list_names is not None
        for name in list_names:
            matches = conn.execute(
                "SELECT * FROM lists WHERE name = ? ORDER BY folder", (name,)
            ).fetchall()
            if not matches:
                raise ValueError(f"unknown list name: {name!r}")
            for row in matches:
                if row["folder"] not in seen_folders:
                    seen_folders.add(row["folder"])
                    selected.append(row)
    return selected


def _file_generation(extraction: dict[str, Any], pipeline_version: str | None) -> int | None:
    """The extraction generation an imported ``extraction`` record stands for.

    The record's own ``extraction_version`` when it carries one — the routine
    named there is the one that produced the text, whatever the importing build
    runs. Files written before the key existed are inferred from the message
    record's ``pipeline_version`` by the same mapping migration 011 applies
    locally (:func:`~.store.extraction_version_for_app_version`), which returns
    ``None`` only when the app version is unknown too.
    """
    generation = extraction.get("extraction_version")
    if generation is not None:
        return generation
    return extraction_version_for_app_version(pipeline_version)


# --- Export -------------------------------------------------------------------


def export_lists(
    store: Store,
    list_names: Sequence[str] | None,
    out_path: str | Path,
    *,
    all_lists: bool = False,
    compress: bool = True,
    date_from: str | None = None,
    date_to: str | None = None,
) -> ExportSummary:
    """Export one or more lists and everything derived from their messages.

    Lists are chosen by ``lists.name`` (a name may match several rows — every
    match is exported, keyed by ``folder``) or, with ``all_lists=True``, every
    list that has at least one message in scope. Passing both ``list_names`` and
    ``all_lists`` — or neither — is a :class:`ValueError`, as is an unknown name.

    ``date_from`` / ``date_to`` bound the exported messages by ``messages.date``,
    inclusively at both ends and independently (either may be given alone). The
    comparison is the lexical one :func:`~.store._build_message_where` applies to
    the dashboard's date filter, over the same UTC ISO-8601 column, so a range
    selects in the export exactly what it selects in the explorer — including its
    one sharp edge: a bare ``date_to`` day ("2026-03-01") excludes that day's
    messages, whose stored value carries a time. Named lists are exported whether
    or not the range leaves them any message; with ``all_lists=True`` a list is
    selected only if it has one in range.

    A ranged export omits every ``pull_state`` record, because a cursor asserts
    that a list is present up to ``last_uid`` and a partial file cannot say that;
    the format is otherwise identical, so it imports like any other export.

    Only persons/addresses actually referenced by the exported messages are
    written, each once (deduplicated across the whole file). Records are emitted
    in the fixed order ``header`` → (``list``, optional ``pull_state``) per list
    → ``person``s → ``address``es → ``message``s (extraction and score embedded)
    → ``trailer``.

    With ``compress`` (the default) the file is zstd-compressed and
    :data:`~.codec.COMPRESSED_SUFFIX` is appended to ``out_path`` unless it is
    already there; the returned summary's ``path`` is always the path actually
    written, so a caller that passed ``export.jsonl`` can report the
    ``export.jsonl.zst`` it got. ``compress=False`` writes plain text to
    ``out_path`` as given. The output suffix has no other meaning — nothing about
    the format is inferred from it, on write or on read.

    Messages are streamed: each record is written as its row is read, so peak
    memory is independent of how much mail is being exported. Purely a local
    database read: no IMAP, no Pangram, no caps involved.
    """
    conn = store.conn

    # The date range as a SQL fragment + bound params, appended to every query
    # that walks messages so all three select the identical set.
    def range_clause(column: str) -> str:
        return _range_clause(column, date_from, date_to)

    range_params = _range_params(date_from, date_to)

    # Resolve the selected list rows, deduplicated by folder in a stable order.
    selected = _select_lists(
        conn, list_names, all_lists=all_lists, date_from=date_from, date_to=date_to
    )

    # Pre-pass: which addresses do the exported messages reference? The format
    # puts person and address records ahead of the messages that reference them
    # (see _RECORD_RANK), but the reference set is only knowable by walking the
    # messages — so walk them twice. This pass selects the address_id column
    # alone, which keeps the bodies out of memory; the ordering (lists in
    # `selected` order, messages by id, first sighting wins) is the same
    # first-seen ordering the streaming pass below re-derives, and record order
    # is part of the format.
    referenced_address_ids: list[int] = []
    seen_address_ids: set[int] = set()
    for lst in selected:
        for row in conn.execute(
            "SELECT address_id FROM messages WHERE list_id = ? AND address_id IS NOT NULL"
            f"{range_clause('date')} ORDER BY id",
            [lst["id"], *range_params],
        ):
            address_id = row["address_id"]
            if address_id not in seen_address_ids:
                seen_address_ids.add(address_id)
                referenced_address_ids.append(address_id)

    # Resolve the referenced addresses and, through them, the persons to emit.
    # These are bounded by the number of distinct senders rather than by the
    # number of messages, so they are cheap to hold. The email map they build on
    # the way saves a lookup per message in the streaming pass; an address id
    # with no row is absent from it, which resolves to the same null email the
    # per-message lookup produced.
    address_records: list[dict[str, Any]] = []
    email_by_address_id: dict[int, str] = {}
    person_ids: list[int] = []
    seen_person_ids: set[int] = set()
    for address_id in referenced_address_ids:
        a = conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()
        if a is None:  # pragma: no cover - address_id came from a live FK
            continue
        email_by_address_id[address_id] = a["email"]
        person_key = None
        if a["person_id"] is not None:
            person_key = f"p{a['person_id']}"
            if a["person_id"] not in seen_person_ids:
                seen_person_ids.add(a["person_id"])
                person_ids.append(a["person_id"])
        address_records.append(
            {
                "type": "address",
                "email": a["email"],
                "display_name": a["display_name"],
                "person_key": person_key,
            }
        )

    person_records: list[dict[str, Any]] = []
    for person_id in person_ids:
        p = conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        if p is None:  # pragma: no cover - person_id came from a live FK
            continue
        person_records.append(
            {
                "type": "person",
                "person_key": f"p{person_id}",
                "canonical_name": p["canonical_name"],
            }
        )

    schema_row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    schema_version = schema_row["v"] if schema_row and schema_row["v"] is not None else 0
    folders = [lst["folder"] for lst in selected]

    written_path = compressed_path(out_path) if compress else out_path

    # Counted during the streaming pass; the trailer is written after the
    # messages, so the totals are complete by the time they are needed.
    n_messages = 0
    n_extractions = 0
    n_scores = 0

    with open_write_text(written_path, compress=compress) as fh:

        def emit(record: dict[str, Any]) -> None:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

        emit(
            {
                "type": "header",
                "format": FORMAT_NAME,
                "format_version": FORMAT_VERSION,
                "app_version": __version__,
                # Diagnostics only: the generation the exporting build runs. The
                # authoritative value is the per-extraction one below, because a
                # store can hold text from several generations at once.
                "extraction_version": EXTRACTION_VERSION,
                "exported_at": _utcnow_iso(),
                "schema_version": schema_version,
                "folders": folders,
                # Provenance for a partial export, so a file can say which
                # messages it was asked for rather than only which it holds.
                # Absent (not null) when unbounded, keeping the header identical
                # to one an export without a range would have written; the
                # importer reads neither key.
                **({"date_from": date_from} if date_from else {}),
                **({"date_to": date_to} if date_to else {}),
            }
        )
        for lst in selected:
            emit(
                {
                    "type": "list",
                    "name": lst["name"],
                    "folder": lst["folder"],
                    "last_synced_at": lst["last_synced_at"],
                    "removed_from_server_at": lst["removed_from_server_at"],
                    "last_message_at": lst["last_message_at"],
                }
            )
            # A cursor asserts that everything up to ``last_uid`` is present,
            # which a ranged export does not carry. Importing one into a fresh
            # target would make the next pull start above the mail the range
            # left out and skip it for good, so a ranged export ships no cursor
            # and the target keeps pulling the list from where it actually is.
            ps = (
                None
                if range_params
                else conn.execute(
                    "SELECT * FROM pull_state WHERE list_id = ?", (lst["id"],)
                ).fetchone()
            )
            if ps is not None:
                emit(
                    {
                        "type": "pull_state",
                        "folder": lst["folder"],
                        "uidvalidity": ps["uidvalidity"],
                        "last_uid": ps["last_uid"],
                    }
                )
        for record in person_records:
            emit(record)
        for record in address_records:
            emit(record)

        # The streaming pass. Iterating the cursor rather than fetching it means
        # only one message row — one body — is live at a time; each record is
        # serialised and handed to the writer before the next row is read. The
        # per-message extraction/score lookups run on their own cursors, which
        # SQLite is happy to interleave with the open one.
        for lst in selected:
            for m in conn.execute(
                f"SELECT * FROM messages WHERE list_id = ?{range_clause('date')} ORDER BY id",
                [lst["id"], *range_params],
            ):
                n_messages += 1
                email = (
                    email_by_address_id.get(m["address_id"])
                    if m["address_id"] is not None
                    else None
                )

                extraction_obj: dict[str, Any] | None = None
                ext = conn.execute(
                    "SELECT * FROM extractions WHERE message_id = ?", (m["id"],)
                ).fetchone()
                if ext is not None:
                    n_extractions += 1
                    score_obj: dict[str, Any] | None = None
                    sc = conn.execute(
                        "SELECT * FROM scores WHERE extraction_id = ?", (ext["id"],)
                    ).fetchone()
                    if sc is not None:
                        n_scores += 1
                        score_obj = {
                            "fraction_ai": sc["fraction_ai"],
                            "fraction_ai_assisted": sc["fraction_ai_assisted"],
                            "fraction_human": sc["fraction_human"],
                            "label": sc["label"],
                            "detector_version": sc["detector_version"],
                            "raw_response": sc["raw_response"],
                            "text_sha256": sc["text_sha256"],
                            "scored_at": sc["scored_at"],
                        }
                    extraction_obj = {
                        "method": ext["method"],
                        "char_count": ext["char_count"],
                        "status": ext["status"],
                        "extraction_version": ext["extraction_version"],
                        "created_at": ext["created_at"],
                        "text": _text_pointer(ext["extracted_text"], m["raw_body"]),
                        "sha256": sha256_text(ext["extracted_text"]),
                        "score": score_obj,
                    }

                emit(
                    {
                        "type": "message",
                        "folder": lst["folder"],
                        "message_id": m["message_id"],
                        "email": email,
                        "from_name": m["from_name"],
                        # Bytes, so base64 in a JSON Lines file. Absent (not
                        # null) when the row has no headers, keeping the line
                        # identical to one an older export would have written.
                        **(
                            {"raw_headers_b64": base64.b64encode(m["raw_headers"]).decode("ascii")}
                            if m["raw_headers"] is not None
                            else {}
                        ),
                        "subject": m["subject"],
                        "date": m["date"],
                        "in_reply_to": m["in_reply_to"],
                        "raw_body": m["raw_body"],
                        "raw_html": m["raw_html"],
                        "uid": m["uid"],
                        "fetched_at": m["fetched_at"],
                        "pipeline_version": m["pipeline_version"],
                        "auto_generated": m["auto_generated"],
                        "extraction": extraction_obj,
                    }
                )

        emit(
            {
                "type": "trailer",
                "lists": len(selected),
                "messages": n_messages,
                "extractions": n_extractions,
                "scores": n_scores,
            }
        )

    summary = ExportSummary(
        lists=len(selected),
        messages=n_messages,
        extractions=n_extractions,
        scores=n_scores,
        path=str(written_path),
    )
    return summary


# --- Import -------------------------------------------------------------------


class _Importer:
    """One import pass over a stream, accumulating state and summary counters.

    Isolated in a class so the per-record handlers can share the folder/email/
    person lookup maps built as records stream by. All writes go through raw SQL
    on ``store.conn`` inside a single transaction owned by :func:`import_file`.
    """

    def __init__(self, store: Store) -> None:
        self.conn = store.conn

        # File-scoped resolution maps (all references are backward references).
        self.folder_to_list_id: dict[str, int] = {}
        self.person_meta: dict[str, str] = {}  # person_key -> canonical_name
        self.group_person_id: dict[str, int] = {}  # person_key -> resolved person id
        self.seen_emails: set[str] = set()

        # Ordering / structure state.
        self.records_seen = 0
        self.max_rank = -1
        self.header_seen = False
        self.trailer_seen = False

        # Trailer-verification counters (records present in the file).
        self.file_lists = 0
        self.file_messages = 0
        self.file_extractions = 0
        self.file_scores = 0

        # Summary counters.
        self.lists_created = 0
        self.lists_existing = 0
        self.pull_states_created = 0
        self.persons_created = 0
        self.addresses_upserted = 0
        self.messages_inserted = 0
        self.messages_skipped = 0
        self.body_mismatches = 0
        self.extractions_inserted = 0
        self.scores_inserted = 0
        self.extractions_updated = 0
        self.scores_updated = 0
        self.versions_bumped = 0

    # -- dispatch -------------------------------------------------------------

    def handle(self, record: Any, lineno: int) -> None:
        if not isinstance(record, dict):
            raise ExportImportError(f"line {lineno}: record is not a JSON object")
        rtype = record.get("type")
        if rtype not in _RECORD_RANK:
            raise ExportImportError(f"line {lineno}: unknown record type {rtype!r}")

        if self.trailer_seen:
            raise ExportImportError(f"line {lineno}: record after trailer")

        rank = _RECORD_RANK[rtype]
        if self.records_seen == 0:
            if rtype != "header":
                raise ExportImportError(f"line {lineno}: first record must be a header")
        else:
            if rtype == "header":
                raise ExportImportError(f"line {lineno}: duplicate header")
            if rank < self.max_rank:
                raise ExportImportError(f"line {lineno}: {rtype!r} record out of order")
        self.max_rank = max(self.max_rank, rank)
        self.records_seen += 1

        handler = getattr(self, f"_handle_{rtype}")
        handler(record, lineno)

    def finish(self) -> None:
        if not self.header_seen:
            raise ExportImportError("missing header record")
        if not self.trailer_seen:
            raise ExportImportError("missing trailer record (file truncated?)")

    # -- per-record handlers --------------------------------------------------

    def _handle_header(self, record: dict[str, Any], lineno: int) -> None:
        if record.get("format") != FORMAT_NAME:
            raise ExportImportError(
                f"line {lineno}: unexpected format {record.get('format')!r} "
                f"(expected {FORMAT_NAME!r})"
            )
        if record.get("format_version") != FORMAT_VERSION:
            raise ExportImportError(
                f"line {lineno}: unsupported format_version {record.get('format_version')!r} "
                f"(expected {FORMAT_VERSION})"
            )
        self.header_seen = True

    def _handle_list(self, record: dict[str, Any], lineno: int) -> None:
        self.file_lists += 1
        folder = record.get("folder")
        if not isinstance(folder, str) or not folder:
            raise ExportImportError(f"line {lineno}: list folder must be a non-empty string")
        if _FOLDER_FORBIDDEN.intersection(folder):
            # The stored folder is later sent to the IMAP server by the ranged
            # pull and preview; CR, LF or NUL cannot be quoted there.
            raise ExportImportError(f"line {lineno}: list folder {folder!r} contains CR, LF or NUL")
        existing = self.conn.execute("SELECT id FROM lists WHERE folder = ?", (folder,)).fetchone()
        if existing is not None:
            # An existing row is authoritative; its metadata is not overwritten.
            self.folder_to_list_id[folder] = existing["id"]
            self.lists_existing += 1
            return
        cur = self.conn.execute(
            "INSERT INTO lists(name, folder, last_synced_at, removed_from_server_at, "
            "last_message_at) VALUES (?, ?, ?, ?, ?)",
            (
                record["name"],
                folder,
                record.get("last_synced_at"),
                record.get("removed_from_server_at"),
                record.get("last_message_at"),
            ),
        )
        self.folder_to_list_id[folder] = cur.lastrowid
        self.lists_created += 1

    def _handle_pull_state(self, record: dict[str, Any], lineno: int) -> None:
        folder = record["folder"]
        list_id = self.folder_to_list_id.get(folder)
        if list_id is None:
            raise ExportImportError(f"line {lineno}: pull_state for unseen folder {folder!r}")
        # An existing cursor reflects the target's own sync state and always wins.
        existing = self.conn.execute(
            "SELECT 1 FROM pull_state WHERE list_id = ?", (list_id,)
        ).fetchone()
        if existing is not None:
            return
        self.conn.execute(
            "INSERT INTO pull_state(list_id, uidvalidity, last_uid) VALUES (?, ?, ?)",
            (list_id, record["uidvalidity"], record["last_uid"]),
        )
        self.pull_states_created += 1

    def _handle_person(self, record: dict[str, Any], lineno: int) -> None:
        person_key = record.get("person_key")
        if not isinstance(person_key, str):
            raise ExportImportError(f"line {lineno}: person record missing person_key")
        # Registered only; persons are created lazily, when an address needs one.
        self.person_meta[person_key] = record["canonical_name"]

    def _handle_address(self, record: dict[str, Any], lineno: int) -> None:
        self.addresses_upserted += 1
        email = record["email"].strip().lower()
        self.seen_emails.add(email)
        # Same display-name backfill semantics as Store.upsert_address.
        self.conn.execute(
            "INSERT INTO addresses(email, display_name) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "display_name = COALESCE(NULLIF(addresses.display_name, ''), excluded.display_name)",
            (email, record.get("display_name")),
        )
        row = self.conn.execute(
            "SELECT id, person_id FROM addresses WHERE email = ?", (email,)
        ).fetchone()

        person_key = record.get("person_key")
        if person_key is None:
            return
        if person_key not in self.person_meta:
            raise ExportImportError(
                f"line {lineno}: address references unseen person_key {person_key!r}"
            )

        if row["person_id"] is not None:
            # Already linked in the target: keep it, and let it recruit the group
            # so later unlinked members of the same group join this person.
            self.group_person_id.setdefault(person_key, row["person_id"])
            return

        target_id = self.group_person_id.get(person_key)
        if target_id is None:
            cur = self.conn.execute(
                "INSERT INTO persons(canonical_name) VALUES (?)",
                (self.person_meta[person_key],),
            )
            target_id = cur.lastrowid
            self.group_person_id[person_key] = target_id
            self.persons_created += 1
        self.conn.execute("UPDATE addresses SET person_id = ? WHERE id = ?", (target_id, row["id"]))

    def _handle_message(self, record: dict[str, Any], lineno: int) -> None:
        self.file_messages += 1
        folder = record["folder"]
        list_id = self.folder_to_list_id.get(folder)
        if list_id is None:
            raise ExportImportError(f"line {lineno}: message references unseen folder {folder!r}")

        extraction = record.get("extraction")
        if extraction is not None:
            self.file_extractions += 1
            if extraction.get("score") is not None:
                self.file_scores += 1

        address_id = None
        email = record.get("email")
        if email is not None:
            normalized = email.strip().lower()
            if normalized not in self.seen_emails:
                raise ExportImportError(f"line {lineno}: message references unseen email {email!r}")
            addr = self.conn.execute(
                "SELECT id FROM addresses WHERE email = ?", (normalized,)
            ).fetchone()
            address_id = addr["id"] if addr else None

        raw_body = record.get("raw_body")
        cur = self.conn.execute(
            "INSERT INTO messages("
            "message_id, list_id, address_id, subject, date, in_reply_to, raw_body, uid, "
            "fetched_at, raw_html, pipeline_version, auto_generated, from_name, raw_headers"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(list_id, message_id) DO NOTHING",
            (
                record["message_id"],
                list_id,
                address_id,
                record.get("subject"),
                record.get("date"),
                record.get("in_reply_to"),
                raw_body,
                record.get("uid"),
                record["fetched_at"],
                record.get("raw_html"),
                record.get("pipeline_version"),
                # Absent in files written before the field existed → NULL.
                record.get("auto_generated"),
                record.get("from_name"),
                _decode_raw_headers(record, lineno),
            ),
        )

        if cur.rowcount == 0:
            # Collision: the message already exists. Its own row is never
            # modified. Its embedded extraction/score are not imported either,
            # unless the file carries a later pipeline_version (see below).
            self.messages_skipped += 1
            existing = self.conn.execute(
                "SELECT id, raw_body, pipeline_version "
                "FROM messages WHERE list_id = ? AND message_id = ?",
                (list_id, record["message_id"]),
            ).fetchone()
            if existing is None:  # pragma: no cover - conflict implies a row exists
                return
            if existing["raw_body"] != raw_body:
                self.body_mismatches += 1
                # Identifiers only — never the body itself.
                log.warning(
                    "raw_body mismatch for existing message (folder=%s message_id=%s); "
                    "keeping stored copy",
                    folder,
                    record["message_id"],
                )
            # Version-aware refresh: only when the file's pipeline_version is
            # strictly later (tuple comparison, NULL oldest). Differing derived
            # data is replaced; identical derived data means the later pipeline
            # validated what the target already holds, so only the version stamp
            # is adopted — the data is correct as of that newer version.
            file_version = record.get("pipeline_version")
            if version_key(file_version) > version_key(existing["pipeline_version"]):
                if self._derived_matches(existing["id"], extraction):
                    self.conn.execute(
                        "UPDATE messages SET pipeline_version = ? WHERE id = ?",
                        (file_version, existing["id"]),
                    )
                    self._advance_generation(existing["id"], extraction, file_version)
                    self.versions_bumped += 1
                else:
                    self._refresh_derived(
                        existing["id"], extraction, raw_body, file_version, lineno
                    )
            return

        self.messages_inserted += 1
        if extraction is not None:
            self._insert_extraction(
                cur.lastrowid, extraction, raw_body, record.get("pipeline_version"), lineno
            )

    def _write_extraction(
        self,
        message_pk: int,
        extraction: dict[str, Any],
        raw_body: str | None,
        pipeline_version: str | None,
        lineno: int,
    ) -> bool:
        """Insert one extraction (and its score when present) for ``message_pk``.

        Reconstructs the extracted text from its pointer, verifies it against the
        stored ``sha256``, and writes the ``extractions`` row plus an embedded
        ``scores`` row if any. Returns ``True`` when a score row was inserted.
        Touches no summary counters — the caller records inserted/updated counts.

        Both version columns are written from the file, never from the importing
        build: ``pipeline_version`` is the message record's stamp (the release
        that produced this derived state), and ``extraction_version`` is
        :func:`_file_generation` of the record. Stamping the running
        :data:`~.extraction.EXTRACTION_VERSION` here would claim this build's
        routine produced text it never saw.
        """
        status = extraction.get("status")
        if status not in EXTRACTION_STATUSES:
            raise ExportImportError(
                f"line {lineno}: invalid extraction status {status!r}; "
                f"expected one of {EXTRACTION_STATUSES}"
            )
        text = _resolve_pointer(extraction.get("text"), raw_body)
        expected = extraction.get("sha256")
        actual = sha256_text(text)
        if actual != expected:
            raise ExportImportError(
                f"line {lineno}: extraction text hash mismatch "
                f"(expected {expected!r}, got {actual!r}) — file corrupt"
            )
        char_count = extraction.get("char_count")
        if char_count is None:
            char_count = len(text)
        cur = self.conn.execute(
            "INSERT INTO extractions("
            "message_id, extracted_text, method, char_count, status, created_at, "
            "pipeline_version, extraction_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_pk,
                text,
                extraction["method"],
                char_count,
                status,
                extraction["created_at"],
                pipeline_version,
                _file_generation(extraction, pipeline_version),
            ),
        )

        score = extraction.get("score")
        if score is not None:
            # Exports written before v1.4.1 carry the derived "AI-Assisted"
            # label; normalise it back to the "Mixed" the API returned, exactly
            # as migration 013 did for stored rows.
            label = score.get("label")
            if label == "AI-Assisted":
                label = "Mixed"
            self.conn.execute(
                "INSERT INTO scores("
                "extraction_id, fraction_ai, fraction_ai_assisted, fraction_human, "
                "label, detector_version, raw_response, text_sha256, scored_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    cur.lastrowid,
                    score.get("fraction_ai"),
                    score.get("fraction_ai_assisted"),
                    score.get("fraction_human"),
                    label,
                    score.get("detector_version"),
                    score.get("raw_response"),
                    score["text_sha256"],
                    score["scored_at"],
                ),
            )
            return True
        return False

    def _insert_extraction(
        self,
        message_pk: int,
        extraction: dict[str, Any],
        raw_body: str | None,
        pipeline_version: str | None,
        lineno: int,
    ) -> None:
        """Insert the extraction (and score) for a freshly inserted message,
        counting them under ``extractions_inserted`` / ``scores_inserted``."""
        score_inserted = self._write_extraction(
            message_pk, extraction, raw_body, pipeline_version, lineno
        )
        self.extractions_inserted += 1
        if score_inserted:
            self.scores_inserted += 1

    def _derived_matches(self, message_pk: int, extraction: dict[str, Any] | None) -> bool:
        """Return whether the target's derived state already equals the file's.

        Compares the existing ``extractions`` / ``scores`` rows for ``message_pk``
        against the file's embedded ``extraction`` (which may be ``None``).
        Presence/absence on either side counts as a difference; otherwise the
        extraction is compared on text ``sha256``/``method``/``status``/
        ``char_count`` and the score on ``text_sha256``, the three fractions,
        ``label``, ``detector_version`` and ``raw_response``.
        """
        target_ext = self.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        if extraction is None:
            return target_ext is None
        if target_ext is None:
            return False
        if sha256_text(target_ext["extracted_text"]) != extraction.get("sha256"):
            return False
        if target_ext["method"] != extraction.get("method"):
            return False
        if target_ext["status"] != extraction.get("status"):
            return False
        file_char = extraction.get("char_count")
        if file_char is not None and target_ext["char_count"] != file_char:
            return False

        target_score = self.conn.execute(
            "SELECT * FROM scores WHERE extraction_id = ?", (target_ext["id"],)
        ).fetchone()
        file_score = extraction.get("score")
        if (target_score is None) != (file_score is None):
            return False
        if file_score is not None:
            for column, key in (
                ("text_sha256", "text_sha256"),
                ("fraction_ai", "fraction_ai"),
                ("fraction_ai_assisted", "fraction_ai_assisted"),
                ("fraction_human", "fraction_human"),
                ("label", "label"),
                ("detector_version", "detector_version"),
                ("raw_response", "raw_response"),
            ):
                if target_score[column] != file_score.get(key):
                    return False
        return True

    def _advance_generation(
        self, message_pk: int, extraction: dict[str, Any] | None, file_version: str | None
    ) -> None:
        """Raise the target extraction's generation to the file's, never lower it.

        Only reached from the version-bump branch, where :meth:`_derived_matches`
        has already established that the file's text hash, method, status and
        character count equal the target's — so a file of a later generation has
        demonstrated that its routine produces exactly the text the target holds,
        and the target's stamp can adopt it without re-deriving anything. The
        write is a maximum, so a file of an *earlier* generation (or one whose
        generation is unknown) leaves the stamp alone; ``extractions.pipeline_version``
        is provenance of the row as written and is not touched here.
        """
        if extraction is None:
            return
        generation = _file_generation(extraction, file_version)
        if generation is None:
            return
        target = self.conn.execute(
            "SELECT id, extraction_version FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        if target is None:  # pragma: no cover - _derived_matches saw a row
            return
        if (target["extraction_version"] or 0) < generation:
            self.conn.execute(
                "UPDATE extractions SET extraction_version = ? WHERE id = ?",
                (generation, target["id"]),
            )

    def _refresh_derived(
        self,
        message_pk: int,
        extraction: dict[str, Any] | None,
        raw_body: str | None,
        file_version: str | None,
        lineno: int,
    ) -> None:
        """Replace the target's derived state with the file's (message untouched).

        Deletes the existing extraction (its score cascades away), inserts the
        file's extraction and score when present — a ``None`` extraction just
        clears the old derived data — and stamps the message's
        ``pipeline_version`` with the file's later value. The new extraction row
        takes both of its own version stamps from the file too (see
        :meth:`_write_extraction`). Records
        ``extractions_updated`` once, and ``scores_updated`` when the score state
        changed (a score was inserted or an existing one removed).
        """
        target_ext = self.conn.execute(
            "SELECT id FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        target_had_score = target_ext is not None and (
            self.conn.execute(
                "SELECT 1 FROM scores WHERE extraction_id = ?", (target_ext["id"],)
            ).fetchone()
            is not None
        )

        self.conn.execute("DELETE FROM extractions WHERE message_id = ?", (message_pk,))
        file_has_score = False
        if extraction is not None:
            file_has_score = self._write_extraction(
                message_pk, extraction, raw_body, file_version, lineno
            )

        self.extractions_updated += 1
        if file_has_score or target_had_score:
            self.scores_updated += 1

        self.conn.execute(
            "UPDATE messages SET pipeline_version = ? WHERE id = ?",
            (file_version, message_pk),
        )

    def _handle_trailer(self, record: dict[str, Any], lineno: int) -> None:
        expected = {
            "lists": self.file_lists,
            "messages": self.file_messages,
            "extractions": self.file_extractions,
            "scores": self.file_scores,
        }
        for key, count in expected.items():
            if record.get(key) != count:
                raise ExportImportError(
                    f"line {lineno}: trailer {key}={record.get(key)!r} does not match "
                    f"{count} record(s) seen — file truncated or corrupt"
                )
        self.trailer_seen = True

    def summary(self, *, dry_run: bool) -> ImportSummary:
        return ImportSummary(
            lists_created=self.lists_created,
            lists_existing=self.lists_existing,
            pull_states_created=self.pull_states_created,
            persons_created=self.persons_created,
            addresses_upserted=self.addresses_upserted,
            messages_inserted=self.messages_inserted,
            messages_skipped=self.messages_skipped,
            body_mismatches=self.body_mismatches,
            extractions_inserted=self.extractions_inserted,
            scores_inserted=self.scores_inserted,
            extractions_updated=self.extractions_updated,
            scores_updated=self.scores_updated,
            versions_bumped=self.versions_bumped,
            dry_run=dry_run,
        )


def import_file(
    store: Store,
    in_path: str | Path,
    *,
    dry_run: bool = False,
    max_line_chars: int = MAX_LINE_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> ImportSummary:
    """Import an export file into ``store`` as one all-or-nothing transaction.

    Streams the JSON Lines file once, line by line, so memory is independent of
    the file's size. The container is recognised from the file's leading bytes,
    not its name (see :func:`~.codec.detect`): zstd, the gzip of older exports,
    and plain text are all accepted, under any suffix. A compressed stream that
    is truncated or corrupt raises :class:`ExportImportError`, the same type as
    every other bad-file failure.

    Validates structure as it goes — header first with a matching
    format/version, records in their fixed order, no forward references, valid
    extraction statuses, extraction hashes that resolve, and a trailer whose
    counts match the records seen. Messages colliding on
    ``(list_id, message_id)`` are skipped (their extraction/score with them);
    everything else is inserted with raw SQL.

    The whole pass runs inside a single explicit transaction on ``store.conn``,
    committed once on success and rolled back on any error (which is re-raised).
    ``dry_run`` takes the identical path and rolls back instead of committing, so
    its returned :class:`ImportSummary` (with ``dry_run=True``) is exact. Any
    validation failure raises :class:`ExportImportError`; a missing file still
    raises :class:`FileNotFoundError`.

    Two limits bound what a file can demand, each raising
    :class:`ExportImportError` and rolling back: a line longer than
    ``max_line_chars`` characters (default :data:`MAX_LINE_CHARS`), detected
    while reading so the line is never held whole, and more than
    ``max_total_chars`` decompressed characters in all (default
    :data:`MAX_TOTAL_CHARS`). A line whose JSON nests too deeply to parse is
    reported the same way.
    """
    conn = store.conn
    importer = _Importer(store)

    conn.execute("BEGIN")
    try:
        try:
            with open_read_text(in_path) as fh:
                total_chars = 0
                lineno = 0
                while True:
                    # One character over the cap distinguishes a line of exactly
                    # max_line_chars (plus its newline) from a longer one, and
                    # readline stops there instead of reading the whole line.
                    line = fh.readline(max_line_chars + 1)
                    if not line:
                        break
                    lineno += 1
                    if len(line) > max_line_chars and not line.endswith("\n"):
                        raise ExportImportError(
                            f"line {lineno}: longer than {max_line_chars} characters"
                        )
                    total_chars += len(line)
                    if total_chars > max_total_chars:
                        raise ExportImportError(
                            f"line {lineno}: file exceeds {max_total_chars} decompressed characters"
                        )
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        record = json.loads(stripped)
                    except RecursionError as exc:
                        raise ExportImportError(
                            f"line {lineno}: invalid JSON: nested too deeply"
                        ) from exc
                    except ValueError as exc:
                        # JSONDecodeError, and also the ValueError json raises for
                        # an integer longer than sys.get_int_max_str_digits().
                        raise ExportImportError(f"line {lineno}: invalid JSON: {exc}") from exc
                    importer.handle(record, lineno)
        except CodecError as exc:
            # One exception type for callers: a broken container is as much a
            # bad file as a broken record, and the CLI/webapp handlers catch
            # ExportImportError.
            raise ExportImportError(f"cannot read {in_path}: {exc}") from exc
        except UnicodeDecodeError as exc:
            # Detection classifies anything without a zstd or gzip magic as
            # plain text, so arbitrary binary reaches the UTF-8 decoder here
            # rather than the codec layer. Same contract as a broken container.
            raise ExportImportError(f"cannot read {in_path}: not valid UTF-8 text: {exc}") from exc
        importer.finish()
    except Exception:
        conn.rollback()
        raise

    if dry_run:
        conn.rollback()
    else:
        conn.commit()
        # Imported messages/extractions may complete (or be) reply pairs.
        store.recompute_timing()

    return importer.summary(dry_run=dry_run)
