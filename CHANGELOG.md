# Changelog

All notable changes to `mailing-list-ai-check`, newest release first. The
project follows [semantic versioning](https://semver.org/); the bump policy is
recorded in `CLAUDE.md`.

## Format

The file is machine-readable. Every release is one section with exactly three
kinds of line, in this order:

```
## [<version>] - <date>

Summary: <one-line summary of the release>

- <one-line description of an individual change>
- <one-line description of an individual change>
```

Extraction rules (skip lines inside fenced code blocks — the template above is
itself a fenced block, and its placeholder header would otherwise parse as a
release):

- Release header: `^## \[(?P<version>[^\]]+)\] - (?P<date>\S+)$`
  `version` is a semantic version (`MAJOR.MINOR.PATCH`). `date` is either an
  ISO-8601 date (`YYYY-MM-DD`) or the literal `unreleased` for a version that
  is bumped in the source but not yet committed as a release.
- Summary: `^Summary: (?P<summary>.+)$` — exactly one per release, the first
  non-blank line after the release header.
- Change: `^- (?P<change>.+)$` — zero or more per release, each a single line
  (no wrapped continuation lines, no nested bullets).

Any other line (headings above level 2, blank lines, prose in this Format
section) is not part of a release record and can be ignored. Outside fenced
code blocks, no release section appears before the first `## [` header.

No 1.1.0 release exists: the version went from 1.0.5 to 1.2.0.

## [1.15.4] - unreleased

Summary: Container images for development and deployment, from one Dockerfile with a dev target and a Kubernetes-ready production target.

- Add a `Dockerfile` with two build targets: `dev`, the dev-container image, and `prod`, a self-contained deployment image.
- Build the dashboard in the image's frontend stage, so `frontend/dist` ships inside the production image and cannot lag the API.
- Add `docker/mlac-web`, the production web entry point: gunicorn serving `webapp:create_app()`, with every server setting read from an environment variable.
- Add a `prod` optional-dependency group holding gunicorn; `mail-ai-web` remains the development server.
- Install the package in editable mode in both image targets, because the app locates `frontend/dist` and the documentation set relative to the package's own file.
- Default the production image to `DATABASE_PATH=/data/mail.db`, a fixed non-root uid/gid of 1001 and a root-owned `/app`, so the container writes only to its data mount and a temporary directory.
- Add a `HEALTHCHECK` on `GET /api/capabilities`, the one GET that never opens the database.
- Build the dev container from the repo's Dockerfile instead of the base image plus the Python feature, and drop the now-unused feature lock file.
- Add `.devcontainer/post-create.sh`: an editable install of the bind-mounted tree and the frontend dependency install.
- Bind the Vite dev server to every interface, because `localhost` resolves to `::1` alone in the dev container and a forwarded port dials 127.0.0.1.
- Mount `frontend/node_modules` as a named volume in the dev container, keeping the host's platform-specific install out of the container.
- Resolve the Makefile's Python tool paths through `VENV_BIN`: `./.venv` when the tree has one, `PATH` otherwise, which is what the dev container uses.
- Add `make image` and `make image-dev`, tagging the deployment image with the package version.
- Add `.dockerignore`, keeping credentials, local databases and export files out of the build context.
- Add `docs/deployment.md`: both image targets, the environment variables the image reads, and a Kubernetes deployment with the pipeline stages as jobs.

## [1.15.3] - 2026-08-21

Summary: Replace the messages pane's preloaded From dropdown with a server-side type-ahead search.

- Replace the From filter's preloaded select, which was capped at 2,000 senders, with a combobox that searches /api/senders?q= per keystroke.
- Show per-sender message counts, an "(anyone)" clear entry and a remaining-match count in the From combobox's results.

## [1.15.2] - 2026-08-19

Summary: Add a read-only mode that refuses every state-changing request, plus per-endpoint export switches, for exposing the dashboard on an untrusted network.

- Added the `PUBLIC_READONLY` setting (env / `Config.public_readonly`); when enabled the web app rejects any non-GET/HEAD/OPTIONS request with a 403 before it reaches its view.
- Added the `ALLOW_EXPORT` setting (env / `Config.allow_export`, default on); when off, `GET /api/export` returns a 403 before any database read.
- Added the `ALLOW_STATS_EXPORT` setting (env / `Config.allow_stats_export`, default on); when off, `GET /api/export/stats` returns a 403 before any database read.
- Added the `GET /api/capabilities` endpoint reporting the read-only and export flags, and had the dashboard read it to hide the export button (and, when only one export is offered, the format chooser) rather than surface a 403.
- Added `PUBLIC_READONLY`, `ALLOW_EXPORT` and `ALLOW_STATS_EXPORT` to `.env.example`.

## [1.15.1] - 2026-08-19

Summary: Show the app version and extraction generation in the documentation drawer.

- Added `app_version` and `extraction_version` to the `GET /api/docs` index response.
- Added a footer at the bottom of the documentation drawer's index column showing the app version and the extraction generation.

## [1.15.0] - 2026-08-19

Summary: Stop the HTML quote oracle deleting Gmail inline replies and remove re-wrapped quote remainders via parent-diff continuation matching (extraction generation 4).

- Treat a Gmail `gmail_quote`/`gmail_quote_container` wrapper that holds a `<blockquote>` as transparent, so the author's inline replies written between the blockquotes are novel text instead of quoted; a wrapper with no blockquote (the forward shape) stays a quoted container.
- Add Gmail's `gmail_attr` attribution line to the quoted-container class hooks.
- Add a continuation rule to the parent-diff assist: an unmarked line joins the removal when an adjacent marked line and it appear contiguously in the parent's word-stream, removing the short remainder lines a re-wrapped quoted paragraph leaves behind.
- Gate the continuation rule on at least 3 already-marked lines and enable it only for the thread-parent pass, not the HTML quote oracle, so inline citations and interleaved replies are never chained into.
- Increment `EXTRACTION_VERSION` to 4; the fixture-corpus digest is unchanged because the affected shapes occur only in stored mail.

## [1.14.4] - 2026-08-19

Summary: Show the "Too short to test" status in place of the Analysis dash rather than under it.

- Remove the dash above "Too short to test" in the Analysis column; the status replaces the pill row on gated rows.

## [1.14.3] - 2026-08-19

Summary: Reduce the messages pane's Analysis column to verdict pills, flag humanized messages, and colour the per-window scores by verdict.

- Remove the Pangram headline text from the Analysis column; the column shows the prediction pill alone.
- Move the "Too short to test" status under the Analysis pill slot instead of beside it.
- Add a purple Humanizer pill to the right of the prediction pill when any window of the message's score is flagged is_humanized.
- Add is_humanized to each window entry in GET /api/messages' score payload; null on rows scored under detector v3.
- Narrow the Analysis column from 230px to 160px.
- Frame each score/confidence pair in the AI Score column with a 2px border in the window's verdict colour.
- Add label to each window entry in GET /api/messages' score payload.

## [1.14.2] - 2026-08-19

Summary: Sort the aggregate-analysis columns by the count of AI + Mixed messages instead of the AI share, and extend the Timelines screen's sort captions.

- Add a Msgs sort caption to the Timelines screen; the initial count-descending order now shows its indicator and can be flipped.
- Add an AI Count caption at the far left of the Timelines header, sorting the stack by the number of AI + Mixed messages.
- Sort the lists-index aggregate analysis caption by the count of AI + Mixed messages instead of the AI share; ties still break on message count descending.
- Sort GET /api/senders' ai order by the sender's count of AI + Mixed messages instead of the AI share; ties still break on message count descending.
- The stats export's ai_share column is unchanged.

## [1.14.1] - 2026-08-19

Summary: Add rug plots above the aggregate bars, fix every rug at gapless 2px columns, extend sorting across the tables and Timelines screen, and give the thread graph a month axis, handle-date hover and full-history default.

- Add GET /api/messages/timeline: the filtered message set as one slim [id, epoch, bucket] timeline, honouring the same filters as GET /api/messages.
- Add a timeline rug of the filtered message set to the messages toolbar, left of the detection-mix bar and matching its 200px width.
- Add a per-list history rug above each lists-index row's aggregate analysis bar, on the corpus-wide time domain.
- Make the lists-index List and Msgs captions sortable; a new caption applies its natural first order and clicking the active one flips it.
- Move the senders table's per-sender history rugs above the aggregate analysis bars.
- Move the sender detail card's history rug above the aggregate detection-mix summary.
- Fix every rug plot at 2px-wide columns, removing the adaptive 2-6px column width and the per-plot fixed-width override.
- Remove the 1px gap between rug columns; adjacent occupied bins now touch.
- Replace the rug columns' native title tooltips with a fixed-position tooltip that appears after 120 ms.
- Add timing_cpm to GET /api/messages' sort columns; rows without a stored rate sort last under either order.
- Make the messages table's Chars/min header sortable.
- Add List name and AI share sort captions to the Timelines screen, using the same AI-share ordering as the lists index.
- Break equal AI-share values by message count descending in the lists-index, Timelines and GET /api/senders AI sorts.
- Make GET /api/lists/thread-graph default to the whole list and honour explicit spans in full, removing the 100-message default window and the 500-message span clamp.
- Add a month axis with vertical dividers along the top of the thread graph.
- Show the date a thread-graph slider handle sits on while the handle is held or dragged.

## [1.14.0] - 2026-08-18

Summary: Make the lists index load in milliseconds instead of seconds.

- Add migration 018: a covering index on messages(list_id, date) so the lists index's per-list count and earliest-date aggregate reads index pages alone (measured 4.3 s to 0.03 s over a 110,725-message store).
- Pin the join order of the lists index's label-mix and too_short aggregates to drive from messages, keeping every join step inside a covering index (measured 3.3 s and 2.8 s to about 0.1 s and 0.3 s).

## [1.13.3] - 2026-08-18

Summary: Replace the senders' cumulative-AI sparklines with per-sender history rugs.

- Change GET /api/senders/timelines points from [epoch, ai] to the [id, epoch, bucket] shape GET /api/lists/timelines serves, and echo the bucket names.
- Draw an adaptive TimelineRug under each sender row's aggregate analysis bar and under the sender detail card's detection-mix summary, on the shared scope-wide domain.
- Open a message from a single-message rug column; a binned column on the detail card filters the messages pane to its date span.
- Remove the CumulativeAiSpark component.

## [1.13.2] - 2026-08-18

Summary: Add cumulative-AI sparklines under the senders' aggregate analysis bars.

- Add GET /api/senders/timelines serving each requested sender's dated messages as slim [epoch, ai] points plus the scope-wide date domain.
- Add Store.sender_timelines backing the endpoint, scoped by person ids, address emails and an optional list.
- Add the CumulativeAiSpark component: a normalized cumulative-posts area in grey with a cumulative-AI step line over it, on a shared time axis.
- Draw the sparkline under each sender row's aggregate analysis bar in the senders table, scoped to the active list filter as the bar is.
- Draw the sparkline under the sender detail card's detection-mix summary, across all lists on the corpus-wide domain.

## [1.13.1] - 2026-08-18

Summary: Adaptive rug plots at any message volume, and a stacked all-lists Timelines screen.

- Add TimelineRug, a shared adaptive rug plot: individual bars while every time bin holds one message, count-scaled columns stacked by prediction bucket where messages outnumber the pixels.
- Replace the fixed-cap rug plots (the list-stats rug's last 100 messages, the reply rugs' 50 per direction) with TimelineRug over each source's full history.
- Add GET /api/lists/timelines serving slim uncapped per-list message points (id, epoch seconds, prediction bucket, subject when a single list is requested).
- Remove the 50-row default from GET /api/senders/reply-rugs; limit is now optional and omitting it returns every matching message.
- Clicking a multi-message rug column filters the dashboard to the column's list and date span; single-message columns keep opening the message.
- Add a Timelines screen (route /timelines, opened by a Lists-pane header button) stacking one timeline per list with messages, gapless, on a shared month axis.
- Bin every Timelines row with one fixed column width, so marks and bin boundaries are identical whatever a list's message count.
- Draw a vertical gridline down the whole Timelines stack at every calendar month boundary.

## [1.13.0] - 2026-08-18

Summary: Restrict cursor writes to runs whose completeness claim is true, so a date-, count- or sender-filtered pull can no longer advance a cursor past messages it never stored.

- Advance or create a `pull_state` cursor only from an unfiltered `--incremental` run or the first unfiltered pull of a list; date- and count-based pulls over a tracked list leave the cursor untouched.
- Never write a cursor from a `--from`-filtered pull, which omits every other sender and so can neither adopt an untracked list nor advance a tracked one.
- Rely on the next unfiltered `--incremental` run to fetch what a filtered pull passed over, deduplicating already-stored UIDs; previously the advanced cursor hid such messages from every later run.
- Keep the empty-folder cursor seeding and the UIDVALIDITY resync rewrite unchanged; both claims hold regardless of filters.

## [1.12.0] - 2026-08-18

Summary: Stop `--all-lists --incremental` backfilling the full history of lists it has never pulled, and register empty folders so a later incremental run tracks them.

- Skip a folder with no stored cursor under `--all-lists --incremental` instead of pulling it from UID 0, counted as `untracked_skipped`; the skip precedes the `EXAMINE`, so such a folder costs no round trip.
- Keep the full first pull for a named list, so `mail-ai-pull last-call --incremental` still bootstraps one.
- Seed a cursor at `UIDNEXT - 1` for a folder the server reports empty during a date- or count-based pull, counted as `cursors_seeded`, so an incremental run picks up that list's first message.
- Add `require_cursor` to `DepthMode`, set from `--all-lists`, and return the folder's `FolderStatus` from `compute_uids` rather than its UIDVALIDITY alone.
- Report `untracked_skipped` and `cursors_seeded` in the pull summary line.

## [1.11.0] - 2026-08-18

Summary: Recognize Outlook quote-header blocks that the extraction previously missed, so top-posted replies no longer keep the quoted thread as new text (extraction generation 3).

- Truncate at a quote-header block found on the intact body before email-reply-parser runs; fragment re-joining could glue a signature line above the `From:` and disguise the block.
- Treat only transport fields (`Message-ID:`, `References:`, `In-Reply-To:`, `Received:`, `Return-Path:`, `Resent-*`) as pasted-header evidence, so a signature `Tel:` or banner `Classification:` line above a `From:` no longer hides a real block.
- Recognize quote-header blocks whose `From:` line carries an address when header lines are folded without leading whitespace or separated by single blank lines.
- Match the English attribution line without a word boundary before `wrote:`, covering the address glued onto it when email-reply-parser unwraps a wrapped attribution.
- Report method `erp+custom` when the pre-ERP truncation removed content, matching the previous naming for the same cleanup.
- Increment `EXTRACTION_VERSION` to 3; the fixture-corpus digest is unchanged because the affected shapes occur only in stored mail.

## [1.10.3] - 2026-08-18

Summary: Key the stats export by mail-native identifiers, describe it with a standard Frictionless datapackage.json, and drop its pseudonymous variant (stats format version 2).

- Identify `messages.csv` rows by `message_id` (not unique: a cross-posted message appears once per exported list) instead of a file-scoped `message_key`, dropping the `parent_key` and `is_reply` columns; threads join `in_reply_to` to `message_id`.
- Identify senders in `messages.csv` by `email` instead of an app-specific `sender_key`.
- Reduce `senders.csv` to the two-column sender grouping — synthetic `sender_key`, `email` — one row per address in the file, a person's addresses sharing a key; the per-sender aggregate columns are removed.
- Remove the pseudonymous variant: the `--pseudonymous` CLI flag, the `pseudonymous` API param and the dialog checkbox.
- Replace the bespoke `manifest.json` with a Frictionless Data Package `datapackage.json`: a typed Table Schema per CSV (enum vocabularies, fraction bounds, primary and foreign keys) with app provenance and row counts under a custom `mlac` property.
- Raise `STATS_FORMAT_VERSION` to 2 and document the version-1 differences in `docs/stats-export.md`.

## [1.10.2] - 2026-08-17

Summary: Add a stats export — scores and message metadata as CSV, with no message text, for analysis outside the app.

- Add `stats_export.py`: a zip of `messages.csv`, `lists.csv`, `senders.csv`, `manifest.json` and a data-dictionary `README.md`, designed in `docs/stats-export.md`; not importable.
- Include unscored and too-short messages in `messages.csv`, so share calculations over the file reproduce the dashboard's denominators.
- Compute `lists.csv` and `senders.csv` aggregates over the exported scope with the dashboard's `ai_share` definition, so they sum exactly to `messages.csv`.
- Add a pseudonymous variant that omits sender addresses, names and Message-IDs, keying senders `s1, s2, …` per export; reply links survive via a file-scoped `parent_key`.
- Add the `mail-ai-export-stats` console script, taking the same selection flags as `mail-ai-export` plus `--pseudonymous`.
- Add `GET /api/export/stats`, taking the same selection params as `GET /api/export` plus `pseudonymous`, streamed like the full export.
- Add a Full / Stats format choice to the dashboard's export dialog, with a Pseudonymous option shown for the stats format.

## [1.10.1] - 2026-08-17

Summary: Let an export select several lists and a range of message dates, from the dashboard, the CLI and the API alike.

- Add `date_from` / `date_to` to `export_lists()`, bounding the exported messages by `messages.date` inclusively at both ends, each usable alone, with the lexical comparison the dashboard's date filter already applies.
- Apply the range in the address pre-pass as well as the streaming pass, so a sender whose only messages fall outside it is not written to the file.
- Select only lists holding a message in range under `all_lists`; a list named explicitly is still exported whether or not the range leaves it any.
- Omit every `pull_state` record from a ranged export, a cursor being an assertion that a list is present up to `last_uid` that a partial file cannot make.
- Record the requested range in the header as optional `date_from` / `date_to` keys, additive within format version 2 and read by nothing on import.
- Accept a repeatable `list` param on `GET /api/export`, de-duplicated in first-seen order, alongside validated `date_from` / `date_to` params.
- Return 404 from `GET /api/export` when the selection holds no message, rather than serving a file with nothing in it.
- Name a multi-list download `mlac-export-<n>-lists-<date>.jsonl.zst`, keeping the list's own name for a single-list export.
- Add `--date-from` / `--date-to` to `mail-ai-export`, each validated as ISO-8601 at parse time so a typo cannot become a silently wrong lexical range.
- Replace the dashboard's one-click export with a dialog offering a searchable multi-list picker, pre-ticked with the pane's current list filter, and from/to date inputs.
- Repeat array-valued params in the frontend's query builder, so one key can carry several values.

## [1.10.0] - 2026-08-14

Summary: Index the Senders pane's aggregates, which took about 32 seconds per page on a 110,000-message store and now take about 0.1.

- Add four covering indexes (migration 017): `messages(address_id, list_id, auto_generated)`, `messages(address_id, from_name)`, `extractions(message_id, status)` and `scores(extraction_id, label)`, each making one join step of the sender aggregates read index pages alone.
- Run `ANALYZE` at the end of the migration, without which the planner prefers a UNIQUE autoindex to a covering index and the gain is roughly halved.
- Measured on a 109,931-message store: an unfiltered page of the Senders pane fell from about 32 s to about 0.1 s, and a list-scoped one from about 6 s to about 0.02 s.

## [1.9.2] - 2026-08-14

Summary: Add a search box to the Lists pane, matching the Senders pane's.

- Add a "search lists…" box to the Lists pane header, filtering the index by a case-insensitive substring over a list's name or its server folder, as the Senders pane's box matches a sender's name or any of its addresses.
- Filter in place rather than refetching, the whole index already being loaded, so the index narrows as each character is typed.
- Show a distinct hint when a search matches nothing, separate from the existing hint for an index with no active lists.
- Hide the box in the per-list stats mode, where there is no index to search.
- Rename the remaining "Mail AI Check" occurrences in the design handoff document and the documentation-endpoint test fixture.

## [1.9.1] - 2026-08-14

Summary: Name the dashboard "Mailing List AI Check" in its header and window title.

- Change the dashboard header's brand from "Mail AI Check" to "Mailing List AI Check".
- Change the page title, which names the browser tab and window, to match.

## [1.9.0] - 2026-08-14

Summary: Link each DMARC-rewritten address to the address it stands for, and raise the many-names threshold to five.

- Link an address on `dmarc.ietf.org` to the address it was rewritten from, into one person, at the end of every non-dry-run pull; `maarten.simon=40sidn.nl@dmarc.ietf.org` and `maarten.simon@sidn.nl` become one sender.
- Add `Store.link_dmarc_rewrites`, which reconciles every such pair in the store and is idempotent, and `dmarc_rewrite_original`, which unescapes a rewrite's `=XX` sequences back to the original address.
- Name a person created by that linking after the original address's display name, the one its sender chose, rather than the rewrite's, which the list server sets.
- Attach to the existing person when either address already has one, and leave a pair whose two addresses belong to different persons alone, logging it rather than merging two hand-made groupings.
- Raise `MULTI_NAME_ADDRESS_THRESHOLD` from 3 to 5, so an individual who varies their own name across a few messages keeps their name while genuinely shared addresses do not.
- Report `named_by_address` on each sender entry, so the dashboard no longer repeats the threshold.

## [1.8.2] - 2026-08-14

Summary: Name an address that has sent under three or more different From names by the address itself.

- Name an unlinked address in the Senders pane by its address, not by its stored `display_name`, once it has presented `MULTI_NAME_ADDRESS_THRESHOLD` (3) or more different `From` names; no one of them represents the address.
- Count the distinct names over all of an address's messages, never scoped to the list filter, so a sender's name does not change as that filter moves.
- Report the count as `distinct_from_names` on each sender entry, and name it in the sender row's tooltip when the rule applies.
- Leave a person's `canonical_name` alone, it being set by hand, and leave the stored `addresses.display_name` unchanged in every case.

## [1.8.1] - 2026-08-14

Summary: Render the Senders pane's Show All control as the same toggle switch the Lists pane uses.

- Replace the Senders pane's plain checkbox with the sliding toggle switch of the Lists pane's Show All control, including its `role="switch"` and `aria-checked` state.
- Move the control from the right of the pane header to immediately after the pane subtitle, where the Lists pane places its own.

## [1.8.0] - 2026-08-14

Summary: Hide senders whose mail is never scored from the Senders pane, behind a "Show all" switch.

- Omit senders from `GET /api/senders` when every one of their messages is auto-generated, and so can never be scored; a sender with any scoreable message, or with no messages at all, is never omitted.
- Add the `include_excluded` query parameter to `GET /api/senders` (boolean, default false) to include them, and echo it in the response.
- Report `excluded_count` and `excluded_from_scoring` on each sender entry.
- Add a "Show all" switch to the Senders pane header, off by default and persisted to localStorage; the pane refetches when it changes.
- Mark an included sender with an "excluded" tag next to its address.

## [1.7.0] - 2026-08-14

Summary: Discard messages a date-based pull fetches whose own Date header predates the pull period.

- Discard, instead of storing, any message fetched by a `--since` or `--days` pull whose `Date` header is earlier than the period start; the server-side `SINCE` search matches on arrival time (INTERNALDATE), so re-imported or late-delivered history otherwise accretes into the store with dates far outside the pulled period.
- Keep messages with no parsable `Date` header, and apply no discard to `--count`, `--incremental` or explicit-UID pulls, which have no period.
- Count discards in a new `discarded_early` field of the fetch summary and its log line.
- Pre-filter date-based pulls server-side with `SENTSINCE` (the `Date`-header search key, live-verified on the archive server), so early-dated history is excluded before any body is downloaded; the search uses a one-day margin because `SENTSINCE` disregards the header's time zone, and the client-side discard remains the precise gate.
- Skip re-downloading messages whose UIDs the store already holds for a list when the folder's UIDVALIDITY is unchanged; skipped messages are counted as duplicates, and the pull cursor advances only over the contiguous processed prefix of the search result.
- Add `Store.uids_for_list`, the stored-UID set the skip subtracts from a search result.

## [1.6.0] - 2026-08-13

Summary: Store each message's own From display name and its verbatim header block, so senders that present a different name per message are no longer all shown under the first name seen.

- Add `messages.from_name` (migration 015), the display name parsed from the message's own `From` header; the fetcher stores it on insert.
- Add `messages.raw_headers` (migration 016), the verbatim header block as bytes, so any header-derived field can be recomputed locally without an IMAP re-fetch; the fetcher slices it from the bytes it already parses, at no extra network cost.
- Add a `--backfill-headers` pull mode that fetches `BODY.PEEK[HEADER]` for stored messages lacking a header block, stores it, and re-derives the `From` display name for messages that have none; `--limit` caps a run, defaulting to 10.
- Add `ImapClient.fetch_full_headers`, a whole-header-block fetch alongside the existing three-field `fetch_headers`.
- Reject `--backfill-html` and `--backfill-headers` together, and reject a depth mode with either.
- Serve the message's own name from `/api/messages` and `/api/messages/<id>`, falling back to the address's stored `display_name` when it is NULL (rows fetched before migration 015, and headers that carried no name).
- Prefer the message's own name in the stale-data report's sender column and in the list panel's thread graph, on the same fallback; the thread graph previously served the address's name under the field name `from_name`.
- Carry `from_name` and `raw_headers` (base64, as `raw_headers_b64`) through export and import as additive fields; `format_version` is unchanged, so files remain compatible in both directions, and a malformed base64 header block is rejected rather than imported.

## [1.5.1] - 2026-08-13

Summary: Stop the Pangram client from auto-retrying submit requests that may already have created a billed job.

- Retry a `POST /task` or `POST /bulk` submit only on HTTP 429 or a connect-phase timeout; a read timeout, mid-request connection failure or 5xx response now raises immediately instead of re-sending a request the server may already have accepted (a retried bulk submit was observed creating one billed job per attempt).
- Give the bulk submit its own request timeout (`bulk_submit_timeout`, default 120 s); accepting a 1,000-item job was measured taking longer than the 10 s general request timeout.
- Update docs/findings/pangram.md: per-generation pricing in the rate-limits section (the previous figures were Pangram-3-era) and a note recording the duplicate-submission hazard.

## [1.5.0] - 2026-08-12

Summary: Add Pangram Bulk API scoring, auto-generated-mail exclusion rules, and AI-share sorting for the lists index and senders table.

- Add `autogen.py`: header- and sender-based classification of auto-generated messages, derived from a survey of IETF list traffic since 2025-07-01 (see docs/findings/auto-generated.md).
- Classify every fetched message at parse time and store the reason in a new `messages.auto_generated` column (migration 014); flagged messages are excluded from extraction and scoring.
- Keep datatracker-delivered IESG ballot positions (identified by their `To: iesg@ietf.org` header) in scope despite their `Auto-Submitted: auto-generated` header, since the ballot text is human-written.
- Skip lists that carry only auto-generated traffic during `--all-lists` pulls; a new `--include-excluded-lists` flag restores them, and explicitly named lists are always honoured.
- Report the number of auto-generated messages in the fetch summary and expose the classification reason in the message list and detail API payloads.
- Round-trip `messages.auto_generated` through export and import (additive format change; older files import with the field unset).
- Add docs/findings/auto-generated.md, recording the survey method and the measured exclusion rules.

- Add `PangramClient.predict_bulk()`, a Pangram Bulk API client: submit one job of many texts, poll to a terminal status, page the results, and return per-item verdicts and errors.
- Add a `--bulk` flag to `mail-ai-score` that submits the run's texts as a single Bulk API job, submitting identical cleaned text once and fanning its verdict out to every extraction sharing it.
- Estimate bulk-run spend at the Bulk API's 20% discount off the realtime per-word price.
- Add an `ai` sort key to `GET /api/senders`, ordering senders by the AI share of their mix (default direction descending).
- Add `store.ai_share()`, the AI fraction of one aggregate mix over the scored messages plus those gated under the reliability floor.
- Make the senders table's "Aggregate analysis" column header sort by AI share, ascending or descending.
- Make the lists index's "Aggregate analysis" column header sort by AI share, descending then ascending, with a third click returning to the message-count order.

## [1.4.1] - 2026-07-30

Summary: Store Pangram's prediction_short verbatim instead of deriving an "AI-Assisted" label.

- Remove the derived "AI-Assisted" label: `scores.label` now stores the response's `prediction_short` exactly as returned (`AI` / `Human` / `Mixed`).
- Add migration 013, restoring the `Mixed` label on rows migration 003 had rebadged.
- Normalize `AI-Assisted` labels to `Mixed` when importing exports written by earlier versions.
- Remove the un-rebadging `CASE` folds and the `Mixed` filter expansion; label queries now match `scores.label` directly.
- Count only `AI` verdicts as flagged in sender aggregates (assisted-dominant `Mixed` rows were previously flagged).
- Remove the AI-Assisted entries and unused helpers from the dashboard's label vocabulary.

## [1.4.0] - 2026-07-30

Summary: Score with the Pangram 4 detector and surface its per-window humanizer verdicts.

- Pin the Pangram detector generation to `pangram-4` on every submit; the API's own default still routes to Pangram 3 until that generation is deprecated.
- Add a `model` parameter to `PangramClient` for overriding the requested detector generation.
- Report the Pangram 4 per-window `is_humanized` and `humanizer_score` fields from the message detail endpoint and show them in the dashboard's window table and hover popover.
- Update the end-of-run spend estimate to the Pangram 4 realtime price of $0.05 per 100 words.
- Add an `app_settings` table (migration 012) and `GET`/`PUT /api/settings` for persistent app settings.
- Add a "Use Pangram v3 (old)" switch to the dashboard header that stores the detector selection and routes scoring to Pangram 3.
- Add a `--model` option to `mail-ai-score` that overrides the stored detector selection for one run.
- Filter the score cache by detector generation so a cached verdict from one generation is never served to a run requesting another.
- Show a one-time notice when a database holds Pangram 3 verdicts, stating the default detector and price change and offering to keep using v3, re-score the old verdicts, or decide later.
- Add `GET`/`PUT /api/pangram/notice` and `POST /api/pangram/retest` backing the upgrade notice.
- Generalize the header warning icon to cycle through active warnings (stale extractions, Pangram upgrade notice).
- Price the end-of-run spend estimate by the active detector generation ($0.05 per 100 words for Pangram 4, $0.05 per 1,000 for Pangram 3).

## [1.3.0] - 2026-07-28

Summary: Give the text-extraction routine its own version number, so staleness detection no longer depends on the app's semantic version, and stream the dashboard's export download.

- Add `EXTRACTION_VERSION`, a hand-incremented integer in `extraction.py` identifying the routine that derives extracted text (`extraction.py`, `cleaning.py` and `html_text.py` together), currently 2.
- Add schema migration 011 (schema version 11): a nullable `extractions.extraction_version` column recording the generation that produced each extraction's text, backfilled from `pipeline_version` — a 1.2.x stamp maps to generation 2, any other non-NULL stamp to generation 1, and NULL stays NULL.
- Back up the database before opening it with this release for the first time: migration 011 rewrites a column, and migrations are one-way with no downgrade path.
- Compare `extraction_version` rather than the app version in `staleness.check`, using `<` instead of `!=` so an older app opening a store written by a newer routine reads it as current instead of offering to downgrade the text.
- Keep `extractions.pipeline_version` as provenance of the release that wrote the row, and leave `messages.pipeline_version` unchanged.
- Replace `store.extraction_generation` with `store.extraction_version_for_app_version`, mapping an app version onto the generation it derived text with.
- Stamp `extraction_version` on insert and on re-extraction in `Store.insert_extraction`, `Store.replace_extraction` and `Store.set_extraction_version`, and group `Store.extraction_version_counts` by it.
- Add `tests/test_extraction_version.py`, pinning the routine's output over the whole fixture corpus to a SHA-256 digest so a behaviour change cannot land without an `EXTRACTION_VERSION` increment and a re-recorded digest in the same commit.
- Return `extraction_version` from `GET /api/staleness`, in each per-generation count, and on each row of `POST /api/staleness/check`.
- Report the extraction routine's version alongside the app version in the dashboard's stale-data prompt.
- Change the app's version bump policy to ordinary semantic versioning, with no component reserved for extraction changes.
- Carry the extraction generation in the export format additively at `FORMAT_VERSION` 2: `extraction_version` on each embedded extraction, plus the exporting build's value in the header for diagnostics.
- Take an imported extraction's `pipeline_version` and `extraction_version` from the file rather than the importing build, inferring the generation from the file's app version for exports written before the field existed.
- Fix imported extractions being written with no `pipeline_version`, which left every imported row reading as stale.
- Raise an existing extraction's `extraction_version` to the file's when a later-version import finds the derived data already identical, never lowering it.
- Stream `GET /api/export` from the finished temporary file in 64 KB chunks instead of reading it into memory, cutting the peak resident memory a 400 MB export download adds from about 400 MB to about 0.2 MB while still sending `Content-Length`.
- Unlink the export's temporary file while its descriptor is still open, so no exit path — an export error, a HEAD request, or a client disconnecting mid-download — can leave it behind.
- Raise the connection busy timeout from 30 to 120 seconds, measured against an import of 100,000 messages holding the write lock for about 27 seconds.
- Set `PRAGMA journal_size_limit = 67108864`, capping the WAL file a completed checkpoint leaves behind at 64 MB after an import grows it to roughly the size of the database.
- Document the import's cost at scale in docs/export-import.md, including why the single-transaction import was kept.
- Correct the `ImportSummary` definition in the public API block of docs/export-import.md, which was missing `extractions_updated`, `scores_updated` and `versions_bumped`, and name the error class `ExportImportError` instead of the placeholder `ImportError_`.

## [1.2.10] - 2026-07-28

Summary: Prepare export/import for databases of 100,000 messages and more — compress exports with zstd by default, detect an import file's container from its content rather than its name, and stream export records so peak memory no longer tracks export size.

- Size export/import for a 100,000-message database, projected at 2.2 GB uncompressed from the measured 20,944 bytes per message: zstd level 3 compresses that in about 5 seconds to roughly a tenth of the size, and streaming holds peak memory flat instead of the projected 4.5 GiB.
- Add a codec module owning export compression: zstd at level 3, the `.zst` suffix, content-based container detection, streaming read and write helpers, and one CodecError type for every backend failure.
- Use the standard library's `compression.zstd`, adding no third-party compression dependency.
- Raise `requires-python` from `>=3.11` to `>=3.14`, the release that added `compression.zstd`.
- Add a keyword-only `compress` parameter to export_lists, defaulting to True, which writes the file zstd-compressed and appends `.zst` to the output path unless it is already there.
- Report the path actually written in ExportSummary.path, so a caller that passed `export.jsonl` sees the `export.jsonl.zst` it got.
- Detect an import file's container from its leading bytes instead of its suffix, accepting zstd, gzip and uncompressed input under any name.
- Raise ExportImportError for a corrupt or truncated compressed import file, the same type as every other bad-file failure.
- Raise ExportImportError for an import file that is not valid UTF-8 text, so binary input carrying no container magic returns exit code 1 from mail-ai-import and 400 from POST /api/import instead of an unhandled UnicodeDecodeError.
- Write each export record as its row is read instead of accumulating records in memory, and iterate the per-list message cursor instead of fetching it, which cut peak resident memory on a 430 MB export from 480.8 MB to 29.8 MB.
- Add `--no-compress` to mail-ai-export, writing plain uncompressed JSON Lines to the output path as given.
- Serve GET /api/export as an `application/zstd` attachment named `mlac-export-<slug>-<YYYYMMDD>.jsonl.zst`.
- Save a POST /api/import upload under a neutral temporary name, relying on content detection instead of reconstructing a suffix from the uploaded file name.
- Request `application/zstd` from the dashboard's export button, default the downloaded name to `mailing-list-export.jsonl.zst`, and accept `.zst` alongside `.jsonl` and older `.gz` files in the import picker.
- Document compression, content detection and the bounded-memory property of both directions in docs/export-import.md and the README, including that compression is not part of the record format and leaves FORMAT_VERSION at 2.
- Correct the FORMAT_VERSION value in the public API block of docs/export-import.md from 1 to 2, matching the code.
- Add `ruff format --check .` to the `make lint` target, which previously ran only `ruff check .` and so never verified formatting.
- Reformat tests/seed.py and tests/test_store_query.py, which had drifted from `ruff format` while unchecked.

## [1.2.9] - 2026-07-28

Summary: Add a per-list thread chart to the dashboard, drawing each message as a circle coloured by its prediction and each reply as a line to its parent, with a message-window slider in an 80%-wide lightbox.

- Add GET /api/lists/thread-graph, returning a start/end rank window of a list's messages in IMAP receipt (UID) order grouped into reply threads, with sender, subject, date, prediction, chars-per-minute rate and in-window parent linkage per message.
- Resolve thread parents with the same normalised In-Reply-To linkage as the reply-timing analysis, dropping self-references and parents outside the window.
- Clamp the requested window span to 500 messages, keeping the most-recent handle and echoing the effective start/end in the response.
- Add a "Show thread chart" button to the list panel, opening a text-free SVG thread chart in an 80%-wide lightbox: one circle per message coloured like the rug plots, threads in horizontal rows, receipt order left to right, straight lines linking each reply to its parent.
- Draw a wider tinted underlay beneath a reply's line when its implied writing rate is flagged, using the same chars-per-minute bands and colours as the messages table.
- Show the message's sender, subject and date/time in a tooltip when hovering a circle; open its detail view on click.
- Add a dual-handle slider under the chart selecting the shown window by message count over the whole list, labelled with the date/time of the first and last shown messages, fetching the chart lazily on open and on handle release.
- Ignore the local graphify-out/ knowledge-graph directory.

## [1.2.8] - 2026-07-27

Summary: Serialize schema migrations, surface too-short messages in the aggregate bars and rug plots, and add list coverage dates, sender reply rug plots, and a chars-per-minute column to the dashboard.

- Run the schema-version check and the whole migration batch in one BEGIN IMMEDIATE transaction, so concurrent connections opening an out-of-date database apply each migration exactly once instead of failing with duplicate-column errors.
- Execute migration statements individually instead of via executescript, whose implicit commit would break the migration transaction.
- Set a 30-second busy timeout on every connection so lock waiters queue behind a migration catch-up instead of raising "database is locked".
- Retry the one-time WAL journal-mode conversion on a busy database, which bypasses the busy timeout and could fail concurrent first opens of a brand-new database.
- Add a regression test opening a version-8 database from six threads at once.
- Rename the message table's analysis text for too-short messages to "Too short to test".
- Append a grey too-short segment to the end of every aggregate detection bar, computing all shares over a total that includes the too-short messages.
- Label the too-short segment "Too short" in the detection bar's hover popover and in the detection summary caption.
- Colour rug-plot bars for too-short messages with the Observable-10 grey, and merely-unscored bars with a lighter neutral.
- Add too_short_count to the list rows of GET /api/lists, the sender rows of GET /api/senders, and each by_list row of GET /api/summary.
- Add an earliest_message_at field to each GET /api/lists row, the oldest stored message date for the list.
- Add an "Earliest" column to the lists index showing the date and time of the earliest fetched message.
- Narrow the lists-index name column to two thirds of its width, truncating long names with an ellipsis and a full-name tooltip.
- Add GET /api/senders/reply-rugs, returning per sender and list the last 50 messages the sender replied to and the last 50 replies from others to the sender's messages, resolving parents with the same In-Reply-To linkage as the reply-timing analysis.
- Add "Replied to" and "Reply from" rug-plot columns to the sender screen's activity-by-list table, with extraction_status carried on the rug rows.
- Move the message table's timing column to the last position and rename its header to "Chars/min".
- Show the implied chars-per-minute rate in the timing column, in the chars column's monospace style, moving the timing classification to the cell tooltip.
- Tint the chars/min cell background in ten steps of the Observable-10 purple, one step per hundred chars/minute from 100 to 1000 and above.
- Expose the rate behind each timing classification as timing_cpm on message rows returned by GET /api/messages.
- Document the chars-per-minute column and its tint ramp in the README.
- Store the reply-timing rate in a new messages.timing_cpm column (migration 010, backfilled on first open), written together with the band in one recompute pass and served to the message list instead of being recomputed per page.
- Replace the message list's timing band filter with inclusive cpm_min and cpm_max bounds on the reply-timing rate, applied in SQL so counts and pagination match.
- Replace the chars/min column's band select with minimum and maximum chars-per-minute filter inputs, synced to the URL and shown as active-filter chips.

## [1.2.7] - 2026-07-26

Summary: Classify each reply by the writing rate its new text implies over the gap since its parent message.

- Add a timing column to messages classifying the implied composition rate of a reply's extracted text over the gap since its parent: implausible at >= 250 chars/minute, suspicious at >= 100, normal below, NULL when not computable (migration 009, backfilled on first open).
- Recompute the timing classification after every fetch, extract, re-extract and import run.
- Expose timing in the message list and detail API responses, add a timing filter to the shared filter params, and add a timing_distribution to the summary endpoint.
- Add a Timing column with a matching filter to the dashboard message table, and a Timing row to the message detail drawer.
- Document the reply-timing analysis in the README.

## [1.2.6] - 2026-07-25

Summary: Widen the per-list rug plot to the last 100 messages.

- Fetch 100 messages for the list card's rug plot instead of 50; the heading already reports the count returned.

## [1.2.5] - 2026-07-25

Summary: Label messages gated under the word-count floor, and explain the per-window table in the message drawer.

- Show "Too short" in the Analysis column for a message whose extraction is `too_short`, in the monospace face to distinguish it from a Pangram headline.
- Add a "Windows" heading and a note on Pangram's 500-token windowing above the per-window table in the message drawer.

## [1.2.4] - 2026-07-25

Summary: Detect stored text derived by an older extraction routine and offer to re-process the affected messages.

- Add schema migration 008: an `extractions.pipeline_version` column recording the app version that produced each extraction's text, backfilled from `messages.pipeline_version`.
- Stamp `extractions.pipeline_version` on insert and rewrite it on re-extraction; scoring never touches it, so it identifies the routine behind the stored text.
- Add `Store.extracted_message_ids`, `Store.extraction_version_counts`, `Store.set_extraction_version`, `Store.replace_extraction` and `Store.delete_score_for_extraction`.
- Add `extraction_generation`, comparing versions by their `(major, minor)` pair — the granularity at which extraction changes are released.
- Add the `staleness` module: `check` compares recorded versions, `diff` re-derives every stored extraction and reports the ones that differ, `reextract` rewrites chosen rows.
- Stamp extractions that re-derive identically with the running version, so a check that finds no difference stops the prompt returning.
- Delete the score of a re-extracted message only when its cleaned (scored) text changed, leaving verdicts that still apply in place.
- Add `GET /api/staleness`, reporting whether any stored extraction predates the current routine, with per-version counts.
- Add `POST /api/staleness/check`, re-deriving every stored extraction and returning the affected messages.
- Add `POST /api/staleness/reextract` and `POST /api/staleness/rescore`, both taking up to 1000 message ids.
- Add a `message_ids` filter to `run_score`, restricting a scoring run to given messages' extractions.
- Open a prompt at dashboard start-up when stored text may be out of date, with the affected messages in a scrolling table (total, character counts before and after, what changed) and a "Run process ($)" button that re-extracts and re-scores only those messages.
- Add an alert icon beside the header's info button while any extraction predates the running version, reopening the same prompt.
- Document in the README how schema migrations are applied on database open, that they are one-way, and that the front end needs a separate rebuild.

## [1.2.3] - 2026-07-25

Summary: Add an in-dashboard documentation viewer, opened by an info button in the header.

- Add `GET /api/docs`, listing the servable documentation files (`README.md`, `CHANGELOG.md`, and the Markdown files at the top level of `docs/`) with each file's first level-1 heading as its title.
- Add `GET /api/docs/<path>`, returning one file's raw Markdown; a path that is not in the index is a 404, so no request path reaches the filesystem.
- Add a `DOCS_ROOT` app config key and a `docs_root` argument to `create_app`, defaulting to the repository root.
- Add an ⓘ button beside the app name in the header that opens the documentation panel.
- Add `DocsDrawer.vue`: a panel sliding in from the left of the screen with the file index in the left column and the rendered document in the right.
- Render the Markdown with `marked` (new front-end dependency), including GFM tables, fenced code blocks and inline code.
- Rewrite links in a rendered document: repository paths the API does not serve are shown as plain text, links to another listed document switch the viewer, and external links open in a new tab.
- Close the documentation panel on the Close button, Escape, or a click on the backdrop.

## [1.2.2] - 2026-07-25

Summary: Align the dashboard with Pangram's three-category vocabulary and repaint it in the Observable 10 palette.

- Fold the stored four-band label into Pangram's three `prediction_short` buckets (Human / Mixed / AI) for every aggregate view; `AI-Assisted` merges into `Mixed`.
- Add `foldToPrediction`, `predictionShort` and `PRED_ORDER` to the shared label vocabulary, and drop `MIX_CAPTION`.
- Replace the Okabe-Ito palette with Observable 10: blue for Human, orange for Mixed/AI-Assisted, red for AI, grey for unscored, with matching tints behind the percent pills.
- Serve `prediction_short` and Pangram's free-text `headline` from the messages API, parsed out of the stored raw response with a fallback derived from the stored label.
- Select `scores.raw_response` in the message-row query so the API can read the headline and prediction without a second lookup.
- Match both `Mixed` and `AI-Assisted` rows when the dashboard filters on `Mixed`, so the filter agrees with the folded bars and pills.
- Replace the Score column with two columns: Analysis (prediction pill plus headline) and AI Score (Confidence).
- Generalise `MixBar` with `order`, `fold`, `phrases`, `colors` and `show-counts` props so one component draws every aggregate breakdown.
- Show message counts alongside percentages in the aggregate mix-bar hover popup.
- Measure the hover popup off-screen before positioning it, so it is no longer pinned to the left edge on first hover, and clamp it to the viewport width.
- Remove the redundant native `title` tooltip from the mix bar.
- Reduce the label filter to the three prediction buckets and remove the min/max fraction-AI inputs.
- Rename the lists-pane mix column header to "Aggregate analysis".
- Delete `ScoreCell.vue`, whose job the new Analysis and AI Score cells now do.
- Remove `LABEL_MUTED`, the muted mid-tone palette the previous per-message score bar used.
- Add `docs/pangram-output.md`, a reference for every field Pangram returns, its observed value domain, and how each maps to the dashboard.
- Remove the loaded-count footer bar from the messages pane.
- Stack the messages-pane filter controls two rows deep, so the Date column holds one full-width date input per row instead of two half-width ones, and narrow Date from 176px to 120px while widening List from 100px to 156px.
- Return each Pangram window's `ai_assistance_score` and `confidence` from the messages API, so the list can show per-window scores.
- Show the `prediction_short` bucket as the Analysis pill (Human blue, Mixed orange, AI red) with Pangram's headline beside it as plain uncoloured text, the pill in a fixed slot so the headlines all start at the same offset.
- Replace the AI Score column with "AI Score (Confidence)", listing every window's score to two decimal places with its confidence abbreviated to H / M / L, clipped to the column width with a trailing ellipsis.
- Show the window count and every window's score and confidence when hovering the AI Score column.
- Remove the Extraction column from the messages pane, moving its scored / unscored filter under AI Score (Confidence).
- Speak the three bucket names in the mix-bar hover popups instead of Pangram's headline phrases, and remove `PRED_PHRASE`.
- Extract the hover-popup positioning into `lib/hoverPop.js` and the popup styling into a shared `.hover-pop` class, shared by the mix bars and the score cells.
- Locate each Pangram window in the extracted text, reporting its `{line, col}` start and end from the message detail API alongside its label, characters and word count.
- Rename the drawer's score card to "Analysis" and show the prediction pill, the headline and the analysis engine and version ("Pangram detector 3.3.2").
- Replace the drawer's three fraction bars with a table of every window's number, characters, score, confidence and label.
- Combine the drawer's extracted-text and raw-body cards into one text card holding either view: with "Show ignored" off (the default) it shows only the text the checking service saw, and on it shows the whole message with everything else dimmed, drawing no distinction between what extraction removed and what post-processing removed.
- Align the extracted text against the raw body line by line to build the whole-message view, falling back to the extracted text with a note when the two do not align (a message whose text came from the HTML part).
- Mark each window in the drawer text with a numbered box at its first character and a bracket down a right-hand wire gutter spanning its lines, labelled with the same numbered box; where one window ends and the next begins on a line, both brackets share it.
- Draw every window number as an Observable 10 grey box — in the analysis table, inline in the text and beside the bracket — and the brackets in the same grey.
- Light up a window's number boxes and its bracket in the palette's light blue while any of them is hovered, and make the table's box the link that jumps to the window in the text.
- Hover a window's number box anywhere for its score, confidence, label and size, one field per line.
- Show a `too_short` extraction's text in the drawer instead of "(no extracted text)".
- Add `windowBucket` to map Pangram's per-window labels onto the three prediction buckets, colouring the analysis table's label swatches by verdict.

## [1.2.1] - 2026-07-24

Summary: Run the pull pipeline from a staged progress modal in the dashboard.

- Rename the Go and Fetch-and-check buttons to "Run process ($)".
- Drive fetch, extract and check as three sequential API calls with per-stage progress in a centred modal (`RunProcessModal.vue`).
- Add the `/pull/fetch`, `/pull/range/fetch`, `/extract` and `/score` endpoints that back the individual stages.
- Close the originating form or popover when a run starts.
- Replace the list-stats "Pull 50 newest" button with the Add popover's footer button.

## [1.2.0] - 2026-07-24

Summary: Exclude localized quote headers and custom signature blocks from extracted text.

- Recognize Chinese quote-header blocks (发件人 / 发送时间 / 收件人 / 抄送 / 主题) with ASCII or full-width colons, including U+3000-padded 主　题, as produced by Alibaba Mail and Chinese Outlook.
- Drop the dashed divider Alibaba Mail draws above such a quote-header block.
- Recognize the Chinese "Original Message" dividers `-----邮件原件-----` and `-----原始邮件-----`.
- Recognize Japanese attribution lines ("<date>、<who>のメール:", Spark / Apple Mail), anchored on a leading year.
- Truncate at a custom punctuation-rule signature divider ("========") only when the line above is blank and a name line follows, so Markdown heading underlines and authored section breaks do not qualify.
- Allow short capitalized prefixes in identifier-keyword debris lines ("VSO BLOG:") and add D-U-N-S to the keyword set.
- Treat postal-address lines containing a digit ("Tokyo Office: … 150-0021 …") as per-line debris.
- Treat "Label: URL" lines inside a sign-off's trailing block as debris.
- Reprocessed 16 stored extractions and scores; three verdicts moved to AI 1.0 once leaked quoted text and signature furniture were stripped.

## [1.0.5] - 2026-07-24

Summary: Add per-list message preview and ranged fetch to the lists pane.

- Add an "Add" button per list row opening a two-tab popover: "New since last fetch" and "Before last fetch".
- Preview candidate messages server-side (sender, subject, date) before pulling anything.
- Add `POST /api/lists/preview`, a read-only header fetch used by the preview tabs.
- Add `POST /api/pull/range`, a directional pull that caps "all" at 1000 messages and never regresses the incremental cursor.
- Fetch, extract and score the chosen range from the popover.
- Replace the Show active / Show all button pair with a "Show All" switch.
- Rename and restyle the lists-pane header buttons to match export/import.

## [1.0.4] - 2026-07-23

Summary: Rewrite the documentation in a factual, impersonal style.

- Remove opinionated flourishes, colloquialisms and first-person voice from the README and everything under `docs/`.
- Add a hard rule to `CLAUDE.md` requiring simple, factual, impersonal documentation.

## [1.0.3] - 2026-07-23

Summary: Add app favicons and a detection-bar hover popup.

- Add `favicon.svg`, `favicon-32.png` and `apple-touch-icon.png`, served from `frontend/public` to the `dist` root, and reference them from `index.html`.
- Show a popup on mix-bar hover giving every label's share ("Human (x%) · Mixed (x%) · Assisted (x%) · AI (x%)").
- Teleport the popup to `<body>` with fixed positioning so scroll-clipping panes cannot cut it off.
- Tighten the shared `MIX_CAPTION` header to "Human·Mixed·Assisted·AI" so the uppercased column header fits on one line.

## [1.0.2] - 2026-07-23

Summary: Add export/import to the dashboard and document the CLI commands.

- Add export and import buttons to the Messages pane header, right of the filter controls.
- Export the active list filter's list, or every list with messages when no filter is set, as the gzip JSON Lines export format.
- Import an uploaded export file and show a compact result digest (inserted / skipped / updated, body mismatches when nonzero) or the server's error, then refresh the pane.
- Add `GET /api/export[?list=<name>]`, streaming the export as an attachment named `mlac-export-<list>-<date>.jsonl.gz`.
- Add `POST /api/import`, taking a multipart upload with an optional `dry_run` and returning the import summary as JSON, or 400 with the reason on validation failure.
- Add an "Exporting and importing lists" section to the README covering `mail-ai-export` and `mail-ai-import`.
- Stop hardcoding the version in the README; it points at `mailing_list_ai_check.__version__`.

## [1.0.1] - 2026-07-23

Summary: Improve the dashboard filters and the sender pane.

- Offer only lists that have messages in the list filter dropdown, instead of every list in the IMAP index.
- Offer every sender in the displayed list(s) — linked persons and unlinked addresses — in the From filter, scoped live to the list filter.
- Set the address filter when an unlinked address is picked; "anyone" clears both sender filters.
- Show an unlinked sender's display name above the email address on the sender detail card, instead of the bare address as the title.

## [1.0.0] - 2026-07-23

Summary: First versioned release, adding export/import of lists with their full pipeline state.

- Add `mail-ai-export` and `mail-ai-import`, moving a list row, pull cursor, senders/persons, messages, extractions and Pangram scores between databases as one JSON Lines file (gzip via a `.gz` suffix).
- Document the format and design in `docs/export-import.md`.
- Store extraction text as a full-body marker or a character span into the static `raw_body`, inlining it only when it is not a contiguous substring, always with a SHA-256 the importer verifies.
- Carry what was sent to Pangram as its stored `text_sha256` plus the verbatim `raw_response`.
- Make import all-or-nothing: one transaction, rolled back on any error, with `--dry-run` running the same path and rolling back.
- Dedupe messages on (list, Message-ID) so re-imports are no-ops, skipping existing rows and their embedded extraction and score.
- Warn about, and never overwrite, a skipped message whose stored body differs from the file copy.
- Refresh an existing message's extraction and score when the file copy carries a later pipeline version and the derived data differs; otherwise advance only its version stamp.
- Introduce semantic versioning at 1.0.0, sourced solely from `mailing_list_ai_check.__version__`, with `pyproject.toml` reading it dynamically.
- Add migration 007 (`messages.pipeline_version`), stamped by pull, extract and score so each message records the version that last processed it.
- Record the app version in the export header and each message's pipeline version in its record.
- Document the bump policy in `CLAUDE.md` and the README: minor for extraction and post-extraction processing changes, patch for everything else.
