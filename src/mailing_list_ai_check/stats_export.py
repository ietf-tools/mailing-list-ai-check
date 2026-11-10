"""One-way export of scores and message metadata for statistical analysis.

Writes a zip archive of CSV files — one row per message, a per-list aggregate,
the sender grouping, a Frictionless Data Package descriptor and a data dictionary
— for analysis outside the app, in a spreadsheet, pandas or R. The full format
and semantics live in
``docs/stats-export.md``; this module is the authoritative implementation of
that spec. It complements :mod:`.export_import`, which moves complete databases
between installs: nothing here is ever read back by the app, and the archive
carries no message content — no bodies, extracted text, subjects, raw headers or
detector responses.

Four properties follow from that purpose:

- **Denominators, not only hits.** Every message in scope is exported, scored or
  not, because a share calculation needs the messages that were never scored and
  those gated under the reliability floor. ``lists.csv`` aggregates the identical
  scope, so its counts sum to ``messages.csv`` and an analyst can verify one
  against the other.
- **Open everywhere.** Zip and CSV rather than the full export's zstd JSON
  Lines: the audience is analysis tools, not this app, and the archive bundles
  its own data dictionary with the data. It describes itself in a standard
  ``datapackage.json``, so the unzipped directory is a valid Frictionless Data
  Package: the CSVs can be validated against their declared schemas, and a
  reader that understands data packages loads them with the right types.
- **Mail-native identity.** A row names its message by RFC 5322 Message-ID and
  its sender by address, never by one of this app's row ids or a file-scoped
  surrogate. ``message_id`` is consequently not unique — a message cross-posted
  to several exported lists appears once per list, and ``(folder, message_id)``
  is the unique pair. The one synthetic value in the format is
  ``senders.csv``'s ``sender_key``, which exists to express the grouping mail
  itself cannot: the several addresses the app has linked to one person.
- **The message pass streams.** Rows are written into the zip member one at a
  time (:meth:`zipfile.ZipFile.open` in write mode), so no message row is held
  beyond the one being written. The pre-pass that precedes it holds only the
  addresses the messages in scope refer to, never a message row.

Purely a local database read: no IMAP, no Pangram, no caps involved.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .export_import import _range_clause, _range_params, _select_lists
from .store import (
    EXTRACTION_STATUSES,
    TIMING_IMPLAUSIBLE_CPM,
    TIMING_SUSPICIOUS_CPM,
    Store,
)

#: Format identifiers written into ``datapackage.json``.
#: :data:`STATS_FORMAT_VERSION` is this format's own number, independent of the
#: app version and of the full export's ``FORMAT_VERSION``. Nothing reads the file
#: back, so the version exists for analysts and their scripts rather than for
#: rejection logic: an added column does not bump it, a removed or re-defined one
#: does. Version 1 carried surrogate ``message_key`` / ``sender_key`` columns in
#: ``messages.csv``, a per-sender aggregate ``senders.csv``, a pseudonymous
#: variant and a bespoke ``manifest.json``; version 2 identifies rows by
#: ``message_id`` and ``email``, reduces ``senders.csv`` to the two-column
#: grouping, has one variant only, and describes itself in ``datapackage.json``.
STATS_FORMAT_NAME = "mlac-stats"
STATS_FORMAT_VERSION = 2

#: Suffix of the archive, appended to the caller's path unless already present.
ZIP_SUFFIX = ".zip"

#: The five archive members, in the order they are written.
MESSAGES_MEMBER = "messages.csv"
LISTS_MEMBER = "lists.csv"
SENDERS_MEMBER = "senders.csv"
DATAPACKAGE_MEMBER = "datapackage.json"
README_MEMBER = "README.md"

#: Resource names in :data:`DATAPACKAGE_MEMBER`: the member names without their
#: suffix, which is what a foreign key's ``reference.resource`` names.
MESSAGES_RESOURCE = "messages"
LISTS_RESOURCE = "lists"
SENDERS_RESOURCE = "senders"

#: The Data Package profile the descriptor declares, and the package's identity.
#: The descriptor is a standard Frictionless Data Package (v2), so the unzipped
#: archive validates with ``frictionless validate datapackage.json``.
DATAPACKAGE_PROFILE = "https://datapackage.org/profiles/2.0/datapackage.json"
PACKAGE_TITLE = "Mailing List AI Check stats export"
PACKAGE_DESCRIPTION = "Scores and message metadata for analysis; no message text."

#: Pangram ``prediction_short`` values, which ``scores.label`` stores verbatim,
#: and the reply-timing bands :func:`~.store.classify_timing` assigns. Both are
#: written into the descriptor as ``enum`` constraints, so a consumer reads the
#: closed set behind the ``label`` and ``timing`` columns off the schema rather
#: than inferring it from the rows present. ``extraction_status``'s vocabulary is
#: :data:`~.store.EXTRACTION_STATUSES`, the column's own definition.
LABELS = ("Human", "Mixed", "AI")
TIMING_BANDS = ("normal", "suspicious", "implausible")

#: A fraction's declared range: the detector's fractions and ``ai_share`` are
#: proportions, so the schema bounds them rather than leaving them unbounded
#: numbers.
_FRACTION_CONSTRAINTS: dict[str, Any] = {"minimum": 0, "maximum": 1}


@dataclass(frozen=True)
class _Column:
    """One CSV column: its name, its declared type, and what it means.

    The single definition of a column, from which both the descriptor's Table
    Schema field and the archive README's data-dictionary row are generated, so
    the two cannot describe the same column differently. ``description`` is the
    dictionary wording, one sentence, Markdown as the README renders it.
    """

    name: str
    type: str
    description: str
    constraints: dict[str, Any] | None = None

    def as_field(self) -> dict[str, Any]:
        """This column as a Table Schema field."""
        field: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "description": self.description,
        }
        if self.constraints is not None:
            field["constraints"] = dict(self.constraints)
        return field


#: ``messages.csv`` columns in file order, which is part of the format. The row
#: is identified by ``message_id`` — not unique, ``(folder, message_id)`` is —
#: and its sender by ``email``.
_MESSAGE_COLUMNS = (
    _Column(
        "message_id",
        "string",
        "RFC 5322 Message-ID; not unique (see the caveats below)",
    ),
    _Column("list", "string", "the mailing list's name; not unique across lists"),
    _Column(
        "folder",
        "string",
        f"the list's IMAP folder, its unique key; joins to `{LISTS_MEMBER}`",
    ),
    _Column("date", "datetime", "the message's `Date`, UTC ISO-8601; empty when it had none"),
    _Column(
        "email",
        "string",
        "sender address, empty when the message has none; joins to "
        f"`{SENDERS_MEMBER}` on its `email`",
    ),
    _Column(
        "sender_name",
        "string",
        "the message's `From` name, falling back to the address's display name",
    ),
    _Column("in_reply_to", "string", "stored `In-Reply-To` value, empty when not a reply"),
    _Column(
        "auto_generated",
        "string",
        "the marker that classified the message auto-generated, empty when none",
    ),
    _Column(
        "timing",
        "string",
        "reply-timing band: `normal`, `suspicious`, `implausible`, or empty",
        {"enum": list(TIMING_BANDS)},
    ),
    _Column(
        "timing_cpm",
        "number",
        "the characters-per-minute rate behind the band, empty exactly where `timing` is",
    ),
    _Column(
        "extraction_status",
        "string",
        "`ok`, `empty`, `too_short`, `failed`, or empty when never extracted",
        {"enum": list(EXTRACTION_STATUSES)},
    ),
    _Column(
        "extraction_method",
        "string",
        "the extraction routine that produced the text, empty when never extracted",
    ),
    _Column(
        "extraction_chars",
        "integer",
        "characters of extracted text, empty when never extracted",
    ),
    _Column(
        "extraction_version",
        "integer",
        "generation of the extraction routine, may be empty",
    ),
    _Column(
        "pipeline_version",
        "string",
        "app version that last processed the message, may be empty",
    ),
    _Column(
        "label",
        "string",
        "detector verdict `Human`, `Mixed` or `AI`; empty when unscored",
        {"enum": list(LABELS)},
    ),
    _Column(
        "fraction_ai",
        "number",
        "detector fraction of fully AI text, in [0, 1]; empty when unscored",
        _FRACTION_CONSTRAINTS,
    ),
    _Column(
        "fraction_ai_assisted",
        "number",
        "detector fraction of AI-assisted text, in [0, 1]; empty when unscored",
        _FRACTION_CONSTRAINTS,
    ),
    _Column(
        "fraction_human",
        "number",
        "detector fraction of human text, in [0, 1]; empty when unscored",
        _FRACTION_CONSTRAINTS,
    ),
    _Column(
        "detector_version",
        "string",
        "the detector build that produced the score, empty when unscored",
    ),
    _Column(
        "scored_at", "datetime", "when the score was written, UTC ISO-8601; empty when unscored"
    ),
)

#: ``lists.csv`` columns in file order.
_LIST_COLUMNS = (
    _Column("list", "string", f"as in `{MESSAGES_MEMBER}`"),
    _Column("folder", "string", f"as in `{MESSAGES_MEMBER}`"),
    _Column("messages", "integer", "messages in scope"),
    _Column("scored", "integer", "messages with a score"),
    _Column(
        "too_short",
        "integer",
        "messages gated under the reliability floor (`extraction_status = too_short`)",
    ),
    _Column("human", "integer", "scored messages labelled `Human`"),
    _Column("mixed", "integer", "scored messages labelled `Mixed`"),
    _Column("ai", "integer", "scored messages labelled `AI`"),
    _Column(
        "ai_share",
        "number",
        "`ai / (scored + too_short)`, empty when that denominator is 0",
        _FRACTION_CONSTRAINTS,
    ),
    _Column("first_date", "datetime", "oldest `date` in scope, empty when none"),
    _Column("last_date", "datetime", "newest `date` in scope, empty when none"),
)

#: ``senders.csv`` columns in file order: the sender grouping, and nothing else.
_SENDER_COLUMNS = (
    _Column("sender_key", "string", "synthetic key `s1`, `s2`, … identifying one sender"),
    _Column(
        "email",
        "string",
        f"one address belonging to that sender; joins to `{MESSAGES_MEMBER}`",
    ),
)

#: The join chain every query in this module walks: a message, its extraction if
#: it has one, and that extraction's score if it has one. Both joins are on
#: UNIQUE columns, so neither multiplies rows and ``COUNT(*)`` counts messages.
_MESSAGE_JOINS = (
    " FROM messages m "
    "LEFT JOIN extractions e ON e.message_id = m.id "
    "LEFT JOIN scores sc ON sc.extraction_id = e.id"
)

#: The aggregate expressions behind ``lists.csv``, counted over the same scope as
#: the message pass so the two tables agree. The label literals are the constants
#: in :data:`LABELS` (no user input), so inlining them is safe.
_AGGREGATE_COLUMNS = (
    "COUNT(*) AS messages, "
    "COUNT(sc.id) AS scored, "
    "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short, "
    "COUNT(CASE WHEN sc.label = 'Human' THEN 1 END) AS human, "
    "COUNT(CASE WHEN sc.label = 'Mixed' THEN 1 END) AS mixed, "
    "COUNT(CASE WHEN sc.label = 'AI' THEN 1 END) AS ai, "
    "MIN(m.date) AS first_date, MAX(m.date) AS last_date"
)

#: The per-message columns, named so the streaming pass reads them by name.
_MESSAGE_SELECT = (
    "SELECT m.message_id AS message_id, m.date AS date, "
    "m.in_reply_to AS in_reply_to, m.address_id AS address_id, m.from_name AS from_name, "
    "m.auto_generated AS auto_generated, m.timing AS timing, m.timing_cpm AS timing_cpm, "
    "m.pipeline_version AS pipeline_version, "
    "e.status AS extraction_status, e.method AS extraction_method, "
    "e.char_count AS extraction_chars, e.extraction_version AS extraction_version, "
    "sc.id AS score_id, sc.label AS label, sc.fraction_ai AS fraction_ai, "
    "sc.fraction_ai_assisted AS fraction_ai_assisted, sc.fraction_human AS fraction_human, "
    "sc.detector_version AS detector_version, sc.scored_at AS scored_at"
)


@dataclass(frozen=True)
class StatsExportSummary:
    """Tally of one :func:`export_stats` run.

    ``senders`` counts the rows of ``senders.csv`` — the addresses with a message
    in scope, not the distinct senders they group into.
    """

    lists: int
    senders: int
    messages: int
    scored: int
    path: str

    def as_line(self) -> str:
        return (
            f"lists={self.lists} senders={self.senders} messages={self.messages} "
            f"scored={self.scored} path={self.path}"
        )


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def zip_path(path: str | Path) -> Path:
    """Return ``path`` with :data:`ZIP_SUFFIX` appended, idempotently.

    A path already ending ``.zip`` is returned unchanged, so a caller that passes
    ``stats.zip`` does not end up with ``stats.zip.zip``.
    """
    p = Path(path)
    if p.name.endswith(ZIP_SUFFIX):
        return p
    return p.with_name(p.name + ZIP_SUFFIX)


#: Text fields a spreadsheet may evaluate as a formula: a leading ``=``, ``+``,
#: ``-``, ``@``, tab or carriage return (the OWASP CSV-injection trigger set),
#: here optionally preceded by ``'`` characters. Most of these values come from
#: mail headers (``From`` names, addresses, Message-IDs) or from an imported
#: export file, so their content is chosen by whoever wrote the mail.
#:
#: Including the leading-quote prefix makes the transformation exactly
#: reversible: every escaped field matches ``^'+`` followed by a trigger
#: character, and no unescaped field does, so removing one ``'`` from each
#: field that matches recovers the stored value. A value that merely begins
#: with ``'`` (some ``From`` names are written ``'Name'``) is not altered.
_FORMULA_TRIGGER = re.compile(r"'*[=+\-@\t\r]")

#: The character prepended to a field matching :data:`_FORMULA_TRIGGER`. A
#: spreadsheet reads a leading ``'`` as "treat as text" and does not evaluate it.
_FORMULA_ESCAPE = "'"


def _neutralise_formula(value: str) -> str:
    """Prefix ``value`` with :data:`_FORMULA_ESCAPE` when it matches :data:`_FORMULA_TRIGGER`.

    Applied to every text field of every CSV member, so both sides of a join
    (``messages.csv`` ``email`` to ``senders.csv``, ``folder`` to ``lists.csv``)
    receive the same transformation and the keys still match. An email address
    that begins with ``+`` or ``-`` (valid in RFC 5322) is escaped like any other
    text, because a spreadsheet evaluates it the same way.
    """
    if _FORMULA_TRIGGER.match(value):
        return _FORMULA_ESCAPE + value
    return value


def _cell(value: Any) -> Any:
    """One CSV field: NULL as an empty field, a boolean as ``true`` / ``false``.

    A text value is passed through :func:`_neutralise_formula`. Numbers are
    handed to :mod:`csv` unchanged, which renders a float at full stored
    precision (``repr``) rather than rounding it; a negative number is a number,
    not text, and is never escaped.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _neutralise_formula(value)
    return value


def _share(ai: int, scored: int, too_short: int) -> float | str:
    """The ``ai_share`` field of one aggregate row, empty when it has no meaning.

    The dashboard's definition (:func:`~.store.ai_share`): the AI verdicts over
    every message the mix bar draws, the scored ones plus those gated under the
    reliability floor. A zero denominator is written as an empty field rather
    than the 0.0 the dashboard displays, because a share of nothing is not zero
    and an empty field cannot be averaged into one by mistake.
    """
    denominator = scored + too_short
    if denominator == 0:
        return ""
    return ai / denominator


def _aggregate_row(row: Any) -> dict[str, Any]:
    """The aggregate columns of one ``GROUP BY`` row, as a dict."""
    return {
        "messages": row["messages"],
        "scored": row["scored"],
        "too_short": row["too_short"],
        "human": row["human"],
        "mixed": row["mixed"],
        "ai": row["ai"],
        "first_date": row["first_date"],
        "last_date": row["last_date"],
    }


def _empty_aggregate() -> dict[str, Any]:
    """A zeroed aggregate, for a named list the date range leaves no message."""
    return {
        "messages": 0,
        "scored": 0,
        "too_short": 0,
        "human": 0,
        "mixed": 0,
        "ai": 0,
        "first_date": None,
        "last_date": None,
    }


class _CsvMember:
    """One CSV member of the archive, written row by row.

    Wraps the binary stream :meth:`zipfile.ZipFile.open` returns in a UTF-8 text
    layer and a :mod:`csv` writer with ``\\n`` line endings, so a caller writes
    dicts keyed by column name and never sees the encoding. The header row is the
    member's :class:`_Column` definitions in order, the same order the
    descriptor's Table Schema declares.
    """

    def __init__(self, archive: zipfile.ZipFile, name: str, columns: Sequence[_Column]) -> None:
        self.columns = [column.name for column in columns]
        self._raw = archive.open(name, "w")
        self._text = io.TextIOWrapper(self._raw, encoding="utf-8", newline="")
        self._writer = csv.writer(self._text, lineterminator="\n")
        self._writer.writerow(self.columns)

    def write(self, values: dict[str, Any]) -> None:
        self._writer.writerow([_cell(values.get(column)) for column in self.columns])

    def close(self) -> None:
        # Closing the text layer flushes it and closes the member; the member's
        # own close is idempotent, so closing it again is harmless.
        self._text.close()
        self._raw.close()

    def __enter__(self) -> "_CsvMember":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --- Data Package descriptor ----------------------------------------------------


def _resource(
    name: str,
    member: str,
    columns: Sequence[_Column],
    primary_key: Sequence[str],
    foreign_keys: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """One CSV member as a Data Package resource with a full Table Schema.

    The fields are the member's :class:`_Column` definitions in file order, so the
    declared schema and the header row cannot disagree. ``primary_key`` states the
    member's uniqueness fact and ``foreign_keys`` its joins.
    """
    schema: dict[str, Any] = {
        "fields": [column.as_field() for column in columns],
        "primaryKey": list(primary_key),
    }
    if foreign_keys:
        schema["foreignKeys"] = [dict(key) for key in foreign_keys]
    return {
        "name": name,
        "path": member,
        "format": "csv",
        "mediatype": "text/csv",
        "encoding": "utf-8",
        "schema": schema,
    }


def _resources() -> list[dict[str, Any]]:
    """The three CSV members as resources, in the order they are written.

    ``messages.csv``'s two foreign keys are the joins the format promises: its
    ``folder`` names a row of ``lists.csv`` and its ``email`` a row of
    ``senders.csv``.
    """
    return [
        _resource(
            MESSAGES_RESOURCE,
            MESSAGES_MEMBER,
            _MESSAGE_COLUMNS,
            ["folder", "message_id"],
            [
                {
                    "fields": ["folder"],
                    "reference": {"resource": LISTS_RESOURCE, "fields": ["folder"]},
                },
                {
                    "fields": ["email"],
                    "reference": {"resource": SENDERS_RESOURCE, "fields": ["email"]},
                },
            ],
        ),
        _resource(LISTS_RESOURCE, LISTS_MEMBER, _LIST_COLUMNS, ["folder"]),
        _resource(SENDERS_RESOURCE, SENDERS_MEMBER, _SENDER_COLUMNS, ["email"]),
    ]


def _datapackage(
    *,
    schema_version: int,
    folders: Sequence[str],
    date_from: str | None,
    date_to: str | None,
    rows: dict[str, int],
    detector_versions: Sequence[str],
    extraction_versions: Sequence[int],
) -> dict[str, Any]:
    """The archive's ``datapackage.json``: a Frictionless Data Package descriptor.

    Standard properties carry what the standard can say — each CSV is a resource
    with a Table Schema typing every column, ``primaryKey`` states its uniqueness
    fact and ``foreignKeys`` its joins — so the unzipped archive validates under
    ``frictionless validate datapackage.json`` and a data-package reader loads the
    files with the right types. App-specific provenance that has no standard home
    lives under the custom ``mlac`` property, which the standard permits: this
    format's own version, the app and schema versions behind the file, the
    selected folders and date range, the row counts, and the distinct detector and
    extraction versions the rows actually carry.
    """
    return {
        "$schema": DATAPACKAGE_PROFILE,
        "name": STATS_FORMAT_NAME,
        "title": PACKAGE_TITLE,
        "description": PACKAGE_DESCRIPTION,
        "created": _utcnow_iso(),
        "mlac": {
            "stats_format_version": STATS_FORMAT_VERSION,
            "app_version": __version__,
            "schema_version": schema_version,
            "folders": list(folders),
            # Provenance for a partial export, so a file can say which messages
            # it was asked for rather than only which it holds. Absent (not
            # null) when the bound was not given.
            **({"date_from": date_from} if date_from else {}),
            **({"date_to": date_to} if date_to else {}),
            "rows": dict(rows),
            "detector_versions": list(detector_versions),
            "extraction_versions": list(extraction_versions),
        },
        "resources": _resources(),
    }


# --- Export -------------------------------------------------------------------


def export_stats(
    store: Store,
    list_names: Sequence[str] | None,
    out_path: str | Path,
    *,
    all_lists: bool = False,
    date_from: str | None = None,
    date_to: str | None = None,
) -> StatsExportSummary:
    """Export the scores and metadata of the selected messages as a CSV zip archive.

    Selection is identical to :func:`~.export_import.export_lists`, with which it
    shares :func:`~.export_import._select_lists`: lists by ``lists.name`` or,
    with ``all_lists=True``, every list with a message in scope; passing both —
    or neither — is a :class:`ValueError`, as is an unknown name. ``date_from`` /
    ``date_to`` bound the messages by ``messages.date``, inclusively at both ends
    and independently, by the same lexical comparison the dashboard's date filter
    applies, including its one sharp edge: a bare ``date_to`` day excludes that
    day's messages, whose stored value carries a time. A named list is exported
    whether or not the range leaves it any message.

    Every message in scope is exported whether or not it was scored, named by its
    Message-ID and its sender's address. ``senders.csv`` carries the sender
    grouping those addresses roll up to, keyed ``s1``, ``s2``, … in the order the
    addresses are first seen; the keys are file-scoped and mean nothing to the
    app.

    :data:`ZIP_SUFFIX` is appended to ``out_path`` unless it is already there;
    the returned summary's ``path`` is the path actually written, so a caller
    that passed ``stats`` can report the ``stats.zip`` it got.

    Purely a local database read: no IMAP, no Pangram, no caps involved.
    """
    conn = store.conn
    selected = _select_lists(
        conn, list_names, all_lists=all_lists, date_from=date_from, date_to=date_to
    )
    range_clause = _range_clause("m.date", date_from, date_to)
    range_params = _range_params(date_from, date_to)
    # List ids are integers read from the database, never user input, so they are
    # inlined rather than bound: the count of selected lists is unbounded and
    # SQLite's bound-parameter limit is not.
    scope = f" WHERE m.list_id IN ({','.join(str(int(lst['id'])) for lst in selected)})"

    # Pre-pass. One cross-reference has to be resolvable before a message row can
    # be written: the address it was sent from, both for the ``email`` and
    # ``sender_name`` columns and for the sender the address rolls up to, since a
    # person groups several addresses and their shared key is an ordinal over
    # first sightings. Only the addresses are collected — a message contributes
    # nothing — so what is held scales with the addresses in scope rather than
    # with the messages or their size.
    addresses: dict[int, Any] = {}  # address id -> addresses row, in first-seen order
    sender_key_of_address: dict[int, str] = {}  # address id -> senders.csv key
    sender_keys: dict[str, str] = {}  # grouping id -> "s<n>"

    def register_address(address_id: int) -> None:
        """Record one address and the sender it belongs to, on first sight."""
        if address_id in sender_key_of_address:
            return
        row = conn.execute(
            "SELECT id, email, display_name, person_id FROM addresses WHERE id = ?",
            (address_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - address_id came from a live FK
            sender_key_of_address[address_id] = ""
            return
        addresses[address_id] = row
        person_id = row["person_id"]
        # A linked person is one sender across its addresses; an unlinked address
        # is its own sender — the grouping the dashboard's Senders pane applies.
        grouping = f"p{person_id}" if person_id is not None else f"a{address_id}"
        key = sender_keys.get(grouping)
        if key is None:
            key = f"s{len(sender_keys) + 1}"
            sender_keys[grouping] = key
        sender_key_of_address[address_id] = key

    for lst in selected:
        for row in conn.execute(
            "SELECT m.address_id AS address_id FROM messages m "
            f"WHERE m.list_id = ?{range_clause} AND m.address_id IS NOT NULL ORDER BY m.id",
            [lst["id"], *range_params],
        ):
            register_address(row["address_id"])

    n_messages = 0
    n_scored = 0
    detector_versions: set[str] = set()
    extraction_versions: set[int] = set()

    written_path = zip_path(out_path)
    with zipfile.ZipFile(written_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        # The streaming pass: one message row is live at a time, serialised into
        # the open member before the next is read.
        with _CsvMember(archive, MESSAGES_MEMBER, _MESSAGE_COLUMNS) as member:
            for lst in selected:
                for row in conn.execute(
                    f"{_MESSAGE_SELECT}{_MESSAGE_JOINS} WHERE m.list_id = ?{range_clause} "
                    "ORDER BY m.id",
                    [lst["id"], *range_params],
                ):
                    n_messages += 1
                    if row["score_id"] is not None:
                        n_scored += 1
                    if row["detector_version"] is not None:
                        detector_versions.add(row["detector_version"])
                    if row["extraction_version"] is not None:
                        extraction_versions.add(row["extraction_version"])

                    address = addresses.get(row["address_id"]) if row["address_id"] else None
                    member.write(
                        {
                            "message_id": row["message_id"],
                            "list": lst["name"],
                            "folder": lst["folder"],
                            "date": row["date"],
                            "email": address["email"] if address else "",
                            # The message's own From name, falling back to the
                            # display name stored for its address.
                            "sender_name": row["from_name"]
                            or (address["display_name"] if address else None),
                            "in_reply_to": row["in_reply_to"],
                            "auto_generated": row["auto_generated"],
                            "timing": row["timing"],
                            "timing_cpm": row["timing_cpm"],
                            "extraction_status": row["extraction_status"],
                            "extraction_method": row["extraction_method"],
                            "extraction_chars": row["extraction_chars"],
                            "extraction_version": row["extraction_version"],
                            "pipeline_version": row["pipeline_version"],
                            "label": row["label"],
                            "fraction_ai": row["fraction_ai"],
                            "fraction_ai_assisted": row["fraction_ai_assisted"],
                            "fraction_human": row["fraction_human"],
                            "detector_version": row["detector_version"],
                            "scored_at": row["scored_at"],
                        }
                    )

        # The aggregates: one GROUP BY over the identical scope, so their counts
        # sum to the rows just written.
        list_aggregates = {
            row["list_id"]: _aggregate_row(row)
            for row in conn.execute(
                f"SELECT m.list_id AS list_id, {_AGGREGATE_COLUMNS}{_MESSAGE_JOINS}"
                f"{scope}{range_clause} GROUP BY m.list_id",
                range_params,
            )
        }
        with _CsvMember(archive, LISTS_MEMBER, _LIST_COLUMNS) as member:
            for lst in selected:
                # A named list with nothing in range is exported as a zero row:
                # the file says the list was asked for and held no message.
                counts = list_aggregates.get(lst["id"]) or _empty_aggregate()
                member.write(
                    {
                        "list": lst["name"],
                        "folder": lst["folder"],
                        **counts,
                        "ai_share": _share(counts["ai"], counts["scored"], counts["too_short"]),
                    }
                )

        # The sender grouping: one row per address with a message in scope, in the
        # order the pre-pass first saw them, so a person's addresses repeat their
        # shared key and an unlinked address holds one of its own.
        with _CsvMember(archive, SENDERS_MEMBER, _SENDER_COLUMNS) as member:
            for address_id, address in addresses.items():
                member.write(
                    {
                        "sender_key": sender_key_of_address[address_id],
                        "email": address["email"],
                    }
                )

        schema_row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        descriptor = _datapackage(
            schema_version=schema_row["v"] if schema_row and schema_row["v"] is not None else 0,
            folders=[lst["folder"] for lst in selected],
            date_from=date_from,
            date_to=date_to,
            rows={
                "messages": n_messages,
                "lists": len(selected),
                # senders.csv rows: addresses, not the senders they group into.
                "senders": len(addresses),
            },
            detector_versions=sorted(detector_versions),
            extraction_versions=sorted(extraction_versions),
        )
        archive.writestr(
            DATAPACKAGE_MEMBER, json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n"
        )
        archive.writestr(README_MEMBER, _readme())

    return StatsExportSummary(
        lists=len(selected),
        senders=len(addresses),
        messages=n_messages,
        scored=n_scored,
        path=str(written_path),
    )


# --- Data dictionary ------------------------------------------------------------


def _table(columns: Iterable[_Column]) -> str:
    """A Markdown table of the column dictionary: name, declared type, meaning.

    Generated from the same :class:`_Column` definitions the descriptor's Table
    Schema is, so the prose dictionary and the machine-readable schema describe
    every column identically.
    """
    lines = ["| column | type | meaning |", "|---|---|---|"]
    lines += [f"| `{c.name}` | {c.type} | {c.description} |" for c in columns]
    return "\n".join(lines)


def _readme() -> str:
    """The archive's ``README.md``: the data dictionary for this exact file.

    Written for someone who has only the zip, so it describes every column of
    every member and the caveats that stop the obvious misreadings.
    """
    bands = f"{int(TIMING_SUSPICIOUS_CPM)}, `implausible` from {int(TIMING_IMPLAUSIBLE_CPM)}"

    return f"""# Mailing-list AI check — statistics export

One row per message, plus a per-list aggregate and the sender grouping, from an
AI-detection pipeline run over one or more mailing lists. The archive carries no
message content: no bodies, no extracted text, no subjects, no raw headers and
no detector responses. Nothing here is read back by the application that wrote
it; this is an analysis artifact.

## Members

| member | contents |
|---|---|
| `{MESSAGES_MEMBER}` | one row per message in scope, scored or not |
| `{LISTS_MEMBER}` | one row per exported list, aggregated over the same scope |
| `{SENDERS_MEMBER}` | the sender grouping: synthetic key to email address |
| `{DATAPACKAGE_MEMBER}` | a Frictionless Data Package descriptor: a schema per CSV, plus this file's provenance |
| `{README_MEMBER}` | this file |

The CSV files are UTF-8, RFC 4180, with a header row and `\\n` line endings. A
NULL is an empty field, booleans are `true` / `false`, dates are the stored UTC
ISO-8601 strings, and fractions are written at full stored precision, unrounded.

A text field that begins with `=`, `+`, `-`, `@`, a tab or a carriage return,
optionally after one or more `'` characters, is written with one `'` prepended,
so that a spreadsheet does not evaluate it as a formula. Most text values come
from mail headers written by the senders. The rule applies to every text field
of every CSV member, email addresses included, so the `email` and `folder` joins
still match; numeric fields are never altered. To recover the stored value,
remove the first character of each field that matches the regular expression
`^'+[=+\\-@\\t\\r]`; no other field is altered. Values in `{DATAPACKAGE_MEMBER}`
are not altered.

Rows are named by mail's own values: a message by its `message_id`, a sender by
its `email`. None of the application's internal row ids appear. The export is
not anonymised: sender addresses and names are present, as are the real header
values.

## `{DATAPACKAGE_MEMBER}`

`{DATAPACKAGE_MEMBER}` is a standard [Frictionless Data
Package](https://datapackage.org/) descriptor, so this archive unzips into a
valid data package. Each CSV is a resource with a schema typing every column and
stating the keys: after unzipping, `frictionless validate {DATAPACKAGE_MEMBER}`
checks the files against those schemas, and a reader that understands data
packages loads them with the right types instead of hand-written parsing.

This file's own provenance — the format version, the versions of the application
and its database schema, the lists and date range selected, the row counts, and
the detector and extraction versions the rows carry — is under the descriptor's
`mlac` key, where the standard permits application-specific properties.

## `{MESSAGES_MEMBER}`

Every message in scope, whether or not it was scored: a share calculation needs
the messages that carry no verdict as much as those that do.

{_table(_MESSAGE_COLUMNS)}

## `{LISTS_MEMBER}`

One row per exported list. The counts are over the messages in this file, so
they sum exactly to `{MESSAGES_MEMBER}`.

{_table(_LIST_COLUMNS)}

## `{SENDERS_MEMBER}`

The sender grouping, and nothing else: one row per address that appears in
`{MESSAGES_MEMBER}`. Addresses the application has linked to one person share a
`sender_key`, one row each; an unlinked address is its own sender with a single
row. A message with no sender address at all has an empty `email` and no row
here: it is counted in the list aggregate, and under no sender.

The keys are assigned in the order the addresses are first seen and hold for
this file alone — they are not comparable with any other export. Per-sender
aggregates are not shipped: `{MESSAGES_MEMBER}` carries `email` on every row, so
grouping by sender is one join away.

{_table(_SENDER_COLUMNS)}

## Reading the numbers

- `label` is the detector's own verdict, stored verbatim; the application
  derives nothing from it.
- A share must include the too-short messages in its denominator to match the
  application's own figures. The reliability floor gates messages under 50 words
  of authored text: they are never sent to the detector, so they are neither
  human nor AI, but they are messages.
- `message_id` is not a unique key: a message cross-posted to several exported
  lists appears once per list, with the same `message_id` and a different
  `folder`. `(folder, message_id)` is unique.
- Threads join `in_reply_to` to `message_id`. Most resolve directly; a small
  minority of `In-Reply-To` headers carry extra tokens — surrounding whitespace,
  a trailing comment, more than one Message-ID — and need normalising to the
  first bracketed Message-ID before the join.
- Scores in one file may come from different detector versions and different
  extraction generations. Both are per-row columns, and the distinct values this
  file carries are listed under `mlac` in `{DATAPACKAGE_MEMBER}`; an aggregate
  over a mixed file mixes them.
- A reply's timing band is the implied composition rate of its new text, in
  characters per minute of the gap between the parent message and the reply:
  `suspicious` from {bands}.
  An empty band means the rate could not be computed — the message is not a
  reply, its parent is not stored, a date is missing or unusable, or the message
  has no extracted text — not that it was normal.
- The date range, when one was applied, is recorded under `mlac` in
  `{DATAPACKAGE_MEMBER}`. Its
  comparison is lexical over the stored dates, so a bare `date_to` day
  ("2026-03-01") excludes that day's messages, whose stored value carries a
  time.
"""
