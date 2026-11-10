# Pangram output reference

Every value Pangram returns for a scored text, what it means, and what this
application does with it.

Pangram restates the same judgement in several vocabularies at once: one
document produces a long sentence, a compact category, a headline phrase, three
fractions, three segment counts, and a per-window label with its own score.
None of these are independent measurements — they are different projections of
one windowed classification. This document lists each field, its value domain,
and the relationship between them so that two fields disagreeing in wording can
be read without ambiguity.

Two sources are used:

- The vendor contract (endpoints, request schema, error codes, documented field
  list) is in [`findings/pangram.md`](findings/pangram.md).
- The observed values below come from this repository's own corpus: 481 scored
  messages, detector version `3.3.2`, scored 2026-07-21 to 2026-07-24. Observed
  ranges describe that corpus, not the vendor's full value space; fields
  documented by the vendor but never seen are listed in section 7.

---

## 1. Response envelope

| Field | Type | Values | Meaning |
|---|---|---|---|
| `task_id` | str | opaque | Returned by `POST /task`; used to poll `GET /task/{task_id}`. Not part of the result. |
| `stage` | str | `STAGE_SUCCESS` (481/481) | Async task state. `STAGE_SUCCESS` and `STAGE_FAILED` are terminal; other values mean "keep polling". |
| `version` | str | `3.3.2` (481/481) | Detector version. Stored as `scores.detector_version`. |
| `text` | str | — | Verbatim echo of the submitted text. Window offsets index into it. |
| `detail` | str | — | Failure message, present with `STAGE_FAILED` instead of a result. |

---

## 2. Document-level verdict fields

Six field groups describe the whole document. They are ordered here from most
verbose to most mechanical.

| Field | Type | Observed values | What it adds |
|---|---|---|---|
| `prediction` | str | 9 distinct sentences (listed below) | Free-text sentence. Not an enumeration; wording varies with the mix, including trailing-period inconsistency. |
| `prediction_short` | str | `Human` (339), `AI` (124), `Mixed` (18) | The only categorical field. Three values in practice — see section 7 on `AI-Assisted`. |
| `headline` | str | `Human Written` (338), `AI Generated` (122), `AI Detected` (17), `AI Assisted` (2), `Mostly Human Written` (1), `Mostly Human, AI Detected` (1) | Short display phrase. Finer-grained than `prediction_short` but still free text. |
| `fraction_ai`, `fraction_ai_assisted`, `fraction_human` | float | 0.0–1.0, summing to 1.0 in all 481 responses | Share of the document's characters in each bucket (section 4). Recomputing them from the window spans matches to ~1e-8 rather than bit-exactly, consistent with 32-bit float serialization. |
| `num_ai_segments`, `num_ai_assisted_segments`, `num_human_segments` | int | 0–6, 0–2, 0–6 | Count of windows carrying each bucket's label. The three always sum to `len(windows)`. |
| `windows` | list | 1–7 entries per document | The underlying per-segment classification (section 3). |

### `prediction` sentences observed

| Count | Sentence |
|---|---|
| 338 | We believe that this document is fully human-written |
| 122 | We believe that this document is fully AI-generated |
| 9 | We believe that this document is a mix of AI-generated, and human-written content |
| 6 | We believe that this document is a mix of AI-generated, AI-assisted, and human-written content |
| 2 | We believe that this document is lightly AI-assisted, but not fully AI-generated. |
| 1 | We believe that this document is primarily human-written, with a small amount of AI content detected |
| 1 | We believe that this document is primarily human-written, with some AI-generated content detected |
| 1 | We believe that this document is primarily AI-generated with some human-written content |
| 1 | We believe that this document is mainly AI-generated, with some AI-assisted content. |

### Crosswalk — the combinations that actually occur

| Count | `prediction_short` | `headline` | `prediction` |
|---|---|---|---|
| 338 | `Human` | Human Written | fully human-written |
| 122 | `AI` | AI Generated | fully AI-generated |
| 9 | `Mixed` | AI Detected | a mix of AI-generated, and human-written content |
| 6 | `Mixed` | AI Detected | a mix of AI-generated, AI-assisted, and human-written content |
| 2 | `Mixed` | AI Assisted | lightly AI-assisted, but not fully AI-generated |
| 1 | `Human` | Mostly Human Written | primarily human-written, with a small amount of AI content detected |
| 1 | `AI` | AI Detected | primarily AI-generated with some human-written content |
| 1 | `AI` | AI Detected | mainly AI-generated, with some AI-assisted content |
| 1 | `Mixed` | Mostly Human, AI Detected | primarily human-written, with some AI-generated content detected |

Consequences of this table:

- `headline` is not a function of `prediction_short`: `AI Detected` appears
  under both `Mixed` and `AI`.
- `prediction_short: Human` does not imply `fraction_ai == 0`: the
  `Mostly Human Written` row has `fraction_ai == 0.053`.
- `prediction_short: Mixed` covers two different situations — an AI/human mix
  and text that is entirely AI-assisted with no AI-generated part.

---

## 3. `windows[]` — the per-segment classification

Windows tile the document: sorted by `start_index` they are contiguous and
non-overlapping, with no gaps, and the last `end_index` equals `len(text)`.
Each window's `text` is exactly `text[start_index:end_index]`.

| Field | Type | Observed values | Meaning |
|---|---|---|---|
| `text` | str | — | The window's own text. |
| `label` | str | `Human Written` (448), `AI-Generated` (194), `Lightly AI-Assisted` (7), `Moderately AI-Assisted` (3) | The window's verdict. Note the vocabulary differs from every document-level field: hyphenation and wording do not match `headline` or `prediction_short`. |
| `ai_assistance_score` | float | 0.0018–0.9940 | The only continuous score Pangram emits. Per window only; there is no document-level equivalent. |
| `confidence` | str | `High` (496), `Medium` (113), `Low` (43) | Confidence in that window's label. Not numeric, and independent of the score: `High` spans 0.0018–0.9940 and `Low` spans 0.0614–0.7765. |

`ai_assistance_score` is printed with 11 to 19 decimal places, but every value
in the corpus round-trips exactly through IEEE-754 single precision, so the
resolution is about 7 significant decimal digits and the remaining digits are a
serialization artefact. The values are not quantised to a coarser grid: 647 of
652 are distinct, with a smallest gap of 1.6e-8. Only about two decimal places
are meaningful in use, since the label cut points sit near 0.25, 0.5 and 0.75
and the document fractions are derived from the labels rather than the scores.
The document-level `fraction_*` fields print the same way, apart from the exact
`0.0` and `1.0` values that single-window documents produce.
| `start_index`, `end_index` | int | 0–13806, 254–14040 | Character offsets into `text`. |
| `word_count` | int | 5–409 | Words in the window. Windows far below Pangram's own 50-word reliability floor occur routinely inside longer documents (29 of 652). Pangram's count also differs slightly from the pipeline's: one single-window document reports 48 words having passed the 50-word gate. |
| `token_length` | int | 13–500 | Tokens in the window. No window exceeds 500 and 6 sit exactly at 500, consistent with a 500-token window cap. |

### Window size

Windowing is capped on tokens, not on words or characters. Across 652 windows:

| Measure | Min | Median | p90 | Max |
|---|---|---|---|---|
| `token_length` | 13 | 294 | 484 | 500 |
| `word_count` | 5 | 177 | 316 | 409 |
| characters (`end_index − start_index`) | 60 | 1,222 | 2,070 | 2,839 |

Six windows sit at exactly 500 tokens and 47 at 490 or above, with none over
500. The word and character maxima are therefore incidental: token density
ranges from 1.7 to 6.1 characters per token, so the 500-token windows span
1,367 characters / 105 words of identifier-dense text up to 2,473 characters /
387 words of prose. As a working figure, one window holds about 400 words or
2,500 characters of ordinary prose, and proportionally fewer for text with long
tokens such as URLs or code. The vendor material records no window size, so
these figures are measurements rather than a documented limit.

`label` and `ai_assistance_score` are consistent with fixed cut points at
approximately 0.25, 0.5 and 0.75:

| `label` | Windows | `ai_assistance_score` range |
|---|---|---|
| `Human Written` | 448 | 0.0018 – 0.2482 |
| `Lightly AI-Assisted` | 7 | 0.2554 – 0.4766 |
| `Moderately AI-Assisted` | 3 | 0.5154 – 0.6241 |
| `AI-Generated` | 194 | 0.7661 – 0.9940 |

Both assisted labels roll up into `fraction_ai_assisted` and
`num_ai_assisted_segments`.

Window counts per document: 1 window in 374 of 481 messages, 2 in 67, 3 in 27,
4 in 7, 5 in 2, 6 in 3, 7 in 1. Most mailing-list messages therefore yield a
single window, which forces the document fractions to 0.0 or 1.0.

---

## 4. How the document fractions are derived

Each `fraction_*` is the share of the document's characters whose window carries
that bucket's label:

```
fraction_X = Σ (end_index − start_index) over windows labelled X
             ────────────────────────────────────────────────────
             Σ (end_index − start_index) over all windows
```

Verified against all 481 stored responses: character-span weighting reproduces
all three fractions exactly (differences ≤ 1.5e-8, i.e. float32 rounding).
Weighting by segment count, `word_count` or `token_length` does not reproduce
them.

Two consequences:

- The fractions are **length shares, not probabilities or confidences**. A
  document with `fraction_ai_assisted == 1.0` is one where every window was
  labelled assisted, which for a single-window document means one window. The
  strength of that single judgement is in `windows[0].ai_assistance_score` and
  `windows[0].confidence`, not in the fraction.
- A long human passage plus one short AI passage produces a small `fraction_ai`
  even though an AI-generated segment was detected. `headline` may still say
  `AI Detected`.

---

## 5. What this application stores and derives

Stored per score (`scores` table, see `store.py`):

| Column | Source |
|---|---|
| `fraction_ai`, `fraction_ai_assisted`, `fraction_human` | verbatim |
| `detector_version` | `version` |
| `raw_response` | the complete JSON response, verbatim, including `text` and `windows` |
| `label` | derived — see below |
| `text_sha256`, `scored_at` | computed locally |

`label` is the dashboard's four-band vocabulary and is the one value this
application invents. Because `prediction_short` never emits `AI-Assisted`,
`PangramResult.label` (`pangram.py`) rebadges a `Mixed` verdict as
`AI-Assisted` when `fraction_ai_assisted` exceeds both other fractions; every
other verdict passes `prediction_short` through unchanged. Corpus result:
`Human` 339, `AI` 124, `Mixed` 14, `AI-Assisted` 4.

The message-list API (`/api/messages`, `_serialize_message_row`) exposes the
three fractions, the derived `label`, `prediction_short` and `headline` read
back out of `raw_response`, each window's `ai_assistance_score` and
`confidence`, `detector_version` and `scored_at`.

The detail API (`/api/messages/<id>`) adds, per window, its `label`, `chars`,
`word_count` and where it sits in the extracted text: `start` and `end` are
`{line, col}` pairs, 0-based into `extracted_text.split("\n")`. Because scoring
sends `clean_for_scoring(extracted_text)`, window offsets index a text whose
lines are a subsequence of the extracted ones; `_window_details` walks the two
in order to convert them, and reports `null` positions for a window it cannot
locate (a message re-extracted after it was scored). Leading and trailing
whitespace is trimmed off each window first, so a position marks its first real
character. The stored `raw_response` is not returned by the detail API; it is
available through the full export.

Dashboard use:

| Element | Value shown |
|---|---|
| ANALYSIS pill | `prediction_short`, coloured blue / orange / red |
| ANALYSIS text | `headline`, verbatim and uncoloured |
| AI SCORE (CONFIDENCE) column | every window's `ai_assistance_score` at two decimal places with its `confidence` abbreviated to H / M / L; hover gives the window count and the full list |
| Label filters, aggregate bars, flagged counts | the derived four-band `label` (aggregate bars fold `AI-Assisted` back into `Mixed`) |
| Detail drawer analysis card | `prediction_short`, `headline`, `detector_version`, and a table of every window's chars, score, confidence and label |
| Detail drawer text | a numbered marker at each window's first character and a bracket spanning its lines, both hovering to that window's row |

Two gates are this application's, not Pangram's: the 50-word "too short to
score" floor (`SCORE_MIN_WORDS` in `cli.py`) and the extraction step that
decides what text is submitted at all.

`public_dashboard_link` is sent as `false`, so no `dashboard_link` is returned.

---

## 6. Reading a result

1. For a categorical decision, use `prediction_short` (or the derived
   four-band `label`). It is the only enumerated document-level field.
2. For "how much AI", use the fractions, remembering they are length shares.
3. For "how sure", use the per-window `ai_assistance_score` and `confidence`.
   No document-level equivalent exists.
4. Treat `prediction` and `headline` as display strings. Both are free text
   whose wording varies with the mix; neither is stable enough to branch on.
5. Expect the vocabularies to differ between levels: `AI-Generated` (window)
   versus `AI Generated` (headline) versus `AI` (`prediction_short`) versus
   `fully AI-generated` (prediction) all describe the same bucket.

---

## 7. Documented or implied but not observed

- `prediction_short: "AI-Assisted"` — listed in the vendor material recorded in
  [`findings/pangram.md`](findings/pangram.md), never emitted in 481 responses.
  Assisted-dominant text arrives as `Mixed`, which is why the four-band `label`
  exists.
- A window label above `Moderately AI-Assisted` (the naming implies a heavier
  band) — not seen; the 0.62–0.77 score gap between the highest assisted window
  and the lowest `AI-Generated` window is unpopulated in this corpus.
- `dashboard_link` — not requested.
- `STAGE_FAILED`, non-terminal stages, and the HTTP error codes in
  [`findings/pangram.md`](findings/pangram.md) — handled by the client, absent
  from stored results by construction.
