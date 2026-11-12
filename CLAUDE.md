# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`mailing-list-ai-check` — a Python app for AI-assisted checking of mailing-list mail.

## Layout

- `src/mailing_list_ai_check/` — package source (src layout)
  - `config.py` — loads credentials/settings from environment
  - `store.py` — SQLite schema, migrations, and typed storage API
  - `staleness.py` — detection and repair of out-of-date extractions (older
    routine generation; parent stored after the reply)
  - `imap_client.py` / `fetcher.py` — IMAP connection and the pull pipeline
  - new-text extraction comes from the `email-reply-extractor` library
    (<https://github.com/JayDaley/email-reply-extractor>), extracted from this
    codebase in v1.15.5 — see "Extraction version" below
  - `pangram.py` — Pangram AI-detection API client
  - `cli.py` — the CLI entry points (pull / extract / score)
  - `webapp/` — Flask API and built-dashboard serving
- `frontend/` — Vue 3 + Vite dashboard (`make build` emits `frontend/dist`)
- `tests/` — pytest suite (the hand-labeled extraction corpus lives in the email-reply-extractor repo)
- `docs/findings/` — Phase 0 spike findings (IMAP, extraction, Pangram)
- `pyproject.toml` — project metadata, dependencies, tooling config
- `Makefile` — dev/build/test/lint targets
- `.env.example` — template for required secrets (copy to `.env`)
- `CHANGELOG.md` — one section per release (see Changelog below)

## Secrets — important

This repo is **public on GitHub**. Users clone it and supply their own secrets:
IMAP credentials and a Pangram API key.

- Credentials live only in environment variables / a local `.env` file.
- `.env` and other credential files are gitignored. **Never** commit a secret,
  print one to logs, or paste one into source (even as a placeholder/example
  value). When adding a new secret, add its key to `.env.example` with an
  empty value so users know it exists.

## Hard testing limits

When testing or experimenting (spikes, manual runs, integration tests, demos —
anything that is not an explicit user-requested production pull):

- Pull **no more than 10 messages** from the IMAP server per run.
- Send **no more than 10 texts** to the Pangram API per run.

Pangram calls cost real money and IMAP is a shared public service. Bake these
caps into test code as defaults (e.g. `--limit 10`), don't rely on remembering.

## Documentation style — hard rule

All documentation (README, everything under `docs/`, and any other published
prose) must be written in a simple, factual, impersonal style. No
editorialising: no opinionated flourishes, colloquialisms, first-person
voice, or subjective commentary — state facts, measurements, and rationale
plainly.

## Searching the source — use CodeGraph, not grep

This repository is indexed by CodeGraph (`.codegraph/` at the repo root). To
find or understand code, call the `codegraph_explore` MCP tool **before**
reaching for `grep`, `rg`, `find`, or bulk `Read` calls — one call returns the
verbatim line-numbered source of the relevant symbols, the call paths between
them, and what depends on them.

- Name symbols or files in the query (e.g. `codegraph_explore` with
  `"extract_new_text clean_text pipeline_version"`), or ask a plain question
  about the code.
- If the tool is listed but deferred, load it by name via tool search first.
  The shell fallback is `codegraph explore "<symbols or question>"`.
- `grep` remains appropriate for non-source text: literal strings in fixtures,
  data files, docs, and lockfiles.

## Conventions

- Python >= 3.14 (the release that added `compression.zstd`), `src/` layout.
- Lint/format with `ruff`; test with `pytest`.
- Access configuration through `Config.load()`, not `os.environ` directly.

## Versioning

The app uses [semantic versioning](https://semver.org/); the current version is
**1.18.0**. The single source of truth is `mailing_list_ai_check.__version__`
(in `__init__.py`); `pyproject.toml` reads it dynamically, so the two never
drift.

Bump policy — the component to bump is decided by whether the change affects
the stored data, not by how large or how visible it is:

- **major** — never, at present. No condition currently bumps it.
- **minor** — any change that affects the data: a schema migration, or any
  other change to what is stored or to how a stored value is derived.
- **patch** — every other change, however large. A new feature, an API or
  dashboard change, a bug fix or an internal refactor is a patch when the data
  in the database is unaffected.

A migration is the clearest case of a minor bump, but not the only one: a
change that alters what a pull stores, or the value written into an existing
column, affects the data just as much without touching the schema.

Extraction changes are not tied to the app version; they carry their own number
(see "Extraction version" below).

## Extraction version

The routine that derives an extraction's text lives in the
`email-reply-extractor` library, and `EXTRACTION_VERSION` (an `int`) is
imported from it. The full contract — when the constant is bumped, the corpus
digest test that enforces it — is maintained in that repository
(`CONTRIBUTING.md` and `docs/versioning.md` there). What still matters in this
repo:

- It only ever increases. `staleness.check()` compares a stored stamp with the
  running value using `<`, so an older app opening a store written by a newer
  routine reads that store as current instead of offering to re-derive text it
  cannot reproduce.
- Upgrading the pinned library version in `pyproject.toml` is a **minor** app
  bump when the new library release changed extraction output (its
  `EXTRACTION_VERSION` moved — stored data is affected on re-extraction), and a
  **patch** bump otherwise.
- Six generations exist: **1** (initial release), **2** (from v1.2.0), **3**
  (from v1.11.0) and **4** (from v1.15.0) were produced in this codebase before
  the library was extracted; **5** shipped in the library's v1.1.0 (fragment
  edges kept, Outlook-boundary fix uncapped), first pinned by app v1.16.0;
  **6** shipped in the library's v1.2.0 (localized Outlook quote-header blocks
  and original-message dividers for French, German, Spanish, Italian,
  Portuguese and Dutch, with an inline-reply guard), first pinned by app
  v1.17.0.
- Re-derivation deletes a score only when the cleaned text changed **in
  substance** (`email_reply_extractor.texts_equivalent`); a whitespace-only
  movement keeps the verdict, re-keyed to the new text's hash (see
  `staleness.reextract`).

Version stamps in the database:

- `messages.pipeline_version` — the app version that last ran a pipeline stage
  end-to-end against the message; stamped on insert and re-stamped whenever its
  extraction or score is written.
- `extractions.pipeline_version` — the app version that wrote the extraction row.
  Provenance only: it names the release, and scoring never touches it.
- `extractions.extraction_version` — the generation of the routine that produced
  the text (migration 011, nullable, backfilled from `pipeline_version`). This is
  the only value the staleness check compares (see `staleness.py`); NULL reads as
  older than every generation.

## Parent-arrival invalidation

An extraction also depends on the thread parent's body
(`Store.get_parent_body`), not only on the routine's generation. A reply
extracted before its parent was stored holds text derived without parent-diff,
and `staleness.check()` does not see that (it compares generations only).

- `staleness.refresh_for_new_parents(store)` repairs it: it takes the
  Message-IDs first stored since the last check, finds the extracted messages
  whose parsed `In-Reply-To` names one, and passes them to
  `staleness.reextract`. It is called at the start of `cli.run_extract` and at
  the start of a non-dry-run `cli.run_score`.
- Progress is the `parent_check_message_id` key in `app_settings` (the highest
  `messages.id` checked), written after the re-derivation completes. Unset, it
  defaults to the highest message id that has an extraction, so replies whose
  parent arrived before that point are not revisited.
- Replies it leaves unscored need no hand-over: an `ok` extraction with no
  score is the scoring queue.
- Any new code path that inserts into `messages` is covered without further
  work, because the check reads the table. A new code path that derives
  extraction text must resolve the parent the same way (`get_parent_body` with
  `exclude_message_id`).

## Changelog — maintain it

`CHANGELOG.md` records every release, newest first, and is parsed by scripts.
Its own "Format" section is the specification; keep to it exactly.

- Every version bump gets a section. Bumping `__version__` without adding or
  updating a `CHANGELOG.md` section is incomplete work.
- Section shape, in this order: `## [<version>] - <date>`, then one
  `Summary: <one line>` line, then one `- ` bullet per individual change.
- `<date>` is `YYYY-MM-DD` for a committed release, or the literal
  `unreleased` while the version is bumped in the source but not yet
  committed. Replace `unreleased` with the commit date when releasing.
- One line per bullet — no wrapped continuation lines, no nested bullets, no
  extra headings or prose inside a release section.
- Write bullets at the granularity of an individual change (a new endpoint, a
  renamed control, a new extraction rule), not per file touched.
- The documentation style rule above applies: factual and impersonal.
- Do not rewrite the history of released sections; add to the newest one, or
  start a new one.
