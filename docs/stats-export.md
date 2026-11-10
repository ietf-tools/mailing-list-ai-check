# Stats export design

A one-way export of scores and message metadata for statistical analysis
outside the app. It complements the full export (`docs/export-import.md`),
which moves complete databases between installs: the stats export is for
people who will analyse the results further — in a spreadsheet, pandas, R or
similar — and will not run extraction or AI detection themselves. It carries
no message text and cannot be imported.

## Goals

- Give an analyst every scored result and the metadata needed to aggregate it
  any way they choose: per list, per sender, per month, per thread, by timing
  band, by detector version.
- Ship the denominators, not only the hits: unscored and gated messages are
  included, because any share calculation needs them (the dashboard computes
  AI share over scored + too-short, and an export that omitted the gated
  messages could not reproduce the dashboard's numbers).
- Open everywhere: CSV files in a zip archive, readable by Excel, pandas and R
  without this app or any decompression tooling beyond the OS.
- Carry no message content: no bodies, no extracted text, no subjects, no raw
  headers, no raw detector responses. The export answers "what did the
  pipeline conclude", never "what did the message say".
- Identify rows by mail-native values (email addresses, RFC 5322
  Message-IDs), never by the app's internal row ids or file-scoped surrogate
  keys. The one synthetic key in the format exists to express sender grouping,
  which mail itself cannot.

## Non-goals

- Re-import. The file is an analysis artifact; the full export remains the
  only transfer format. Nothing in this format is read by the app.
- Anonymisation. The export names senders; the data comes from public
  mailing-list archives. There is no pseudonymous variant.
- Filtering beyond the full export's selection (lists and a date range). The
  per-message rows are the primitive; consumers filter downstream.
- Reproducing the cleaned text or re-scoring. Consumers of this export do no
  AI analysis by definition; `text_sha256` and `raw_response` stay out.

## File format

A zip archive (stdlib `zipfile`, deflate) containing UTF-8 CSV files
(RFC 4180, header row, `\n` line endings) plus a standard descriptor and a
data dictionary:

| Member | Contents |
|---|---|
| `messages.csv` | one row per message in scope — the primitive table |
| `lists.csv` | one row per exported list, aggregated over the scope |
| `senders.csv` | the sender grouping: synthetic key → email address |
| `datapackage.json` | Frictionless Data Package descriptor: provenance, row counts, and a Table Schema per CSV |
| `README.md` | the column dictionary and interpretation caveats, self-contained |

CSV conventions: a NULL is an empty field; booleans are `true`/`false`;
dates are the stored UTC ISO-8601 strings; fractions are written at full
stored precision, unrounded. Column order is part of the format.

Formula escaping: a text field that begins with `=`, `+`, `-`, `@`, a tab or a
carriage return, optionally after one or more `'` characters, is written with
one `'` prepended, so that a spreadsheet does not evaluate it as a formula.
Most text values come from mail headers written by the senders. The rule
applies to every text field of every CSV, email addresses included, so the
`email` and `folder` joins still match; numeric fields are never altered. The
stored value is recovered by removing the first character of each field that
matches `^'+[=+\-@\t\r]`. Values in `datapackage.json` are not altered.

Zip rather than zstd because the audience is analysis tools, not this app:
every OS opens a zip, and the archive bundles the data dictionary with the
data. Without message text the rows are small (roughly 200 bytes each), so
compression ratio is not the constraint it is for the full export.

### `messages.csv`

One row per message in scope, scored or not.

| Column | Meaning |
|---|---|
| `message_id` | RFC 5322 Message-ID; not unique — a message cross-posted to several exported lists appears once per list |
| `list` | `lists.name` (not unique across lists) |
| `folder` | `lists.folder` — the unique list key; joins to `lists.csv` |
| `date` | `messages.date`, UTC ISO-8601, may be empty |
| `email` | sender address, empty when the message has none; joins to `senders.csv` |
| `sender_name` | the message's From name, falling back to the address's display name |
| `in_reply_to` | stored In-Reply-To value, empty when not a reply; joins to `message_id` for thread analysis |
| `auto_generated` | the stored auto-generated marker, empty when none |
| `timing` | reply-timing band: `normal`, `suspicious`, `implausible`, or empty |
| `timing_cpm` | the chars/minute rate behind the band, empty exactly where `timing` is |
| `extraction_status` | `ok`, `empty`, `too_short`, `failed`, or empty when never extracted |
| `extraction_method` | empty when never extracted |
| `extraction_chars` | `extractions.char_count`, empty when never extracted |
| `extraction_version` | generation of the extraction routine, may be empty |
| `pipeline_version` | app version that last processed the message, may be empty |
| `label` | Pangram `prediction_short` verbatim: `Human`, `Mixed`, `AI`; empty when unscored |
| `fraction_ai` | Pangram fraction in [0, 1], empty when unscored |
| `fraction_ai_assisted` | as above |
| `fraction_human` | as above |
| `detector_version` | empty when unscored |
| `scored_at` | UTC ISO-8601, empty when unscored |

A reply is a row with a non-empty `in_reply_to`. The join from `in_reply_to`
to `message_id` resolves most threads directly; the archive README notes that
a small minority of In-Reply-To headers carry extra tokens that need
normalising before the join.

### `lists.csv`

One row per exported list. All counts are over the messages in scope (the
date range applied), so they sum exactly to `messages.csv` — an analyst can
verify the aggregates against the primitive table.

| Column | Meaning |
|---|---|
| `list`, `folder` | as in `messages.csv` |
| `messages` | messages in scope |
| `scored` | messages with a score |
| `too_short` | messages gated under the reliability floor (`extraction_status = too_short`) |
| `human`, `mixed`, `ai` | scored messages by label |
| `ai_share` | `ai / (scored + too_short)`, empty when that denominator is 0 |
| `first_date`, `last_date` | oldest and newest `date` in scope, empty when none |

`ai_share` uses the dashboard's definition (`ai_share()` in `store.py` /
`labels.js`): the AI count over scored plus too-short, so the values here
equal the shares the dashboard's mix bars report.

### `senders.csv`

The sender grouping, and nothing else — exactly two columns:

| Column | Meaning |
|---|---|
| `sender_key` | synthetic key `s1`, `s2`, … |
| `email` | one address belonging to that sender |

One row per address that appears in `messages.csv`. Addresses the app has
linked to one person share a `sender_key` (one row per address); an unlinked
address is its own sender with a single row. This is the same grouping as the
dashboard's Senders pane, expressed without the app's internal person and
address ids. Keys are assigned in first-seen (message-emission) order and are
file-scoped: they are not stable across exports and mean nothing to the app.

Per-sender aggregates are deliberately not shipped: `messages.csv` carries
`email` on every row, so grouping by sender is one join away, and the
audience is people doing their own analysis.

### `datapackage.json`

The descriptor is a standard [Frictionless Data
Package](https://datapackage.org/) (v2 profile), so the archive unzips into a
valid data package: `frictionless validate datapackage.json` checks the CSVs
against their declared schemas, and dataframe libraries that read data
packages load the files with correct types without hand-written parsing.

```jsonc
{
  "$schema": "https://datapackage.org/profiles/2.0/datapackage.json",
  "name": "mlac-stats",
  "title": "Mailing List AI Check stats export",
  "description": "Scores and message metadata for analysis; no message text.",
  "created": "<UTC ISO-8601>",
  "mlac": {                            // custom provenance, namespaced
    "stats_format_version": 2,
    "app_version": "1.10.3",
    "schema_version": 16,
    "folders": ["ietf.announce"],
    "date_from": "2025-07-01",         // present only when given
    "date_to": "2026-08-01",           // present only when given
    "rows": {"messages": 57435, "lists": 12, "senders": 3120},
    "detector_versions": ["…"],        // distinct values present in the file
    "extraction_versions": [1, 2]      // distinct values present in the file
  },
  "resources": [
    {"name": "messages", "path": "messages.csv", "format": "csv",
     "mediatype": "text/csv", "encoding": "utf-8",
     "schema": {
       "fields": [/* one per column: name, type, description; enum
                     constraints on label and timing; [0, 1] bounds on the
                     fractions */],
       "primaryKey": ["folder", "message_id"],
       "foreignKeys": [
         {"fields": ["folder"], "reference": {"resource": "lists", "fields": ["folder"]}},
         {"fields": ["email"], "reference": {"resource": "senders", "fields": ["email"]}}
       ]
     }},
    {"name": "lists", "path": "lists.csv", /* …, primaryKey ["folder"] */},
    {"name": "senders", "path": "senders.csv", /* …, primaryKey ["email"] */}
  ]
}
```

Standard properties carry what the standard can say: each CSV is a resource
with a Table Schema typing every column (ISO datetimes, integers, numbers,
enum constraints giving the label and timing vocabularies, [0, 1] bounds on
the fractions), `primaryKey` states the uniqueness facts (`(folder,
message_id)` for messages; `folder` for lists; `email` for senders), and
`foreignKeys` state the joins. App-specific provenance that has no standard
home — the format version, app and schema versions, the selected folders and
date range, row counts, and the distinct detector/extraction versions present
— lives under the custom `mlac` property, which the standard permits.
`mlac.rows.senders` counts `senders.csv` rows (addresses, not distinct keys).

`mlac.stats_format_version` is this format's own number, independent of the
app version and of the full export's `FORMAT_VERSION`. Since nothing imports
the file, the version exists for analysts and their scripts, not for
rejection logic; additive column changes do not bump it, and a removed or
re-defined column does. Version 1 was the initial release (surrogate
`message_key` / `sender_key` columns in `messages.csv`, a per-sender
aggregate `senders.csv`, a pseudonymous variant, and a bespoke
`manifest.json` descriptor); version 2 identifies rows by `message_id` and
`email`, reduces `senders.csv` to the two-column grouping, has no
pseudonymous variant, and describes itself with `datapackage.json`.

### `README.md` (inside the archive)

The data dictionary, written for someone who has only the zip: every column
of every CSV, plus the caveats that stop the obvious misreadings —

- `label` is Pangram's `prediction_short` verbatim; the app derives nothing
  from it.
- Share calculations must include the too-short messages in the denominator
  to match the dashboard (the reliability floor gates messages under 50 words
  of authored text; they are never sent to the detector).
- `message_id` is not a unique key: cross-posted messages appear once per
  exported list. `(folder, message_id)` is unique.
- Threads join `in_reply_to` to `message_id`; some In-Reply-To headers carry
  extra tokens and need normalising first.
- Scores in one file may come from different detector versions and different
  extraction generations; both are per-row columns and listed in the descriptor.
- The timing bands' thresholds, and that an empty band means the rate could
  not be computed, not that it was normal.
- The date-range edge: a bare `date_to` day excludes that day's messages,
  whose stored dates carry a time.

## Selection

Identical to the full export, so the two dialogs and CLIs stay congruent:
lists by `lists.name` (or all lists with a message in scope) and an optional
inclusive `date_from` / `date_to` on `messages.date`, with the same lexical
comparison and the same `date_to` edge. Every message in scope is exported
whether or not it was scored; there are no further filters.

## Streaming and cost

Purely a local database read: no IMAP, no Pangram, no caps involved. The
message pass streams one row at a time into the zip member
(`ZipFile.open(name, "w")`), so peak memory is independent of the export's
size, matching the full export's discipline. The aggregates for `lists.csv`
are a single GROUP BY query over the same scope; `senders.csv` comes from the
same address pre-pass the full export runs (ids and emails only, never
bodies). Without text the output is small — roughly 10 MB of CSV per 50,000
messages before compression.

## Public API — `src/mailing_list_ai_check/stats_export.py`

```python
STATS_FORMAT_NAME = "mlac-stats"
STATS_FORMAT_VERSION = 2

@dataclass(frozen=True)
class StatsExportSummary:
    lists: int
    senders: int          # senders.csv rows (addresses)
    messages: int
    scored: int
    path: str

    def as_line(self) -> str: ...

def export_stats(
    store: Store, list_names: Sequence[str] | None, out_path: str | Path,
    *, all_lists: bool = False,
    date_from: str | None = None, date_to: str | None = None,
) -> StatsExportSummary: ...   # '.zip' appended to out_path unless present;
                               # selection semantics identical to export_lists
```

Selection validation (names xor `all_lists`, unknown name is a `ValueError`)
is shared with `export_lists` rather than duplicated.

## CLI

```
mail-ai-export-stats LIST [LIST…] -o FILE [--all-lists]
                                  [--date-from ISO] [--date-to ISO] [--db PATH]
```

A separate console script rather than a flag on `mail-ai-export`, because the
two commands produce different artifacts with different guarantees (one
re-importable archive, one analysis bundle) and share only their selection
arguments. Flags follow `mail-ai-export`: `-o/--output` required, dates
validated as ISO-8601 at parse time, summary via the module logger, exit
`0`/`1`.

## HTTP

`GET /api/export/stats` with the same selection params as `GET /api/export`
(`list` repeatable, `date_from`, `date_to`; unknown list and empty selection
are `404`, a bad date is `400`). The attachment is named
`mlac-stats-<slug>-<YYYYMMDD>.zip` with the same `<slug>` rules as the full
export, and is streamed through the same
build-to-unlinked-tempfile-then-chunk pattern.

## Dashboard

The export dialog offers a format choice above the list picker:

- **Full** — the existing re-importable archive (`.jsonl.zst`).
- **Stats** — this format (`.zip`).

List and date selection are shared between the two formats; the dialog's
summary line names the chosen format.

## Versioning and changelog

Changes to the stats export change nothing stored, so each is a **patch**
bump with a `CHANGELOG.md` section. `STATS_FORMAT_VERSION` moves
independently, on the rules given under `datapackage.json`.

## Testing

`tests/test_stats_export.py`:

- archive member set is exactly the five files; `datapackage.json` parses,
  its resource paths name the members, its `mlac.rows` match the CSV row
  counts, and its declared column names/order match each file's header;
- a real export validates clean under the `frictionless` tool (schemas,
  primary keys, foreign keys), exercised in CI-independent local verification
  rather than as a test dependency;
- `messages.csv` rows equal the store's rows for the scope (including
  unscored and gated messages), read back with the stdlib `csv` module;
- `lists.csv` aggregates equal the same aggregates computed from
  `messages.csv`, and `ai_share` matches `ai_share()`;
- `senders.csv` is exactly two columns, covers exactly the addresses present
  in `messages.csv`, gives a person's addresses one shared dense `s<n>` key,
  and gives an unlinked address its own;
- a message with no sender address has an empty `email` and no `senders.csv`
  row;
- cross-posted messages appear once per list under the same `message_id`;
- the date range selects the same messages as the full export's range;
- webapp: content type, attachment name, `404`/`400` cases.
