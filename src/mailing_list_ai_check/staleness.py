"""Detection and repair of extractions derived by an older extraction routine.

The routine that derives an extraction's text carries its own generation number,
:data:`~email_reply_extractor.extraction.EXTRACTION_VERSION`, incremented by hand
whenever that routine changes; every ``extractions`` row records the generation
that produced its text (``extractions.extraction_version``). A row whose stamp is
lower than the running value may therefore hold text the current routine would
not produce. :func:`check` performs exactly that comparison and nothing else; it
is what the dashboard runs at start-up. The app's semantic version plays no part:
releases that do not touch the pipeline leave the generation alone. The
comparison is ``<`` rather than ``!=`` on purpose, so a store written by a *newer*
routine and opened by an older app reads as current instead of offering to
downgrade text that is better than what this build can derive.

The generation stamp decides only whether to offer the check, never the answer.
:func:`diff` re-derives every stored extraction with the current routine and
compares the result against what is stored, so the affected set is measured
rather than inferred. Rows that come out identical are stamped with the running
generation — they are provably current whatever their old stamp said — and drop out
of the report, which is also what stops a store with no real differences from
being reported stale again on the next start-up. :func:`reextract` then rewrites
only the rows the caller passes in.

Two texts are compared per message, because either can move independently:

- the extracted text, which is what the dashboard displays; and
- the cleaned text (:func:`~email_reply_extractor.cleaning.clean_for_scoring`),
  which is what a score is a verdict on.

A cleaned text changed *in substance* is what invalidates a score: the
comparison is :func:`email_reply_extractor.texts_equivalent`, so a cleaned text
whose bytes moved but whose non-blank, edge-stripped lines are identical (the
usual effect of a generation whose changes are mostly whitespace, such as
generation 5) keeps its verdict, re-keyed to the new text's hash. Only a
substantive change makes :func:`reextract` delete the score row and report the
message as needing a re-score. A message whose extracted text changed but whose
cleaned text did not keeps its verdict untouched, and costs nothing.

The generation is not the only thing an extraction's text depends on. A reply
is derived against the body of its thread parent, resolved from ``In-Reply-To``
(:meth:`~mailing_list_ai_check.store.Store.get_parent_body`), so a reply
extracted while its parent was absent holds text the same generation would not
produce once the parent is stored. :func:`refresh_for_new_parents` covers that
case: it finds the messages stored since it last ran, re-derives the
already-extracted replies to them through :func:`reextract`, and records how far
it has checked. The generation comparison in :func:`check` does not see this
kind of drift, which is why it is repaired as part of the pipeline rather than
offered on the dashboard.

Nothing in this module calls IMAP or Pangram: it re-runs local extraction and
cleaning only. Re-scoring the rewritten rows is the caller's separate, paid step
(:func:`mailing_list_ai_check.cli.run_score`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from . import __version__
from email_reply_extractor import clean_for_scoring
from email_reply_extractor import EXTRACTION_VERSION, extract_new_text
from email_reply_extractor import split_html_parts, texts_equivalent
from .store import SETTING_PARENT_CHECK_MESSAGE_ID, Extraction, Message, Store, sha256_text

# --- reports ------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationCount:
    """How many extractions carry one generation stamp, and whether it is old."""

    extraction_version: int | None
    count: int
    stale: bool


@dataclass(frozen=True)
class StalenessReport:
    """The result of :func:`check` — a generation comparison, no text re-derived."""

    app_version: str
    extraction_version: int
    stale: bool
    stale_count: int
    current_count: int
    total: int
    versions: list[GenerationCount] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractionDiff:
    """One message whose stored extraction differs from a fresh re-derivation."""

    message_id: int
    extraction_id: int
    list_name: str | None
    date: str | None
    subject: str | None
    from_address: str | None
    from_display_name: str | None
    pipeline_version: str | None
    extraction_version: int | None
    old_chars: int
    new_chars: int
    old_status: str
    new_status: str
    text_changed: bool
    scored_text_changed: bool
    scored_text_changed_in_substance: bool
    scored: bool


@dataclass(frozen=True)
class DiffReport:
    """The result of :func:`diff`: what was checked, and what differs."""

    app_version: str
    checked: int
    unchanged: int
    stamped: int
    differing: list[ExtractionDiff] = field(default_factory=list)


@dataclass
class ReextractSummary:
    """Tally of one :func:`reextract` run.

    ``rescore_message_ids`` are the messages whose extraction now has no score
    and an ``ok`` status — the set worth handing to the scoring stage.
    ``scores_carried`` counts verdicts kept because the cleaned text moved only
    in whitespace, each re-keyed to the new text's hash.
    """

    processed: int = 0
    rewritten: int = 0
    unchanged: int = 0
    not_ok: int = 0
    scores_invalidated: int = 0
    scores_carried: int = 0
    rescore_message_ids: list[int] = field(default_factory=list)


@dataclass
class ParentRefreshSummary:
    """Tally of one :func:`refresh_for_new_parents` run.

    ``new_parents`` counts the Message-IDs first stored since the previous run;
    ``reextract`` is the :func:`reextract` tally over the already-extracted
    replies to them (all zero when there were none).
    """

    new_parents: int = 0
    reextract: ReextractSummary = field(default_factory=ReextractSummary)

    @property
    def rescore_message_ids(self) -> list[int]:
        return self.reextract.rescore_message_ids

    def as_line(self) -> str:
        r = self.reextract
        return (
            f"new_parents={self.new_parents} replies_rechecked={r.processed} "
            f"rewritten={r.rewritten} unchanged={r.unchanged} "
            f"scores_invalidated={r.scores_invalidated} scores_carried={r.scores_carried} "
            f"to_rescore={len(r.rescore_message_ids)}"
        )


# --- shared re-derivation -----------------------------------------------------


@dataclass(frozen=True)
class _Rederived:
    """A fresh extraction for one message, next to what is stored for it."""

    extraction: Extraction
    text: str
    method: str
    status: str
    scored_text: str
    stored_scored_text: str

    @property
    def text_changed(self) -> bool:
        return self.text != self.extraction.extracted_text

    @property
    def scored_text_changed(self) -> bool:
        return self.scored_text != self.stored_scored_text

    @property
    def scored_text_changed_in_substance(self) -> bool:
        """True when the cleaned text moved beyond whitespace — the rescore test."""
        return self.scored_text_changed and not texts_equivalent(
            self.scored_text, self.stored_scored_text
        )

    @property
    def changed(self) -> bool:
        return self.text_changed or self.scored_text_changed


def _signature_hint(message: Message) -> str | None:
    """The HTML signature-container text for a message, or ``None``.

    The same stage-2 cleaning hint :func:`mailing_list_ai_check.cli.run_score`
    passes, so a cleaned text computed here matches the one that would be sent to
    Pangram.
    """
    if not message.raw_html:
        return None
    return split_html_parts(message.raw_html).signature_text or None


def _rederive(store: Store, message: Message, extraction: Extraction) -> _Rederived:
    """Re-run extraction and cleaning for one message; write nothing.

    Mirrors :func:`mailing_list_ai_check.cli.run_extract`'s call exactly —
    including the thread-parent body resolved from ``In-Reply-To`` — so the text
    is what a fresh pipeline run would store.
    """
    parent_body = (
        store.get_parent_body(message.in_reply_to, exclude_message_id=message.message_id)
        if message.in_reply_to
        else None
    )
    result = extract_new_text(message.raw_body, parent_body, html_body=message.raw_html)
    hint = _signature_hint(message)
    return _Rederived(
        extraction=extraction,
        text=result.text,
        method=result.method,
        status=result.status,
        scored_text=clean_for_scoring(result.text, hint).text,
        stored_scored_text=clean_for_scoring(extraction.extracted_text, hint).text,
    )


# --- the three operations -----------------------------------------------------


def check(store: Store) -> StalenessReport:
    """Report whether any stored extraction predates the current routine.

    A generation comparison over one grouped query — no message is read and no
    text is re-derived, so this is cheap enough to run on every dashboard load.
    An extraction is counted stale when its ``extraction_version`` is lower than
    the running :data:`~email_reply_extractor.extraction.EXTRACTION_VERSION`
    (NULL counts as 0, older than every generation, and a stamp *above* the
    running value is not stale — see the module docstring); a store with no
    extractions at all is never stale.
    """
    versions: list[GenerationCount] = []
    stale_count = 0
    current_count = 0
    for generation, count in store.extraction_version_counts():
        is_stale = (generation or 0) < EXTRACTION_VERSION
        versions.append(GenerationCount(extraction_version=generation, count=count, stale=is_stale))
        if is_stale:
            stale_count += count
        else:
            current_count += count
    return StalenessReport(
        app_version=__version__,
        extraction_version=EXTRACTION_VERSION,
        stale=stale_count > 0,
        stale_count=stale_count,
        current_count=current_count,
        total=stale_count + current_count,
        versions=versions,
    )


def diff(store: Store) -> DiffReport:
    """Re-derive every stored extraction and report the ones that differ.

    Walks every message that has an extraction, re-runs extraction and cleaning
    (:func:`_rederive`), and compares both the extracted and the cleaned text
    against what is stored. Rows that match are stamped with the running
    generation and counted in ``unchanged``; the rest are returned in
    ``differing``, ordered by message id. The only write is that stamp: no text
    is rewritten, no score is touched, and Pangram is never called.
    """
    differing: list[ExtractionDiff] = []
    checked = unchanged = stamped = 0

    for message_pk in store.extracted_message_ids():
        message = store.get_message(message_pk)
        extraction = store.extraction_for_message(message_pk)
        if message is None or extraction is None:  # pragma: no cover - concurrent delete
            continue
        checked += 1
        rederived = _rederive(store, message, extraction)

        if not rederived.changed:
            unchanged += 1
            if extraction.extraction_version != EXTRACTION_VERSION:
                store.set_extraction_version(extraction.id)
                stamped += 1
            continue

        mailing_list = store.get_list(message.list_id)
        address = store.get_address(message.address_id) if message.address_id is not None else None
        differing.append(
            ExtractionDiff(
                message_id=message.id,
                extraction_id=extraction.id,
                list_name=mailing_list.name if mailing_list else None,
                date=message.date,
                subject=message.subject,
                from_address=address.email if address else None,
                from_display_name=message.from_name or (address.display_name if address else None),
                pipeline_version=extraction.pipeline_version,
                extraction_version=extraction.extraction_version,
                old_chars=len(extraction.extracted_text),
                new_chars=len(rederived.text),
                old_status=extraction.status,
                new_status=rederived.status,
                text_changed=rederived.text_changed,
                scored_text_changed=rederived.scored_text_changed,
                scored_text_changed_in_substance=rederived.scored_text_changed_in_substance,
                scored=store.score_for_extraction(extraction.id) is not None,
            )
        )

    return DiffReport(
        app_version=__version__,
        checked=checked,
        unchanged=unchanged,
        stamped=stamped,
        differing=differing,
    )


def reextract(store: Store, message_ids: Sequence[int]) -> ReextractSummary:
    """Re-extract the given messages, rewriting the rows whose text has moved.

    For each message with an extraction row: re-derive it, and

    - if neither the extracted nor the cleaned text changed, only stamp the
      generation (counted in ``unchanged``);
    - otherwise rewrite the extraction in place
      (:meth:`~mailing_list_ai_check.store.Store.replace_extraction`). A score
      is deleted only when the cleaned text changed **in substance**
      (:func:`email_reply_extractor.texts_equivalent`) — that verdict was
      reached on text that no longer exists. A cleaned text that moved only in
      whitespace keeps its verdict, re-keyed to the new text's hash
      (:meth:`~mailing_list_ai_check.store.Store.rekey_score_for_extraction`).

    Messages without an extraction row are skipped. Nothing is re-scored here:
    ``rescore_message_ids`` lists the messages whose extraction is now ``ok`` and
    unscored, for the caller to pass to the scoring stage.
    """
    summary = ReextractSummary()

    for message_pk in message_ids:
        message = store.get_message(message_pk)
        extraction = store.extraction_for_message(message_pk)
        if message is None or extraction is None:
            continue
        summary.processed += 1
        rederived = _rederive(store, message, extraction)

        if not rederived.changed:
            summary.unchanged += 1
            if extraction.extraction_version != EXTRACTION_VERSION:
                store.set_extraction_version(extraction.id)
            continue

        store.replace_extraction(
            extraction.id,
            extracted_text=rederived.text,
            method=rederived.method,
            status=rederived.status,
        )
        summary.rewritten += 1
        if rederived.status != "ok":
            summary.not_ok += 1

        if rederived.scored_text_changed:
            if rederived.scored_text_changed_in_substance:
                if store.delete_score_for_extraction(extraction.id):
                    summary.scores_invalidated += 1
            elif store.rekey_score_for_extraction(
                extraction.id, sha256_text(rederived.scored_text)
            ):
                summary.scores_carried += 1

        if rederived.status == "ok" and store.score_for_extraction(extraction.id) is None:
            summary.rescore_message_ids.append(message_pk)

    # Rewritten extractions change char_count/status, the reply-timing inputs.
    if summary.rewritten:
        store.recompute_timing()

    return summary


def refresh_for_new_parents(store: Store) -> ParentRefreshSummary:
    """Re-derive extracted replies whose thread parent has been stored since.

    A reply extracted before its parent was in the store was derived without
    the parent-diff step. This finds the Message-IDs first stored since the
    previous call, selects the already-extracted messages whose ``In-Reply-To``
    names one of them, and passes those to :func:`reextract` — which rewrites
    only rows whose text moved, deletes a score only on a change of substance,
    and returns the messages left needing a score. Those messages need no
    hand-over: an ``ok`` extraction with no score is the scoring queue.

    Progress is one stored number, the highest ``messages.id`` already checked
    (``app_settings`` key :data:`~mailing_list_ai_check.store.SETTING_PARENT_CHECK_MESSAGE_ID`),
    written after the re-derivation completes. A call with no message stored
    since the previous one is two indexed lookups; an interrupted call is
    repeated in full by the next one, and repeating it changes nothing. Every
    route into ``messages`` is covered (CLI pull, dashboard pull, import)
    because the check reads the table, not a pull's own tally.

    On a store that has never been checked, the starting point is the highest
    message id that has an extraction: messages stored after the last extracted
    one are treated as new, and replies to parents stored before it are not
    revisited.
    """
    summary = ParentRefreshSummary()
    newest = store.max_message_pk()
    stored = store.get_setting(SETTING_PARENT_CHECK_MESSAGE_ID)
    checked = int(stored) if stored is not None else store.max_extracted_message_pk()
    if newest <= checked:
        # Nothing new. A mark above the newest id means rows were deleted; pull
        # it back so ids reused by later inserts are not skipped.
        if stored is None or newest < checked:
            store.set_setting(SETTING_PARENT_CHECK_MESSAGE_ID, str(newest))
        return summary

    parents = store.message_ids_first_stored_after(checked)
    summary.new_parents = len(parents)
    if parents:
        replies = store.extracted_replies_to(parents)
        if replies:
            summary.reextract = reextract(store, replies)
    store.set_setting(SETTING_PARENT_CHECK_MESSAGE_ID, str(newest))
    return summary
