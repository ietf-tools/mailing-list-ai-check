# mailing-list-ai-check

A tool for checking mailing-list mail for AI-generated content, against any
IMAP-accessible mailing-list archive. The pipeline runs as three idempotent
stages over a local SQLite database: **pull** (fetch messages over IMAP) →
**extract** (isolate the new text each author wrote, stripping quotes and
signatures) → **score** (a verdict from the [Pangram](https://www.pangram.com/)
AI-detection API). A Flask + Vue web dashboard reads the results.

![The dashboard with no filter applied](docs/images/dashboard.png)

The dashboard is one screen: the messages pane across the top, the lists and
senders panes below, and a message drawer over them.

### Limitations

- AI detectors are probabilistic: Pangram returns a likelihood, not proof, and
  can be wrong in either direction.
- Short texts are not scored: anything under 50 words is marked `too_short`
  rather than sent, because detection is unreliable below that length.
- Extraction of an author's new text is heuristic: quote and signature stripping
  can fail on unusual formatting.

## Requirements

- Python ≥ 3.14 — the release that added `compression.zstd`, which the
  export/import format uses; the floor keeps compression a standard-library
  concern with no third-party dependency.
- Node.js (only to build the dashboard front end)

Both are supplied by the repo's container image, which is an alternative to
installing them: see "Containers" below.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Build the dashboard front end (needs Node)
make install-frontend   # npm install
make build              # npm run build -> frontend/dist
```

### Containers

One `Dockerfile` defines both environments the app runs in, as two build
targets, so the interpreter and the dependency set cannot drift between them.

- `dev` — the image `.devcontainer/devcontainer.json` builds. It carries the
  toolchain and the dependencies but no source: the code tree is bind-mounted
  from the developer's disk and installed in editable mode, and the database is a
  file inside that same mount. Opening the repo in a devcontainer-aware editor
  builds it and runs `.devcontainer/post-create.sh`; `make image-dev` builds it
  by hand.
- `prod` — a self-contained deployment image, built by `make image`: the source
  tree, the built dashboard and the documentation set at `/app`, gunicorn serving
  the Flask app as an unprivileged user, and the SQLite file on a separate
  persistent mount at `/data`. The pipeline commands are on `PATH` in the same
  image, so pull, extract, score and import run as batch jobs against it.

[docs/deployment.md](docs/deployment.md) documents both targets, every
environment variable the image reads, and a Kubernetes deployment — including
the reason a deployment runs exactly one replica (the store is one SQLite file)
and how an export file is loaded into a running instance.

## Configuration

Copy the template and edit as needed:

```bash
cp .env.example .env
```

- **IMAP** — set `IMAP_HOST` to your mailing-list archive's IMAP server (there
  is no default); `IMAP_PORT` defaults to `993` (implicit TLS). If the server
  offers anonymous or guest access, set `IMAP_USERNAME` / `IMAP_PASSWORD` to the
  guest login documented by that server; otherwise use your own credentials.
- **`PANGRAM_API_KEY`** — required only for the scoring stage. Pulling,
  extraction, and the dashboard all work without it. Get a key from
  <https://www.pangram.com/>.
- **`DATABASE_PATH`** — SQLite file, defaults to `./data/mail.db`.

`.env` is gitignored; never commit it. See `.env.example` for the full list of
keys and defaults.

## Database and schema migrations

The database is a single SQLite file at `DATABASE_PATH`. There is no migration
command: the schema is brought up to date automatically whenever the database is
opened.

Each schema change is one numbered SQL script in `store.py`, and the applied
numbers are recorded in the database's own `schema_version` table; opening the
database runs the missing scripts, each committed in turn. Every entry point —
the pipeline commands, export and import, and the web app (one connection per
request) — opens the database the same way, so whichever runs first after an
upgrade performs the migration. Re-running against an already-current database
does nothing.

Two consequences:

- **Migrations are one-way.** There is no downgrade path, and a migration may
  rewrite rows as well as add columns. Copy the database file before running a
  new version against it for the first time; include the `-wal` and `-shm`
  side-files if they are present.
- **An older app version is not guarded against a newer database.** It reads the
  columns it knows about and ignores the rest, so downgrading the code without
  restoring the matching database copy can produce results that look valid but
  are derived from a partial view of the schema.

Upgrading the front end is a separate step: `mail-ai-web` serves whatever is in
`frontend/dist`, so run `make build` after pulling a new version or the dashboard
stays at the built version while the API moves on.

## Usage

The pipeline is three commands, run in order. Each is idempotent — it only
processes rows that lack its output — so runs resume cleanly after an interrupt.

### `mail-ai-pull` — fetch mail

Fetch messages from one or more lists into the store. Name lists as positional
arguments, or use `--all-lists` (touches ~1374 folders).

```bash
# 200 most recent messages from one list
mail-ai-pull last-call --count 200

# Messages since a date, from two lists
mail-ai-pull quic tls --since 2026-01-01

# Last 30 days across every list
mail-ai-pull --all-lists --days 30

# Resume from where the last pull left off (per-list cursor, UIDVALIDITY-aware)
mail-ai-pull last-call --incremental

# Catch up every list already being tracked (the routine incremental update)
mail-ai-pull --all-lists --incremental

# Only mail from particular senders (server-side FROM filter, repeatable/OR-ed)
mail-ai-pull tls --from alice@example.com --from bob@example.com

# See what would match without fetching or storing anything
mail-ai-pull tls --since 2026-06-01 --dry-run
```

Depth is one of `--count N`, `--since YYYY-MM-DD`, `--days N`, or
`--incremental`. `--limit N` is a hard cap on messages fetched this run — use
`--limit 10` when testing (see Costs and usage limits).

`--incremental` resumes each list from its stored cursor, which records the
UIDVALIDITY and the highest UID stored for that list. It is the cheaper and more
exact way to catch up: the search is a bounded `UID last+1:*`, no already-stored
message is re-examined, and a message that reaches the archive long after it was
sent is still picked up, because UID order follows arrival rather than the `Date`
header. A date-based pull filters on arrival and then discards anything whose own
`Date` predates the period, so such a message is lost to it.

The two combine as follows:

- `--all-lists --incremental` pulls only lists that already have a cursor, and
  skips the rest rather than fetching their whole history. Use it for routine
  catch-up. Skipped lists are counted as `untracked_skipped`.
- A named list with no cursor takes a full first pull, so
  `mail-ai-pull last-call --incremental` bootstraps that list.
- A date-based pull over `--all-lists` is the discovery sweep: it takes on lists
  that are new on the server, and registers a list whose folder is empty by
  seeding its cursor (counted as `cursors_seeded`), so a later `--incremental`
  run catches that list's first message.
- Only a run whose completeness claim is true writes a cursor: an unfiltered
  `--incremental` run, or the first unfiltered pull of a list (adoption — its
  period defines the list's scope). A date- or count-based pull over a list
  that already has a cursor, and any `--from`-filtered pull, leaves the cursor
  untouched: their searches omit UIDs without naming them, so advancing would
  claim unfetched messages as stored. The next `--incremental` run covers the
  span such a pull left behind.

Every fetched message is classified against the auto-generated-mail rules
(`docs/findings/auto-generated.md`); flagged messages are stored with their
classification reason and excluded from extraction and scoring. `--all-lists`
skips the lists that carry only auto-generated traffic (announcement lists,
DMARC reports, GitHub mirrors, meeting broadcasts); `--include-excluded-lists`
restores them, and explicitly named lists are always pulled.

### `mail-ai-extract` — isolate each author's new text

```bash
mail-ai-extract              # process every message without an extraction
mail-ai-extract --limit 50   # stop after 50 messages
```

Runs email-reply-parser plus a custom cleanup pass (normalization, attribution
lines, indented quotes, signatures, digest over-strip guard). No credentials or
network needed.

### `mail-ai-score` — Pangram AI detection

```bash
mail-ai-score                # default: at most 10 API calls
mail-ai-score --limit 500    # a production run
mail-ai-score --limit 500 --bulk   # the same run as one Bulk API job (20% cheaper)
mail-ai-score --dry-run      # show what would be scored / gated / cached
```

Requires `PANGRAM_API_KEY`. Extractions under 50 words are marked `too_short`
and never sent, and identical text is served from the score cache without an
API call. `--limit N` caps API calls per run (cache hits are free and uncapped)
and **defaults to 10** to limit accidental spending — pass a larger value for
production runs. Scoring uses the Pangram 4 detector by default, at roughly
**$0.05 per 100 words** on realtime calls (Pangram 3 cost $0.05 per 1,000
words).

`--bulk` submits the run's texts as one Pangram Bulk API job instead of one
realtime call per text: bulk words are billed at a 20% discount, identical
cleaned text is submitted once with the verdict shared by every message carrying
it, and `--limit` caps the texts submitted. Bulk suits large catch-up runs; for
a handful of new messages the realtime default is simpler and faster. Texts a
bulk job fails to score stay queued and are retried on the next run.

The detector can be changed for one run with `--model {pangram-4,default}`
(`default` routes to Pangram 3 until Pangram deprecates it), or persistently
with the dashboard's "Use Pangram v3 (old)" header switch, which applies to
both the dashboard and the CLI. A database holding Pangram 3 verdicts shows a
one-time dashboard notice offering to keep using v3 and to re-score the old
verdicts.

### `mail-ai-web` — the dashboard

```bash
mail-ai-web    # serves the built dashboard + API at http://127.0.0.1:8050
```

For a production view, build the front end (`make build`) first; `mail-ai-web`
then serves `frontend/dist` directly. For front-end development, use the
two-terminal workflow (see `make dev`).

One filter — list, sender (person or address), date range, subject/text search,
Pangram label, AI-score range and reply rate — drives all three panes at once,
and the filter state lives in the URL query string, so any view of the data is a
shareable link.

- **Messages** (top) — a sortable, infinite-scrolling table of the messages under
  the current filter, with the detection mix of that set beside the count and a
  row of filter controls under the column headings. The last column, Chars/min,
  carries the reply-timing rate (see below), filtered by a minimum and/or a
  maximum rate. Clicking a row opens the message drawer.
- **Lists** (lower left) — with no list in the filter, every list with its message
  count and aggregate detection mix. "Add list" and each row's "Add" button run
  the three pipeline stages from the dashboard ("Run process ($)"), so a list can
  be pulled, extracted and scored without the CLI. "Timelines" opens a screen
  stacking one activity timeline per list with messages on a shared time axis.
- **Senders** (lower right) — one row per sender: a person (a group of linked
  addresses) or a single unlinked address, searchable and sortable by volume or
  AI share. The ⇄ control links addresses into one person, with groupings
  suggested from matching display names, so one contributor's mail is analyzed
  together.

With a list in the filter, the lists pane becomes that list's statistics —
message and scored counts, the detection mix, a timeline of the list's whole
history, and a thread chart — and the senders pane narrows to the senders who
posted to it. Timelines adapt to volume: each message is an individual bar
while every time bin holds one, and where messages outnumber the pixels the
bins become columns, height scaling with the bin's count and color stacking
its detection mix; clicking a column filters the messages table to its date
span.

![The dashboard filtered to one list](docs/images/dashboard-list.png)

With a sender in the filter, the senders pane becomes that sender's profile:
posts, detection mix, and per-list activity with two rugs — the messages this
sender's replies point at, and other senders' replies to this sender.

![The dashboard filtered to one sender](docs/images/dashboard-sender.png)

The message drawer, opened from any row and deep-linked at `/messages/<id>`,
shows the message metadata and reply-timing band, the analysis card (the Pangram
prediction, headline, detector version, and one row per scored window), and the
text that was analyzed, numbered by line and marked with each window's extent.
The ↑ and ↓ buttons step through the filtered result set without leaving the
drawer.

![The message drawer](docs/images/message-detail.png)

"Show ignored" widens the text card from the analyzed text to the whole message,
with everything extraction and post-processing removed — quoted passages,
attribution lines, signatures — dimmed in place, which shows what the detector
was and was not given.

![The same message with "Show ignored" on](docs/images/message-ignored-text.png)

The header holds the store's unfiltered totals, the "Use Pangram v3 (old)"
detector switch, and an Anonymous toggle that hides the sender-identifying parts
of the interface: the From column, the senders pane, and the sender filters.

The ⓘ button beside the app name in the header opens a documentation panel: a
file list on the left, the rendered Markdown on the right. It shows `README.md`,
`CHANGELOG.md` and the Markdown files at the top level of `docs/`, read from the
checkout at request time. Files in sub-directories of `docs/` are not included.

### Reply-timing analysis

Every reply whose parent message is also in the store is classified by its
implied composition rate: the character count of its extracted new text divided
by the interval between the parent message's date and the reply's. That
interval is an upper bound on the time the author had to read the parent and
compose the reply, so the rate is a lower bound on the writing speed the reply
implies.

The rate is stored in the messages table (`timing_cpm`) alongside the band it
falls in (`timing`); the two are always written together. The Messages table's
Chars/min column shows the rate, and the message detail shows the band:

- **implausible** — at or above 250 characters per minute.
- **suspicious** — at or above 100 characters per minute.
- **normal** — below 100 characters per minute.
- empty — not computable: the message is not a reply, its parent is not in the
  store, a date is missing or malformed, the interval is not positive, or the
  message has no extraction with authored text (status `ok` or `too_short`).

The Chars/min column filters on the stored rate: the `cpm_min` and `cpm_max`
query parameters are inclusive bounds in characters per minute, and either one
excludes every message whose rate is not computable.

From 100 characters per minute up, the Chars/min cell is tinted in ten purple
steps, one per hundred characters per minute; lower rates and empty cells are
untinted.

The signal is one-sided: a high rate shows the text was not composed within the
interval, while a low rate shows nothing. It is not by itself evidence of AI
generation — pasting a previously drafted passage, or replying to a message
first seen through another channel, produces the same rate. Both dates come
from the sender-set `Date:` header, so the interval depends on the senders'
clocks. The classification is recomputed after every pull, extract, re-extract
and import.

### Re-processing text derived by an older extraction routine

The routine that derives an author's new text carries its own version number,
separate from the app's, incremented whenever a change could alter the extracted
text, the text sent to Pangram, or an extraction's status. Every extraction
records the routine number that produced its text, so one recorded against a
lower number may hold text the current routine would not produce. A release that
does not change the routine leaves the number alone, so upgrading the app does
not by itself make stored text out of date.

The dashboard compares those numbers on load. When any extraction predates the
running routine it opens a prompt reporting how many, and offers to identify the
affected messages:

- **Show affected messages** re-runs the current extraction and post-processing
  over every stored message and compares the result with what is stored. This is
  local work only: no text is rewritten, no score is changed, and nothing is sent
  to Pangram. Messages whose text would change are listed in a table with a
  total, showing the character counts before and after and what moved (the
  extracted text, the text that gets scored, or the extraction status).
  Extractions that come out identical are stamped with the running routine's
  number, so the prompt does not reappear for them.
- **Run process ($)** re-extracts the listed messages and re-scores them. A
  message keeps its stored score unless the *scored* text changed, since only
  then was the verdict reached on text that no longer exists; each message that
  does need a new verdict is one paid Pangram call unless its new text is already
  in the score cache. Both stages report their counts as they run.
- **Not now** leaves everything untouched. An alert icon then sits beside the ⓘ
  button for as long as any extraction predates the running routine; it reopens
  the same prompt.

An extraction recorded against a *higher* number than the running routine — a
database written by a newer version of the app, then opened by an older one — is
not reported, so an older build never offers to replace text it cannot
reproduce.

### Exporting and importing lists

Move a list's messages and their full pipeline state (extractions and Pangram
scores) between databases as a single portable file — for backup, sharing, or
seeding another checkout. Neither command touches IMAP or Pangram; both are pure
local database operations.

```bash
# Export named lists to a file (writes export.jsonl.zst)
mail-ai-export announce last-call -o export.jsonl

# Export every list that has at least one message
mail-ai-export --all-lists -o all-lists.jsonl

# Export without compression (writes plain-lists.jsonl as given)
mail-ai-export --all-lists -o plain-lists.jsonl --no-compress

# Export only the messages dated within a range (either bound may stand alone)
mail-ai-export announce -o q1.jsonl --date-from 2026-01-01 --date-to 2026-03-31

# Import into another database
mail-ai-import export.jsonl.zst

# Preview an import without writing anything
mail-ai-import export.jsonl.zst --dry-run
```

`mail-ai-export` takes one or more list names, or `--all-lists` (not both), and
requires `-o/--output`. The file is zstd-compressed and `.zst` is appended to
the output path unless it is already there; the summary line reports the path
actually written. `--no-compress` writes plain JSON Lines to the path as given.

`--date-from` and `--date-to` limit the export to messages dated within the
range. Both bounds are inclusive and either can be used alone. The comparison is
the one the dashboard's date filter uses, so a range selects the same messages
in both; a bare `--date-to` day therefore excludes that day's own messages,
whose stored dates carry a time. A ranged export omits each list's pull cursor,
because the file no longer covers the list up to that point and a target
inheriting the cursor would skip the mail left out on its next pull.

`mail-ai-import` needs no flag for compression: it identifies the container from
the file's leading bytes rather than its name, so zstd, gzip and uncompressed
input are all accepted under any suffix, and exports produced before zstd became
the default still import. A corrupt or truncated file is rejected like any other
malformed input, and nothing is written.

Import is **idempotent and collision-safe**: a message already present in the
target (same Message-ID on the same list) is skipped along with its
extraction and score, so importing the same file twice — or back into the
database it came from — is a no-op. The whole import is **all-or-nothing** (one
transaction, rolled back on any error), and `--dry-run` runs the identical path
but rolls back, so its report is exact. Exports carry the app version that
produced them; when an imported message was processed by a **later** pipeline
version than the target's copy, its extraction and score are refreshed from the
file (the message body itself is never overwritten). Each exported extraction
also carries the version of the extraction routine that produced its text, and an
imported extraction keeps that number rather than being credited to the importing
build; for files written before the field existed it is inferred from the app
version in the file.

Export and import are also available from the dashboard's **Messages** pane, via
its Export and Import buttons. Export opens a dialog for choosing the lists —
any number of them, searchable, pre-ticked with the pane's current list filter,
or all lists when none is ticked — and an optional range of message dates. No
other filter in the pane affects what is exported.

### Stats export

Export scores and message metadata for analysis outside the app — in a
spreadsheet, pandas or R. The file is a zip of CSVs and carries no message
text (no bodies, subjects, headers or extracted text) and cannot be imported;
the full export above remains the only transfer format. The format is
specified in [docs/stats-export.md](docs/stats-export.md).

```bash
# Per-message scores and metadata for named lists (writes stats.zip)
mail-ai-export-stats announce last-call -o stats

# Every list with a message in scope, bounded by message date
mail-ai-export-stats --all-lists -o stats --date-from 2025-07-01
```

The archive holds `messages.csv` (one row per message in scope, scored or
not, keyed by Message-ID and sender email, with label, the three Pangram
fractions, timing band and extraction status), `lists.csv` (per-list
aggregates over the same scope, using the dashboard's AI-share definition),
`senders.csv` (the sender grouping: a synthetic key and an email per row, a
person's addresses sharing a key), `datapackage.json` (a standard [Frictionless
Data Package](https://datapackage.org/) descriptor: per-file column schemas,
provenance, row counts) and a `README.md` data dictionary. Rows are identified
by mail-native values — Message-IDs and email addresses — never by the app's
internal ids; a Message-ID is therefore not unique when a message was
cross-posted to several exported lists. Unscored and too-short messages are
included so share calculations over the file reproduce the dashboard's
numbers. List selection and the date range behave exactly as in
`mail-ai-export`, including the `--date-to` edge described above. Like the
full export, the command is a pure local database read.

The dashboard's export dialog offers the same choice: a Full export (the
re-importable archive) or a Stats export. `GET /api/export/stats` takes the
same parameters as `GET /api/export`.

## Costs and usage limits

- **Pangram spend** is controlled three ways: the score cache never pays twice
  for identical text, the 50-word gate skips text too short to score reliably,
  and `--limit` (default 10) caps calls per run. Use `--dry-run` to preview.
- **The archive IMAP server is a shared public service.** When testing or
  experimenting, pull no more than **10 messages** per run (and send no more
  than 10 texts to Pangram). These are project conventions, not enforced limits.

## Development

```bash
make test        # pytest
make lint        # ruff check
make dev         # prints the two-terminal (Vite + Flask) dev workflow
make image       # build the deployment image (see docs/deployment.md)
make image-dev   # build the dev-container image
```

`make test` and `make lint` run the Python tools from `./.venv` when the tree has
one and from `PATH` otherwise, so the same targets work in a virtualenv on the
host and in the dev container, where the virtualenv is at `/opt/venv`. Set
`VENV_BIN` to force either (`make test VENV_BIN=.venv/bin/`).

Layout:

- `src/mailing_list_ai_check/` — package (src layout): `config.py`, `store.py`
  (SQLite schema + typed API), `imap_client.py` / `fetcher.py` (pull),
  `extraction.py`, `pangram.py`, `cli.py` (the three CLI entry points), and
  `webapp/` (Flask API + SPA serving).
- `frontend/` — Vue 3 + Vite dashboard; `make build` emits `frontend/dist`.
- `tests/` — pytest suite, including `tests/fixtures/` (a hand-labeled corpus of
  real public-archive messages with expected extractions, used to grade the
  extractor).
- `docs/findings/` — the Phase 0 spike findings (IMAP, extraction, Pangram) that
  the design is built on, including the rationale for the main design decisions
  (email-reply-parser over Talon, stdlib `sqlite3` over an ORM, the Pangram
  contract), plus the auto-generated-mail survey behind the exclusion rules.

### Secret-scanning guardrail

This repo is public and users supply their own credentials. Two guards keep
secrets out of commits:

- **Local (pre-commit):** install once per clone — `pip install pre-commit &&
  pre-commit install`.
- **CI:** [`gitleaks`](.github/workflows/gitleaks.yml) runs on every push and
  pull request.

## Versioning

The app follows [semantic versioning](https://semver.org/); the current version
lives in `mailing_list_ai_check.__version__` (`pyproject.toml` reads it
dynamically). Which component is bumped depends on whether the change affects
the stored data, not on how large the change is. The minor version is bumped for
a change that affects the data — a schema migration, or any other change to what
is stored or to how a stored value is derived. The patch version is bumped for
every other change, a new feature and a dashboard change included, when the data
in the database is unaffected. No condition currently bumps the major version.
Each message records the app version that last processed it.

The text-extraction routine carries a separate version number of its own,
incremented whenever a change could alter its output; see "Re-processing text
derived by an older extraction routine".

## License

MIT — see [LICENSE](LICENSE).
