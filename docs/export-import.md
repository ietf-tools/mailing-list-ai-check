# Export / import design

> **Includes the app/pipeline versioning scheme** (see "Versioning" below):
> the app carries a semantic version, every message records the app version
> that last processed it, exports carry both, and imports from a later pipeline
> version refresh a message's extraction/score without touching the message
> itself. The routine that derives extracted text carries a separate number,
> `extraction.EXTRACTION_VERSION`; each exported extraction carries the
> generation that produced its text, and the importer preserves it rather than
> claiming the importing build's.

Move everything related to a list's messages — the messages themselves, what
was extracted from each, and what Pangram was sent and returned — between
databases as a single portable file, without ever corrupting the target
database on re-import.

## Goals

- Export all data for one or more named lists: list row, pull cursor, sender
  addresses and person groupings, messages, extractions, scores.
- Keep the file small: messages are static, so derived text is stored as
  **pointers into the message body** where possible, not as duplicate text.
- Import is **idempotent and collision-safe**: a message already present in
  the target (same RFC 5322 Message-ID on the same list) is skipped, along
  with its extraction/score, so importing the same file twice — or importing
  into the database it came from — is a no-op.
- Import is **all-or-nothing**: one transaction, rolled back on any error, so
  a truncated or malformed file can never leave a half-imported database.

## What "everything related to that message" means

| Data | Exported as |
|---|---|
| `lists` row | full row (keyed by `folder`) |
| `pull_state` row | full row (imported only if the target has none for the list) |
| `messages` rows | full rows incl. `raw_body`, `raw_html` |
| sender `addresses` | full rows (keyed by `email`) for addresses referenced by exported messages |
| `persons` | canonical name + membership, for persons referenced by those addresses |
| `extractions` | metadata + a text **pointer** (see below) |
| `scores` | full row: fractions, label, detector version, `raw_response` (the complete Pangram JSON), `text_sha256`, timestamps |

What was *sent* to Pangram is the cleaned text, which the pipeline derives on
the fly and never persists; the database (and therefore the export) carries
its SHA-256 (`scores.text_sha256`) — a stable pointer to it — plus the full
raw response. The cleaned text itself is reproducible from the extraction via
`cleaning.clean_for_scoring`.

## Versioning

The app uses [semantic versioning](https://semver.org/), starting at
**1.0.0**. The single source of truth is
`mailing_list_ai_check.__version__` (``__init__.py``); `pyproject.toml`
reads it dynamically (`dynamic = ["version"]` +
`[tool.setuptools.dynamic]`), so the two can never drift.

Bump policy (also recorded in `CLAUDE.md` and the README) turns on whether a
change affects the stored data: **minor** for a change that does — a schema
migration, or any other change to what is stored or to how a stored value is
derived — and **patch** for every other change, however large. No condition
currently bumps the major version. No component of the app version is reserved
for extraction changes.

The routine that derives extracted text lives in the
[email-reply-extractor](https://github.com/JayDaley/email-reply-extractor)
library and carries its own integer generation, `EXTRACTION_VERSION`,
incremented there whenever a change could alter the derived text or an
extraction's status; upgrading the pinned library version is a minor app bump
when the generation moved and a patch bump otherwise. Every `extractions` row records the generation that
produced its text (`extractions.extraction_version`, migration 011), and that is
the value the staleness check compares. `extractions.pipeline_version` remains as
provenance: the app version that wrote the row.

### Per-message pipeline version

`messages.pipeline_version` (TEXT, nullable; migration 007) records the app
version that last ran a pipeline stage end-to-end against the message:

- stamped with the current version when the message is inserted
  (`Store.upsert_message`, `pipeline_version` parameter defaulting to the
  package version — tests may pass an explicit value);
- re-stamped to the current version by `Store.insert_extraction` and
  `Store.insert_score` (each updates the owning message's row), so after a
  full pull → extract → score run the column holds the version that
  completed the process, and re-running a stage under a newer version
  bumps it.
- `NULL` (legacy rows from before the column existed) sorts **older than
  every real version**.

Version strings compare as parsed `(major, minor, patch)` integer tuples —
never lexically. An unparsable or missing version compares as `(0, 0, 0)`.

### Versions in the export format

- The `header` record carries `"app_version"`: the exporter's package
  version.
- Every `message` record carries its `"pipeline_version"` (may be null).
- This is a breaking format change, so `FORMAT_VERSION` is **2**; version-1
  files are rejected (none exist in the wild — the format shipped
  unreleased).
- The extraction generation was added to version 2 **additively**, leaving
  `FORMAT_VERSION` at 2: each embedded `extraction` object carries
  `"extraction_version"` (may be null), and the `header` carries the exporting
  build's `EXTRACTION_VERSION` for diagnostics only — a store can hold text from
  several generations at once, so the per-extraction value is the authoritative
  one. A bump was avoided because neither direction needs rejecting: new code
  reading an older file finds the key absent and infers the generation from the
  message's `pipeline_version` (`>= 1.2.0` is generation 2, anything earlier is
  generation 1, unknown stays null — `store.extraction_version_for_app_version`,
  the same mapping migration 011 applies locally), and older code reading a newer
  file accepts the unchanged `format_version` and ignores the key, because every
  record handler reads only the keys it names and no handler validates a record
  against a closed key list. Removing a key, or changing the meaning of an
  existing one, is still a `FORMAT_VERSION` bump.
- Compression is not part of the format and is not format-versioned. The
  records, their field order and `FORMAT_VERSION` are the same whether the
  file is zstd-compressed, gzip-compressed or plain; the container is chosen
  on write and detected on read (see "Compression").

### Version-aware import of existing messages

When an imported message already exists in the target (same list folder +
Message-ID), the message row itself is still **never** modified (subject,
body, dates etc. stay authoritative; the body-mismatch warning still
applies). But its derived data may now be refreshed:

1. Compare the file message's `pipeline_version` with the target row's
   (tuple comparison, NULL oldest).
2. If the file's version is **not later**, skip the message and its embedded
   extraction/score (`messages_skipped`). One stamp may still move: when the
   derived data is equal (the same comparison as step 3) and the file
   record's `extraction_version` is higher than the target extraction's, the
   target's `extraction_version` is raised to it (counted as
   `generations_advanced`). The staleness check re-stamps an unchanged
   extraction's generation without moving the message's `pipeline_version`,
   so a copy of a re-checked store carries a higher generation under an equal
   `pipeline_version`. No other column changes.
3. If the file's version **is later**, compare derived data:
   - extraction equality: `sha256` of the text, `method`, `status`,
     `char_count`;
   - score equality: `text_sha256`, all three fractions, `label`,
     `detector_version`, `raw_response`.
   If everything (including presence/absence on both sides) is equal, the
   later pipeline has validated exactly what the target already holds, so only
   version stamps move (counted as `versions_bumped`): the message's
   `pipeline_version` is advanced to the file's value, and the target
   extraction's `extraction_version` is raised to the file record's generation
   when that is higher. The latter write is a maximum, so a file of an earlier or
   unknown generation leaves the stamp alone, and `extractions.pipeline_version`
   is not touched. If anything differs,
   **replace** the target's derived state with
   the file's: delete the existing extraction row (the score cascades),
   insert the file's extraction (pointer-reconstructed and hash-verified as
   usual) and its score when present, and update the message's
   `pipeline_version` to the file's value. A file message whose extraction is
   null clears the target's extraction/score (the later pipeline produced
   no extraction, so that result supersedes). Counted as `extractions_updated` and
   `scores_updated` in the summary (the message still counts in
   `messages_skipped` — its own row was not inserted).

`ImportSummary` gains `extractions_updated: int = 0`,
`scores_updated: int = 0`, `versions_bumped: int = 0` and
`generations_advanced: int = 0`.

### Version stamps on an imported extraction

An `extractions` row written by the importer takes both of its version stamps
from the file, never from the importing build:

- `pipeline_version` is the file message's `pipeline_version` (the release that
  produced the derived state), so an imported row carries the same provenance it
  had in the source database.
- `extraction_version` is the record's own `extraction_version` when it has one,
  and otherwise the generation inferred from that same `pipeline_version`.

Stamping the importing build's values instead would claim that this build's
routine produced text it never derived, and would hide genuinely older text from
the staleness check.

## File format

A UTF-8 [JSON Lines](https://jsonlines.org/) stream, one record per line, in a
fixed order: `header`, then per list `list` → `pull_state?`, then `person`s,
then `address`es, then `message`s (extraction and score **embedded** in the
message record so a skipped message atomically skips its children), then
`trailer`. The stream is written zstd-compressed by default; see "Compression"
below.

Cross-references use **natural keys**, never local integer ids:
list → `folder`, address → `email`, message → `(folder, message_id)`.
Persons have no natural key, so each gets a file-scoped synthetic key
(`"p<local id>"`) referenced by its addresses.

### Records

```jsonc
{"type": "header", "format": "mlac-export", "format_version": 2,
 "app_version": "1.0.0", "extraction_version": 2,   // exporting build's, diagnostics only
 "exported_at": "<UTC ISO-8601>",
 "schema_version": 7, "folders": ["ietf.announce"],
 "date_from": "2026-01-01", "date_to": "2026-03-31"}
// date_from/date_to: the requested message-date range, each present only when
// that bound was given. Provenance for a partial export; the importer reads
// neither key.

{"type": "list", "name": "announce", "folder": "ietf.announce",
 "last_synced_at": null, "removed_from_server_at": null, "last_message_at": null}

{"type": "pull_state", "folder": "ietf.announce", "uidvalidity": 123, "last_uid": 456}
// present only when the source has a cursor for the list

{"type": "person", "person_key": "p3", "canonical_name": "Alice Smith"}

{"type": "address", "email": "alice@example.org", "display_name": "Alice Smith",
 "person_key": "p3"}   // person_key null when unlinked

{"type": "message", "folder": "ietf.announce", "message_id": "<m1@example.org>",
 "email": "alice@example.org",          // null when the message has no sender address
 "subject": "…", "date": "…", "in_reply_to": null,
 "raw_body": "…", "raw_html": null, "uid": 101, "fetched_at": "…",
 "pipeline_version": "1.0.0",       // null for legacy rows

 "extraction": {                         // null when the message has no extraction row
   "method": "reply_parser", "char_count": 42, "status": "ok", "created_at": "…",
   "extraction_version": 2,              // generation that derived the text; absent
                                         // in files written before the key existed
   "text": {"kind": "span", "start": 0, "length": 42},   // see "Text pointers"
   "sha256": "<sha256 of extracted_text>",
   "score": {                            // null when the extraction has no score row
     "fraction_ai": 0.95, "fraction_ai_assisted": 0.03, "fraction_human": 0.02,
     "label": "AI", "detector_version": "…",
     "raw_response": "<verbatim stored JSON string, may be null>",
     "text_sha256": "<sha256 of the cleaned text sent to Pangram>",
     "scored_at": "…"
   }
 }}

{"type": "trailer", "lists": 1, "messages": 250, "extractions": 240, "scores": 200}
```

### Text pointers

`extraction.text` is one of, chosen by the exporter in this order:

1. `{"kind": "full_body"}` — `extracted_text == raw_body`.
2. `{"kind": "span", "start": S, "length": L}` — `extracted_text` occurs
   verbatim in `raw_body` at character offset `S` (first occurrence via
   `str.find`); `L = len(extracted_text)`.
3. `{"kind": "inline", "value": "<full text>"}` — fallback whenever the text
   is not a contiguous substring of the body (or `raw_body` is null).

`extraction.sha256` is always present. The importer reconstructs the text,
recomputes the hash and **aborts the import on mismatch** — a pointer that no
longer resolves means the file is corrupt.

## Compression

Compression is a property of the container, not of the record format. It is
declared on write and detected on read; a file's name never decides how it is
read. `codec.py` owns both directions and raises a single error type,
`CodecError`.

Writing:

- `export_lists(..., compress=True)` (the default) writes a zstd stream at
  level 3 and appends `.zst` to the output path unless the path already ends
  with it. `ExportSummary.path` reports the path actually written, so a caller
  that passed `export.jsonl` can report the `export.jsonl.zst` it got.
- `compress=False` writes plain UTF-8 JSON Lines to the path as given; no
  suffix is added.
- Level 3 is the zstd library default. On JSON Lines it compresses about as
  well as gzip's default while being several times faster in both directions;
  higher levels cost more CPU for a small further reduction.
- gzip is no longer written.

Reading:

- `import_file` classifies the input from its first bytes: the zstd frame magic
  `0x28B52FFD`, the gzip member magic `0x1F8B`, or otherwise plain text. The
  suffix is not consulted, so a compressed export imports under any name.
- zstd, gzip and uncompressed input are all accepted. Exports written as gzip
  before zstd became the default, and exports written with `--no-compress`,
  remain importable unchanged.
- A corrupt or truncated compressed stream raises `ExportImportError`, the same
  error type as a malformed record, whether the failure surfaces on open or
  part-way through the stream.

Backend: zstd is in the standard library from Python 3.14 as
`compression.zstd`. That release is the project's `requires-python` floor, so
compression needs no third-party dependency.

### Memory

Neither direction holds more than one message in memory, compressed or
decompressed:

- The exporter writes each record as its row is read, iterating the per-list
  message cursor rather than fetching it. Only the small per-file structures —
  the selected lists, and the addresses and persons the messages reference —
  are collected up front, from a pre-pass that selects `address_id` alone.
- The importer consumes one line at a time, and decompression is chunked
  behind the text handle it reads.

Peak resident memory is therefore a function of the compressor's buffer sizes
rather than of the file's size. Measured on a 430 MB export, peak RSS fell from
480.8 MB to 29.8 MB and no longer tracks the export's size.

The dashboard's download path is bounded the same way. `GET /api/export` builds
the export into a temporary file and then streams that file to the client in
64 KB chunks instead of reading it into a single response body; measured on a
400 MB export, the peak resident memory the response adds fell from about 400 MB
to about 0.2 MB. The temporary file is opened and immediately unlinked, so the
bytes stay readable through the open descriptor while no name survives any exit
path — success, an export error, a HEAD request whose body is never iterated, or
a client that disconnects part-way through. `Content-Length` is still sent: the
export is complete before the response is constructed, so its size is final.

### Cost at scale

An import runs as one transaction by design, and that choice was re-measured
against a 100,000-message database rather than relaxed:

- An import takes about 45 seconds and holds the write lock for about 27 seconds
  of that. `Store.__init__` sets `PRAGMA busy_timeout = 120000`, so a concurrent
  writer waits rather than failing; the previous 30-second timeout left about
  three seconds of margin, so a store roughly 15% larger would have failed a
  concurrent writer outright with `database is locked`.
- Readers are never blocked. They read the pre-import WAL snapshot throughout;
  maximum measured reader latency during an import was 2.5 ms.
- The WAL grows to roughly the size of the resulting database (2–3 GB at 100,000
  messages) and stays at that size until the last connection closes, because a
  checkpoint alone only rewinds the file. `PRAGMA journal_size_limit = 67108864`
  caps what a completed checkpoint leaves behind at 64 MB. An explicit truncating
  checkpoint is deliberately not issued: one was measured blocking for 32 seconds
  while a reader held an open transaction.
- Free disk of about 2.5x the final database size is required during an import.
- `--dry-run` costs the same as a real import: it performs the identical work and
  rolls back. The rollback itself takes about 7 ms.

Splitting the transaction was investigated and rejected. Savepoints free no WAL
bytes, and checkpointing mid-transaction is not possible, so the only real option
is batching into separate transactions. That would reduce peak WAL from 2.19 GB
to 1.11 GB, at the cost of atomicity, and it would still not make a malformed
file resumable — a partially applied import would have to be identified and
undone by hand.

## Export semantics

- Lists are selected by `lists.name` (`--all-lists` = every list that has at
  least one message in scope). A name may match several rows (name is not
  unique); all matches are exported. An unknown name is an error (`ValueError`).
- Only persons/addresses actually referenced by the exported messages are
  included, each once (deduplicated across lists in the same file).
- The file is zstd-compressed unless `compress=False` (`--no-compress` on the
  CLI); see "Compression" for the suffix and path rules.
- Purely a local database read: no IMAP, no Pangram, no caps involved.

### Date range

`date_from` / `date_to` bound the exported messages by `messages.date`,
inclusively at both ends and independently — either may be given alone. The
comparison is the lexical one `_build_message_where` applies to the dashboard's
date filter, over the same UTC ISO-8601 column, so a range selects in the export
exactly what it selects in the explorer. That includes its one sharp edge: a
bare `date_to` day (`2026-03-01`) sorts before `2026-03-01T10:00:00+00:00` and
so excludes that day's messages. The dialog says so; the CLI's `--date-to` help
says so.

The range narrows the messages, not the format. Two consequences follow from
it:

- The address pre-pass applies the same bounds as the streaming pass, so a
  sender whose only messages fall outside the range is not written at all.
- A ranged export writes **no `pull_state` record**, for any list. A cursor
  asserts that a list is present up to `last_uid`; a partial file cannot say
  that, and a target importing one would start its next pull above the mail the
  range left out and skip it permanently. Without a cursor the target keeps
  pulling the list from wherever it actually is.

Which lists appear does depend on how they were chosen: a named list is
exported whether or not the range leaves it any message, while `--all-lists`
selects only lists that have one.

## Import semantics

One pass over the stream, inside a single explicit transaction on the store's
connection (raw SQL, not the per-call-committing `Store` methods); `COMMIT` at
the end, `ROLLBACK` on any error. `--dry-run` runs the identical code path and
rolls back instead of committing, so its report is exact.

Per record type:

- **header** — must be first; `format`/`format_version` must match, else error.
- **list** — insert if the `folder` is new; an existing row is left untouched
  (its metadata is not overwritten).
- **pull_state** — inserted only when the target has no cursor for that list;
  an existing cursor always wins (it reflects the target's own sync state).
- **person / address** — addresses upsert by `email` with the same
  display-name backfill rule as `Store.upsert_address`. If the address already
  belongs to a person in the target, that link is kept. Otherwise the file's
  `person_key` is resolved: the first address of a group that already has a
  target person recruits that person for the whole group; if none does, a new
  person is created with the exported `canonical_name` (created lazily — only
  for persons actually needed).
- **message** — the collision guard. `INSERT … ON CONFLICT(list_id,
  message_id) DO NOTHING`; when the row already existed the message counts as
  *skipped* and its embedded extraction/score are **not** imported (the
  existing message's pipeline state is authoritative) — unless the file
  message carries a **later** `pipeline_version`, in which case the derived
  data is refreshed per "Version-aware import of existing messages" above
  (the message row itself still isn't touched). On skip, the stored
  `raw_body` is compared with the file's; a difference is logged as a warning
  and counted (`body_mismatches`) — never overwritten.
  For inserted messages: extraction text is reconstructed from its pointer,
  verified against `sha256`, and inserted with both version stamps taken from
  the file (see "Version stamps on an imported extraction"); then the score, if
  present.
- **trailer** — must be last; its counts must equal the records actually seen,
  else the file is truncated/corrupt → error (rollback).

Any unknown `type`, out-of-order record, forward reference (message naming an
unseen folder/email, address naming an unseen `person_key`), invalid
extraction status, or JSON parse failure → error, rollback, non-zero exit.

## Public API — `src/mailing_list_ai_check/export_import.py`

```python
FORMAT_NAME = "mlac-export"
FORMAT_VERSION = 2

@dataclass(frozen=True)
class ExportSummary:
    lists: int
    messages: int
    extractions: int
    scores: int
    path: str

    def as_line(self) -> str: ...

@dataclass(frozen=True)
class ImportSummary:
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
    generations_advanced: int = 0
    dry_run: bool = False

    def as_line(self) -> str: ...

class ExportImportError(Exception): ...   # module-specific error, raised for every
                                          # validation failure described above

def export_lists(
    store: Store, list_names: Sequence[str] | None, out_path: str | Path,
    *, all_lists: bool = False, compress: bool = True,
    date_from: str | None = None, date_to: str | None = None,
) -> ExportSummary: ...        # compress ⇒ zstd, '.zst' appended to out_path;
                               # ExportSummary.path is the path written;
                               # date_from/date_to bound messages.date inclusively

def import_file(
    store: Store, in_path: str | Path, *, dry_run: bool = False,
) -> ImportSummary: ...
```

## CLI

Two new console scripts in `pyproject.toml`, entry points in `cli.py`
following the existing `mail-ai-pull` / `mail-ai-extract` / `mail-ai-score`
patterns (argparse, `Config.load()` for the default `--db`, logging setup,
summary via the module logger, return `0`/`1`):

```
mail-ai-export LIST [LIST…] -o FILE [--all-lists] [--no-compress]
                            [--date-from ISO] [--date-to ISO] [--db PATH]
mail-ai-import FILE [--db PATH] [--dry-run]
```

- `mail-ai-export`: list names or `--all-lists` (not both, mirroring
  `mail-ai-pull` validation); `-o/--output` required; the file is
  zstd-compressed and `.zst` is appended to the output path unless it is
  already there, and `--no-compress` writes plain JSON Lines to the path as
  given. `--date-from` / `--date-to` bound the exported messages (see "Date
  range"); each is validated as ISO-8601 at parse time, because the comparison
  itself is lexical and would accept a typo as a silently wrong range.
- `mail-ai-import`: positional file; no compression flag, because the
  container is detected from the file's content; `--dry-run` reports without
  writing; import validation errors log the reason and exit `1`.

Message bodies are never logged (matching the existing convention).

## HTTP

`GET /api/export` takes the same selection as the CLI:

| Param | | |
| --- | --- | --- |
| `list` | repeatable | one per list; omit entirely for every list |
| `date_from` | ISO-8601 | inclusive lower bound on the message date |
| `date_to` | ISO-8601 | inclusive upper bound on the message date |

An unknown list name and a selection holding no message are both `404`; an
unparseable date is `400`. The attachment is named
`mlac-export-<slug>-<YYYYMMDD>.jsonl.zst`, where `<slug>` is the single list's
sanitized name, `<n>-lists` for several, or `all`.

`POST /api/import` takes the file as a multipart `file` field, with `dry_run`
as a query or form param.
