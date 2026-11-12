"""SQLite storage layer for the mail pipeline.

Everything downstream (fetch, extraction, scoring, API) reads and writes through
:class:`Store`. The design is deliberately plain ``sqlite3`` rather than an ORM
(see the module docstring rationale below).

Why ``sqlite3`` and not SQLAlchemy
----------------------------------
This project is a single-writer CLI pipeline feeding a read-mostly Flask API,
against one local SQLite file. The schema is small, fixed, and hand-tuned for a
few known access paths (dedupe upserts, a hash-keyed score cache, dashboard
filter indexes). An ORM's value — database portability, unit-of-work session
management, lazy relationship graphs, cross-backend migrations — buys us little
here, while adding a dependency, a mapping layer, and indirection over the exact
SQL (``INSERT ... ON CONFLICT``, ``PRAGMA`` tuning, partial-index choices) that
this layer's correctness depends on. The stdlib ``sqlite3`` module keeps the
dependency set tiny (a stated project goal), makes every query auditable, and is
more than adequate for the volumes involved. Typed :mod:`dataclasses` give us
the ergonomic "rows as objects" benefit without the ORM machinery.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Container, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from email_reply_extractor import EXTRACTION_VERSION

log = logging.getLogger(__name__)

# --- Extraction status values -------------------------------------------------

#: Allowed values for ``extractions.status``.
EXTRACTION_STATUSES = ("ok", "empty", "too_short", "failed")

# --- Reply-timing analysis ------------------------------------------------------

#: Allowed values for ``messages.timing`` (NULL means "not computable").
TIMING_VALUES = ("implausible", "suspicious", "normal")
#: Implied composition rate (extracted chars per minute of reply gap) at or
#: above which a reply is classified "implausible": faster than even a fast
#: writer composes original prose, so the text cannot have been written within
#: the window between the parent message and the reply.
TIMING_IMPLAUSIBLE_CPM = 250.0
#: Rate at or above which a reply is classified "suspicious": possible for a
#: fast writer but in the top few percent of observed rates.
TIMING_SUSPICIOUS_CPM = 100.0
#: Extraction statuses whose text counts as authored new text for the timing
#: analysis. "empty" has no new text to time; "failed" text is unreliable.
_TIMING_STATUSES = ("ok", "too_short")

# --- Dashboard query constants ------------------------------------------------

#: Pangram ``prediction_short`` labels that count as "flagged" for the dashboard.
#: Only fully AI verdicts are flagged; "Human"/"Mixed" are not (assisted or
#: partial AI content arrives as "Mixed" — see ``fraction_ai_assisted``).
FLAGGED_LABELS = ("AI",)
#: Pre-built SQL ``IN`` list of the flagged labels. Values are trusted constants
#: defined here (no user input), so inlining them is safe from injection.
_FLAGGED_IN = "(" + ", ".join(f"'{label}'" for label in FLAGGED_LABELS) + ")"

#: Columns a message list may be sorted by, mapped to their SQL expression.
#: The request's direction is appended to the expression, so an entry may hold
#: several comma-separated terms and pin the direction of every term but the
#: last. ``timing_cpm`` uses that to lead with ``IS NULL ASC``: most rows have
#: no stored rate (only replies whose parent is on the list are classified),
#: and the leading term keeps them together at the bottom in both directions
#: instead of filling the first page under ``asc``.
SORT_COLUMNS = {
    "date": "m.date",
    "fraction_ai": "s.fraction_ai",
    "timing_cpm": "m.timing_cpm IS NULL ASC, m.timing_cpm",
}
#: Default and maximum page sizes for :meth:`Store.query_messages`.
DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200

#: How many of a sender's lists reply rugs are computed for, most-posted first.
#: Matches the ``by_list`` cap in :meth:`Store.summary`, whose rows they decorate.
REPLY_RUG_MAX_LISTS = 20
#: The prediction buckets a timeline point may carry, by index (see
#: :meth:`Store.list_timelines`). The first three are the ``prediction_short``
#: vocabulary; ``too_short`` marks a message gated under the reliability floor
#: and ``unscored`` everything else without a label.
TIMELINE_BUCKETS = ("Human", "Mixed", "AI", "too_short", "unscored")
_TIMELINE_LABEL_BUCKETS = {"Human": 0, "Mixed": 1, "AI": 2}
#: Batch size for ``IN (...)`` lookups over an unbounded id/Message-ID set.
#: SQLite's bound-parameter limit is 999 on older builds; stay well under it.
_IN_CHUNK = 400


# --- Settings keys ------------------------------------------------------------

#: Settings key holding the Pangram detector selector every scoring run sends
#: (see :data:`mailing_list_ai_check.pangram.MODEL_GENERATIONS` for the accepted
#: values). Absent means the client default, Pangram 4.
SETTING_PANGRAM_MODEL = "pangram_model"
#: Settings key holding the state of the dashboard's Pangram-upgrade notice:
#: "pending", "later" or "dismissed". Absent means the state has never been set
#: and the reader resolves it from the stored scores.
SETTING_PANGRAM_NOTICE = "pangram_upgrade_notice"
#: ``app_settings`` key holding the highest ``messages.id`` whose arrival has
#: been checked against already-extracted replies (see
#: :func:`mailing_list_ai_check.staleness.refresh_for_new_parents`). Written by
#: the pipeline, not chosen by a user.
SETTING_PARENT_CHECK_MESSAGE_ID = "parent_check_message_id"


# --- Schema migrations --------------------------------------------------------

_MIGRATION_001 = """
CREATE TABLE lists (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    folder         TEXT NOT NULL UNIQUE,
    last_synced_at TEXT
);

CREATE TABLE pull_state (
    list_id     INTEGER PRIMARY KEY REFERENCES lists(id) ON DELETE CASCADE,
    uidvalidity INTEGER NOT NULL,
    last_uid    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE persons (
    id             INTEGER PRIMARY KEY,
    canonical_name TEXT NOT NULL
);

CREATE TABLE addresses (
    id           INTEGER PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    display_name TEXT,
    person_id    INTEGER REFERENCES persons(id) ON DELETE SET NULL
);

CREATE INDEX idx_addresses_person_id ON addresses(person_id);
CREATE INDEX idx_addresses_display_name ON addresses(display_name);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    message_id  TEXT NOT NULL,          -- RFC 5322 Message-ID
    list_id     INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    address_id  INTEGER REFERENCES addresses(id) ON DELETE SET NULL,
    subject     TEXT,
    date        TEXT,                   -- UTC ISO-8601
    in_reply_to TEXT,
    raw_body    TEXT,                   -- text/plain body as fetched
    uid         INTEGER,                -- IMAP UID within the folder
    fetched_at  TEXT NOT NULL,
    UNIQUE(list_id, message_id)
);

CREATE INDEX idx_messages_date ON messages(date);
CREATE INDEX idx_messages_address_id ON messages(address_id);
CREATE INDEX idx_messages_list_id ON messages(list_id);

CREATE TABLE extractions (
    id             INTEGER PRIMARY KEY,
    message_id     INTEGER NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE,
    extracted_text TEXT NOT NULL,
    method         TEXT NOT NULL,
    char_count     INTEGER NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('ok', 'empty', 'too_short', 'failed')),
    created_at     TEXT NOT NULL
);

CREATE INDEX idx_extractions_status ON extractions(status);

CREATE TABLE scores (
    id                   INTEGER PRIMARY KEY,
    extraction_id        INTEGER NOT NULL UNIQUE REFERENCES extractions(id) ON DELETE CASCADE,
    fraction_ai          REAL,
    fraction_ai_assisted REAL,
    fraction_human       REAL,
    label                TEXT,
    detector_version     TEXT,
    raw_response         TEXT,          -- full Pangram JSON response
    text_sha256          TEXT NOT NULL,
    scored_at            TEXT NOT NULL
);

CREATE INDEX idx_scores_text_sha256 ON scores(text_sha256);
CREATE INDEX idx_scores_label ON scores(label);
"""

# NULL while the list exists on the IMAP server; stamped with the refresh time
# when a lists-index refresh finds it gone but local messages still reference it
# (server-deleted lists without messages are removed outright).
_MIGRATION_002 = """
ALTER TABLE lists ADD COLUMN removed_from_server_at TEXT;
"""

#: Ordered ``(version, sql)`` migrations. Append new ones; never edit applied.

# Backfill for PangramResult.label: Pangram's prediction_short never emits
# "AI-Assisted" — assisted-dominated text arrives as "Mixed". Rebadge stored
# rows the same way new scores are labeled so the dashboard's AI-Assisted band
# reflects the fractions. raw_response keeps the original prediction_short.
_MIGRATION_003 = """
UPDATE scores SET label = 'AI-Assisted'
WHERE label = 'Mixed'
  AND fraction_ai_assisted > COALESCE(fraction_ai, 0.0)
  AND fraction_ai_assisted > COALESCE(fraction_human, 0.0);
"""

# The decoded ``text/html`` part, captured alongside ``raw_body`` from Phase 8
# onward so the HTML structure can serve as an extraction oracle (see
# :mod:`email_reply_extractor.html_text`). NULL for rows fetched before this column
# existed and backfilled by the ``--backfill-html`` pull mode.
_MIGRATION_004 = """
ALTER TABLE messages ADD COLUMN raw_html TEXT;
"""

# ISO-8601 UTC timestamp of the newest message the IMAP server holds for the
# folder, or NULL when the list has never been checked against the server. Only
# tracked lists (those with local messages) are ever checked, so index-only
# lists keep NULL here — the server is never EXAMINEd for them.
_MIGRATION_005 = """
ALTER TABLE lists ADD COLUMN last_message_at TEXT;
"""

# get_parent_body and thread lookups filter by bare message_id;
# UNIQUE(list_id, message_id) cannot serve that predicate, so give
# message_id its own index.
_MIGRATION_006 = """
CREATE INDEX idx_messages_message_id ON messages(message_id);
"""

# The app version (semantic, e.g. "1.0.0") that last ran a pipeline stage
# end-to-end against the message: stamped on insert and re-stamped whenever the
# message's extraction or score is (re)written. NULL for legacy rows fetched
# before this column existed, which sort older than every real version.
_MIGRATION_007 = """
ALTER TABLE messages ADD COLUMN pipeline_version TEXT;
"""

# The app version that produced this extraction's text: stamped on insert and
# rewritten whenever the row is re-extracted, and never touched by scoring. It
# exists because ``messages.pipeline_version`` cannot answer "which version
# derived this text" — scoring re-stamps that column too, so a message extracted
# under one version and scored under a later one reads as the later version.
#
# The backfill uses the message stamp, which is an upper bound on the extraction
# version: a message extracted before an extraction change and scored after it
# backfills as if it were current. That is why the version stamp only decides
# whether to *offer* the check (see :mod:`mailing_list_ai_check.staleness`) —
# the check itself re-derives every extraction and compares the text.
_MIGRATION_008 = """
ALTER TABLE extractions ADD COLUMN pipeline_version TEXT;

UPDATE extractions SET pipeline_version = (
    SELECT m.pipeline_version FROM messages m WHERE m.id = extractions.message_id
);
"""

# Reply-timing classification: how plausibly the reply's new text could have
# been composed within the gap since its parent message. Values are
# 'implausible' (>= TIMING_IMPLAUSIBLE_CPM chars/min), 'suspicious'
# (>= TIMING_SUSPICIOUS_CPM) or 'normal', and NULL when the rate cannot be
# computed (not a reply, parent not stored, missing dates, non-positive gap,
# or no extraction with authored text). Backfilled in Python by
# :meth:`Store.__init__` right after this migration applies — the parent
# lookup normalizes In-Reply-To the way :func:`_parent_message_id` does,
# which plain SQL cannot.
_MIGRATION_009 = """
ALTER TABLE messages ADD COLUMN timing TEXT;

CREATE INDEX idx_messages_timing ON messages(timing);
"""

# The chars/minute rate behind the ``timing`` band: the value
# :func:`chars_per_minute` returns for the same two inputs, stored rather than
# recomputed so the dashboard can filter on a numeric range in SQL — a filter
# applied after the fact, to one page of rows, would not agree with the match
# count or the pagination. NULL exactly where ``timing`` is; the two columns
# are always written together by :meth:`Store.recompute_timing`. Backfilled by
# the same Python recompute that backfilled ``timing`` in migration 009, run by
# :meth:`Store.__init__` right after this migration applies.
_MIGRATION_010 = """
ALTER TABLE messages ADD COLUMN timing_cpm REAL;

CREATE INDEX idx_messages_timing_cpm ON messages(timing_cpm);
"""

# The generation of the routine that derived this extraction's text
# (:data:`email_reply_extractor.extraction.EXTRACTION_VERSION`), stamped on
# insert and on re-extraction. It replaces ``pipeline_version`` as the input to
# the staleness check, which frees the app's semantic version from having to
# encode extraction changes; ``pipeline_version`` stays as provenance ("which
# release wrote this row").
#
# The backfill maps the existing semver stamps onto the two generations that
# have existed: 1.2.0 introduced the localized quote-header and custom
# signature-block rules, so a 1.2.x stamp is generation 2 and anything older is
# generation 1. 1.2.x is the whole upper range because every row this backfill
# can see predates the column, and no release after 1.2.x wrote one — later
# releases stamp the column directly (see :func:`extraction_version_for_app_version`
# for the same mapping in Python, which the importer applies to file records).
# GLOB rather than a comparison because versions compare lexically in SQL, where
# '1.10.0' < '1.2.0'. A NULL stamp stays NULL and reads as generation 0 — older
# than every real generation, so those rows are offered for the (free, local)
# re-derivation check rather than assumed current.
_MIGRATION_011 = """
ALTER TABLE extractions ADD COLUMN extraction_version INTEGER;

UPDATE extractions SET extraction_version = 2 WHERE pipeline_version GLOB '1.2.[0-9]*';

UPDATE extractions SET extraction_version = 1
WHERE extraction_version IS NULL AND pipeline_version IS NOT NULL;
"""

# Persistent application settings: a small key/value table for choices the user
# makes in the dashboard that must survive a restart, such as which Pangram
# detector generation to score with (``pangram_model``) and whether the upgrade
# notice has been dismissed (``pangram_upgrade_notice``). Values are stored as
# text; each caller knows its own key's vocabulary. An absent key means "not
# set" and every reader supplies its own default, so a fresh database needs no
# seed rows.
_MIGRATION_012 = """
CREATE TABLE app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Undo migration 003's rebadge: the app no longer derives a label, it stores
# Pangram's prediction_short verbatim (AI / Human / Mixed). Assisted-dominated
# rows return to the "Mixed" the API actually returned; the distinction stays
# visible in fraction_ai_assisted and raw_response.
_MIGRATION_013 = """
UPDATE scores SET label = 'Mixed' WHERE label = 'AI-Assisted';
"""

# Auto-generated-mail classification (see autogen.py and
# docs/findings/auto-generated.md): a reason slug when the fetcher classified
# the message as machine-generated, NULL for human mail. Flagged messages are
# excluded from the extraction queue and therefore never scored. Rows fetched
# before the column existed stay NULL (headers are not stored, so they cannot
# be reclassified locally; a re-pull into a fresh store classifies them).
_MIGRATION_014 = """
ALTER TABLE messages ADD COLUMN auto_generated TEXT;
"""

# The message's own From display name, as parsed from its header. Before this
# column the name was kept only on the address row, which is keyed on the email
# alone and backfills once (see Store.upsert_address), so the first name ever
# seen for an address was shown for every later message from it. That is wrong
# for any sender whose display name varies per message — notification senders
# such as noreply@github.com, which put the acting person's name in From, are
# the clearest case. NULL for rows fetched before the column existed; readers
# fall back to the address name for those. Headers are not stored, so existing
# rows cannot be backfilled locally — only a re-pull recovers their names.
_MIGRATION_015 = """
ALTER TABLE messages ADD COLUMN from_name TEXT;
"""

# The message's verbatim header block, exactly as the server sent it. Stored so
# that anything derived from a header can be re-derived locally, without an IMAP
# re-fetch: the From display name (migration 015), the auto-generated
# classification (migration 014, whose note that "headers are not stored, so they
# cannot be reclassified locally" this column is what lifts), and whatever a
# later rule needs. A BLOB rather than TEXT because the value's purpose is to be
# re-parsed byte-for-byte by the stdlib email parser, which takes bytes; deciding
# a decoding here would be a lossy guess at a header that may carry raw 8-bit
# octets. Three-state exactly like raw_html: NULL means never captured (the
# backfill queue), non-empty is the header block, and b'' is the tombstone for a
# message the backfill fetched and got nothing for, which keeps a capped run
# moving. NULL for every row fetched before this column existed.
_MIGRATION_016 = """
ALTER TABLE messages ADD COLUMN raw_headers BLOB;
"""

# Covering indexes for the Senders pane's aggregates (see Store.sender_rows),
# which group every message of every sender through the messages → extractions →
# scores chain on each request. Measured over a 109,931-message store, the pane
# took ~32 s per page before these and ~0.1 s after; the list-scoped variant went
# from ~6 s to ~0.02 s.
#
# Each index exists to make one join step *covering*, so the aggregate reads
# index pages alone and never fetches a table row. That matters most for
# extractions and scores, whose rows carry the extracted text and the full
# Pangram JSON: without the extra column in the index, counting one status or
# label per message drags those payloads through the page cache.
#
# - messages(address_id, list_id, auto_generated) serves the mix aggregate both
#   unscoped and under a list filter, and its auto_generated count.
# - messages(address_id, from_name) makes COUNT(DISTINCT from_name) per address
#   (the many-names rule) a covering index walk rather than a temp B-tree.
# - extractions(message_id, status) covers the too_short count.
# - scores(extraction_id, label) covers the label mix.
#
# ANALYZE runs last because the planner passes over a covering index in favour
# of a UNIQUE autoindex until it has statistics to compare them.
_MIGRATION_017 = """
CREATE INDEX IF NOT EXISTS idx_messages_address_list_autogen
    ON messages(address_id, list_id, auto_generated);

CREATE INDEX IF NOT EXISTS idx_messages_address_from_name
    ON messages(address_id, from_name);

CREATE INDEX IF NOT EXISTS idx_extractions_message_status
    ON extractions(message_id, status);

CREATE INDEX IF NOT EXISTS idx_scores_extraction_label
    ON scores(extraction_id, label);

ANALYZE;
"""

# Covering index for the lists index's base aggregate (see Store.list_rows),
# which counts every list's messages and takes MIN(date) over them on each
# request. idx_messages_list_id finds the rows, but reading m.date then fetches
# each wide messages row (the raw body drags its overflow pages through the
# page cache); with (list_id, date) the aggregate is a covering index walk.
# Measured over a 110,725-message store, the query took ~4.3 s before and
# ~0.03 s after. The same request's mix and too_short aggregates are fixed by
# join order in list_rows itself, not by an index.
#
# ANALYZE for the same reason as migration 17: without statistics the planner
# passes over a new covering index.
_MIGRATION_018 = """
CREATE INDEX IF NOT EXISTS idx_messages_list_date
    ON messages(list_id, date);

ANALYZE;
"""

MIGRATIONS: list[tuple[int, str]] = [
    (1, _MIGRATION_001),
    (2, _MIGRATION_002),
    (3, _MIGRATION_003),
    (4, _MIGRATION_004),
    (5, _MIGRATION_005),
    (6, _MIGRATION_006),
    (7, _MIGRATION_007),
    (8, _MIGRATION_008),
    (9, _MIGRATION_009),
    (10, _MIGRATION_010),
    (11, _MIGRATION_011),
    (12, _MIGRATION_012),
    (13, _MIGRATION_013),
    (14, _MIGRATION_014),
    (15, _MIGRATION_015),
    (16, _MIGRATION_016),
    (17, _MIGRATION_017),
    (18, _MIGRATION_018),
]

#: The migrations whose backfill runs in Python (see :meth:`Store.__init__`).
#: Both add a reply-timing column, and one recompute fills either of them.
_TIMING_MIGRATIONS = frozenset({9, 10})


def _statements(script: str) -> list[str]:
    """Split a migration script into its individual SQL statements.

    Lines are accumulated until :func:`sqlite3.complete_statement` reports a
    complete statement, so a semicolon inside a string literal never splits.
    This requires every migration statement to end its own line with ``;``
    (how each one is formatted) and no comment to follow the final semicolon.
    """
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending)
            pending = ""
    if pending.strip():
        statements.append(pending)
    return statements


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Bring ``conn``'s database up to the latest schema version.

    Idempotent: creates the ``schema_version`` bookkeeping table if missing and
    applies only migrations newer than the recorded version, so calling this on
    an already-current database is a no-op. Returns the versions applied by
    this call (empty when the database was already current), so the caller can
    run any Python-side backfill a migration needs.

    Concurrency-safe: several connections routinely open the same database at
    once (the webapp opens one :class:`Store` per request), so the version
    check and the whole catch-up batch run inside one ``BEGIN IMMEDIATE``
    transaction. The first connection to take the write lock applies every
    pending migration and commits; the rest block on the lock (see the busy
    timeout set in :meth:`Store.__init__`), then re-read a current version and
    apply nothing. Statements run one at a time via :func:`_statements`
    because ``executescript`` issues an implicit COMMIT, which would break the
    transaction and reopen the race.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")

    def current_version() -> int:
        value = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        return value if value is not None else 0

    # Fast path: the everyday case stays read-only and never takes the lock.
    if current_version() >= MIGRATIONS[-1][0]:
        return []

    applied: list[int] = []
    # A caller may arrive with an implicit transaction open (legacy sqlite3
    # isolation auto-begins on DML). The old executescript path committed it
    # as a side effect; do the same explicitly so BEGIN IMMEDIATE can start.
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = current_version()  # re-read now that the write lock is held
        for version, script in MIGRATIONS:
            if version > current:
                for statement in _statements(script):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
                applied.append(version)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return applied


# --- Row dataclasses ----------------------------------------------------------


@dataclass(frozen=True)
class MailingList:
    """A mailing list / IMAP folder pairing."""

    id: int
    name: str
    folder: str
    last_synced_at: str | None
    removed_from_server_at: str | None = None
    last_message_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "MailingList":
        return cls(
            id=row["id"],
            name=row["name"],
            folder=row["folder"],
            last_synced_at=row["last_synced_at"],
            removed_from_server_at=row["removed_from_server_at"],
            last_message_at=row["last_message_at"],
        )


@dataclass(frozen=True)
class PullState:
    """Incremental-pull cursor for one list (UIDVALIDITY + last UID seen)."""

    list_id: int
    uidvalidity: int
    last_uid: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PullState":
        return cls(
            list_id=row["list_id"],
            uidvalidity=row["uidvalidity"],
            last_uid=row["last_uid"],
        )


@dataclass(frozen=True)
class Address:
    """An email address seen as a message sender."""

    id: int
    email: str
    display_name: str | None
    person_id: int | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Address":
        return cls(
            id=row["id"],
            email=row["email"],
            display_name=row["display_name"],
            person_id=row["person_id"],
        )


@dataclass(frozen=True)
class Person:
    """A person entity grouping one or more addresses."""

    id: int
    canonical_name: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Person":
        return cls(id=row["id"], canonical_name=row["canonical_name"])


@dataclass(frozen=True)
class Message:
    """A fetched message (its ``text/plain`` body plus metadata).

    ``raw_html`` is the decoded ``text/html`` part when the message carried one
    (NULL otherwise, and NULL for rows fetched before the column existed until
    the ``--backfill-html`` pull mode fills them in).

    ``pipeline_version`` is the app version that last ran a pipeline stage
    against the message (NULL for legacy rows predating the column).

    ``timing`` is the reply-timing classification (one of
    :data:`TIMING_VALUES`, or NULL when it cannot be computed — see
    :meth:`Store.recompute_timing`).

    ``auto_generated`` is the fetch-time classification reason when the
    message is machine-generated (see :mod:`~mailing_list_ai_check.autogen`),
    NULL for human mail and for rows fetched before migration 014.

    ``from_name`` is this message's own ``From`` display name (NULL when the
    header carried none, and for rows fetched before migration 015). The
    address row's ``display_name`` is the per-address fallback, so a sender
    whose name varies per message is reported correctly per message.

    ``raw_headers`` is the verbatim header block as bytes (NULL for rows
    fetched before migration 016 and not yet backfilled, ``b""`` for a
    backfilled message the server returned no headers for). It is the
    provenance every header-derived field can be recomputed from locally.
    """

    id: int
    message_id: str
    list_id: int
    address_id: int | None
    subject: str | None
    date: str | None
    in_reply_to: str | None
    raw_body: str | None
    uid: int | None
    fetched_at: str
    raw_html: str | None = None
    pipeline_version: str | None = None
    timing: str | None = None
    auto_generated: str | None = None
    from_name: str | None = None
    raw_headers: bytes | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Message":
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            list_id=row["list_id"],
            address_id=row["address_id"],
            subject=row["subject"],
            date=row["date"],
            in_reply_to=row["in_reply_to"],
            raw_body=row["raw_body"],
            uid=row["uid"],
            fetched_at=row["fetched_at"],
            raw_html=row["raw_html"],
            pipeline_version=row["pipeline_version"],
            timing=row["timing"],
            auto_generated=row["auto_generated"],
            from_name=row["from_name"],
            raw_headers=row["raw_headers"],
        )


@dataclass(frozen=True)
class Extraction:
    """The author's newly written text extracted from a message.

    ``extraction_version`` is the generation of the routine that produced
    ``extracted_text``
    (:data:`~email_reply_extractor.extraction.EXTRACTION_VERSION`): the value the
    staleness check compares against. NULL only where the migration backfill had
    no ``pipeline_version`` to map, which reads as older than every generation.

    ``pipeline_version`` is the app version that wrote the row — provenance
    only, kept because it names the release. Unlike ``messages.pipeline_version``
    it is never re-stamped by scoring.
    """

    id: int
    message_id: int
    extracted_text: str
    method: str
    char_count: int
    status: str
    created_at: str
    pipeline_version: str | None = None
    extraction_version: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Extraction":
        return cls(
            id=row["id"],
            message_id=row["message_id"],
            extracted_text=row["extracted_text"],
            method=row["method"],
            char_count=row["char_count"],
            status=row["status"],
            created_at=row["created_at"],
            pipeline_version=row["pipeline_version"],
            extraction_version=row["extraction_version"],
        )


@dataclass(frozen=True)
class Score:
    """A Pangram verdict for one extraction."""

    id: int
    extraction_id: int
    fraction_ai: float | None
    fraction_ai_assisted: float | None
    fraction_human: float | None
    label: str | None
    detector_version: str | None
    raw_response: str | None
    text_sha256: str
    scored_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Score":
        return cls(
            id=row["id"],
            extraction_id=row["extraction_id"],
            fraction_ai=row["fraction_ai"],
            fraction_ai_assisted=row["fraction_ai_assisted"],
            fraction_human=row["fraction_human"],
            label=row["label"],
            detector_version=row["detector_version"],
            raw_response=row["raw_response"],
            text_sha256=row["text_sha256"],
            scored_at=row["scored_at"],
        )


@dataclass(frozen=True)
class MessageUpsert:
    """Result of :meth:`Store.upsert_message`: the row plus whether it was new."""

    message: Message
    inserted: bool


@dataclass(frozen=True)
class MergeSuggestion:
    """A candidate person grouping: one display name shared by several emails."""

    display_name: str
    address_ids: tuple[int, ...]
    emails: tuple[str, ...]


@dataclass(frozen=True)
class MessageFilters:
    """Combinable filters for :meth:`Store.query_messages` and :meth:`Store.summary`.

    The dashboard's global filter bar maps onto this object. Every field is
    optional; ``None`` means "do not constrain on this dimension". Pagination and
    sort live here too so the API layer parses one shape. All filters combine
    with ``AND``.
    """

    list_name: str | None = None
    address: str | None = None
    person_id: int | None = None
    date_from: str | None = None
    date_to: str | None = None
    label: str | None = None
    min_likelihood: float | None = None
    max_likelihood: float | None = None
    q: str | None = None
    has_score: bool | None = None
    cpm_min: float | None = None
    cpm_max: float | None = None
    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    sort: str = "date"
    order: str = "desc"


# --- Helpers ------------------------------------------------------------------


def _utcnow_iso() -> str:
    """Current time as a UTC ISO-8601 string (second precision)."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    """Return the hex SHA-256 of ``text`` (the score cache key)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def version_key(v: str | None) -> tuple[int, int, int]:
    """Return a sortable ``(major, minor, patch)`` tuple for a semver string.

    Used to compare ``messages.pipeline_version`` values by precedence rather
    than lexically. ``None`` or any string that is not exactly ``"X.Y.Z"`` of
    integers parses to ``(0, 0, 0)``, so a missing or unparsable version sorts
    older than every real version.
    """
    if v is None:
        return (0, 0, 0)
    parts = v.split(".")
    if len(parts) != 3:
        return (0, 0, 0)
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return (0, 0, 0)
    return (major, minor, patch)


def extraction_version_for_app_version(v: str | None) -> int | None:
    """Map an app version onto the extraction generation it produced text with.

    The Python form of migration 011's backfill, for rows whose
    ``extraction_version`` is unknown but whose app version is not — an imported
    file written before the column existed, say. Version 1.2.0 introduced the
    localized quote-header and custom signature-block rules, so ``>= 1.2.0``
    derived generation 2 text and every earlier release generation 1. ``None``
    maps to ``None``: nothing is known, and the caller stores NULL rather than
    guessing a generation.
    """
    if v is None:
        return None
    return 2 if version_key(v)[:2] >= (1, 2) else 1


def _word_count(text: str) -> int:
    return len(text.split())


def _parent_message_id(in_reply_to: str) -> str:
    """The parent Message-ID named by a raw ``In-Reply-To`` header value.

    The header may carry surrounding whitespace or (rarely) several ids / CFWS
    comments; the first angle-bracket ``<...>`` token is the parent Message-ID,
    falling back to the stripped raw value when there is no such token.
    """
    match = re.search(r"<[^>]+>", in_reply_to)
    return match.group(0) if match else in_reply_to.strip()


def _parse_message_date(value: str | None) -> datetime | None:
    """Parse a stored ``messages.date`` into an aware datetime, or ``None``.

    Stored dates are UTC ISO-8601, but the column ultimately comes from the
    sender-set ``Date:`` header, so tolerate the malformed: an unparsable value
    yields ``None`` and a naive one is taken as UTC.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _epoch_seconds(value: str | None) -> int | None:
    """A stored ``messages.date`` as integer epoch seconds, or ``None``."""
    parsed = _parse_message_date(value)
    return int(parsed.timestamp()) if parsed is not None else None


def chars_per_minute(char_count: int, gap_seconds: float) -> float:
    """The composition rate a reply's new text implies, in characters per minute.

    ``gap_seconds`` (> 0) is the interval from the parent message to the reply
    — an upper bound on the time the author had to read the parent and compose
    ``char_count`` characters of new text, so the rate is a lower bound on the
    implied writing speed. This is the single definition of the rate: it is
    stored in ``messages.timing_cpm``, and the band it falls in (see
    :func:`classify_timing`) in ``messages.timing``.
    """
    return char_count / (gap_seconds / 60.0)


def _band_for_rate(rate: float) -> str:
    """The timing band a chars/minute ``rate`` falls in."""
    if rate >= TIMING_IMPLAUSIBLE_CPM:
        return "implausible"
    if rate >= TIMING_SUSPICIOUS_CPM:
        return "suspicious"
    return "normal"


def classify_timing(char_count: int, gap_seconds: float) -> str:
    """Classify the implied composition rate of a reply's new text.

    The rate from :func:`chars_per_minute` maps to
    :data:`TIMING_IMPLAUSIBLE_CPM` and :data:`TIMING_SUSPICIOUS_CPM`. The bound
    is one-sided: a low rate proves nothing, only high rates are informative.
    """
    return _band_for_rate(chars_per_minute(char_count, gap_seconds))


# --- Dashboard query building -------------------------------------------------

#: The shared FROM/JOIN skeleton for every dashboard query. A message has at
#: most one extraction (``extractions.message_id`` is UNIQUE) and an extraction
#: at most one score (``scores.extraction_id`` is UNIQUE), so these LEFT JOINs
#: never multiply message rows.
_MESSAGE_FROM = """
FROM messages m
JOIN lists l ON l.id = m.list_id
LEFT JOIN addresses a ON a.id = m.address_id
LEFT JOIN persons p ON p.id = a.person_id
LEFT JOIN extractions e ON e.message_id = m.id
LEFT JOIN scores s ON s.extraction_id = e.id
"""

#: The columns a message-list row exposes (joined across all five tables).
_MESSAGE_COLUMNS = """
    m.id AS id,
    m.message_id AS message_id,
    l.name AS list,
    m.date AS date,
    m.subject AS subject,
    m.in_reply_to AS in_reply_to,
    m.timing AS timing,
    m.timing_cpm AS timing_cpm,
    m.auto_generated AS auto_generated,
    m.list_id AS list_id,
    a.email AS from_address,
    m.from_name AS from_name,
    a.display_name AS from_display_name,
    a.person_id AS person_id,
    p.canonical_name AS person_name,
    e.status AS extraction_status,
    e.method AS extraction_method,
    e.char_count AS extraction_char_count,
    s.fraction_ai AS fraction_ai,
    s.fraction_ai_assisted AS fraction_ai_assisted,
    s.fraction_human AS fraction_human,
    s.label AS label,
    s.detector_version AS detector_version,
    s.raw_response AS raw_response,
    s.scored_at AS scored_at
"""

#: The slim per-message columns a rug plot needs: the id to open the message,
#: the date/subject for the bar's tooltip, the prediction bucket that colours it,
#: and the extraction status, which tells a message gated under the reliability
#: floor (``too_short``) from a merely unscored one — the two take different bar
#: colours. ``scores.label`` stores the response's ``prediction_short`` verbatim
#: (since migration 013), so it is served under both names — without loading the
#: full Pangram JSON per bar.
_RUG_COLUMNS = """
    m.id AS id,
    m.message_id AS message_id,
    l.name AS list,
    m.date AS date,
    m.subject AS subject,
    e.status AS extraction_status,
    s.label AS label,
    s.label AS prediction_short
"""


def _in_chunks(items: Sequence[Any], size: int = _IN_CHUNK) -> Iterator[Sequence[Any]]:
    """Yield ``items`` in batches small enough to bind into one ``IN (...)``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


#: How many different ``From`` display names one address may present before the
#: Senders pane stops naming it after any of them. At or above this count no
#: single name represents the address — a notification sender puts the acting
#: person's name in ``From``, so the stored per-address name is only whichever
#: was seen first — and the address itself is the honest label.
#:
#: Five rather than three: an individual who varies their own name across a few
#: messages ("Blake Morrison", "Blake", "~blake") reaches three without being a
#: shared address, while every genuinely shared address measured in the archive
#: sits far above five (the datatracker's, at 330, highest).
MULTI_NAME_ADDRESS_THRESHOLD = 5

#: Unlinked addresses that the Senders pane omits unless its "Show all" switch is
#: on, because each one carries mail written by many different people. One row
#: for such an address compares nothing about any one sender. Its messages stay
#: scored and counted everywhere else in the dashboard.
#:
#: ``noreply@ietf.org`` is the datatracker's address: it delivers IESG ballot
#: positions, review results and other notifications under the acting person's
#: name (343 distinct ``From`` names in the archive, 2026-11-06).
SHARED_SENDER_ADDRESSES = frozenset({"noreply@ietf.org"})


#: The IETF list server's DMARC From-rewriting domain. When a list rewrites a
#: sender whose domain publishes a strict DMARC policy, the message is re-sent
#: from ``<local>=40<domain>@dmarc.ietf.org`` — the original address with its
#: characters hex-escaped as ``=XX`` (``@`` becoming ``=40``) and moved into the
#: local part. The same person therefore appears under two addresses.
DMARC_REWRITE_DOMAIN = "dmarc.ietf.org"

_DMARC_ESCAPE_RE = re.compile(r"=([0-9A-Fa-f]{2})")


def dmarc_rewrite_original(email: str) -> str | None:
    """The address a :data:`DMARC_REWRITE_DOMAIN` rewrite stands for, or ``None``.

    ``maarten.simon=40sidn.nl@dmarc.ietf.org`` unescapes to
    ``maarten.simon@sidn.nl``. ``None`` for any address not on the rewriting
    domain, and for one whose local part does not unescape to a single plausible
    address — the escaping is mechanical, so anything else is not a rewrite this
    should claim to understand.
    """
    local, _, domain = email.strip().lower().rpartition("@")
    if not local or domain != DMARC_REWRITE_DOMAIN:
        return None
    decoded = _DMARC_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), local)
    original_local, separator, original_domain = decoded.partition("@")
    if not separator or not original_local or not original_domain:
        return None
    if "@" in original_domain or not decoded.isascii() or any(c.isspace() for c in decoded):
        return None
    return decoded


def _address_display_name(email: str, display_name: str | None, distinct_names: int) -> str:
    """The Senders pane's name for one address.

    Its stored ``display_name`` normally, falling back to the address when there
    is none. An address that has presented
    :data:`MULTI_NAME_ADDRESS_THRESHOLD` or more different ``From`` names is
    labelled with the address regardless, because naming it after one of them
    attributes every message it sent to whichever person happened to be seen
    first (see ``messages.from_name``, migration 015).
    """
    if distinct_names >= MULTI_NAME_ADDRESS_THRESHOLD:
        return email
    return display_name or email


def ai_share(label_counts: Mapping[str, Any] | None, too_short_count: int = 0) -> float:
    """The ``AI`` fraction of one aggregate mix, in ``[0, 1]``.

    The denominator is every message the dashboard's mix bar draws — the scored
    ones plus those gated under the reliability floor — so the value matches the
    ``AI`` percentage the bar reports. A mix with nothing in it is 0.0.
    """
    counts = label_counts or {}
    scored = sum(int(n or 0) for n in counts.values())
    denom = scored + int(too_short_count or 0)
    if denom == 0:
        return 0.0
    return int(counts.get("AI") or 0) / denom


def ai_count(label_counts: Mapping[str, Any] | None) -> int:
    """The number of ``AI`` and ``Mixed`` messages in one aggregate mix.

    The count every aggregate-analysis sort orders by — the dashboard's lists
    index and Timelines screen mirror it client-side (``aiCount`` in
    ``lib/labels.js``). An empty mix is 0. Distinct from :func:`ai_share`,
    which the stats export still reports.
    """
    counts = label_counts or {}
    return int(counts.get("AI") or 0) + int(counts.get("Mixed") or 0)


def _sender_scope(person_id: int | None, address: str | None) -> tuple[str, str, list[Any]]:
    """SQL predicates selecting (and excluding) one sender's messages.

    Returns ``(is_sender, is_not_sender, params)``, both fragments taking the
    same single bound parameter and assuming ``addresses a`` is joined as
    ``_MESSAGE_FROM`` does. A person covers every address linked to them; an
    address covers only itself, lower-cased exactly as
    :func:`_build_message_where` does. ``is_not_sender`` spells out the NULL case
    because a message whose ``address_id`` is NULL is not this sender's, yet
    ``a.person_id != ?`` would evaluate to NULL and drop the row.
    """
    if person_id is not None:
        return ("a.person_id = ?", "(a.person_id IS NULL OR a.person_id != ?)", [person_id])
    if address:
        return ("a.email = ?", "(a.email IS NULL OR a.email != ?)", [address.strip().lower()])
    raise ValueError("a sender scope needs either person_id or address")


def _build_message_where(f: MessageFilters) -> tuple[str, list[Any]]:
    """Build the ``WHERE`` fragment + bound params for ``f`` (empty if no filters).

    Applied identically by :meth:`Store.query_messages` and :meth:`Store.summary`
    so the explorer and the overview always agree on what a filter selects.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if f.list_name:
        clauses.append("l.name = ?")
        params.append(f.list_name)
    if f.address:
        clauses.append("a.email = ?")
        params.append(f.address.strip().lower())
    if f.person_id is not None:
        clauses.append("a.person_id = ?")
        params.append(f.person_id)
    if f.date_from:
        clauses.append("m.date >= ?")
        params.append(f.date_from)
    if f.date_to:
        clauses.append("m.date <= ?")
        params.append(f.date_to)
    if f.label:
        # The dashboard filters by prediction_short (Human / Mixed / AI), which
        # is exactly what scores.label stores.
        clauses.append("s.label = ?")
        params.append(f.label)
    if f.min_likelihood is not None:
        clauses.append("s.fraction_ai >= ?")
        params.append(f.min_likelihood)
    if f.max_likelihood is not None:
        clauses.append("s.fraction_ai <= ?")
        params.append(f.max_likelihood)
    if f.q:
        like = f"%{f.q}%"
        clauses.append("(m.subject LIKE ? OR e.extracted_text LIKE ?)")
        params.extend([like, like])
    if f.has_score is True:
        clauses.append("s.id IS NOT NULL")
    elif f.has_score is False:
        clauses.append("s.id IS NULL")
    # Inclusive bounds on the stored reply-timing rate. A message with no rate
    # (``timing_cpm`` NULL) never satisfies either comparison, so setting one
    # bound excludes every unclassifiable message.
    if f.cpm_min is not None:
        clauses.append("m.timing_cpm >= ?")
        params.append(f.cpm_min)
    if f.cpm_max is not None:
        clauses.append("m.timing_cpm <= ?")
        params.append(f.cpm_max)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# --- Store --------------------------------------------------------------------


class Store:
    """Typed access to the SQLite database.

    Open with a filesystem path (or ``":memory:"``). Use as a context manager,
    or call :meth:`close` explicitly. Each connection runs with WAL journaling
    and foreign-key enforcement enabled, and the schema is migrated to the
    latest version on open.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # Concurrent opens are routine (the webapp opens one Store per request),
        # and the first open after an upgrade holds the write lock for a full
        # migration catch-up plus backfill — give waiters time to sit it out.
        # 120 s rather than 30 s because an import of 100,000 messages was
        # measured holding the write lock for ~27 s: at 30 s a store only 15%
        # larger would fail a concurrent pull outright with "database is
        # locked", an error nothing in the codebase catches.
        self.conn.execute("PRAGMA busy_timeout = 120000")
        # WAL is requested on every open, a no-op read once the database is
        # converted. The one-time conversion of a fresh rollback-journal
        # database needs exclusive access and returns SQLITE_BUSY *without*
        # consulting the busy handler while other connections hold locks —
        # which is exactly what several requests opening a brand-new database
        # at once do — so retry briefly rather than failing the open.
        for attempt in range(5):
            try:
                self.conn.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
        # A bulk import was measured growing the WAL to roughly the size of the
        # database itself (2-3 GB at 100,000 messages), where it stays until the
        # last connection closes because a checkpoint alone only rewinds the
        # file. This caps the file a completed checkpoint leaves behind at 64 MB.
        # An explicit truncating checkpoint is deliberately not issued: one was
        # measured blocking for 32 s while a reader held an open transaction.
        self.conn.execute("PRAGMA journal_size_limit = 67108864")
        self.conn.execute("PRAGMA foreign_keys = ON")
        applied = apply_migrations(self.conn)
        # The timing columns' backfill needs Python (In-Reply-To normalization
        # and date parsing), so it runs here, once, when either of their
        # migrations applies.
        if _TIMING_MIGRATIONS.intersection(applied):
            self.recompute_timing()

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lists ----------------------------------------------------------------

    def upsert_list(self, name: str, folder: str) -> MailingList:
        """Insert the list if new (keyed on ``folder``), else return existing.

        Idempotent — re-inserting the same folder leaves the row untouched and
        returns it.
        """
        self.conn.execute(
            "INSERT INTO lists(name, folder) VALUES (?, ?) ON CONFLICT(folder) DO NOTHING",
            (name, folder),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM lists WHERE folder = ?", (folder,)).fetchone()
        return MailingList.from_row(row)

    def get_list(self, list_id: int) -> MailingList | None:
        """Return the list with ``list_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM lists WHERE id = ?", (list_id,)).fetchone()
        return MailingList.from_row(row) if row else None

    def get_list_by_name(self, name: str) -> MailingList | None:
        """Return the list whose ``name`` matches exactly, or ``None``.

        Names are unique (schema ``UNIQUE`` constraint), so at most one row
        matches. Used by the ranged-pull / preview endpoints to resolve a
        user-supplied list name to an existing row without ever creating one.
        """
        row = self.conn.execute("SELECT * FROM lists WHERE name = ?", (name,)).fetchone()
        return MailingList.from_row(row) if row else None

    def min_uid_for_list(self, list_id: int) -> int | None:
        """Return the smallest stored IMAP ``uid`` for ``list_id``, or ``None``.

        Ignores rows with a NULL ``uid`` (messages imported without one). ``None``
        means the list has no UID-bearing message, so there is nothing to anchor a
        "before" (older-than-earliest-stored) pull against.
        """
        row = self.conn.execute(
            "SELECT MIN(uid) AS u FROM messages WHERE list_id = ? AND uid IS NOT NULL",
            (list_id,),
        ).fetchone()
        return row["u"] if row and row["u"] is not None else None

    def max_uid_for_list(self, list_id: int) -> int | None:
        """Return the largest stored IMAP ``uid`` for ``list_id``, or ``None``.

        Ignores rows with a NULL ``uid``. Used as the baseline for a "new"
        (newer-than-anything-stored) pull when no incremental cursor exists for
        the list's current UIDVALIDITY.
        """
        row = self.conn.execute(
            "SELECT MAX(uid) AS u FROM messages WHERE list_id = ? AND uid IS NOT NULL",
            (list_id,),
        ).fetchone()
        return row["u"] if row and row["u"] is not None else None

    def uids_for_list(self, list_id: int) -> set[int]:
        """Return every stored IMAP ``uid`` for ``list_id`` (rows with one).

        A pull subtracts these from a search result before fetching bodies, so
        already-stored messages are never re-downloaded (valid only while the
        folder's UIDVALIDITY matches the stored cursor's — the caller checks).
        """
        rows = self.conn.execute(
            "SELECT uid FROM messages WHERE list_id = ? AND uid IS NOT NULL",
            (list_id,),
        ).fetchall()
        return {row["uid"] for row in rows}

    def set_list_synced(self, list_id: int, when: str | None = None) -> None:
        """Stamp ``lists.last_synced_at`` (defaults to now)."""
        self.conn.execute(
            "UPDATE lists SET last_synced_at = ? WHERE id = ?",
            (when or _utcnow_iso(), list_id),
        )
        self.conn.commit()

    def set_list_last_message(self, list_id: int, when: str | None) -> None:
        """Record ``lists.last_message_at`` — the server's newest-message time.

        ``when`` is a UTC ISO-8601 string, or ``None`` when the folder holds no
        messages / has not been checked. Unlike :meth:`set_list_synced` there is
        no "now" default: the value is always the server's INTERNALDATE.
        """
        self.conn.execute(
            "UPDATE lists SET last_message_at = ? WHERE id = ?",
            (when, list_id),
        )
        self.conn.commit()

    def tracked_list_folders(self) -> list[tuple[int, str]]:
        """Return ``(id, folder)`` for every list worth checking against the server.

        A list is "tracked" once it holds at least one local message (the user
        has pulled it). This restricts the caller to those lists and excludes any
        the server no longer carries (``removed_from_server_at`` set), so the
        ~1,400 index-only folders are never EXAMINEd. Ordered by ``folder`` for a
        deterministic sweep.
        """
        rows = self.conn.execute(
            "SELECT l.id, l.folder FROM lists l "
            "WHERE l.removed_from_server_at IS NULL "
            "AND EXISTS (SELECT 1 FROM messages m WHERE m.list_id = l.id) "
            "ORDER BY l.folder"
        ).fetchall()
        return [(row["id"], row["folder"]) for row in rows]

    def refresh_lists_index(self, entries: Sequence[tuple[str, str]]) -> dict[str, int]:
        """Reconcile the ``lists`` table with the server's full folder enumeration.

        ``entries`` is every ``(name, folder)`` pair the IMAP ``LIST`` command
        returned. Rather than wiping and re-inserting (which would orphan
        messages), rows are reconciled:

        - folders new to the store are inserted;
        - known folders seen again have any ``removed_from_server_at`` cleared;
        - stored folders **missing** from the enumeration are deleted when no
          messages reference them, otherwise kept and stamped
          ``removed_from_server_at`` so the list and its messages survive.

        Returns counts: ``added``, ``restored``, ``deleted``, ``kept_missing``
        and the resulting ``total``. The enumeration goes through a temp table
        so ~1,400 folders never hit SQLite's bound-parameter limit.
        """
        conn = self.conn
        conn.execute("DROP TABLE IF EXISTS temp._server_folders")
        conn.execute(
            "CREATE TEMP TABLE _server_folders (name TEXT NOT NULL, folder TEXT PRIMARY KEY)"
        )
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO _server_folders(name, folder) VALUES (?, ?)", entries
            )

            added = conn.execute(
                "INSERT INTO lists(name, folder) "
                "SELECT name, folder FROM _server_folders WHERE true "
                "ON CONFLICT(folder) DO NOTHING"
            ).rowcount
            restored = conn.execute(
                "UPDATE lists SET removed_from_server_at = NULL "
                "WHERE removed_from_server_at IS NOT NULL "
                "AND folder IN (SELECT folder FROM _server_folders)"
            ).rowcount
            deleted = conn.execute(
                "DELETE FROM lists "
                "WHERE folder NOT IN (SELECT folder FROM _server_folders) "
                "AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.list_id = lists.id)"
            ).rowcount
            # Everything still missing after the delete has messages: keep it,
            # stamped so the UI can say the server no longer carries the list.
            kept_missing = conn.execute(
                "UPDATE lists SET removed_from_server_at = ? "
                "WHERE removed_from_server_at IS NULL "
                "AND folder NOT IN (SELECT folder FROM _server_folders)",
                (_utcnow_iso(),),
            ).rowcount
            total = conn.execute("SELECT COUNT(*) FROM lists").fetchone()[0]
            conn.commit()
        finally:
            conn.execute("DROP TABLE IF EXISTS temp._server_folders")
        return {
            "added": added,
            "restored": restored,
            "deleted": deleted,
            "kept_missing": kept_missing,
            "total": total,
        }

    # -- pull_state -----------------------------------------------------------

    def get_pull_state(self, list_id: int) -> PullState | None:
        """Return the incremental cursor for ``list_id``, or ``None`` if unset."""
        row = self.conn.execute("SELECT * FROM pull_state WHERE list_id = ?", (list_id,)).fetchone()
        return PullState.from_row(row) if row else None

    def set_pull_state(self, list_id: int, uidvalidity: int, last_uid: int) -> PullState:
        """Create or replace the cursor for ``list_id``.

        On a UIDVALIDITY change the caller resyncs and then calls this with the
        new ``uidvalidity`` and recomputed ``last_uid``; the row is overwritten.
        """
        self.conn.execute(
            "INSERT INTO pull_state(list_id, uidvalidity, last_uid) VALUES (?, ?, ?) "
            "ON CONFLICT(list_id) DO UPDATE SET "
            "uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid",
            (list_id, uidvalidity, last_uid),
        )
        self.conn.commit()
        return PullState(list_id=list_id, uidvalidity=uidvalidity, last_uid=last_uid)

    # -- addresses ------------------------------------------------------------

    def upsert_address(self, email: str, display_name: str | None = None) -> Address:
        """Insert or fetch an address, normalizing ``email`` to lowercase.

        If the address exists and a ``display_name`` is supplied while the stored
        one is empty, the stored name is backfilled. Returns the current row.
        """
        normalized = email.strip().lower()
        self.conn.execute(
            "INSERT INTO addresses(email, display_name) VALUES (?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "display_name = COALESCE(NULLIF(addresses.display_name, ''), excluded.display_name)",
            (normalized, display_name),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM addresses WHERE email = ?", (normalized,)).fetchone()
        return Address.from_row(row)

    def get_address(self, address_id: int) -> Address | None:
        """Return the address with ``address_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM addresses WHERE id = ?", (address_id,)).fetchone()
        return Address.from_row(row) if row else None

    # -- messages -------------------------------------------------------------

    def upsert_message(
        self,
        *,
        message_id: str,
        list_id: int,
        address_id: int | None,
        subject: str | None,
        date: str | None,
        in_reply_to: str | None,
        raw_body: str | None,
        uid: int | None,
        fetched_at: str | None = None,
        raw_html: str | None = None,
        pipeline_version: str | None = None,
        auto_generated: str | None = None,
        from_name: str | None = None,
        raw_headers: bytes | None = None,
    ) -> MessageUpsert:
        """Insert a message, deduping on ``(list_id, message_id)``.

        Idempotent: a re-pull of the same message is a no-op that returns the
        existing row with ``inserted=False`` (``raw_html``,
        ``pipeline_version``, ``auto_generated``, ``from_name`` and
        ``raw_headers`` are stored only on insert; a conflicting existing row is
        left exactly as-is). New rows return ``inserted=True``.

        ``pipeline_version`` defaults to the current package version
        (:data:`__version__`); tests may pass an explicit value.
        ``auto_generated`` is the fetch-time classification reason (see
        :mod:`~mailing_list_ai_check.autogen`), or ``None`` for human mail.
        ``from_name`` is the message's own ``From`` display name, kept per
        message because one address can present different names.
        ``raw_headers`` is the verbatim header block those header-derived
        fields were computed from.
        """
        cur = self.conn.execute(
            "INSERT INTO messages("
            "message_id, list_id, address_id, subject, date, in_reply_to, raw_body, uid, "
            "fetched_at, raw_html, pipeline_version, auto_generated, from_name, raw_headers"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(list_id, message_id) DO NOTHING",
            (
                message_id,
                list_id,
                address_id,
                subject,
                date,
                in_reply_to,
                raw_body,
                uid,
                fetched_at or _utcnow_iso(),
                raw_html,
                pipeline_version if pipeline_version is not None else __version__,
                auto_generated,
                from_name,
                raw_headers,
            ),
        )
        self.conn.commit()
        inserted = cur.rowcount > 0
        row = self.conn.execute(
            "SELECT * FROM messages WHERE list_id = ? AND message_id = ?",
            (list_id, message_id),
        ).fetchone()
        return MessageUpsert(message=Message.from_row(row), inserted=inserted)

    def get_message(self, message_pk: int) -> Message | None:
        """Return the message with primary key ``message_pk``, or ``None``."""
        row = self.conn.execute("SELECT * FROM messages WHERE id = ?", (message_pk,)).fetchone()
        return Message.from_row(row) if row else None

    def get_parent_body(
        self, in_reply_to: str, *, exclude_message_id: str | None = None
    ) -> str | None:
        """Return the raw body of the message this ``In-Reply-To`` points at.

        ``in_reply_to`` is the raw header value, which may carry surrounding
        whitespace or (rarely) several ids / CFWS comments; the first
        angle-bracket ``<...>`` token is used as the parent Message-ID, falling
        back to the stripped raw value when there is no such token. The same
        message can exist on several lists, so any stored copy is fine. Returns
        ``None`` when no parent is found or the found row has a NULL body.

        ``exclude_message_id`` (the requesting message's own Message-ID) guards
        the self-reply case: a message whose ``In-Reply-To`` names its own id
        would otherwise resolve to its own body, and the parent-diff assist would
        then delete the entire message. When the resolved parent id equals it,
        ``None`` is returned so the message is extracted as if it had no parent.
        """
        parent_id = _parent_message_id(in_reply_to)
        if exclude_message_id is not None and parent_id == exclude_message_id:
            return None
        row = self.conn.execute(
            "SELECT raw_body FROM messages WHERE message_id = ? ORDER BY id LIMIT 1",
            (parent_id,),
        ).fetchone()
        return row["raw_body"] if row else None

    def set_message_raw_html(self, message_pk: int, raw_html: str) -> None:
        """Store the decoded ``text/html`` part for an already-stored message.

        Used by the ``--backfill-html`` pull mode to fill in ``raw_html`` for
        rows fetched before the column existed. Does not touch ``raw_body`` or
        any other field.

        ``raw_html`` is three-state: ``NULL`` means "never checked" (the
        backfill queue), a non-empty string is the captured HTML, and the empty
        string ``""`` is a tombstone meaning "checked — the message carries no
        HTML part". The backfill stamps ``""`` for HTML-less messages so
        :meth:`iter_messages_missing_html` stops returning them; ``""`` is falsy
        everywhere the HTML is consumed, so it behaves exactly like NULL for the
        extraction oracle and the signature hint.
        """
        self.conn.execute(
            "UPDATE messages SET raw_html = ? WHERE id = ?",
            (raw_html, message_pk),
        )
        self.conn.commit()

    def iter_messages_missing_html(self, list_id: int) -> Iterator[Message]:
        """Yield the list's messages that still need an HTML backfill, by UID.

        A message qualifies when ``raw_html IS NULL`` and it has a UID (so it can
        be re-fetched from IMAP). Ordered by ``uid`` so a capped backfill run
        makes deterministic forward progress across runs. Messages the backfill
        has already checked and found HTML-less carry the empty-string tombstone
        (see :meth:`set_message_raw_html`), not NULL, so they are excluded here
        and a run is never stuck re-fetching the same HTML-less messages.
        """
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE list_id = ? AND raw_html IS NULL AND uid IS NOT NULL "
            "ORDER BY uid",
            (list_id,),
        ).fetchall()
        for row in rows:
            yield Message.from_row(row)

    def set_message_headers(
        self, message_pk: int, raw_headers: bytes, *, from_name: str | None = None
    ) -> None:
        """Store the verbatim header block for an already-stored message.

        Used by the ``--backfill-headers`` pull mode to fill ``raw_headers`` for
        rows fetched before the column existed. ``from_name`` is the display name
        re-derived from those headers; it is written only where the column is
        still NULL, so a name already captured at fetch time is never overwritten
        by a later re-parse. Every other field, ``raw_body`` included, is left
        untouched — the backfill adds provenance, it does not rewrite history.

        ``raw_headers`` is three-state as described on migration 016: pass
        ``b""`` to tombstone a message the server returned no headers for, so
        :meth:`iter_messages_missing_headers` stops offering it and a capped run
        keeps making forward progress.
        """
        self.conn.execute(
            "UPDATE messages SET raw_headers = ?, from_name = COALESCE(from_name, ?) WHERE id = ?",
            (raw_headers, from_name, message_pk),
        )
        self.conn.commit()

    def iter_messages_missing_headers(self, list_id: int) -> Iterator[Message]:
        """Yield the list's messages that still need a header backfill, by UID.

        A message qualifies when ``raw_headers IS NULL`` and it has a UID (so it
        can be re-fetched). Ordered by ``uid``, so a capped run makes
        deterministic forward progress across runs; tombstoned rows (``b""``)
        are excluded, exactly as in :meth:`iter_messages_missing_html`.
        """
        rows = self.conn.execute(
            "SELECT * FROM messages "
            "WHERE list_id = ? AND raw_headers IS NULL AND uid IS NOT NULL "
            "ORDER BY uid",
            (list_id,),
        ).fetchall()
        for row in rows:
            yield Message.from_row(row)

    def iter_messages_without_extraction(self) -> Iterator[Message]:
        """Yield messages that have no ``extractions`` row yet (extraction queue).

        Messages classified as auto-generated at fetch time are excluded: they
        never enter the extraction queue, so they are never scored either
        (scores hang off extractions).
        """
        rows = self.conn.execute(
            "SELECT m.* FROM messages m "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "WHERE e.id IS NULL AND m.auto_generated IS NULL ORDER BY m.id"
        ).fetchall()
        for row in rows:
            yield Message.from_row(row)

    # -- reply timing ----------------------------------------------------------

    def recompute_timing(self) -> int:
        """Recompute the reply-timing columns for every message; return rows changed.

        A reply's timing is the implied composition rate of its new text — the
        extraction's ``char_count`` over the gap between the parent message's
        ``date`` and the reply's own. Both columns are written in the same pass
        and always agree: ``timing_cpm`` holds the rate itself (see
        :func:`chars_per_minute`) and ``timing`` the band it falls in (see
        :func:`classify_timing`).

        Both are NULL wherever the rate cannot be computed: the message is not
        a reply, its parent is not stored, its ``In-Reply-To`` resolves to
        itself, either date is missing or unparsable, the gap is not positive
        (sender clocks are untrusted), or it has no extraction with authored
        text (status in :data:`_TIMING_STATUSES`).

        When the same parent Message-ID is stored on several lists, the copy
        on the reply's own list is preferred, then the lowest ``id`` (the
        copies carry the same ``Date:`` header, so this only breaks ties
        deterministically).

        The whole table is recomputed in one pass — a few hundred milliseconds
        at 100k messages — so the callers are the pipeline stages that change
        its inputs (fetch, extract, re-extract, import) plus the migration
        backfill, not per-row writes.
        """
        rows = self.conn.execute(
            "SELECT m.id, m.message_id, m.list_id, m.date, m.in_reply_to, m.timing, "
            "m.timing_cpm AS timing_cpm, "
            "e.char_count AS char_count, e.status AS status "
            "FROM messages m LEFT JOIN extractions e ON e.message_id = m.id "
            "ORDER BY m.id"
        ).fetchall()

        # Message-ID -> [(pk, list_id, date)], in id order (rows are id-sorted).
        parents: dict[str, list[tuple[int, int, str | None]]] = {}
        for row in rows:
            parents.setdefault(row["message_id"], []).append(
                (row["id"], row["list_id"], row["date"])
            )

        def rate_for(row: sqlite3.Row) -> float | None:
            if not row["in_reply_to"] or row["status"] not in _TIMING_STATUSES:
                return None
            parent_mid = _parent_message_id(row["in_reply_to"])
            if parent_mid == row["message_id"]:  # self-reply, as in get_parent_body
                return None
            candidates = parents.get(parent_mid, [])
            chosen = next((c for c in candidates if c[1] == row["list_id"]), None)
            if chosen is None:
                chosen = candidates[0] if candidates else None
            if chosen is None:
                return None
            reply_dt = _parse_message_date(row["date"])
            parent_dt = _parse_message_date(chosen[2])
            if reply_dt is None or parent_dt is None:
                return None
            gap_seconds = (reply_dt - parent_dt).total_seconds()
            if gap_seconds <= 0:
                return None
            return chars_per_minute(row["char_count"], gap_seconds)

        updates: list[tuple[str | None, float | None, int]] = []
        for row in rows:
            rate = rate_for(row)
            band = None if rate is None else _band_for_rate(rate)
            if (band, rate) != (row["timing"], row["timing_cpm"]):
                updates.append((band, rate, row["id"]))
        if updates:
            self.conn.executemany(
                "UPDATE messages SET timing = ?, timing_cpm = ? WHERE id = ?", updates
            )
            self.conn.commit()
        return len(updates)

    # -- extractions ----------------------------------------------------------

    def insert_extraction(
        self,
        *,
        message_id: int,
        extracted_text: str,
        method: str,
        status: str,
        char_count: int | None = None,
        created_at: str | None = None,
        pipeline_version: str | None = None,
        extraction_version: int | None = None,
    ) -> Extraction:
        """Record the extraction for a message (one per message).

        ``char_count`` defaults to ``len(extracted_text)``. ``status`` must be
        one of :data:`EXTRACTION_STATUSES` (also enforced by a CHECK constraint).

        Stamps the new row's ``extraction_version`` with the running routine's
        generation (:data:`~email_reply_extractor.extraction.EXTRACTION_VERSION`)
        and its ``pipeline_version`` — which is also re-stamped on the owning
        message — with the current package version (:data:`__version__`); tests
        may pass explicit values.
        """
        if status not in EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid extraction status {status!r}; expected one of {EXTRACTION_STATUSES}"
            )
        version = pipeline_version if pipeline_version is not None else __version__
        generation = extraction_version if extraction_version is not None else EXTRACTION_VERSION
        cur = self.conn.execute(
            "INSERT INTO extractions("
            "message_id, extracted_text, method, char_count, status, created_at, "
            "pipeline_version, extraction_version"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                extracted_text,
                method,
                len(extracted_text) if char_count is None else char_count,
                status,
                created_at or _utcnow_iso(),
                version,
                generation,
            ),
        )
        self.conn.execute(
            "UPDATE messages SET pipeline_version = ? WHERE id = ?",
            (version, message_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return Extraction.from_row(row)

    def get_extraction(self, extraction_id: int) -> Extraction | None:
        """Return the extraction with ``extraction_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE id = ?", (extraction_id,)
        ).fetchone()
        return Extraction.from_row(row) if row else None

    def _unscored_ok_extractions(self) -> list[Extraction]:
        """Return every ``status='ok'`` extraction that has no ``scores`` row yet."""
        rows = self.conn.execute(
            "SELECT e.* FROM extractions e "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "WHERE s.id IS NULL AND e.status = 'ok' ORDER BY e.id"
        ).fetchall()
        return [Extraction.from_row(row) for row in rows]

    def iter_extractions_needing_score(self, min_words: int = 50) -> Iterator[Extraction]:
        """Yield unscored ``ok`` extractions with at least ``min_words`` words.

        This is the Pangram scoring queue: only ``status='ok'`` extractions that
        have no ``scores`` row yet, filtered to the reliability floor (default 50
        words, per the Pangram findings) so short text is never sent.
        """
        for extraction in self._unscored_ok_extractions():
            if _word_count(extraction.extracted_text) >= min_words:
                yield extraction

    def iter_too_short_extractions(self, min_words: int = 50) -> Iterator[Extraction]:
        """Yield unscored ``ok`` extractions with fewer than ``min_words`` words.

        The complement of :meth:`iter_extractions_needing_score`: these fall
        under the reliability floor and the scorer marks them ``too_short``
        rather than paying Pangram for a verdict the vendor deems untrustworthy.
        """
        for extraction in self._unscored_ok_extractions():
            if _word_count(extraction.extracted_text) < min_words:
                yield extraction

    def update_extraction_status(self, extraction_id: int, status: str) -> Extraction | None:
        """Set ``extractions.status`` for ``extraction_id``; return the updated row.

        ``status`` must be one of :data:`EXTRACTION_STATUSES`. Returns ``None`` if
        no such extraction exists.
        """
        if status not in EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid extraction status {status!r}; expected one of {EXTRACTION_STATUSES}"
            )
        self.conn.execute(
            "UPDATE extractions SET status = ? WHERE id = ?",
            (status, extraction_id),
        )
        self.conn.commit()
        return self.get_extraction(extraction_id)

    def extracted_message_ids(self) -> list[int]:
        """Return the ids of messages that have an extraction row, ascending.

        The work list for a re-extraction pass. Only the ids are read, so a pass
        over a large store can fetch each message and its extraction one at a
        time instead of holding every extracted text in memory at once.
        """
        rows = self.conn.execute(
            "SELECT message_id FROM extractions ORDER BY message_id"
        ).fetchall()
        return [row["message_id"] for row in rows]

    # -- parent arrival ---------------------------------------------------------

    def max_message_pk(self) -> int:
        """Return the highest ``messages.id`` in the store, or 0 when empty."""
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS pk FROM messages").fetchone()
        return row["pk"]

    def max_extracted_message_pk(self) -> int:
        """Return the highest ``messages.id`` that has an extraction, or 0."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(message_id), 0) AS pk FROM extractions"
        ).fetchone()
        return row["pk"]

    def message_ids_first_stored_after(self, message_pk: int) -> set[str]:
        """Return the Message-IDs whose first stored copy has an id above ``message_pk``.

        These are the Message-IDs that :meth:`get_parent_body` could not resolve
        while the store held only rows up to ``message_pk`` and can resolve now.
        A Message-ID that already had a copy at or below ``message_pk`` (the
        same message on another list) is left out: :meth:`get_parent_body`
        reads the lowest-id copy, so a later copy changes nothing. Auto-generated
        messages are included, because a reply to one resolves it as a parent
        like any other.
        """
        rows = self.conn.execute(
            "SELECT DISTINCT n.message_id AS message_id FROM messages n "
            "WHERE n.id > ? AND NOT EXISTS ("
            "SELECT 1 FROM messages o WHERE o.message_id = n.message_id AND o.id <= ?)",
            (message_pk, message_pk),
        ).fetchall()
        return {row["message_id"] for row in rows}

    def extracted_replies_to(self, parent_message_ids: Container[str]) -> list[int]:
        """Return the ids of extracted messages replying to one of ``parent_message_ids``.

        The ``In-Reply-To`` value is normalised in Python by
        :func:`_parent_message_id`, the same parse :meth:`get_parent_body`
        applies, so the linkage is identical to the one extraction uses. A
        message naming its own Message-ID is not a reply and is never returned
        (:meth:`get_parent_body` resolves no parent for it). Ascending id order.
        One scan of the extracted replies: ``in_reply_to`` carries no index.
        """
        cursor = self.conn.execute(
            "SELECT m.id AS id, m.message_id AS message_id, m.in_reply_to AS in_reply_to "
            "FROM messages m JOIN extractions e ON e.message_id = m.id "
            "WHERE m.in_reply_to IS NOT NULL AND m.in_reply_to != '' ORDER BY m.id"
        )
        pks: list[int] = []
        for row in cursor:
            parent_mid = _parent_message_id(row["in_reply_to"])
            if parent_mid != row["message_id"] and parent_mid in parent_message_ids:
                pks.append(row["id"])
        return pks

    def extraction_version_counts(self) -> list[tuple[int | None, int]]:
        """Return ``(extraction_version, count)`` over every extraction row.

        Ordered oldest generation first, with NULL — which predates every
        generation — first of all. This is the whole input to the start-up
        staleness check.
        """
        rows = self.conn.execute(
            "SELECT extraction_version AS v, COUNT(*) AS n FROM extractions "
            "GROUP BY extraction_version"
        ).fetchall()
        return sorted(
            ((row["v"], row["n"]) for row in rows), key=lambda p: (p[0] is not None, p[0])
        )

    def set_extraction_version(
        self,
        extraction_id: int,
        extraction_version: int | None = None,
        pipeline_version: str | None = None,
    ) -> None:
        """Stamp an extraction's versions without touching the text.

        Used when a re-derivation proves the stored text is what the current
        routine produces: the row is up to date whatever its old stamps said.
        ``extraction_version`` defaults to the running routine's generation
        (:data:`~email_reply_extractor.extraction.EXTRACTION_VERSION`) and
        ``pipeline_version`` to the current package version
        (:data:`__version__`).
        """
        self.conn.execute(
            "UPDATE extractions SET extraction_version = ?, pipeline_version = ? WHERE id = ?",
            (
                extraction_version if extraction_version is not None else EXTRACTION_VERSION,
                pipeline_version if pipeline_version is not None else __version__,
                extraction_id,
            ),
        )
        self.conn.commit()

    def replace_extraction(
        self,
        extraction_id: int,
        *,
        extracted_text: str,
        method: str,
        status: str,
        char_count: int | None = None,
        pipeline_version: str | None = None,
        extraction_version: int | None = None,
    ) -> Extraction | None:
        """Rewrite an existing extraction in place; return the updated row.

        Updates the text, method, char count, status and both version stamps of
        an extraction the current routine has re-derived, and re-stamps the
        owning message's ``pipeline_version``. The row keeps its id, so an
        existing ``scores`` row survives — invalidating a score whose text has
        moved is the caller's decision (see :meth:`delete_score_for_extraction`).
        ``created_at`` is left alone: it records when the message was first
        extracted. Returns ``None`` if no such extraction exists.
        """
        if status not in EXTRACTION_STATUSES:
            raise ValueError(
                f"invalid extraction status {status!r}; expected one of {EXTRACTION_STATUSES}"
            )
        if self.get_extraction(extraction_id) is None:
            return None
        version = pipeline_version if pipeline_version is not None else __version__
        generation = extraction_version if extraction_version is not None else EXTRACTION_VERSION
        self.conn.execute(
            "UPDATE extractions SET extracted_text = ?, method = ?, char_count = ?, "
            "status = ?, pipeline_version = ?, extraction_version = ? WHERE id = ?",
            (
                extracted_text,
                method,
                len(extracted_text) if char_count is None else char_count,
                status,
                version,
                generation,
                extraction_id,
            ),
        )
        self.conn.execute(
            "UPDATE messages SET pipeline_version = ? "
            "WHERE id = (SELECT message_id FROM extractions WHERE id = ?)",
            (version, extraction_id),
        )
        self.conn.commit()
        return self.get_extraction(extraction_id)

    # -- scores ---------------------------------------------------------------

    def find_score_by_text_sha256(
        self, text_sha256: str, generation: str | None = None
    ) -> Score | None:
        """Return any existing score for identical text (the Pangram cache).

        Lets the scorer reuse a verdict for text it has already paid to classify,
        keyed on the SHA-256 of the extracted text — never score identical text
        twice.

        ``generation``, when given, restricts the match to verdicts produced by
        that detector generation: rows whose ``detector_version`` starts with
        ``"<generation>."`` (Pangram stamps "4.0", "3.3.2", …). Two generations
        disagree about the same text, so serving a Pangram 3 verdict for a
        Pangram 4 run — or the reverse — would record a result the selected
        detector never produced. A row with no recorded version matches no
        generation. The default ``None`` matches any row.
        """
        sql = "SELECT * FROM scores WHERE text_sha256 = ?"
        params: list[Any] = [text_sha256]
        if generation is not None:
            sql += " AND detector_version LIKE ? || '.%'"
            params.append(generation)
        row = self.conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return Score.from_row(row) if row else None

    def scores_outside_generation(self, generation: str) -> list[tuple[int, int]]:
        """Return ``(message_id, word_count)`` for scores of another generation.

        A score belongs to ``generation`` when its ``detector_version`` starts
        with ``"<generation>."``; every other row — including one with no
        recorded version — holds a verdict the selected detector would derive
        differently, and is what the dashboard offers to re-test. ``word_count``
        is the whitespace-split length of the scored extraction's text, which
        the caller turns into a spend estimate. Rows come back in message order.
        """
        rows = self.conn.execute(
            "SELECT e.message_id AS message_id, e.extracted_text AS extracted_text "
            "FROM scores s JOIN extractions e ON e.id = s.extraction_id "
            "WHERE s.detector_version IS NULL OR s.detector_version NOT LIKE ? || '.%' "
            "ORDER BY e.message_id",
            (generation,),
        )
        return [(row["message_id"], _word_count(row["extracted_text"])) for row in rows]

    def insert_score(
        self,
        *,
        extraction_id: int,
        text_sha256: str,
        fraction_ai: float | None = None,
        fraction_ai_assisted: float | None = None,
        fraction_human: float | None = None,
        label: str | None = None,
        detector_version: str | None = None,
        raw_response: Mapping[str, Any] | str | None = None,
        scored_at: str | None = None,
        pipeline_version: str | None = None,
    ) -> Score:
        """Store a Pangram verdict for ``extraction_id`` (one per extraction).

        ``raw_response`` may be a mapping (serialized to JSON text) or a
        pre-serialized JSON string.

        Re-stamps the owning message's ``pipeline_version`` to the current
        package version (:data:`__version__`); tests may pass an explicit value.
        """
        if isinstance(raw_response, Mapping):
            raw_json: str | None = json.dumps(raw_response)
        else:
            raw_json = raw_response
        cur = self.conn.execute(
            "INSERT INTO scores("
            "extraction_id, fraction_ai, fraction_ai_assisted, fraction_human, "
            "label, detector_version, raw_response, text_sha256, scored_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                extraction_id,
                fraction_ai,
                fraction_ai_assisted,
                fraction_human,
                label,
                detector_version,
                raw_json,
                text_sha256,
                scored_at or _utcnow_iso(),
            ),
        )
        self.conn.execute(
            "UPDATE messages SET pipeline_version = ? "
            "WHERE id = (SELECT message_id FROM extractions WHERE id = ?)",
            (pipeline_version if pipeline_version is not None else __version__, extraction_id),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM scores WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Score.from_row(row)

    def delete_score_for_extraction(self, extraction_id: int) -> bool:
        """Delete the score for ``extraction_id``; return whether a row was deleted.

        Used when re-extraction changes the text that was scored, which makes the
        stored verdict a verdict on text that no longer exists: dropping the row
        returns the extraction to the scoring queue. The verdict is discarded
        with it — if no other extraction shares that ``text_sha256``, the score
        cache loses the entry and a re-score is a paid Pangram call.
        """
        cur = self.conn.execute("DELETE FROM scores WHERE extraction_id = ?", (extraction_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def rekey_score_for_extraction(self, extraction_id: int, text_sha256: str) -> bool:
        """Re-key the score for ``extraction_id`` to a new cleaned-text hash.

        Used when re-extraction moves the scored text's bytes without changing
        its substance (see :func:`~mailing_list_ai_check.staleness.reextract`):
        the verdict is kept and adopted for the new text, so the score cache
        serves the new hash from now on. Returns whether a row was updated.
        """
        cur = self.conn.execute(
            "UPDATE scores SET text_sha256 = ? WHERE extraction_id = ?",
            (text_sha256, extraction_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # -- settings -------------------------------------------------------------

    def get_setting(self, key: str) -> str | None:
        """Return the stored value for ``key``, or ``None`` when it is not set.

        Callers supply their own default for an unset key; the table holds only
        settings a user has explicitly chosen.
        """
        row = self.conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        """Store ``value`` for ``key``, replacing any existing value.

        Validation of the value belongs to the caller, which owns the key's
        vocabulary; this layer stores whatever text it is given.
        """
        self.conn.execute(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)", (key, value)
        )
        self.conn.commit()

    # -- persons --------------------------------------------------------------

    def create_person(self, canonical_name: str) -> Person:
        """Create a person entity and return it."""
        cur = self.conn.execute("INSERT INTO persons(canonical_name) VALUES (?)", (canonical_name,))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM persons WHERE id = ?", (cur.lastrowid,)).fetchone()
        return Person.from_row(row)

    def get_person(self, person_id: int) -> Person | None:
        """Return the person with ``person_id``, or ``None``."""
        row = self.conn.execute("SELECT * FROM persons WHERE id = ?", (person_id,)).fetchone()
        return Person.from_row(row) if row else None

    def assign_address_to_person(self, address_id: int, person_id: int | None) -> None:
        """Link ``address_id`` to ``person_id`` (pass ``None`` to detach)."""
        self.conn.execute(
            "UPDATE addresses SET person_id = ? WHERE id = ?",
            (person_id, address_id),
        )
        self.conn.commit()

    def link_dmarc_rewrites(self) -> int:
        """Link every DMARC-rewritten address to the address it stands for.

        A list that rewrites a sender to survive their domain's DMARC policy
        re-sends the message from ``<local>=40<domain>@dmarc.ietf.org`` (see
        :func:`dmarc_rewrite_original`), so one person arrives under two
        addresses and is counted as two senders. Each pair whose original is
        also in the store is linked into one person.

        Returns the number of pairs newly linked. Idempotent: a pair already
        sharing a person is left alone, so this is safe to run after every pull.

        Two addresses already attached to *different* persons are left alone and
        logged. That grouping was made by hand, and silently merging two people
        on the strength of an address-shaped guess is not a repair this should
        make on its own.
        """
        rewrites = self.conn.execute(
            "SELECT * FROM addresses WHERE email LIKE ?",
            (f"%@{DMARC_REWRITE_DOMAIN}",),
        ).fetchall()

        linked = 0
        for row in rewrites:
            original_email = dmarc_rewrite_original(row["email"])
            if original_email is None:
                continue
            original = self.conn.execute(
                "SELECT * FROM addresses WHERE email = ?", (original_email,)
            ).fetchone()
            if original is None:
                continue

            rewrite_person, original_person = row["person_id"], original["person_id"]
            if rewrite_person is not None and original_person is not None:
                if rewrite_person != original_person:
                    log.warning(
                        "not linking %s to %s: already attached to different senders",
                        row["email"],
                        original_email,
                    )
                continue

            if original_person is not None:
                self.assign_address_to_person(row["id"], original_person)
            elif rewrite_person is not None:
                self.assign_address_to_person(original["id"], rewrite_person)
            else:
                # Name the new person after the original address, whose display
                # name is the one the sender chose; the rewrite's is set by the
                # list server.
                name = original["display_name"] or row["display_name"] or original_email
                person = self.create_person(name)
                self.assign_address_to_person(original["id"], person.id)
                self.assign_address_to_person(row["id"], person.id)
            linked += 1

        if linked:
            log.info("linked %d DMARC-rewritten address pair(s)", linked)
        return linked

    def addresses_for_person(self, person_id: int) -> list[Address]:
        """Return all addresses linked to ``person_id``."""
        rows = self.conn.execute(
            "SELECT * FROM addresses WHERE person_id = ? ORDER BY email",
            (person_id,),
        ).fetchall()
        return [Address.from_row(row) for row in rows]

    def suggest_person_merges(self) -> list[MergeSuggestion]:
        """Suggest person groupings by identical display name across addresses.

        Returns one :class:`MergeSuggestion` per non-empty ``display_name`` shared
        by more than one distinct email, for one-click confirmation in the UI.
        """
        rows = self.conn.execute(
            "SELECT display_name, "
            "GROUP_CONCAT(id) AS ids, GROUP_CONCAT(email) AS emails "
            "FROM addresses "
            "WHERE display_name IS NOT NULL AND display_name <> '' "
            "GROUP BY display_name "
            "HAVING COUNT(DISTINCT email) > 1 "
            "ORDER BY display_name"
        ).fetchall()
        suggestions: list[MergeSuggestion] = []
        for row in rows:
            ids = tuple(int(x) for x in row["ids"].split(","))
            emails = tuple(row["emails"].split(","))
            suggestions.append(
                MergeSuggestion(
                    display_name=row["display_name"],
                    address_ids=ids,
                    emails=emails,
                )
            )
        return suggestions

    def update_person_name(self, person_id: int, canonical_name: str) -> Person | None:
        """Rename ``person_id``; return the updated row, or ``None`` if absent."""
        self.conn.execute(
            "UPDATE persons SET canonical_name = ? WHERE id = ?",
            (canonical_name, person_id),
        )
        self.conn.commit()
        return self.get_person(person_id)

    def delete_person(self, person_id: int) -> bool:
        """Delete ``person_id``; return whether a row was removed.

        The ``addresses.person_id`` FK is ``ON DELETE SET NULL``, so a person's
        addresses are detached (not deleted) automatically.
        """
        cur = self.conn.execute("DELETE FROM persons WHERE id = ?", (person_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # -- dashboard: message detail helpers ------------------------------------

    def find_message_by_message_id(self, message_id: str) -> Message | None:
        """Return the lowest-id message with this RFC 5322 Message-ID, or ``None``.

        Message-IDs are unique only per list, so a Message-ID can in principle
        appear on more than one list; the earliest-stored match is returned. Used
        to resolve a reply's ``in_reply_to`` to a stored thread parent.
        """
        row = self.conn.execute(
            "SELECT * FROM messages WHERE message_id = ? ORDER BY id LIMIT 1",
            (message_id,),
        ).fetchone()
        return Message.from_row(row) if row else None

    def extraction_for_message(self, message_pk: int) -> Extraction | None:
        """Return the extraction for message ``message_pk`` (by FK), or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM extractions WHERE message_id = ?", (message_pk,)
        ).fetchone()
        return Extraction.from_row(row) if row else None

    def score_for_extraction(self, extraction_id: int) -> Score | None:
        """Return the score for ``extraction_id``, or ``None``."""
        row = self.conn.execute(
            "SELECT * FROM scores WHERE extraction_id = ?", (extraction_id,)
        ).fetchone()
        return Score.from_row(row) if row else None

    # -- dashboard: entity listings -------------------------------------------

    def list_rows(self) -> list[dict[str, Any]]:
        """Every list with its message count, scored count and label mix (for /api/lists).

        Each row carries the base list columns plus ``message_count``,
        ``earliest_message_at`` and three scoring aggregates powering the
        lists-index mix bars: ``scored_count`` (messages on the list that have a
        Pangram score), ``label_counts`` (a ``{label: count}`` dict of scored
        messages per label, null labels omitted, empty when nothing on the list is
        scored) and ``too_short_count`` (messages whose extraction was gated under
        the reliability floor, so never scored — the mix bar's trailing grey
        segment). Both come from extra aggregate queries merged in Python,
        joining ``messages → extractions → scores`` (the chain of
        ``_MESSAGE_FROM``, driven from ``messages``).

        ``earliest_message_at`` is the oldest ``messages.date`` stored for the list
        — the message's own ``Date`` header normalised to a UTC ISO-8601 string on
        insert, not the local ``fetched_at`` stamp — so the uniform format makes
        the lexicographic ``MIN`` chronological. It is ``None`` for a list with no
        stored messages, and for one whose messages all lack a usable date (blank
        dates are excluded rather than sorting ahead of real ones). Distinct from
        ``last_message_at``, which is the newest message on the *server*.
        """
        rows = self.conn.execute(
            "SELECT l.id, l.name, l.folder, l.last_synced_at, l.removed_from_server_at, "
            "l.last_message_at, COUNT(m.id) AS message_count, "
            "MIN(NULLIF(m.date, '')) AS earliest_message_at "
            "FROM lists l LEFT JOIN messages m ON m.list_id = l.id "
            "GROUP BY l.id ORDER BY l.name"
        ).fetchall()
        result = [dict(row) for row in rows]

        # CROSS JOIN pins the join order: driven from messages, every hop is a
        # covering index (idx_messages_list_id, the extractions message_id
        # autoindex, idx_scores_extraction_label). Left to itself the planner
        # drives from scores and fetches each wide extractions/messages row
        # just to read message_id/list_id — ~3.3 s instead of ~0.1 s here.
        mix = self.conn.execute(
            "SELECT m.list_id AS list_id, s.label AS label, COUNT(*) AS count "
            "FROM messages m "
            "CROSS JOIN extractions e ON e.message_id = m.id "
            "CROSS JOIN scores s ON s.extraction_id = e.id "
            "GROUP BY m.list_id, s.label"
        ).fetchall()
        scored_by_list: dict[int, int] = {}
        labels_by_list: dict[int, dict[str, int]] = {}
        for row in mix:
            scored_by_list[row["list_id"]] = scored_by_list.get(row["list_id"], 0) + row["count"]
            if row["label"] is not None:
                labels_by_list.setdefault(row["list_id"], {})[row["label"]] = row["count"]

        # Same join-order pin as the mix query: from messages through
        # idx_extractions_message_status, never touching a table row.
        gated = self.conn.execute(
            "SELECT m.list_id AS list_id, COUNT(*) AS count "
            "FROM messages m "
            "CROSS JOIN extractions e ON e.message_id = m.id "
            "WHERE e.status = 'too_short' "
            "GROUP BY m.list_id"
        ).fetchall()
        too_short_by_list = {row["list_id"]: row["count"] for row in gated}

        for item in result:
            item["scored_count"] = scored_by_list.get(item["id"], 0)
            item["label_counts"] = labels_by_list.get(item["id"], {})
            item["too_short_count"] = too_short_by_list.get(item["id"], 0)
        return result

    def address_rows(self, q: str | None = None) -> list[dict[str, Any]]:
        """Every address with person + message count; ``q`` filters email/name.

        The substring match is case-insensitive over both the email and the
        display name.
        """
        sql = (
            "SELECT a.id, a.email, a.display_name, a.person_id, "
            "p.canonical_name AS person_name, COUNT(m.id) AS message_count "
            "FROM addresses a "
            "LEFT JOIN persons p ON p.id = a.person_id "
            "LEFT JOIN messages m ON m.address_id = a.id "
        )
        params: list[Any] = []
        if q:
            sql += "WHERE a.email LIKE ? OR a.display_name LIKE ? "
            like = f"%{q}%"
            params = [like, like]
        sql += "GROUP BY a.id ORDER BY a.email"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def person_rows(self) -> list[dict[str, Any]]:
        """Every person with its addresses and total message count (for /api/persons)."""
        persons = self.conn.execute(
            "SELECT p.id, p.canonical_name, COUNT(m.id) AS message_count "
            "FROM persons p "
            "LEFT JOIN addresses a ON a.person_id = p.id "
            "LEFT JOIN messages m ON m.address_id = a.id "
            "GROUP BY p.id ORDER BY p.canonical_name"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for person in persons:
            addrs = self.conn.execute(
                "SELECT id, email, display_name FROM addresses WHERE person_id = ? ORDER BY email",
                (person["id"],),
            ).fetchall()
            result.append(
                {
                    "id": person["id"],
                    "canonical_name": person["canonical_name"],
                    "message_count": person["message_count"],
                    "addresses": [dict(a) for a in addrs],
                }
            )
        return result

    def sender_rows(
        self,
        *,
        q: str | None = None,
        sort: str = "count",
        order: str = "desc",
        page: int = 1,
        per_page: int = DEFAULT_PER_PAGE,
        list_name: str | None = None,
        include_excluded: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return ``(rows, total)`` of senders for the Senders pane (/api/senders).

        A "sender" is either a **person** (a linked group of addresses) or an
        **unlinked address** (``addresses.person_id`` is NULL). Addresses attached
        to a person never appear as their own entry — they roll up into the person.

        The heavy lifting is two aggregate queries at ``(entity, label)``
        granularity — one over persons, one over unlinked addresses — mirroring
        the ``messages → extractions → scores`` join chain of ``_MESSAGE_FROM``.
        Each ``COUNT(m.id)`` counts messages (the extraction/score joins never
        multiply rows, both being UNIQUE), so summing the per-label counts gives
        the entity's ``message_count`` and the non-null label groups give its
        ``label_counts``. Each query also counts the sender's ``too_short``
        extractions — messages gated under the reliability floor, so never scored
        — summed into ``too_short_count`` for the mix bar's trailing grey segment.
        Senders with zero messages are included. The merge/filter/sort/paginate
        then happens in Python over the small result:

        - ``q`` — case-insensitive substring over the name or ANY email;
        - ``sort`` — ``"count"`` (by ``message_count``), ``"ai"`` (by the mix's
          count of ``AI`` + ``Mixed`` messages, see :func:`ai_count`, ties
          broken by ``message_count`` descending whichever direction is asked
          for), both with a final name asc for a stable order, or ``"name"``
          (case-insensitive);
        - ``order`` — ``"asc"``/``"desc"``;
        - ``page``/``per_page`` — 1-based, ``per_page`` clamped to
          ``[1, MAX_PER_PAGE]``.

        ``total`` is the full match count before pagination.

        An unlinked address entry is named by its stored ``display_name``, except
        when it has presented :data:`MULTI_NAME_ADDRESS_THRESHOLD` or more
        different ``From`` names, in which case the address itself is the name
        (see :func:`_address_display_name`). ``distinct_from_names`` reports the
        count the rule was applied to. A person's ``canonical_name`` is set by
        hand and is never overridden this way.

        Each entry also carries ``excluded_count`` (its messages classified
        auto-generated at fetch time, which never reach extraction and so are
        never scored) and the derived ``excluded_from_scoring``: true when the
        sender has messages and *every* one of them is excluded, i.e. the sender
        can never contribute a verdict. Those senders are dropped unless
        ``include_excluded`` is true — the Senders pane's "Show all" switch —
        because they are noise in a pane whose purpose is comparing detection
        mixes. A sender with even one scoreable message is never dropped, and
        neither is one with no messages at all, having nothing to exclude.

        ``shared_address`` is true for an unlinked address listed in
        :data:`SHARED_SENDER_ADDRESSES` (one address carrying many people's
        mail). Those entries are dropped on the same terms, unless
        ``include_excluded`` is true. A person is never flagged this way.

        When ``list_name`` is given, message joins are restricted (via an extra
        ``AND m.list_id = ?`` inside the ``messages`` ON clause) so that
        ``message_count``, ``label_counts`` and ``too_short_count`` reflect only
        that list, and senders
        who never posted to it (``message_count == 0``) are dropped from the
        result. An unknown ``list_name`` yields ``([], 0)``. When ``None`` (the
        default), all senders are included, zero-message ones among them.
        """
        list_filter = ""
        list_params: list[Any] = []
        if list_name is not None:
            list_row = self.conn.execute(
                "SELECT id FROM lists WHERE name = ?", (list_name,)
            ).fetchone()
            if list_row is None:
                return ([], 0)
            list_filter = " AND m.list_id = ?"
            list_params = [list_row["id"]]

        person_mix = self.conn.execute(
            "SELECT p.id AS person_id, p.canonical_name AS name, "
            "s.label AS label, COUNT(m.id) AS msg_count, "
            "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short_count, "
            "COUNT(CASE WHEN m.auto_generated IS NOT NULL THEN 1 END) AS excluded_count "
            "FROM persons p "
            "LEFT JOIN addresses a ON a.person_id = p.id "
            "LEFT JOIN messages m ON m.address_id = a.id" + list_filter + " "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "GROUP BY p.id, s.label",
            list_params,
        ).fetchall()
        person_addrs = self.conn.execute(
            "SELECT person_id, id, email FROM addresses WHERE person_id IS NOT NULL ORDER BY email"
        ).fetchall()
        unlinked_mix = self.conn.execute(
            "SELECT a.id AS address_id, a.email AS email, a.display_name AS display_name, "
            "s.label AS label, COUNT(m.id) AS msg_count, "
            "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short_count, "
            "COUNT(CASE WHEN m.auto_generated IS NOT NULL THEN 1 END) AS excluded_count "
            "FROM addresses a "
            "LEFT JOIN messages m ON m.address_id = a.id" + list_filter + " "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "WHERE a.person_id IS NULL "
            "GROUP BY a.id, s.label",
            list_params,
        ).fetchall()
        # How many different From names each address has presented. Counted over
        # every message, never scoped to list_name, so an address's displayed
        # name does not change as the list filter moves.
        name_counts = {
            row["address_id"]: row["name_count"]
            for row in self.conn.execute(
                "SELECT address_id, COUNT(DISTINCT from_name) AS name_count "
                "FROM messages WHERE address_id IS NOT NULL AND from_name IS NOT NULL "
                "GROUP BY address_id"
            )
        }

        persons: dict[int, dict[str, Any]] = {}
        for row in person_mix:
            entry = persons.setdefault(
                row["person_id"],
                {
                    "type": "person",
                    "person_id": row["person_id"],
                    "name": row["name"],
                    "emails": [],
                    "address_ids": [],
                    "message_count": 0,
                    "label_counts": {},
                    "too_short_count": 0,
                    "excluded_count": 0,
                },
            )
            entry["message_count"] += row["msg_count"]
            entry["too_short_count"] += row["too_short_count"]
            entry["excluded_count"] += row["excluded_count"]
            if row["label"] is not None:
                entry["label_counts"][row["label"]] = row["msg_count"]
        for row in person_addrs:
            entry = persons.get(row["person_id"])
            if entry is not None:  # ordered by email, so emails come out sorted
                entry["emails"].append(row["email"])
                entry["address_ids"].append(row["id"])

        addresses: dict[int, dict[str, Any]] = {}
        for row in unlinked_mix:
            entry = addresses.setdefault(
                row["address_id"],
                {
                    "type": "address",
                    "address_id": row["address_id"],
                    "name": _address_display_name(
                        row["email"], row["display_name"], name_counts.get(row["address_id"], 0)
                    ),
                    "distinct_from_names": name_counts.get(row["address_id"], 0),
                    # Whether the many-names rule chose the label, so the client
                    # need not know the threshold to explain the substitution.
                    "named_by_address": name_counts.get(row["address_id"], 0)
                    >= MULTI_NAME_ADDRESS_THRESHOLD,
                    "emails": [row["email"]],
                    "message_count": 0,
                    "label_counts": {},
                    "too_short_count": 0,
                    "excluded_count": 0,
                },
            )
            entry["message_count"] += row["msg_count"]
            entry["too_short_count"] += row["too_short_count"]
            entry["excluded_count"] += row["excluded_count"]
            if row["label"] is not None:
                entry["label_counts"][row["label"]] = row["msg_count"]

        entries = [*persons.values(), *addresses.values()]
        for entry in entries:
            entry["excluded_from_scoring"] = (
                entry["message_count"] > 0 and entry["excluded_count"] == entry["message_count"]
            )
            entry["shared_address"] = (
                entry["type"] == "address" and entry["emails"][0].lower() in SHARED_SENDER_ADDRESSES
            )

        if list_name is not None:
            # Scoped to a list: only senders who actually posted to it.
            entries = [e for e in entries if e["message_count"] > 0]

        if not include_excluded:
            entries = [
                e for e in entries if not e["excluded_from_scoring"] and not e["shared_address"]
            ]

        if q:
            needle = q.strip().lower()
            entries = [
                e
                for e in entries
                if needle in e["name"].lower()
                or any(needle in email.lower() for email in e["emails"])
            ]

        # Python's sort is stable: apply the secondary key first, primary last.
        entries.sort(key=lambda e: e["name"].lower())
        if sort == "name":
            if order == "desc":
                entries.reverse()
        elif sort == "ai":
            # Ties on the count are broken by volume, most messages first, in
            # both directions: many senders share a value (0 above all), and
            # the busier of two such senders is the more informative row either
            # way round. The pass runs before the count pass because Python's
            # sort is stable, so its order survives wherever the count compares
            # equal; the name pass above stays the last resort.
            entries.sort(key=lambda e: e["message_count"], reverse=True)
            entries.sort(
                key=lambda e: ai_count(e["label_counts"]),
                reverse=(order != "asc"),
            )
        else:  # count
            entries.sort(key=lambda e: e["message_count"], reverse=(order != "asc"))

        total = len(entries)
        per_page = max(1, min(per_page, MAX_PER_PAGE))
        page = max(1, page)
        offset = (page - 1) * per_page
        return entries[offset : offset + per_page], total

    def db_size_bytes(self) -> int:
        """Size in bytes of the SQLite file backing this store, or 0 if none.

        In-memory stores (``path == ":memory:"``) and any path that is not present
        on disk report 0 rather than raising, so callers (e.g. the summary
        endpoint under an in-memory test store) get a number unconditionally. The
        main database file is measured; WAL/SHM sidecars are not counted.
        """
        if self.path == ":memory:":
            return 0
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0

    # -- dashboard: filtered message query + summary --------------------------

    def query_messages(self, filters: MessageFilters) -> tuple[list[dict[str, Any]], int]:
        """Return ``(rows, total)`` for ``filters`` — the explorer's data source.

        ``rows`` is the requested page (each a dict joining messages + addresses +
        persons + extractions + scores); ``total`` is the full match count before
        pagination. ``per_page`` is clamped to ``[1, MAX_PER_PAGE]`` and ``page``
        to ``>= 1``. ``sort`` names a key of :data:`SORT_COLUMNS` (``date``,
        ``fraction_ai`` or ``timing_cpm``) and an unknown one falls back to
        ``date``; ``order`` is ``asc`` only when explicitly ``"asc"``, else
        ``desc``. A stable secondary sort on ``m.id`` makes pagination
        deterministic.

        Each row also carries ``timing_cpm``: the stored chars/minute rate the
        ``timing`` band was derived from, and ``None`` wherever ``timing`` is
        NULL (see :meth:`recompute_timing`). Sorting on ``timing_cpm`` puts the
        rows without a rate last whichever direction is asked for.
        """
        where, params = _build_message_where(filters)
        total = self.conn.execute(
            "SELECT COUNT(*) AS c" + _MESSAGE_FROM + where, params
        ).fetchone()["c"]

        sort_col = SORT_COLUMNS.get(filters.sort, SORT_COLUMNS["date"])
        order = "ASC" if str(filters.order).lower() == "asc" else "DESC"
        per_page = max(1, min(filters.per_page, MAX_PER_PAGE))
        page = max(1, filters.page)
        offset = (page - 1) * per_page

        sql = (
            "SELECT"
            + _MESSAGE_COLUMNS
            + _MESSAGE_FROM
            + where
            + f" ORDER BY {sort_col} {order}, m.id {order} LIMIT ? OFFSET ?"
        )
        page_rows = [dict(row) for row in self.conn.execute(sql, [*params, per_page, offset])]
        return page_rows, total

    def summary(self, filters: MessageFilters) -> dict[str, Any]:
        """Aggregate the filtered message set for the overview page.

        Honours the same ``filters`` as :meth:`query_messages` (pagination/sort
        are ignored). ``extracted`` counts ``status='ok'`` extractions; ``scored``
        counts messages with a Pangram score; ``too_short`` counts gated ones.
        ``flagged`` means a label in :data:`FLAGGED_LABELS`. Each ``by_list`` row
        repeats its own gated count as ``too_short_count`` beside its
        ``label_counts``, so a per-list mix bar can draw the same trailing grey
        segment the whole-selection bar draws from ``too_short``.
        """
        where, params = _build_message_where(filters)
        base = _MESSAGE_FROM + where

        totals = self.conn.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(CASE WHEN e.status = 'ok' THEN 1 END) AS extracted, "
            "COUNT(s.id) AS scored, "
            "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short, "
            "AVG(s.fraction_ai) AS avg_fraction_ai" + base,
            params,
        ).fetchone()

        label_rows = self.conn.execute(
            "SELECT s.label AS label, COUNT(*) AS count" + base + " GROUP BY s.label",
            params,
        ).fetchall()
        label_distribution = {
            row["label"]: row["count"] for row in label_rows if row["label"] is not None
        }

        timing_rows = self.conn.execute(
            "SELECT m.timing AS timing, COUNT(*) AS count" + base + " GROUP BY m.timing",
            params,
        ).fetchall()
        timing_distribution = {
            row["timing"]: row["count"] for row in timing_rows if row["timing"] is not None
        }

        list_label_rows = self.conn.execute(
            "SELECT l.name AS list, s.label AS label, COUNT(*) AS count"
            + base
            + " GROUP BY l.id, s.label",
            params,
        ).fetchall()
        list_label_counts: dict[str, dict[str, int]] = {}
        for row in list_label_rows:
            if row["label"] is not None:
                list_label_counts.setdefault(row["list"], {})[row["label"]] = row["count"]

        by_list = [
            dict(row)
            for row in self.conn.execute(
                "SELECT l.name AS list, COUNT(*) AS count, AVG(s.fraction_ai) AS avg_fraction_ai, "
                "COUNT(CASE WHEN e.status = 'too_short' THEN 1 END) AS too_short_count"
                + base
                + " GROUP BY l.id ORDER BY count DESC, l.name LIMIT 20",
                params,
            ).fetchall()
        ]
        for item in by_list:
            item["label_counts"] = list_label_counts.get(item["list"], {})

        addr_rows = self.conn.execute(
            "SELECT a.id AS address_id, a.email AS email, a.display_name AS display_name, "
            "a.person_id AS person_id, p.canonical_name AS person_name, "
            "COUNT(s.id) AS scored_count, "
            f"COUNT(CASE WHEN s.label IN {_FLAGGED_IN} THEN 1 END) AS flagged_count, "
            "AVG(s.fraction_ai) AS avg_fraction_ai"
            + base
            + " GROUP BY a.id HAVING scored_count > 0 "
            "ORDER BY scored_count DESC, flagged_count DESC, a.email LIMIT 20",
            params,
        ).fetchall()
        by_address = []
        for row in addr_rows:
            item = dict(row)
            scored = item["scored_count"] or 0
            item["flagged_share"] = (item["flagged_count"] / scored) if scored else 0.0
            by_address.append(item)

        month_rows = self.conn.execute(
            "SELECT substr(m.date, 1, 7) AS month, COUNT(*) AS count, "
            "AVG(s.fraction_ai) AS avg_fraction_ai, "
            f"COUNT(CASE WHEN s.label IN {_FLAGGED_IN} THEN 1 END) AS flagged_count"
            + base
            + " GROUP BY month ORDER BY month",
            params,
        ).fetchall()
        by_month = [dict(row) for row in month_rows if row["month"] is not None]

        return {
            "total": totals["total"],
            "extracted": totals["extracted"],
            "scored": totals["scored"],
            "too_short": totals["too_short"],
            "avg_fraction_ai": totals["avg_fraction_ai"],
            "label_distribution": label_distribution,
            "timing_distribution": timing_distribution,
            "by_list": by_list,
            "by_address": by_address,
            "by_month": by_month,
            "db_size_bytes": self.db_size_bytes(),
        }

    def message_timeline(self, filters: MessageFilters) -> dict[str, Any]:
        """The filtered message set as one slim timeline (for the messages pane's rug).

        Honours the same ``filters`` as :meth:`query_messages` and
        :meth:`summary` (pagination/sort are ignored), so the rug always shows
        exactly the set the table pages through. No row cap applies: the plot
        adapts to any volume client-side.

        Returns ``{"start": s, "end": e, "total": n, "undated": u,
        "points": [...]}`` — ``start``/``end`` the epoch seconds of the
        earliest and latest dated match (``None`` when there is none), and each
        point ``[id, t, bucket]`` as :meth:`list_timelines` serves them,
        ordered by ``(date, id)`` ascending. Messages whose ``date`` is missing
        or unparseable cannot be placed on a time axis; they are counted in
        ``undated`` and carried by no point.
        """
        where, params = _build_message_where(filters)
        cursor = self.conn.execute(
            "SELECT m.id AS id, m.date AS date, e.status AS status, s.label AS label"
            + _MESSAGE_FROM
            + where
            + " ORDER BY m.date, m.id",
            params,
        )

        points: list[list[Any]] = []
        total = 0
        undated = 0
        start: int | None = None
        end: int | None = None
        for row in cursor:
            total += 1
            t = _epoch_seconds(row["date"])
            if t is None:
                undated += 1
                continue
            start = t if start is None or t < start else start
            end = t if end is None or t > end else end
            bucket = (
                3 if row["status"] == "too_short" else _TIMELINE_LABEL_BUCKETS.get(row["label"], 4)
            )
            points.append([row["id"], t, bucket])

        return {"start": start, "end": end, "total": total, "undated": undated, "points": points}

    # -- dashboard: sender reply rugs ------------------------------------------

    def _rug_rows(self, message_pks: Sequence[int], limit: int | None) -> list[dict[str, Any]]:
        """The newest ``limit`` of ``message_pks`` as rug rows, newest first.

        ``limit`` ``None`` keeps every row. Ordered by ``(date, id)``
        descending, blank/NULL dates sorting oldest. The ordering is applied in
        Python because ``message_pks`` can exceed one ``IN (...)`` batch (see
        :func:`_in_chunks`).
        """
        rows: list[dict[str, Any]] = []
        for chunk in _in_chunks(message_pks):
            placeholders = ", ".join("?" * len(chunk))
            rows.extend(
                dict(row)
                for row in self.conn.execute(
                    "SELECT" + _RUG_COLUMNS + _MESSAGE_FROM + f" WHERE m.id IN ({placeholders})",
                    list(chunk),
                ).fetchall()
            )
        rows.sort(key=lambda row: (row["date"] or "", row["id"]), reverse=True)
        return rows[:limit]

    def _parent_pks(self, list_id: int, parent_message_ids: Sequence[str]) -> list[int]:
        """Resolve parent Message-IDs to the stored messages they name.

        Uses the same linkage as the reply-timing analysis (see
        :meth:`recompute_timings`): when the same Message-ID is stored on several
        lists the copy on ``list_id`` — the replying message's own list — wins,
        otherwise the lowest ``id``. Message-IDs with no stored copy are dropped.
        Served by ``idx_messages_message_id``.
        """
        chosen: dict[str, tuple[int, bool]] = {}
        for chunk in _in_chunks(parent_message_ids):
            placeholders = ", ".join("?" * len(chunk))
            rows = self.conn.execute(
                "SELECT id, message_id, list_id FROM messages "
                f"WHERE message_id IN ({placeholders}) ORDER BY id",
                list(chunk),
            ).fetchall()
            for row in rows:
                on_list = row["list_id"] == list_id
                current = chosen.get(row["message_id"])
                # Rows arrive in id order, so the first one seen is the lowest-id
                # fallback; a copy on the reply's own list displaces it.
                if current is None or (on_list and not current[1]):
                    chosen[row["message_id"]] = (row["id"], on_list)
        return [pk for pk, _ in chosen.values()]

    def _replied_to_pks(self, list_id: int, is_sender: str, params: Sequence[Any]) -> list[int]:
        """Parent messages of the sender's replies on ``list_id`` (unordered pks)."""
        rows = self.conn.execute(
            "SELECT m.message_id AS message_id, m.in_reply_to AS in_reply_to FROM messages m "
            "LEFT JOIN addresses a ON a.id = m.address_id "
            f"WHERE m.list_id = ? AND {is_sender} "
            "AND m.in_reply_to IS NOT NULL AND m.in_reply_to != ''",
            [list_id, *params],
        ).fetchall()
        parent_mids: set[str] = set()
        for row in rows:
            parent_mid = _parent_message_id(row["in_reply_to"])
            # A message naming itself as its own parent is not a reply (the same
            # guard the timing analysis applies).
            if parent_mid and parent_mid != row["message_id"]:
                parent_mids.add(parent_mid)
        return self._parent_pks(list_id, sorted(parent_mids))

    def _reply_from_pks(
        self,
        list_id: int,
        is_sender: str,
        is_not_sender: str,
        params: Sequence[Any],
        limit: int | None,
    ) -> list[int]:
        """Other people's replies on ``list_id`` to the sender's messages there.

        Newest first, stopping at ``limit`` (``None`` collects them all). The
        sender's own Message-IDs on the
        list are gathered first (an indexed lookup), then the list's replies are
        streamed newest-first and matched on the normalised ``In-Reply-To``. That
        candidate scan is the one unindexed step here: ``in_reply_to`` carries no
        index, so a list with very many messages is walked until ``limit``
        matches are found. Normalising in Python rather than SQL is deliberate —
        it keeps the linkage byte-identical to the timing analysis's.
        """
        own_mids = {
            row["message_id"]
            for row in self.conn.execute(
                "SELECT m.message_id AS message_id FROM messages m "
                "LEFT JOIN addresses a ON a.id = m.address_id "
                f"WHERE m.list_id = ? AND {is_sender}",
                [list_id, *params],
            ).fetchall()
        }
        if not own_mids:
            return []
        cursor = self.conn.execute(
            "SELECT m.id AS id, m.in_reply_to AS in_reply_to FROM messages m "
            "LEFT JOIN addresses a ON a.id = m.address_id "
            "WHERE m.list_id = ? AND m.in_reply_to IS NOT NULL AND m.in_reply_to != '' "
            f"AND {is_not_sender} ORDER BY m.date DESC, m.id DESC",
            [list_id, *params],
        )
        pks: list[int] = []
        for row in cursor:
            if _parent_message_id(row["in_reply_to"]) in own_mids:
                pks.append(row["id"])
                if limit is not None and len(pks) >= limit:
                    break
        return pks

    def sender_reply_rugs(
        self,
        *,
        person_id: int | None = None,
        address: str | None = None,
        limit: int | None = None,
        max_lists: int = REPLY_RUG_MAX_LISTS,
    ) -> list[dict[str, Any]]:
        """Reply rug data for one sender, per list (for /api/senders/reply-rugs).

        Pass exactly one of ``person_id`` (covering every address linked to that
        person) or ``address`` (that address alone) — the same two sender scopes
        the ``person``/``address`` message filters define. Returns one entry per
        list the sender posted to, ordered by their message count on it
        descending then list name, capped at ``max_lists`` so the entries line up
        with :meth:`summary`'s ``by_list`` rows:

        ``{"list": <name>, "replied_to": [...], "reply_from": [...]}``

        - ``replied_to`` — the messages the sender's replies on that list point
          at, i.e. the parents named by their ``In-Reply-To``. Parents are
          resolved exactly as the reply-timing analysis resolves them (see
          :meth:`_parent_pks`); unstored parents and self-references drop out.
        - ``reply_from`` — replies **by other senders** on that list whose parent
          is one of the sender's own messages there.

        Both lists hold every matching rug row (see :data:`_RUG_COLUMNS`) when
        ``limit`` is ``None``, or at most ``limit`` rows otherwise, newest first
        by ``(date, id)``, and are empty when nothing matches.
        """
        is_sender, is_not_sender, params = _sender_scope(person_id, address)
        list_rows = self.conn.execute(
            "SELECT m.list_id AS list_id, l.name AS list, COUNT(*) AS count FROM messages m "
            "JOIN lists l ON l.id = m.list_id "
            "LEFT JOIN addresses a ON a.id = m.address_id "
            f"WHERE {is_sender} "
            "GROUP BY m.list_id ORDER BY count DESC, l.name LIMIT ?",
            [*params, max_lists],
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in list_rows:
            list_id = row["list_id"]
            result.append(
                {
                    "list": row["list"],
                    "replied_to": self._rug_rows(
                        self._replied_to_pks(list_id, is_sender, params), limit
                    ),
                    "reply_from": self._rug_rows(
                        self._reply_from_pks(list_id, is_sender, is_not_sender, params, limit),
                        limit,
                    ),
                }
            )
        return result

    # -- dashboard: list timelines ----------------------------------------------

    def list_timelines(self, list_name: str | None = None) -> dict[str, Any]:
        """Slim per-list message timelines (for ``GET /api/lists/timelines``).

        Feeds the dashboard's adaptive rug plots, which accept any message
        volume, so no row cap applies. With ``list_name`` the result covers that
        list alone and each point carries the message's subject (for per-bar
        tooltips); without it, every list that has at least one message is
        covered, ordered by message count descending then name, and subjects
        are omitted to keep the payload small.

        Returns ``{"start": s, "end": e, "lists": [...]}`` where ``start`` /
        ``end`` are the epoch seconds of the earliest and latest dated message
        across the result (``None`` when there is none), and each list entry is
        ``{"list": <name>, "total": n, "undated": u, "points": [...]}``. A point
        is ``[id, t, bucket]`` — or ``[id, t, bucket, subject]`` with
        ``list_name`` — ordered by ``(date, id)`` ascending, with ``t`` in epoch
        seconds and ``bucket`` one of :data:`TIMELINE_BUCKETS` by index:
        0 Human, 1 Mixed, 2 AI, 3 too_short (gated under the reliability
        floor), 4 unscored. Messages whose ``date`` is missing or unparseable
        cannot be placed on a time axis; they are counted in ``undated`` and
        carried by no point. An unknown ``list_name`` yields an empty ``lists``.
        """
        with_subject = list_name is not None
        columns = "l.name AS list, m.id AS id, m.date AS date, e.status AS status, s.label AS label"
        if with_subject:
            columns += ", m.subject AS subject"
        where = " WHERE l.name = ?" if list_name is not None else ""
        params = [list_name] if list_name is not None else []
        cursor = self.conn.execute(
            "SELECT " + columns + " FROM messages m "
            "JOIN lists l ON l.id = m.list_id "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id" + where + " ORDER BY m.date, m.id",
            params,
        )

        by_list: dict[str, dict[str, Any]] = {}
        start: int | None = None
        end: int | None = None
        for row in cursor:
            entry = by_list.setdefault(
                row["list"], {"list": row["list"], "total": 0, "undated": 0, "points": []}
            )
            entry["total"] += 1
            t = _epoch_seconds(row["date"])
            if t is None:
                entry["undated"] += 1
                continue
            start = t if start is None or t < start else start
            end = t if end is None or t > end else end
            bucket = (
                3 if row["status"] == "too_short" else _TIMELINE_LABEL_BUCKETS.get(row["label"], 4)
            )
            point: list[Any] = [row["id"], t, bucket]
            if with_subject:
                point.append(row["subject"])
            entry["points"].append(point)

        lists = sorted(by_list.values(), key=lambda e: (-e["total"], e["list"]))
        return {"start": start, "end": end, "lists": lists}

    # -- dashboard: sender cumulative timelines ----------------------------------

    def sender_timelines(
        self,
        *,
        person_ids: Sequence[int] = (),
        addresses: Sequence[str] = (),
        list_name: str | None = None,
    ) -> dict[str, Any]:
        """Slim per-sender message timelines (for ``GET /api/senders/timelines``).

        Feeds the Senders pane's per-sender rug plots: for each requested
        sender, every dated message as an ``[id, t, bucket]`` point — the shape
        :meth:`list_timelines` serves, ``t`` in epoch seconds and ``bucket`` an
        index into :data:`TIMELINE_BUCKETS` — ordered by ``(date, id)``
        ascending. Messages whose ``date`` is missing or unparseable cannot be
        placed on a time axis; they are counted in the entry's ``undated`` and
        carried by no point.

        ``person_ids`` selects persons (each covering every address linked to
        it); ``addresses`` selects addresses by email (each covering itself
        alone, lower-cased as the ``address`` message filter is). With
        ``list_name`` both the points and the domain are restricted to that
        list; an unknown ``list_name`` yields empty maps and a null domain.

        Returns ``{"start": s, "end": e, "persons": {...}, "addresses": {...}}``.
        ``start``/``end`` are the epoch seconds of the scope's earliest and
        latest dated message — the *whole* list or corpus, not just the
        requested senders — so every rug in a table shares one x-domain;
        either is ``None`` when the boundary date is absent or unparseable (the
        client then falls back to the points' own extent). ``persons`` is keyed
        by person id as a string, ``addresses`` by lower-cased email; each value
        is ``{"points": [[id, t, bucket], ...], "undated": u}``. Requested
        senders with no matching messages still appear, with empty points.
        """
        list_filter = ""
        list_params: list[Any] = []
        if list_name is not None:
            list_row = self.conn.execute(
                "SELECT id FROM lists WHERE name = ?", (list_name,)
            ).fetchone()
            if list_row is None:
                return {"start": None, "end": None, "persons": {}, "addresses": {}}
            list_filter = " AND m.list_id = ?"
            list_params = [list_row["id"]]

        # The shared x-domain: the scope's full dated extent. Stored dates are
        # ISO-8601 UTC, so lexical MIN/MAX match chronological order; a stray
        # unparseable extreme yields a null bound rather than a wrong one.
        domain = self.conn.execute(
            "SELECT MIN(m.date) AS lo, MAX(m.date) AS hi FROM messages m "
            "WHERE m.date IS NOT NULL" + list_filter,
            list_params,
        ).fetchone()
        start = _epoch_seconds(domain["lo"])
        end = _epoch_seconds(domain["hi"])

        def _collect(column: str, keys: Sequence[Any]) -> dict[str, Any]:
            entries: dict[str, dict[str, Any]] = {
                str(k): {"points": [], "undated": 0} for k in keys
            }
            if not keys:
                return entries
            marks = ",".join("?" for _ in keys)
            cursor = self.conn.execute(
                f"SELECT a.{column} AS key, m.id AS id, m.date AS date, "
                "e.status AS status, s.label AS label "
                "FROM messages m "
                "JOIN addresses a ON a.id = m.address_id "
                "LEFT JOIN extractions e ON e.message_id = m.id "
                "LEFT JOIN scores s ON s.extraction_id = e.id "
                f"WHERE a.{column} IN ({marks}){list_filter} ORDER BY m.date, m.id",
                [*keys, *list_params],
            )
            for row in cursor:
                entry = entries[str(row["key"])]
                t = _epoch_seconds(row["date"])
                if t is None:
                    entry["undated"] += 1
                    continue
                bucket = (
                    3
                    if row["status"] == "too_short"
                    else _TIMELINE_LABEL_BUCKETS.get(row["label"], 4)
                )
                entry["points"].append([row["id"], t, bucket])
            return entries

        persons = _collect("person_id", list(person_ids))
        address_entries = _collect("email", [a.strip().lower() for a in addresses])

        return {"start": start, "end": end, "persons": persons, "addresses": address_entries}

    # -- dashboard: list thread graph ------------------------------------------

    def thread_graph(
        self, list_id: int, start: int | None = None, end: int | None = None
    ) -> dict[str, Any]:
        """A rank window of ``list_id``'s messages grouped into reply threads.

        The list's messages are ranked by IMAP receipt over the whole list:
        ``uid`` ascending (a folder's UIDs are assigned in arrival order), rows
        without a stored UID sorting oldest, ties broken by ``id``. Rank 0 is
        the furthest back, rank ``list_total - 1`` the most recent.

        ``start`` and ``end`` are 0-based inclusive ranks into that order.
        ``end`` defaults to the most recent rank and ``start`` to 0, so calling
        with neither returns the whole list, oldest rank to newest. The span is
        never capped: an explicit range is honoured however wide it is. Both
        bounds are still clamped to the data — ``end`` down to
        ``list_total - 1``, ``start`` down to ``end`` and up to 0. Callers are
        expected to have rejected negative ranks and ``start > end`` already.

        Returns ``{"list_total": <messages on the list>, "start": <effective
        start rank>, "end": <effective end rank>, "total": <window size>,
        "first_date": …, "last_date": …, "threads": [{"messages": [...]}, ...]}``.
        ``first_date``/``last_date`` are the ``date`` values of the window's
        oldest and newest messages, either of which may be ``None``. For a list
        with no messages ``start``, ``end``, ``first_date`` and ``last_date``
        are all ``None``, ``total`` is 0 and ``threads`` is empty.

        Each message dict carries ``id``, ``message_id``, ``seq`` (its 0-based
        receipt rank within the window, oldest first — the graph's x position),
        ``uid``, ``date``, ``subject``, ``from_name``, ``from_email``,
        ``extraction_status``, ``label``, ``prediction_short`` (both the stored
        label, which holds the response's ``prediction_short`` verbatim, as in
        :data:`_RUG_COLUMNS`),
        ``timing_cpm`` (the stored chars/minute writing rate, or ``None``) and
        ``parent_id`` — the ``id`` of the window message its ``In-Reply-To``
        names, resolved with the same normalisation as the reply-timing
        analysis (see :func:`_parent_message_id`). ``parent_id`` is ``None``
        when the parent is not in the window: unstored, on another list, outside
        the rank range, or a self-reference.

        Threads are the connected components of the parent links, each holding
        its messages oldest first; a message with no links is a one-message
        thread. Threads are ordered by their oldest message's ``seq``.
        """
        list_total = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE list_id = ?", (list_id,)
        ).fetchone()[0]
        if not list_total:
            return {
                "list_total": 0,
                "start": None,
                "end": None,
                "total": 0,
                "first_date": None,
                "last_date": None,
                "threads": [],
            }

        end = list_total - 1 if end is None else min(end, list_total - 1)
        # No start means the whole list: rank 0 through the clamped end.
        start = 0 if start is None else max(0, min(start, end))

        rows = self.conn.execute(
            "SELECT m.id AS id, m.message_id AS message_id, m.uid AS uid, "
            "m.date AS date, m.subject AS subject, m.in_reply_to AS in_reply_to, "
            # The message's own name, else the address's — as the message list
            # and detail do, so one sender reads the same on every surface.
            "COALESCE(m.from_name, a.display_name) AS from_name, a.email AS from_email, "
            "e.status AS extraction_status, s.label AS label, "
            "s.label AS prediction_short, "
            "m.timing_cpm AS timing_cpm "
            "FROM messages m "
            "LEFT JOIN addresses a ON a.id = m.address_id "
            "LEFT JOIN extractions e ON e.message_id = m.id "
            "LEFT JOIN scores s ON s.extraction_id = e.id "
            "WHERE m.list_id = ? "
            "ORDER BY m.uid ASC, m.id ASC LIMIT ? OFFSET ?",
            (list_id, end - start + 1, start),
        ).fetchall()
        window = [dict(row) for row in rows]
        # Message-IDs are unique per list (schema UNIQUE), so this cannot clash.
        by_mid = {msg["message_id"]: i for i, msg in enumerate(window)}

        # Union-find over window indices: one set per thread. Links may point
        # either way in receipt order (a reply can arrive before its parent).
        root = list(range(len(window)))

        def find(i: int) -> int:
            while root[i] != i:
                root[i] = root[root[i]]
                i = root[i]
            return i

        for i, msg in enumerate(window):
            msg["seq"] = i
            parent_idx = None
            if msg["in_reply_to"]:
                parent_mid = _parent_message_id(msg["in_reply_to"])
                # A message naming itself as its own parent is not a reply (the
                # same guard the timing analysis applies).
                if parent_mid != msg["message_id"]:
                    parent_idx = by_mid.get(parent_mid)
            msg["parent_id"] = None if parent_idx is None else window[parent_idx]["id"]
            del msg["in_reply_to"]
            if parent_idx is not None:
                root[find(i)] = find(parent_idx)

        groups: dict[int, list[dict[str, Any]]] = {}
        for i, msg in enumerate(window):
            groups.setdefault(find(i), []).append(msg)
        threads = [
            {"messages": msgs} for msgs in sorted(groups.values(), key=lambda ms: ms[0]["seq"])
        ]
        return {
            "list_total": list_total,
            "start": start,
            "end": start + len(window) - 1,
            "total": len(window),
            "first_date": window[0]["date"],
            "last_date": window[-1]["date"],
            "threads": threads,
        }
