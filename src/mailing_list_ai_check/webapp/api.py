"""The ``/api`` JSON blueprint over the SQLite store.

Every endpoint the Vue dashboard needs lives here: a filterable, paginated
message explorer, message detail, overview aggregates, and the list / address /
person entities plus person management. The heavy filtered query itself lives in
:meth:`mailing_list_ai_check.store.Store.query_messages`; this module only parses and
validates request input, calls the store, and shapes the JSON.

Connection handling
-------------------
A fresh :class:`~mailing_list_ai_check.store.Store` (one ``sqlite3`` connection) is
opened per request via :func:`get_store` and closed on app-context teardown (see
:func:`mailing_list_ai_check.webapp.create_app`). Because the connection is created
and used within a single request — and therefore a single thread — the default
``sqlite3`` ``check_same_thread=True`` is correct and needs no relaxing, even
under the threaded dev server.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Blueprint, Response, current_app, g, jsonify, request

from .. import __version__
from email_reply_extractor import clean_for_scoring
from ..cli import _PRICE_PER_100_WORDS, run_extract, run_score
from ..export_import import ExportImportError, export_lists, import_file
from email_reply_extractor import EXTRACTION_VERSION
from email_reply_extractor import split_html_parts
from ..fetcher import (
    DepthMode,
    FetchRequest,
    open_client,
    parse_header,
    refresh_lists_index,
    resolve_folders,
    run_fetch,
    run_fetch_uids,
)
from ..imap_client import build_search_criteria
from ..pangram import DEFAULT_MODEL, MODEL_GENERATIONS, PangramClient, generation_for_model
from ..staleness import (
    ExtractionDiff,
    check as check_staleness,
    diff as diff_extractions,
    reextract,
)
from ..stats_export import export_stats
from ..store import (
    DEFAULT_PER_PAGE,
    MAX_PER_PAGE,
    SETTING_PANGRAM_MODEL,
    SETTING_PANGRAM_NOTICE,
    SORT_COLUMNS,
    TIMELINE_BUCKETS,
    MessageFilters,
    Store,
)

api_bp = Blueprint("api", __name__, url_prefix="/api")

#: Allowed characters in a mailing-list name (maps to an IMAP folder slug):
#: letters, digits, dot, hyphen, underscore. Guards against odd/injection-y
#: folder names before we ever touch the server.
_LIST_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Bounds on the per-pull message count (the field is the only cap on this
#: network+paid endpoint — there is no server-side testing limit here).
_MIN_PULL_COUNT = 1
_MAX_PULL_COUNT = 1000

#: Default page size for the Senders pane (denser than the message explorer).
_DEFAULT_SENDER_PER_PAGE = 60
#: Sort keys the Senders pane accepts, mapped to their default sort direction
#: (used when the request omits ``order``).
_SENDER_SORTS = {"count": "desc", "name": "asc", "ai": "desc"}


# --- errors -------------------------------------------------------------------


class ApiError(Exception):
    """An error to surface to the client as ``{"error": msg}`` with a status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# --- per-request store --------------------------------------------------------


def get_store() -> Store:
    """Return this request's :class:`Store`, opening one on first use."""
    if "store" not in g:
        g.store = Store(current_app.config["STORE_PATH"])
    return g.store


# --- input parsing / validation ----------------------------------------------


def _parse_int(name: str, raw: str | None) -> int | None:
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be an integer") from exc


def _parse_float(name: str, raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be a number") from exc


def _parse_bool(name: str, raw: str | None) -> bool | None:
    if raw is None or raw == "":
        return None
    low = raw.strip().lower()
    if low in ("1", "true", "yes"):
        return True
    if low in ("0", "false", "no"):
        return False
    raise ApiError(f"{name} must be a boolean (true/false)")


def _validate_iso(name: str, raw: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    try:
        # Accepts both date ("2026-03-01") and datetime forms.
        from datetime import datetime

        datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ApiError(f"{name} must be an ISO-8601 date or datetime") from exc
    return raw


def parse_filters(args: Any) -> MessageFilters:
    """Parse and validate the shared query params into a :class:`MessageFilters`.

    Raises :class:`ApiError` (400) on any malformed value so callers never see a
    500 for bad input. ``per_page`` above the cap is clamped, not rejected.
    """
    page = _parse_int("page", args.get("page"))
    if page is None:
        page = 1
    elif page < 1:
        raise ApiError("page must be >= 1")

    per_page = _parse_int("per_page", args.get("per_page"))
    if per_page is None:
        per_page = DEFAULT_PER_PAGE
    elif per_page < 1:
        raise ApiError("per_page must be >= 1")
    else:
        per_page = min(per_page, MAX_PER_PAGE)

    sort = args.get("sort", "date")
    if sort not in SORT_COLUMNS:
        raise ApiError(f"sort must be one of {sorted(SORT_COLUMNS)}")

    order = args.get("order", "desc").lower()
    if order not in ("asc", "desc"):
        raise ApiError("order must be 'asc' or 'desc'")

    min_l = _parse_float("min_likelihood", args.get("min_likelihood"))
    max_l = _parse_float("max_likelihood", args.get("max_likelihood"))
    for label, value in (("min_likelihood", min_l), ("max_likelihood", max_l)):
        if value is not None and not (0.0 <= value <= 1.0):
            raise ApiError(f"{label} must be between 0 and 1")

    # Inclusive bounds on the reply-timing rate (``messages.timing_cpm``). A
    # rate is a count per minute, so negatives are meaningless; either bound
    # excludes every message with no rate.
    cpm_min = _parse_float("cpm_min", args.get("cpm_min"))
    cpm_max = _parse_float("cpm_max", args.get("cpm_max"))
    for label, value in (("cpm_min", cpm_min), ("cpm_max", cpm_max)):
        if value is not None and value < 0:
            raise ApiError(f"{label} must be >= 0")

    return MessageFilters(
        list_name=args.get("list") or None,
        address=args.get("address") or None,
        person_id=_parse_int("person", args.get("person")),
        date_from=_validate_iso("date_from", args.get("date_from")),
        date_to=_validate_iso("date_to", args.get("date_to")),
        label=args.get("label") or None,
        min_likelihood=min_l,
        max_likelihood=max_l,
        q=args.get("q") or None,
        has_score=_parse_bool("has_score", args.get("has_score")),
        cpm_min=cpm_min,
        cpm_max=cpm_max,
        page=page,
        per_page=per_page,
        sort=sort,
        order=order,
    )


# --- serialization ------------------------------------------------------------


def _window_scores(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The per-window score and confidence from a stored Pangram response.

    One entry per window, in document order — the message list shows these in
    place of a single scalar, since Pangram emits no document-level score. The
    window text and offsets are deliberately left out: the list only needs the
    numbers, and the full response is available from the detail endpoint.
    ``label`` colours each entry by the window's verdict; ``is_humanized``
    rides along so the list can flag messages with a humanized window (null on
    rows scored under detector v3).
    """
    windows = (raw or {}).get("windows")
    if not isinstance(windows, list):
        return []
    out: list[dict[str, Any]] = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        out.append(
            {
                "ai_assistance_score": window.get("ai_assistance_score"),
                "confidence": window.get("confidence"),
                "label": window.get("label"),
                "is_humanized": window.get("is_humanized"),
            }
        )
    return out


def _analysed_to_extracted_lines(analysed: list[str], extracted: list[str]) -> list[int | None]:
    """Line-for-line map from the analysed text back to ``extracted_text``.

    Scoring sends ``clean_for_scoring(extracted_text)`` — the extracted text
    minus its furniture lines, each survivor rstripped — so window offsets index
    a text whose lines are a subsequence of the extracted ones. Walk both in
    order, matching on the rstripped line, and return the extracted-line index
    for each analysed line (``None`` where no match is found, e.g. because the
    message was re-extracted after it was scored).

    Blank analysed lines are left unmapped rather than matched to the next blank
    extracted line, which would let a run of blanks pull the walk out of step.
    """
    out: list[int | None] = []
    next_ext = 0
    for line in analysed:
        key = line.rstrip()
        if not key:
            out.append(None)
            continue
        found = None
        for idx in range(next_ext, len(extracted)):
            if extracted[idx].rstrip() == key:
                found = idx
                break
        out.append(found)
        if found is not None:
            next_ext = found + 1
    return out


def _window_details(raw: dict[str, Any] | None, extracted_text: str | None) -> list[dict[str, Any]]:
    """Per-window scores plus their position in ``extracted_text``.

    Positions are ``{line, col}`` pairs, line 0-based into
    ``extracted_text.split("\\n")`` and col a character offset within that line,
    so the dashboard can mark where each window starts and ends in the text it
    displays. They are ``None`` when the window cannot be located — the window
    is still reported, with its scores.

    Leading and trailing whitespace is trimmed off each window first: Pangram's
    windows routinely start with a blank line, and a marker belongs on the first
    real character, not at the end of the line before.
    """
    windows = (raw or {}).get("windows")
    if not isinstance(windows, list):
        return []

    analysed = (raw or {}).get("text")
    ext_lines = (extracted_text or "").split("\n")
    line_map: list[int | None] = []
    starts: list[int] = []
    if isinstance(analysed, str):
        analysed_lines = analysed.split("\n")
        line_map = _analysed_to_extracted_lines(analysed_lines, ext_lines)
        offset = 0
        for line in analysed_lines:
            starts.append(offset)
            offset += len(line) + 1

    def position(offset: int) -> dict[str, int] | None:
        """An analysed-text character offset as a line/col in extracted_text."""
        if not starts or not isinstance(analysed, str):
            return None
        # The last line whose start is at or before the offset.
        idx = bisect_right(starts, offset) - 1
        if idx < 0 or idx >= len(line_map):
            return None
        ext_line = line_map[idx]
        if ext_line is None:
            return None
        col = offset - starts[idx]
        return {"line": ext_line, "col": max(0, min(col, len(ext_lines[ext_line])))}

    out: list[dict[str, Any]] = []
    for i, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        start = window.get("start_index")
        end = window.get("end_index")
        text = window.get("text")
        if isinstance(start, int) and isinstance(end, int) and isinstance(text, str):
            start += len(text) - len(text.lstrip())
            end -= len(text) - len(text.rstrip())
        out.append(
            {
                "index": i + 1,
                "ai_assistance_score": window.get("ai_assistance_score"),
                "confidence": window.get("confidence"),
                "label": window.get("label"),
                # Pangram 4 humanizer verdicts; null on rows scored under v3.
                "is_humanized": window.get("is_humanized"),
                "humanizer_score": window.get("humanizer_score"),
                "word_count": window.get("word_count"),
                "chars": (end - start) if isinstance(start, int) and isinstance(end, int) else None,
                "start": position(start) if isinstance(start, int) else None,
                "end": position(end) if isinstance(end, int) else None,
            }
        )
    return out


def _serialize_message_row(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a :meth:`Store.query_messages` row into the list-item JSON.

    ``timing`` is the stored classification band and ``timing_cpm`` the stored
    chars/minute rate it was derived from, rounded to one decimal place and
    ``null`` whenever the band is (see :meth:`Store.recompute_timing`).
    """
    extraction = None
    if row["extraction_status"] is not None:
        extraction = {
            "status": row["extraction_status"],
            "method": row["extraction_method"],
            "char_count": row["extraction_char_count"],
        }
    score = None
    if row["scored_at"] is not None:
        # Pull prediction_short and the free-text headline out of the stored
        # Pangram response. The stored label is prediction_short verbatim (since
        # migration 013), so it is the fallback when raw is absent.
        raw = None
        if row.get("raw_response"):
            try:
                raw = json.loads(row["raw_response"])
            except ValueError, TypeError:
                raw = None
        label = row["label"]
        prediction_short = (raw or {}).get("prediction_short")
        if prediction_short is None:
            prediction_short = label
        score = {
            "fraction_ai": row["fraction_ai"],
            "fraction_ai_assisted": row["fraction_ai_assisted"],
            "fraction_human": row["fraction_human"],
            "label": label,
            "prediction_short": prediction_short,
            "headline": (raw or {}).get("headline"),
            "windows": _window_scores(raw),
            "detector_version": row["detector_version"],
            "scored_at": row["scored_at"],
        }
    person = None
    if row["person_id"] is not None:
        person = {"id": row["person_id"], "name": row["person_name"]}
    timing_cpm = row.get("timing_cpm")
    return {
        "id": row["id"],
        "message_id": row["message_id"],
        "list": row["list"],
        "date": row["date"],
        "subject": row["subject"],
        "timing": row["timing"],
        "timing_cpm": round(timing_cpm, 1) if timing_cpm is not None else None,
        "auto_generated": row["auto_generated"],
        # The message's own From name wins; the per-address name is the fallback
        # for rows fetched before migration 015 stored it (and for headers that
        # carried no display name at all).
        "from": {
            "address": row["from_address"],
            "display_name": row["from_name"] or row["from_display_name"],
        },
        "person": person,
        "extraction": extraction,
        "score": score,
    }


def _person_detail(store: Store, person_id: int) -> dict[str, Any] | None:
    """Build the canonical person JSON (name + attached addresses)."""
    person = store.get_person(person_id)
    if person is None:
        return None
    addrs = store.addresses_for_person(person_id)
    return {
        "id": person.id,
        "canonical_name": person.canonical_name,
        "addresses": [
            {"id": a.id, "email": a.email, "display_name": a.display_name} for a in addrs
        ],
    }


def _json_body() -> dict[str, Any]:
    """Return the request JSON object, or ``{}``; 400 if the body is not an object."""
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object")
    return data


def _address_id_list(data: dict[str, Any], key: str) -> list[int]:
    """Validate ``data[key]`` is a list of ints (empty if absent)."""
    raw = data.get(key, [])
    if not isinstance(raw, list):
        raise ApiError(f"{key} must be a list of address ids")
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ApiError(f"{key} must contain integer address ids")
        ids.append(item)
    return ids


def _validate_list_name(data: dict[str, Any]) -> str:
    """Validate and return the request's ``list`` (same rule as :func:`pull`).

    Raises a 400 ``ApiError`` for a missing/blank name or one containing any
    character outside :data:`_LIST_NAME_RE`.
    """
    list_name = data.get("list")
    if not isinstance(list_name, str) or not list_name.strip():
        raise ApiError("list is required")
    list_name = list_name.strip()
    if not _LIST_NAME_RE.match(list_name):
        raise ApiError("list name may contain only letters, digits, '.', '-' and '_'")
    return list_name


def _required_count(data: dict[str, Any], name: str) -> int:
    """Validate a required integer field in 1..1000 (same rule as :func:`pull`).

    A missing value, a non-integer, or a ``bool`` (never a valid integer here)
    is a 400; an out-of-range integer is a 400. Returns the validated value.
    """
    value = data.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(f"{name} must be an integer")
    if not (_MIN_PULL_COUNT <= value <= _MAX_PULL_COUNT):
        raise ApiError(f"{name} must be between {_MIN_PULL_COUNT} and {_MAX_PULL_COUNT}")
    return value


# --- message endpoints --------------------------------------------------------


@api_bp.get("/messages")
def list_messages() -> Any:
    filters = parse_filters(request.args)
    rows, total = get_store().query_messages(filters)
    pages = math.ceil(total / filters.per_page) if filters.per_page else 0
    return jsonify(
        {
            "messages": [_serialize_message_row(r) for r in rows],
            "page": filters.page,
            "per_page": filters.per_page,
            "total": total,
            "pages": pages,
            "sort": filters.sort,
            "order": filters.order,
        }
    )


@api_bp.get("/messages/<int:message_id>")
def message_detail(message_id: int) -> Any:
    store = get_store()
    msg = store.get_message(message_id)
    if msg is None:
        raise ApiError("message not found", 404)

    extraction = store.extraction_for_message(msg.id)
    score = store.score_for_extraction(extraction.id) if extraction is not None else None

    thread_parent_id = None
    if msg.in_reply_to:
        parent = store.find_message_by_message_id(msg.in_reply_to)
        if parent is not None and parent.id != msg.id:
            thread_parent_id = parent.id

    mailing_list = store.get_list(msg.list_id)
    address = store.get_address(msg.address_id) if msg.address_id is not None else None
    person = None
    if address is not None and address.person_id is not None:
        person_row = store.get_person(address.person_id)
        if person_row is not None:
            person = {"id": person_row.id, "name": person_row.canonical_name}

    extraction_json = None
    if extraction is not None:
        # Report what the scoring stage would remove so the dashboard can grey
        # those lines out. ``ignored_lines`` are 0-based indices into
        # ``extracted_text.split("\n")``; ``scored_word_count`` is the word count
        # of the cleaned text that would actually be sent to the detector. The
        # HTML signature hint (when the message has ``raw_html``) is applied so
        # these reflect exactly what scoring would drop.
        html_signature = split_html_parts(msg.raw_html).signature_text if msg.raw_html else None
        clean = clean_for_scoring(extraction.extracted_text, html_signature or None)
        extraction_json = {
            "status": extraction.status,
            "method": extraction.method,
            "char_count": extraction.char_count,
            "extracted_text": extraction.extracted_text,
            "ignored_lines": clean.ignored_lines,
            "scored_word_count": len(clean.text.split()),
        }

    score_json = None
    if score is not None:
        raw_response = None
        if score.raw_response:
            try:
                raw_response = json.loads(score.raw_response)
            except ValueError, TypeError:
                raw_response = None
        score_json = {
            "fraction_ai": score.fraction_ai,
            "fraction_ai_assisted": score.fraction_ai_assisted,
            "fraction_human": score.fraction_human,
            "label": score.label,
            "prediction_short": (raw_response or {}).get("prediction_short") or score.label,
            "headline": (raw_response or {}).get("headline"),
            "windows": _window_details(
                raw_response, extraction.extracted_text if extraction is not None else None
            ),
            "detector_version": score.detector_version,
            "scored_at": score.scored_at,
            "raw_response": raw_response,
        }

    return jsonify(
        {
            "id": msg.id,
            "message_id": msg.message_id,
            "list": mailing_list.name if mailing_list else None,
            "date": msg.date,
            "subject": msg.subject,
            "timing": msg.timing,
            "auto_generated": msg.auto_generated,
            "in_reply_to": msg.in_reply_to,
            "thread_parent_id": thread_parent_id,
            "raw_body": msg.raw_body,
            "from": {
                "address": address.email if address else None,
                # As in the list rows: this message's own name, else the address's.
                "display_name": msg.from_name or (address.display_name if address else None),
            },
            "person": person,
            "extraction": extraction_json,
            "score": score_json,
        }
    )


@api_bp.get("/summary")
def summary() -> Any:
    filters = parse_filters(request.args)
    return jsonify(get_store().summary(filters))


@api_bp.get("/messages/timeline")
def messages_timeline() -> Any:
    """The filtered message set as one slim timeline (the messages pane's rug).

    Takes the same query params as ``GET /messages`` (pagination and sort are
    ignored) and returns ``{"start": s, "end": e, "total": n, "undated": u,
    "buckets": [...], "points": [...]}`` — ``start``/``end`` in epoch seconds
    over the dated matches (``null`` when there is none), ``buckets`` naming
    the point buckets by index, and each point ``[id, t, bucket]`` ordered by
    ``(date, id)`` ascending with ``t`` in epoch seconds. Undated messages
    carry no point (see :meth:`Store.message_timeline`).
    """
    filters = parse_filters(request.args)
    result = get_store().message_timeline(filters)
    result["buckets"] = list(TIMELINE_BUCKETS)
    return jsonify(result)


# --- pull (fetch + extract + score) ------------------------------------------


def _fetch_for_list(config: Any, store: Store, list_name: str, count: int) -> Any:
    """Run the Phase 2 fetcher for one list in ``--count`` mode.

    Mirrors the way ``cli.py``'s pull command wires the fetcher: open a client,
    resolve the single list to its folder, fetch the most recent ``count``
    messages, and always close/log out. Any IMAP-side failure (connection error,
    unknown folder) is surfaced as a 502 ``ApiError`` — never a 500 traceback.
    """
    try:
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc

    try:
        folders = resolve_folders(client, [list_name], all_lists=False)
        fetch_request = FetchRequest(
            folders=tuple(folders),
            depth=DepthMode(count=count),
            limit=count,
        )
        return run_fetch(client, store, fetch_request)
    except Exception as exc:  # noqa: BLE001 - IMAP/fetch failures become a 502
        raise ApiError(f"IMAP fetch failed for list {list_name!r}: {exc}", 502) from exc
    finally:
        try:
            client.close()
            client.logout()
        except Exception:  # noqa: BLE001 - never let teardown mask the real result
            pass


# --- settings -----------------------------------------------------------------
#
# Dashboard choices that must survive a restart live in the store's
# ``app_settings`` table. Only the Pangram detector selector is exposed here;
# the upgrade-notice state has its own endpoints below because its GET carries
# the counts the notice needs.


def _active_model(store: Store) -> str:
    """Return the Pangram detector selector every scoring run should send.

    The stored setting when one has been chosen, otherwise the client default
    (:data:`~mailing_list_ai_check.pangram.DEFAULT_MODEL`, Pangram 4).
    """
    return store.get_setting(SETTING_PANGRAM_MODEL) or DEFAULT_MODEL


@api_bp.get("/capabilities")
def capabilities() -> Any:
    """Report the instance's capability flags so the frontend can adapt its UI.

    These mirror the deployment settings the server enforces anyway (see
    :func:`mailing_list_ai_check.webapp.create_app`): ``public_readonly`` is on
    when every state-changing request is refused, and ``allow_export`` /
    ``allow_stats_export`` say whether each export download is served. The client
    reads them to hide controls that would only 403; the values are advisory to
    the UI and never a substitute for the server-side checks.
    """
    cfg = current_app.config
    return jsonify(
        {
            "public_readonly": bool(cfg.get("PUBLIC_READONLY", False)),
            "allow_export": bool(cfg.get("ALLOW_EXPORT", True)),
            "allow_stats_export": bool(cfg.get("ALLOW_STATS_EXPORT", True)),
        }
    )


@api_bp.get("/settings")
def get_settings() -> Any:
    """Return the persisted dashboard settings.

    Currently one key: ``pangram_model``, the detector selector scoring sends —
    ``"pangram-4"`` for Pangram 4, or ``"default"`` for the API's default
    selector, which resolves to Pangram 3 until that generation is deprecated.
    An unset setting reads as ``"pangram-4"``.
    """
    return jsonify({"pangram_model": _active_model(get_store())})


@api_bp.put("/settings")
def put_settings() -> Any:
    """Persist a dashboard setting and return the settings as :func:`get_settings`.

    Body: ``{"pangram_model": "pangram-4"|"default"}``. An unknown key or a
    value outside that vocabulary is a 400 and nothing is written. Changing the
    selector changes which detector later scoring runs use; it never rewrites a
    stored verdict (see :func:`pangram_retest`).
    """
    data = _json_body()

    unknown = sorted(set(data) - {"pangram_model"})
    if unknown:
        raise ApiError(f"unknown setting(s): {', '.join(unknown)}")

    model = data.get("pangram_model")
    if model not in MODEL_GENERATIONS:
        raise ApiError(f"pangram_model must be one of {sorted(MODEL_GENERATIONS)}")

    store = get_store()
    store.set_setting(SETTING_PANGRAM_MODEL, model)
    return jsonify({"pangram_model": _active_model(store)})


def _run_score_stage(
    config: Any, store: Store, limit: int, message_ids: Sequence[int] | None = None
) -> dict[str, Any]:
    """Run the score stage for up to ``limit`` unscored extractions.

    Scoring runs only when a Pangram API key is configured; otherwise it is
    skipped, Pangram is never called, and ``scoring_skipped`` is true. Returns
    the score summary fields shared by :func:`pull`, :func:`pull_range`, and the
    standalone :func:`score` endpoint. ``message_ids`` restricts the run to those
    messages' extractions (see :func:`mailing_list_ai_check.cli.run_score`).
    """
    if not config.pangram_api_key:
        return {
            "scored": 0,
            "cache_hits": 0,
            "api_calls": 0,
            "too_short": 0,
            "scoring_skipped": True,
        }
    pangram = PangramClient(config.pangram_api_key, model=_active_model(store))
    score_summary = run_score(
        store,
        pangram,
        limit=limit,
        message_ids=set(message_ids) if message_ids is not None else None,
    )
    return {
        "scored": score_summary.scored,
        "cache_hits": score_summary.cache_hits,
        "api_calls": score_summary.api_calls,
        "too_short": score_summary.too_short,
        "scoring_skipped": False,
    }


@api_bp.post("/pull")
def pull() -> Any:
    """Fetch → extract → (optionally) score the most recent messages of a list.

    Body: ``{"list": "<name>", "count": <int 1-1000>}``. Scoring runs only when a
    Pangram API key is configured; otherwise it is skipped and ``scoring_skipped``
    is true in the response. This endpoint deliberately performs network and paid
    work on an explicit user click — ``count`` is the only cap.
    """
    data = _json_body()

    list_name = data.get("list")
    if not isinstance(list_name, str) or not list_name.strip():
        raise ApiError("list is required")
    list_name = list_name.strip()
    if not _LIST_NAME_RE.match(list_name):
        raise ApiError("list name may contain only letters, digits, '.', '-' and '_'")

    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    if not (_MIN_PULL_COUNT <= count <= _MAX_PULL_COUNT):
        raise ApiError(f"count must be between {_MIN_PULL_COUNT} and {_MAX_PULL_COUNT}")

    config = current_app.config["APP_CONFIG"]
    store = get_store()

    fetch_summary = _fetch_for_list(config, store, list_name, count)

    status_counts, _method_counts = run_extract(store, limit=count)

    scoring_skipped = not config.pangram_api_key
    scored = cache_hits = api_calls = too_short = 0
    if not scoring_skipped:
        pangram = PangramClient(config.pangram_api_key, model=_active_model(store))
        score_summary = run_score(store, pangram, limit=count)
        scored = score_summary.scored
        cache_hits = score_summary.cache_hits
        api_calls = score_summary.api_calls
        too_short = score_summary.too_short

    return jsonify(
        {
            "fetched": fetch_summary.fetched,
            "duplicates": fetch_summary.duplicates,
            "parse_errors": fetch_summary.parse_errors,
            "extracted": status_counts.get("ok", 0),
            "empty": status_counts.get("empty", 0),
            "too_short": too_short,
            "scored": scored,
            "cache_hits": cache_hits,
            "api_calls": api_calls,
            "scoring_skipped": scoring_skipped,
        }
    )


# --- pull stages (fetch / extract / score run as separate calls) --------------
#
# The combined /pull above runs fetch → extract → score in one request. The
# endpoints below expose the same three stages individually so the dashboard can
# drive them as sequential HTTP calls and show real per-stage progress. Each
# reuses the exact helpers and validation /pull uses; /pull itself is unchanged.


@api_bp.post("/pull/fetch")
def pull_fetch() -> Any:
    """Run only the fetch stage of :func:`pull` for a list.

    Body: ``{"list": "<name>", "count": <int 1-1000>}`` — validated exactly as
    :func:`pull`. Runs :func:`_fetch_for_list` and nothing else. ``limit`` echoes
    ``count`` so the client can pass it to the extract and score stages. An IMAP
    failure is a 502.
    """
    data = _json_body()
    list_name = _validate_list_name(data)
    count = _required_count(data, "count")

    config = current_app.config["APP_CONFIG"]
    store = get_store()

    fetch_summary = _fetch_for_list(config, store, list_name, count)

    return jsonify(
        {
            "fetched": fetch_summary.fetched,
            "duplicates": fetch_summary.duplicates,
            "parse_errors": fetch_summary.parse_errors,
            "limit": count,
        }
    )


@api_bp.post("/extract")
def extract() -> Any:
    """Run only the extract stage over up to ``limit`` un-extracted messages.

    Body: ``{"limit": <int 1-1000>}`` — validated exactly as :func:`pull`'s
    ``count``. Local work only; no IMAP or Pangram calls.
    """
    data = _json_body()
    limit = _required_count(data, "limit")

    status_counts, _method_counts = run_extract(get_store(), limit=limit)

    return jsonify(
        {
            "extracted": status_counts.get("ok", 0),
            "empty": status_counts.get("empty", 0),
        }
    )


@api_bp.post("/score")
def score() -> Any:
    """Run only the score stage over up to ``limit`` unscored extractions.

    Body: ``{"limit": <int 1-1000>}`` — validated exactly as :func:`pull`'s
    ``count``. Scoring runs only when a Pangram API key is configured; otherwise
    Pangram is never called and ``scoring_skipped`` is true.
    """
    data = _json_body()
    limit = _required_count(data, "limit")

    config = current_app.config["APP_CONFIG"]
    return jsonify(_run_score_stage(config, get_store(), limit))


# --- stale extractions --------------------------------------------------------
#
# Three endpoints back the dashboard's start-up staleness prompt, in the order
# the user meets them: /staleness reports whether any stored extraction predates
# the current extraction routine (a generation comparison, cheap enough to run on
# every dashboard load); /staleness/check re-derives every extraction and reports
# the ones that actually differ (local work only, nothing paid, no text
# rewritten); then /staleness/reextract and /staleness/rescore rewrite and
# re-score only the messages the user chose. See
# :mod:`mailing_list_ai_check.staleness`.


def _message_id_list(data: dict[str, Any]) -> list[int]:
    """Validate ``data["ids"]`` as a non-empty list of at most 1000 message ids.

    The cap matches :data:`_MAX_PULL_COUNT`, so one request can never re-score
    more messages than one pull can fetch; a client with a longer list sends it
    in successive requests.
    """
    raw = data.get("ids")
    if not isinstance(raw, list) or not raw:
        raise ApiError("ids must be a non-empty list of message ids")
    if len(raw) > _MAX_PULL_COUNT:
        raise ApiError(f"ids must contain at most {_MAX_PULL_COUNT} message ids")
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ApiError("ids must contain integer message ids")
        ids.append(item)
    return ids


def _serialize_diff(diff: ExtractionDiff) -> dict[str, Any]:
    """Shape one :class:`ExtractionDiff` into the affected-message row JSON.

    ``id`` is the message primary key (as in :func:`_serialize_message_row`), so
    the client can pass it straight back to :func:`staleness_reextract`.
    ``extraction_version`` is the generation of the routine that produced the
    stored text (``None`` when unrecorded), and ``pipeline_version`` the app
    version that wrote the row; the two move independently.
    """
    return {
        "id": diff.message_id,
        "list": diff.list_name,
        "date": diff.date,
        "subject": diff.subject,
        "from": {"address": diff.from_address, "display_name": diff.from_display_name},
        "pipeline_version": diff.pipeline_version,
        "extraction_version": diff.extraction_version,
        "old_chars": diff.old_chars,
        "new_chars": diff.new_chars,
        "old_status": diff.old_status,
        "new_status": diff.new_status,
        "text_changed": diff.text_changed,
        "scored_text_changed": diff.scored_text_changed,
        "scored_text_changed_in_substance": diff.scored_text_changed_in_substance,
        "scored": diff.scored,
    }


@api_bp.get("/staleness")
def staleness() -> Any:
    """Report whether any stored extraction predates the current routine.

    One grouped query over ``extractions`` — no text is re-derived and no row is
    written. Returns the :class:`~mailing_list_ai_check.staleness.StalenessReport`
    fields: ``app_version``, ``extraction_version`` (the generation of the running
    extraction routine, which moves independently of the app version), ``stale``,
    ``stale_count``, ``current_count``, ``total``, and per-generation counts in
    ``versions`` (each ``{extraction_version, count, stale}``, with a ``None``
    generation for extractions written before the stamp existed).
    """
    return jsonify(asdict(check_staleness(get_store())))


@api_bp.post("/staleness/check")
def staleness_check() -> Any:
    """Re-derive every stored extraction and report the ones that differ.

    Local work only: extraction and cleaning are re-run over every message that
    has an extraction, no extracted text is rewritten, no score is touched and
    Pangram is never called. Extractions that come out identical are stamped with
    the running extraction generation (``stamped``), which is what clears a false
    staleness report for good. Returns ``app_version``, ``checked``,
    ``unchanged``, ``stamped``, ``differing`` (the count) and ``messages`` (the
    affected rows, by message id, each carrying both the ``pipeline_version`` and
    the ``extraction_version`` of the stored extraction — see
    :func:`_serialize_diff`).
    """
    report = diff_extractions(get_store())
    return jsonify(
        {
            "app_version": report.app_version,
            "checked": report.checked,
            "unchanged": report.unchanged,
            "stamped": report.stamped,
            "differing": len(report.differing),
            "rescore_needed": sum(
                1 for d in report.differing if d.scored and d.scored_text_changed_in_substance
            ),
            "messages": [_serialize_diff(d) for d in report.differing],
        }
    )


@api_bp.post("/staleness/reextract")
def staleness_reextract() -> Any:
    """Re-extract the given messages, rewriting those whose text has moved.

    Body: ``{"ids": [<message id>, …]}`` — 1 to 1000 ids, as returned by
    :func:`staleness_check`. Rewrites each changed extraction in place and drops
    the score of any whose cleaned (scored) text changed **in substance**, since
    that verdict was reached on text that no longer exists; a cleaned text that
    moved only in whitespace keeps its verdict, re-keyed to the new text's hash
    (counted in ``scores_carried``). Local work only; the re-scoring it makes
    necessary is the separate, paid :func:`staleness_rescore` call.
    ``rescore_ids`` lists the messages worth passing to it.
    """
    data = _json_body()
    ids = _message_id_list(data)

    summary = reextract(get_store(), ids)
    return jsonify(
        {
            "processed": summary.processed,
            "rewritten": summary.rewritten,
            "unchanged": summary.unchanged,
            "not_ok": summary.not_ok,
            "scores_invalidated": summary.scores_invalidated,
            "scores_carried": summary.scores_carried,
            "rescore_ids": summary.rescore_message_ids,
        }
    )


@api_bp.post("/staleness/rescore")
def staleness_rescore() -> Any:
    """Score the given messages' unscored extractions and nothing else.

    Body: ``{"ids": [<message id>, …]}`` — 1 to 1000 ids, normally the
    ``rescore_ids`` of a :func:`staleness_reextract` call. The rest of the
    scoring queue is left alone. Each message can cost at most one Pangram call,
    so the API-call cap is the number of ids. Scoring runs only when a Pangram API
    key is configured; otherwise Pangram is never called and ``scoring_skipped``
    is true.
    """
    data = _json_body()
    ids = _message_id_list(data)

    config = current_app.config["APP_CONFIG"]
    return jsonify(_run_score_stage(config, get_store(), len(ids), message_ids=ids))


# --- Pangram detector generation ----------------------------------------------
#
# Two endpoints back the dashboard's upgrade notice, which offers to re-test the
# verdicts an earlier detector generation produced. /pangram/notice reports the
# notice state together with how many stored scores the selected detector would
# derive differently and what re-testing them would cost; /pangram/retest drops
# those verdicts for the chosen messages and scores them again (a paid call per
# message). Which generation a selector means is
# :data:`mailing_list_ai_check.pangram.MODEL_GENERATIONS`.

#: Notice states. "pending" is resolved, never stored: it is what an unset
#: setting means while old-generation scores exist. The user's own choices are
#: "later" (ask again next load) and "dismissed" (do not ask again).
_NOTICE_STATES = ("pending", "later", "dismissed")
#: The states a client may store.
_SETTABLE_NOTICE_STATES = ("later", "dismissed")


def _score_generation(detector_version: str | None, generation: str) -> bool:
    """Whether a stored ``detector_version`` belongs to ``generation``.

    Pangram stamps a dotted version ("4.0", "3.3.2"), so the generation is the
    leading component. An unrecorded version belongs to no generation.
    """
    return bool(detector_version) and detector_version.startswith(f"{generation}.")


def _notice_payload(store: Store) -> dict[str, Any]:
    """Shape the upgrade-notice response for the currently selected detector.

    ``old_scores`` counts the stored scores that came from another generation —
    the verdicts the selected detector would derive differently — with
    ``message_ids`` naming their messages, ``estimated_words`` the words a
    re-test would send and ``estimated_cost_v4`` the Pangram 4 realtime price of
    those words. ``state`` is the stored setting when one exists; otherwise it
    resolves to "pending" when there is something to re-test and "dismissed"
    when there is not, so a database that has only ever been scored by the
    current generation never raises the notice. Resolving is not storing: only
    an explicit PUT writes the setting.
    """
    generation = generation_for_model(_active_model(store))
    rows = store.scores_outside_generation(generation) if generation is not None else []
    words = sum(count for _, count in rows)

    stored = store.get_setting(SETTING_PANGRAM_NOTICE)
    if stored in _NOTICE_STATES:
        state = stored
    else:
        state = "pending" if rows else "dismissed"

    return {
        "state": state,
        "old_scores": len(rows),
        "message_ids": [message_id for message_id, _ in rows],
        "estimated_words": words,
        "estimated_cost_v4": round(words / 100 * _PRICE_PER_100_WORDS["4"], 2),
    }


@api_bp.get("/pangram/notice")
def pangram_notice() -> Any:
    """Report the upgrade-notice state and what re-testing would involve.

    One query over ``scores`` joined to ``extractions`` — nothing is written and
    Pangram is never called. Returns ``state``, ``old_scores``, ``message_ids``,
    ``estimated_words`` and ``estimated_cost_v4`` (see :func:`_notice_payload`).
    """
    return jsonify(_notice_payload(get_store()))


@api_bp.put("/pangram/notice")
def put_pangram_notice() -> Any:
    """Persist the upgrade-notice state and return :func:`pangram_notice`'s shape.

    Body: ``{"state": "later"|"dismissed"}``. "pending" is the resolved default
    for an unset setting, not a state a client may store, so it is a 400 — as is
    any other value.
    """
    data = _json_body()

    state = data.get("state")
    if state not in _SETTABLE_NOTICE_STATES:
        raise ApiError(f"state must be one of {sorted(_SETTABLE_NOTICE_STATES)}")

    store = get_store()
    store.set_setting(SETTING_PANGRAM_NOTICE, state)
    return jsonify(_notice_payload(store))


@api_bp.post("/pangram/retest")
def pangram_retest() -> Any:
    """Re-score the given messages with the currently selected detector.

    Body: ``{"ids": [<message id>, …]}`` — 1 to 1000 ids, normally the
    ``message_ids`` of a :func:`pangram_notice` call. Each message whose stored
    verdict came from another generation loses that verdict first (``invalidated``
    counts them), which returns its extraction to the scoring queue; a message
    already scored by the selected generation keeps its verdict and costs
    nothing. The re-score is then the ordinary score stage restricted to those
    messages, so each one can cost at most one Pangram call, and scoring runs
    only when a Pangram API key is configured. The response is
    :func:`_run_score_stage`'s summary plus ``invalidated``.
    """
    data = _json_body()
    ids = _message_id_list(data)

    store = get_store()
    generation = generation_for_model(_active_model(store))

    invalidated = 0
    for message_id in ids:
        extraction = store.extraction_for_message(message_id)
        if extraction is None:
            continue
        score = store.score_for_extraction(extraction.id)
        if score is None:
            continue
        # An unknown selector names no generation, so nothing can be shown to be
        # out of date: keep every verdict rather than discard one that is current.
        if generation is None or _score_generation(score.detector_version, generation):
            continue
        if store.delete_score_for_extraction(extraction.id):
            invalidated += 1

    config = current_app.config["APP_CONFIG"]
    result = _run_score_stage(config, store, len(ids), message_ids=ids)
    return jsonify({**result, "invalidated": invalidated})


# --- add messages: preview + ranged pull -------------------------------------
#
# Two endpoints back the dashboard's "Add messages" popover. Both work in a
# direction relative to what is already stored for a list:
#   - "new":    messages with a UID greater than the incremental cursor (or, with
#               no cursor valid for the folder's current UIDVALIDITY, greater than
#               the largest stored UID; else everything);
#   - "before": messages with a UID smaller than the earliest stored UID.
# /lists/preview is strictly read-only (EXAMINE + UID SEARCH + a header-only
# FETCH — no store write, no pull_state change, no Pangram). /pull/range then
# fetches the chosen bodies and runs the same extract/score pipeline as /pull.

#: How many messages a preview shows (the first N for "new", the last N for
#: "before"); also the default "before" ``count``.
_PREVIEW_COUNT = 25


def _open_client_or_502(config: Any) -> Any:
    """Open an IMAP client, mapping any connection failure to a 502 ``ApiError``."""
    try:
        return open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc


def _close_client_quietly(client: Any) -> None:
    """Close and log out ``client``, never letting teardown mask the real result."""
    try:
        client.close()
        client.logout()
    except Exception:  # noqa: BLE001 - teardown errors are irrelevant to the result
        pass


def _list_and_mode(data: dict[str, Any]) -> tuple[str, str]:
    """Validate and return the shared ``(list_name, mode)`` for both endpoints.

    Raises a 400 ``ApiError`` for a missing/ill-formed list name (same rule as
    :func:`pull`) or a ``mode`` that is not exactly ``"new"`` or ``"before"``.
    List-row existence (a 404) is checked separately by the caller.
    """
    list_name = data.get("list")
    if not isinstance(list_name, str) or not list_name.strip():
        raise ApiError("list is required")
    list_name = list_name.strip()
    if not _LIST_NAME_RE.match(list_name):
        raise ApiError("list name may contain only letters, digits, '.', '-' and '_'")

    mode = data.get("mode")
    if mode not in ("new", "before"):
        raise ApiError("mode must be 'new' or 'before'")
    return list_name, mode


def _resolve_list_or_404(store: Store, list_name: str) -> Any:
    """Return the existing list row for ``list_name`` or raise a 404 ``ApiError``.

    Never creates a row — preview and ranged pull operate only on lists already
    known to the store (indexed or previously pulled).
    """
    row = store.get_list_by_name(list_name)
    if row is None:
        raise ApiError(f"unknown list {list_name!r}", 404)
    return row


def _preview_count(data: dict[str, Any]) -> int:
    """Parse the "before" preview ``count``: default 25, clamped to 1..1000.

    A non-integer (including ``bool``) is a 400; an out-of-range integer is
    clamped rather than rejected.
    """
    count = data.get("count")
    if count is None:
        return _PREVIEW_COUNT
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    return max(_MIN_PULL_COUNT, min(count, _MAX_PULL_COUNT))


def _range_count(data: dict[str, Any], mode: str) -> int | None:
    """Parse the ranged-pull ``count`` per mode.

    ``mode "new"``: a missing/``null`` count means "all" (returned as ``None``;
    the caller caps at :data:`_MAX_PULL_COUNT`); a provided value must be an
    integer in 1..1000. ``mode "before"``: ``count`` is required and must be an
    integer in 1..1000. ``bool`` is never a valid integer here (mirrors
    :func:`pull`). Out-of-range or ill-typed values are a 400.
    """
    count = data.get("count")
    if mode == "new" and count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        raise ApiError("count must be an integer")
    if not (_MIN_PULL_COUNT <= count <= _MAX_PULL_COUNT):
        raise ApiError(f"count must be between {_MIN_PULL_COUNT} and {_MAX_PULL_COUNT}")
    return count


def _candidate_uids(client: Any, store: Store, list_row: Any, mode: str) -> tuple[int, list[int]]:
    """Return ``(uidvalidity, uids)`` for the "new"/"before" candidate set.

    Shared by preview and the ranged pull so both agree on the full set before
    slicing. EXAMINEs the folder read-only, then runs one UID SEARCH:

    - ``"new"``: the baseline is the incremental cursor's ``last_uid`` when a
      cursor exists whose UIDVALIDITY matches the folder's current one, else the
      largest stored UID, else 0. The set is ``UID SEARCH {baseline+1}:*``
      filtered to ``uid > baseline`` (dropping the ``n:*`` echo of the top UID),
      ascending.
    - ``"before"``: anchored on the smallest stored UID; a 404 when the list has
      no UID-bearing message to anchor against. The set is ``UID SEARCH
      1:{min_uid-1}`` (empty when ``min_uid <= 1``), ascending.
    """
    status = client.examine(list_row.folder)
    uidvalidity = status.uidvalidity

    if mode == "new":
        cursor = store.get_pull_state(list_row.id)
        if cursor is not None and cursor.uidvalidity == uidvalidity:
            baseline = cursor.last_uid
        else:
            baseline = store.max_uid_for_list(list_row.id) or 0
        found = client.uid_search(build_search_criteria(uid_range=f"{baseline + 1}:*"))
        return uidvalidity, sorted(u for u in found if u > baseline)

    min_uid = store.min_uid_for_list(list_row.id)
    if min_uid is None:
        raise ApiError(
            f"list {list_row.name!r} has no stored messages to anchor a 'before' pull",
            404,
        )
    if min_uid <= 1:
        return uidvalidity, []
    found = client.uid_search(build_search_criteria(uid_range=f"1:{min_uid - 1}"))
    return uidvalidity, sorted(found)


def _preview_rows(client: Any, uids: Sequence[int]) -> list[dict[str, Any]]:
    """Fetch header-only rows for ``uids`` and shape them, ascending by UID.

    Only ``From``/``Subject``/``Date`` are fetched (``BODY.PEEK[HEADER.FIELDS]``)
    and parsed via :func:`~mailing_list_ai_check.fetcher.parse_header`, so a
    preview never touches a body or the ``\\Seen`` flag. UIDs missing from the
    server response (or an un-mapped echo) are skipped.
    """
    by_uid: dict[int, dict[str, Any]] = {}
    for uid, raw in client.fetch_headers(uids):
        if uid is None:
            continue
        header = parse_header(raw)
        by_uid[uid] = {
            "from_name": header.from_name,
            "from_email": header.from_email,
            "subject": header.subject,
            "date": header.date,
        }
    return [by_uid[u] for u in sorted(by_uid)]


@api_bp.post("/lists/preview")
def preview() -> Any:
    """Preview candidate messages to add for a list, storing nothing.

    Body: ``{"list": "<name>", "mode": "new"|"before", "count": <int>}``. Strictly
    read-only: EXAMINE + UID SEARCH + a header-only FETCH; no body is fetched, no
    row is written, the ``pull_state`` cursor is untouched, and Pangram is never
    called. ``mode "new"`` always previews the first (oldest) 25 newer-than-stored
    messages; ``mode "before"`` previews the last ``count`` (default 25, clamped
    1..1000) messages immediately preceding the earliest stored one. Rows come
    back in ascending UID order. An IMAP connect/enumeration failure is a 502.
    """
    data = _json_body()
    list_name, mode = _list_and_mode(data)
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)
    count = _preview_count(data) if mode == "before" else _PREVIEW_COUNT

    config = current_app.config["APP_CONFIG"]
    client = _open_client_or_502(config)
    try:
        _uidvalidity, uids = _candidate_uids(client, store, list_row, mode)
        chosen = uids[:_PREVIEW_COUNT] if mode == "new" else uids[-count:]
        rows = _preview_rows(client, chosen)
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - IMAP/enumeration failures become a 502
        raise ApiError(f"IMAP preview failed for list {list_name!r}: {exc}", 502) from exc
    finally:
        _close_client_quietly(client)

    total = len(uids)
    shown = len(rows)
    return jsonify(
        {
            "mode": mode,
            "list": list_name,
            "total": total,
            "shown": shown,
            "more": total - shown,
            "messages": rows,
        }
    )


def _run_range_fetch(
    config: Any, store: Store, list_row: Any, mode: str, count: int | None
) -> dict[str, Any]:
    """Perform the fetch stage of a ranged pull, returning its summary.

    Shared verbatim by :func:`pull_range` and :func:`pull_range_fetch`: candidate
    UID selection, capping, :func:`run_fetch_uids`, the "new"-mode ``pull_state``
    cursor advance (never regressing; never touched for "before"),
    :meth:`Store.set_list_synced`, and the best-effort activity stamp. Runs no
    extract or score. An IMAP failure is a 502. The returned dict carries
    ``mode``, ``matched``, ``capped``, ``fetched``, ``duplicates``,
    ``parse_errors``, and ``limit`` (the number of messages actually chosen).
    """
    client = _open_client_or_502(config)
    try:
        uidvalidity, uids = _candidate_uids(client, store, list_row, mode)
        matched = len(uids)
        capped = False
        if mode == "new":
            if count is None:
                capped = matched > _MAX_PULL_COUNT
                chosen = uids[:_MAX_PULL_COUNT]
            else:
                chosen = uids[:count]
        else:  # before — count is a required int here
            chosen = uids[-count:] if count is not None and count < len(uids) else uids

        fetch_summary = run_fetch_uids(client, store, list_row.folder, chosen)

        # Cursor bookkeeping: a "new" pull may only ever advance the cursor; a
        # "before" pull reaches into older UIDs and must never move it.
        if mode == "new" and chosen:
            max_uid = max(chosen)
            cursor = store.get_pull_state(list_row.id)
            if cursor is None or cursor.uidvalidity != uidvalidity or max_uid > cursor.last_uid:
                store.set_pull_state(list_row.id, uidvalidity, max_uid)
        store.set_list_synced(list_row.id)
        # Best-effort activity stamp, exactly like run_fetch — never fatal.
        try:
            when = client.last_message_internaldate(list_row.folder)
            if when is not None:
                store.set_list_last_message(list_row.id, when)
        except Exception:  # noqa: BLE001 - an activity check never fails a pull
            pass
    except ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - IMAP/fetch failures become a 502
        raise ApiError(f"IMAP ranged pull failed for list {list_row.name!r}: {exc}", 502) from exc
    finally:
        _close_client_quietly(client)

    return {
        "mode": mode,
        "matched": matched,
        "capped": capped,
        "fetched": fetch_summary.fetched,
        "duplicates": fetch_summary.duplicates,
        "parse_errors": fetch_summary.parse_errors,
        "limit": len(chosen),
    }


@api_bp.post("/pull/range")
def pull_range() -> Any:
    """Fetch → extract → (optionally) score a directional range of a list's messages.

    Body: ``{"list": "<name>", "mode": "new"|"before", "count": <int|null>}``.
    ``mode "new"`` pulls the first ``count`` newer-than-stored messages (a
    missing/``null`` count means all, capped at 1000); ``mode "before"`` pulls the
    last ``count`` messages preceding the earliest stored one (``count``
    required). The candidate set is recomputed exactly as :func:`preview` does.
    Scoring runs only when a Pangram API key is configured. The ``pull_state``
    cursor advances for a "new" pull (never regressing) and is never touched by a
    "before" pull. An IMAP failure is a 502.
    """
    data = _json_body()
    list_name, mode = _list_and_mode(data)
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)
    count = _range_count(data, mode)

    config = current_app.config["APP_CONFIG"]
    fetch_result = _run_range_fetch(config, store, list_row, mode, count)
    limit = fetch_result.pop("limit")

    status_counts, _method_counts = run_extract(store, limit=limit)
    score_result = _run_score_stage(config, store, limit)

    return jsonify(
        {
            **fetch_result,
            "extracted": status_counts.get("ok", 0),
            "empty": status_counts.get("empty", 0),
            "too_short": score_result["too_short"],
            "scored": score_result["scored"],
            "cache_hits": score_result["cache_hits"],
            "api_calls": score_result["api_calls"],
            "scoring_skipped": score_result["scoring_skipped"],
        }
    )


@api_bp.post("/pull/range/fetch")
def pull_range_fetch() -> Any:
    """Run only the fetch stage of :func:`pull_range` for a directional range.

    Body: ``{"list": "<name>", "mode": "new"|"before", "count": <int|null>}`` —
    validated exactly as :func:`pull_range`. Performs everything :func:`pull_range`
    does through the IMAP fetch (candidate selection, capping, the "new"-mode
    cursor advance, ``set_list_synced``, the activity stamp) but runs no extract or
    score. ``limit`` is the number of messages chosen, for the client to pass to
    the extract and score stages. An IMAP failure is a 502.
    """
    data = _json_body()
    list_name, mode = _list_and_mode(data)
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)
    count = _range_count(data, mode)

    config = current_app.config["APP_CONFIG"]
    return jsonify(_run_range_fetch(config, store, list_row, mode, count))


# --- entity endpoints ---------------------------------------------------------


@api_bp.get("/lists")
def list_lists() -> Any:
    """Every list with its counts, label mix and date stamps (see :meth:`Store.list_rows`).

    ``earliest_message_at`` is the oldest stored message date for the list (the
    message's own date, ISO-8601 UTC), or ``null`` when the list has no dated
    messages. ``too_short_count`` is the list's messages gated under the
    reliability floor, which the mix bar draws as a trailing grey segment.
    """
    return jsonify({"lists": get_store().list_rows()})


@api_bp.post("/lists/regenerate")
def regenerate_lists() -> Any:
    """Re-populate the lists index from the server's IMAP ``LIST`` enumeration.

    A single ``LIST`` round-trip — no message fetches, nothing paid. Lists that
    have disappeared from the server are dropped unless the store holds messages
    for them, in which case the row is kept and stamped ``removed_from_server_at``
    (see :meth:`Store.refresh_lists_index`). Returns the reconciliation counts.
    """
    config = current_app.config["APP_CONFIG"]
    try:
        client = open_client(
            config.imap_host, config.imap_port, config.imap_username, config.imap_password
        )
    except Exception as exc:  # noqa: BLE001 - report any connection failure cleanly
        raise ApiError(f"could not connect to the IMAP server: {exc}", 502) from exc

    try:
        counts = refresh_lists_index(client, get_store())
    except Exception as exc:  # noqa: BLE001 - IMAP failures become a 502
        raise ApiError(f"IMAP list enumeration failed: {exc}", 502) from exc
    finally:
        try:
            client.close()
            client.logout()
        except Exception:  # noqa: BLE001 - never let teardown mask the real result
            pass

    return jsonify(counts)


@api_bp.get("/lists/timelines")
def list_timelines() -> Any:
    """Slim per-list message timelines for the dashboard's adaptive rug plots.

    Query params: an optional ``list`` (a list name). With it the response
    covers that list alone and each point carries the message's subject;
    without it, every list that has at least one message is covered (ordered by
    message count descending then name) and subjects are omitted. No row cap
    applies in either form: the plots adapt to any volume client-side.

    Returns ``{"start": s, "end": e, "buckets": [...], "lists": [...]}``.
    ``start``/``end`` are epoch seconds spanning every dated message returned
    (``null`` when there is none) and ``buckets`` names the point buckets by
    index. Each list entry is ``{"list": <name>, "total": n, "undated": u,
    "points": [[id, t, bucket(, subject)], ...]}`` with points ordered by
    ``(date, id)`` ascending and ``t`` in epoch seconds; undated messages carry
    no point (see :meth:`Store.list_timelines`). An unknown ``list`` yields an
    empty ``lists`` rather than a 404.
    """
    list_name = request.args.get("list") or None
    result = get_store().list_timelines(list_name)
    result["buckets"] = list(TIMELINE_BUCKETS)
    return jsonify(result)


@api_bp.get("/lists/thread-graph")
def list_thread_graph() -> Any:
    """Reply-thread graph data for one list (the list panel's thread graph).

    Query params: ``list`` (required; an unknown list is a 404) and the optional
    window bounds ``start`` and ``end`` — 0-based inclusive ranks into the
    list's IMAP receipt order, rank 0 being the furthest back. ``end`` defaults
    to the list's most recent rank and ``start`` to 0, so omitting both returns
    the whole list. A non-integer bound, a negative one, or ``start`` greater
    than ``end`` is a 400. ``end`` beyond the list's last rank is clamped to it;
    the span itself is never capped, so an explicit range is served in full.

    Returns ``{"list": …, "list_total": …, "start": …, "end": …, "total": …,
    "first_date": …, "last_date": …, "threads": [...]}``, where ``start`` and
    ``end`` echo the effective (clamped) bounds and the dates are those of the
    window's oldest and newest messages. Each thread is ``{"messages": [...]}``
    — one connected reply component, messages oldest first — and each message
    carries ``id``, ``message_id``, ``seq``, ``uid``, ``date``, ``subject``,
    ``from_name``, ``from_email``, ``extraction_status``, ``label``,
    ``prediction_short``, ``timing_cpm`` and ``parent_id``. See
    :meth:`Store.thread_graph` for the exact ordering and reply linkage.
    """
    list_name = request.args.get("list") or None
    if not list_name:
        raise ApiError("pass a 'list'")
    store = get_store()
    list_row = _resolve_list_or_404(store, list_name)

    start = _parse_int("start", request.args.get("start"))
    end = _parse_int("end", request.args.get("end"))
    if start is not None and start < 0:
        raise ApiError("start must be >= 0")
    if end is not None and end < 0:
        raise ApiError("end must be >= 0")
    if start is not None and end is not None and start > end:
        raise ApiError("start must not be greater than end")

    graph = store.thread_graph(list_row.id, start=start, end=end)
    return jsonify({"list": list_name, **graph})


@api_bp.get("/addresses")
def list_addresses() -> Any:
    q = request.args.get("q") or None
    return jsonify({"addresses": get_store().address_rows(q)})


@api_bp.get("/persons")
def list_persons() -> Any:
    return jsonify({"persons": get_store().person_rows()})


@api_bp.get("/senders")
def list_senders() -> Any:
    """One entry per person (linked address group) or per unlinked address.

    Each entry carries ``message_count``, ``label_counts`` and
    ``too_short_count`` (messages gated under the reliability floor), the three
    figures the sender row's mix bar is drawn from.

    Query params (all optional): ``q`` (case-insensitive substring over name or
    any email), ``list`` (restrict to senders who posted to that list, with
    counts/labels scoped to it; an unknown list yields no senders), ``sort``
    (``count`` default, ``name``, or ``ai`` — the sender's count of ``AI`` +
    ``Mixed`` messages), ``order`` (``asc``/``desc`` — defaults to the natural direction for the
    chosen sort: ``desc`` for count and ai, ``asc`` for
    name), ``page`` (default 1), ``per_page`` (default 60, clamped to
    ``MAX_PER_PAGE``), ``include_excluded`` (boolean, default false — when
    false, senders whose every message is auto-generated, and so can never be
    scored, are omitted, as are the shared addresses in
    ``SHARED_SENDER_ADDRESSES``; the pane's "Show all" switch sets it true). Bad
    input yields a 400 like :func:`parse_filters`.

    Each entry reports ``excluded_count``, ``excluded_from_scoring`` and
    ``shared_address`` so the pane can mark such senders when they are shown.

    An unlinked address that has presented ``MULTI_NAME_ADDRESS_THRESHOLD`` or
    more different ``From`` names is named by its address rather than by any one
    of them; ``distinct_from_names`` reports that count and ``named_by_address``
    whether the rule applied, so a client never repeats the threshold.
    """
    args = request.args

    q = args.get("q") or None
    list_name = args.get("list") or None

    sort = args.get("sort", "count")
    if sort not in _SENDER_SORTS:
        raise ApiError(f"sort must be one of {sorted(_SENDER_SORTS)}")

    order = args.get("order")
    if order is None or order == "":
        order = _SENDER_SORTS[sort]  # natural default for the chosen sort
    else:
        order = order.lower()
        if order not in ("asc", "desc"):
            raise ApiError("order must be 'asc' or 'desc'")

    page = _parse_int("page", args.get("page"))
    if page is None:
        page = 1
    elif page < 1:
        raise ApiError("page must be >= 1")

    per_page = _parse_int("per_page", args.get("per_page"))
    if per_page is None:
        per_page = _DEFAULT_SENDER_PER_PAGE
    elif per_page < 1:
        raise ApiError("per_page must be >= 1")
    else:
        per_page = min(per_page, MAX_PER_PAGE)

    include_excluded = _parse_bool("include_excluded", args.get("include_excluded")) or False

    rows, total = get_store().sender_rows(
        q=q,
        sort=sort,
        order=order,
        page=page,
        per_page=per_page,
        list_name=list_name,
        include_excluded=include_excluded,
    )
    return jsonify(
        {
            "senders": rows,
            "total": total,
            "page": page,
            "per_page": per_page,
            "sort": sort,
            "order": order,
            "list": list_name,
            "include_excluded": include_excluded,
        }
    )


@api_bp.get("/senders/reply-rugs")
def sender_reply_rugs() -> Any:
    """Per-list reply rug data for one sender (the sender screen's activity table).

    Query params: exactly one of ``person`` (a person id) or ``address`` (an
    email) — the two sender scopes the message filters define — plus an optional
    ``limit`` (>= 1). ``limit`` omitted means unlimited: the rugs adapt to any
    volume client-side, so every matching message is returned. Passing both
    scopes, neither, or bad input yields a 400.

    Returns ``{"person": …, "address": …, "limit": …, "by_list": [...]}``
    (``limit`` echoes the request, ``null`` when unlimited). Each ``by_list``
    entry is ``{"list": <name>, "replied_to": [...], "reply_from":
    [...]}`` — the messages the sender replied to on that list, and other
    senders' replies to the sender's messages there. Both arrays hold at most
    ``limit`` slim message rows (``id``, ``message_id``, ``list``, ``date``,
    ``subject``, ``extraction_status``, ``label``, ``prediction_short``),
    **newest first**, as
    ``GET /messages`` orders its rows. An unknown person/address yields an empty
    ``by_list`` rather than a 404: the payload decorates rows the client already
    has. See :meth:`Store.sender_reply_rugs` for the exact reply linkage.
    """
    args = request.args
    person_raw = args.get("person") or None
    address = args.get("address") or None
    if (person_raw is None) == (address is None):
        raise ApiError("pass exactly one of 'person' or 'address'")

    person_id = _parse_int("person", person_raw)

    limit = _parse_int("limit", args.get("limit"))
    if limit is not None and limit < 1:
        raise ApiError("limit must be >= 1")

    by_list = get_store().sender_reply_rugs(person_id=person_id, address=address, limit=limit)
    return jsonify({"person": person_id, "address": address, "limit": limit, "by_list": by_list})


@api_bp.get("/senders/timelines")
def sender_timelines() -> Any:
    """Slim per-sender message timelines for the Senders pane's rug plots.

    Query params: ``persons`` (comma-separated person ids) and/or ``addresses``
    (comma-separated emails) — at least one sender in total, at most
    ``MAX_PER_PAGE`` across both — plus an optional ``list`` (a list name)
    restricting points and domain to that list, as the senders table's counts
    are restricted. A non-integer person id, no senders at all, or too many is
    a 400.

    Returns ``{"start": s, "end": e, "buckets": [...], "persons": {...},
    "addresses": {...}, "list": …}``. ``start``/``end`` are epoch seconds
    spanning the whole scope's dated messages (shared x-domain; ``null`` when
    absent) and ``buckets`` names the point buckets by index. ``persons`` is
    keyed by person id as a string and ``addresses`` by lower-cased email,
    each entry ``{"points": [[id, t, bucket], ...], "undated": u}`` with ``t``
    in epoch seconds — the point shape ``GET /lists/timelines`` serves (see
    :meth:`Store.sender_timelines`). Unknown senders yield empty entries and
    an unknown ``list`` empty maps, not a 404.
    """
    args = request.args
    persons_raw = [p for p in (args.get("persons") or "").split(",") if p.strip()]
    person_ids = [_parse_int("persons", p) for p in persons_raw]
    addresses = [a for a in (args.get("addresses") or "").split(",") if a.strip()]
    if not person_ids and not addresses:
        raise ApiError("pass 'persons' and/or 'addresses'")
    if len(person_ids) + len(addresses) > MAX_PER_PAGE:
        raise ApiError(f"at most {MAX_PER_PAGE} senders per request")
    list_name = args.get("list") or None

    result = get_store().sender_timelines(
        person_ids=person_ids, addresses=addresses, list_name=list_name
    )
    return jsonify({**result, "buckets": list(TIMELINE_BUCKETS), "list": list_name})


@api_bp.get("/persons/suggestions")
def person_suggestions() -> Any:
    suggestions = get_store().suggest_person_merges()
    return jsonify(
        {
            "suggestions": [
                {
                    "display_name": s.display_name,
                    "address_ids": list(s.address_ids),
                    "emails": list(s.emails),
                }
                for s in suggestions
            ]
        }
    )


@api_bp.post("/persons")
def create_person() -> Any:
    store = get_store()
    data = _json_body()
    name = data.get("canonical_name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("canonical_name is required")
    address_ids = _address_id_list(data, "address_ids")
    # Validate every address exists before mutating, so a bad id can't leave a
    # half-assigned person behind.
    for aid in address_ids:
        if store.get_address(aid) is None:
            raise ApiError(f"address {aid} not found", 404)

    person = store.create_person(name.strip())
    for aid in address_ids:
        store.assign_address_to_person(aid, person.id)
    return jsonify(_person_detail(store, person.id)), 201


@api_bp.put("/persons/<int:person_id>")
def update_person(person_id: int) -> Any:
    store = get_store()
    if store.get_person(person_id) is None:
        raise ApiError("person not found", 404)

    data = _json_body()
    if "canonical_name" in data:
        name = data["canonical_name"]
        if not isinstance(name, str) or not name.strip():
            raise ApiError("canonical_name must be a non-empty string")
        store.update_person_name(person_id, name.strip())

    add_ids = _address_id_list(data, "add_address_ids")
    remove_ids = _address_id_list(data, "remove_address_ids")
    for aid in (*add_ids, *remove_ids):
        if store.get_address(aid) is None:
            raise ApiError(f"address {aid} not found", 404)
    for aid in add_ids:
        store.assign_address_to_person(aid, person_id)
    for aid in remove_ids:
        store.assign_address_to_person(aid, None)

    return jsonify(_person_detail(store, person_id))


@api_bp.delete("/persons/<int:person_id>")
def delete_person(person_id: int) -> Any:
    store = get_store()
    if not store.delete_person(person_id):
        raise ApiError("person not found", 404)
    return jsonify({"deleted": person_id})


# --- export / import ----------------------------------------------------------


def _export_slug(list_names: Sequence[str]) -> str:
    """A filename-safe slug for the export's selection.

    One list gives its sanitized name, several give ``<n>-lists``, and none —
    the whole-database export — gives ``all``.
    """
    if not list_names:
        return "all"
    if len(list_names) > 1:
        return f"{len(list_names)}-lists"
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", list_names[0])
    return slug or "list"


#: Bytes read from the finished export file per streamed chunk. Peak memory for
#: a download is this, not the export size.
_EXPORT_CHUNK_BYTES = 64 * 1024


def _unlink_quietly(path: str) -> bool:
    """Remove ``path``; return whether it is gone. Never raises."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:  # pragma: no cover - e.g. a platform holding an open file
        return False
    return True


def _selected_lists(args: Any) -> list[str]:
    """The ``list`` query params an export was given, de-duplicated in order.

    A name sent twice does not ask the exporter for the same list twice. Empty
    values are dropped, which keeps a bare ``?list=`` meaning "every list" rather
    than "the list called ''".
    """
    names: list[str] = []
    for raw in args.getlist("list"):
        name = raw.strip()
        if name and name not in names:
            names.append(name)
    return names


def _download_temp_file(
    serve_path: str, temp_paths: set[str], *, filename: str, mimetype: str
) -> Response:
    """Stream a finished temporary export file back as an attachment, then delete it.

    ``serve_path`` is the file to send and ``temp_paths`` every name the build may
    have created (the ``mkstemp`` name and the path actually written); all of them
    are removed. Shared by :func:`export` and :func:`export_stats_download`, whose
    only differences are the builder, the name and the content type.

    Streaming and temp-file lifetime
    --------------------------------
    The body is streamed from the finished file in ``_EXPORT_CHUNK_BYTES`` chunks,
    so peak memory is one chunk rather than the whole export (~220 MB at the
    100k-message scale). The file therefore has to outlive this function: the
    generator runs after the view returns, which is exactly when a ``finally``
    here would already have deleted it.

    The file is instead opened and then unlinked immediately, before the response
    is handed back. The open descriptor keeps the bytes readable while the name is
    already gone, so no exit path — success, an error during the export, a HEAD
    request whose body is never iterated, or a client that disconnects half way
    through — can leave a temporary file behind, and the unlink happens exactly
    once.

    Releasing the descriptor is then hung off two hooks, because neither covers
    every path alone. PEP 3333 requires the server to call ``close()`` on the
    response iterable however the request ends, including an early disconnect;
    werkzeug turns that into ``Response.close()``, which closes the body iterable
    and then runs the :meth:`~werkzeug.wrappers.Response.call_on_close` callbacks.
    The callback is what covers a body that never starts — werkzeug serves HEAD
    and 304 responses by dropping the iterable unstarted, so closing the generator
    does not run its ``finally``. The generator's ``finally`` in turn covers a body
    iterable closed on its own, which is what
    :meth:`~werkzeug.wrappers.Response.get_data` does when anything buffers the
    response. Both are idempotent, and the callback also retries the unlink for
    platforms that refuse to remove a file while it is open.

    :func:`flask.send_file` is deliberately not used: it re-opens by name (the
    name is gone), offers no cleanup hook for the temporary file, and its
    ``direct_passthrough`` hand-off to ``wsgi.file_wrapper`` would put the body
    outside our control while adding range/conditional handling that a file which
    exists for exactly one request cannot honour.
    """
    fh = None
    try:
        fh = open(serve_path, "rb")
        size = os.fstat(fh.fileno()).st_size
    except BaseException:  # pragma: no cover - only a failing open/fstat gets here
        if fh is not None:
            fh.close()
        for path in temp_paths:
            _unlink_quietly(path)
        raise

    # Unlink while the descriptor is open, so the bytes stay readable through
    # ``fh`` under a name that no longer exists. Whatever survives that (a
    # platform that refuses to remove an open file) is retried in ``release``.
    pending = {path for path in temp_paths if not _unlink_quietly(path)}

    def release() -> None:
        """Drop the descriptor, and any name the unlink above could not remove."""
        fh.close()
        for leftover in tuple(pending):
            if _unlink_quietly(leftover):
                pending.discard(leftover)

    def stream() -> Iterator[bytes]:
        try:
            while chunk := fh.read(_EXPORT_CHUNK_BYTES):
                yield chunk
        finally:
            release()

    # The size is final: the export is complete and the file is already unlinked,
    # so nothing can change it and Content-Length cannot go stale.
    response = Response(
        stream(),
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(size),
        },
    )
    response.call_on_close(release)
    return response


@api_bp.get("/export")
def export() -> Any:
    """Download selected messages and their pipeline state as a zstd JSON Lines export.

    Query param ``list`` (optional, repeatable) names the lists to export — one
    ``list=`` per list, an unknown name being a 404; omitting it entirely exports
    every list that has at least one message in scope. ``date_from`` / ``date_to``
    (optional, ISO-8601 date or datetime) bound the exported messages by their
    date, inclusive at both ends and each usable alone; they narrow the messages
    only, never the format, so a ranged export imports like any other. When the
    selection holds no message — an empty database, or a range nothing falls in —
    the response is a 404. The file is built via
    :func:`mailing_list_ai_check.export_import.export_lists` into a temporary
    ``.jsonl.zst`` file and served as an ``application/zstd`` attachment named
    ``mlac-export-<slug>-<YYYYMMDD>.jsonl.zst``, where ``<slug>`` is the one list's
    name, ``<n>-lists`` for several, or ``all``. A local database read only — no
    IMAP or Pangram calls, and message bodies are never logged.

    The finished file is streamed and cleaned up by :func:`_download_temp_file`,
    whose docstring covers the temp-file lifetime.

    Served only when ``ALLOW_EXPORT`` is on (the default); a disabled instance
    refuses it with a 403 before any database read.
    """
    if not current_app.config.get("ALLOW_EXPORT", True):
        raise ApiError("data export is disabled on this instance", 403)

    store = get_store()
    list_names = _selected_lists(request.args)
    date_from = _validate_iso("date_from", request.args.get("date_from"))
    date_to = _validate_iso("date_to", request.args.get("date_to"))

    # The temp name already carries the compressed suffix the exporter would
    # otherwise append, so it is written in place. The summary reports the path
    # actually written either way, and that — not the name guessed here — is what
    # gets served and unlinked.
    fd, tmp_path = tempfile.mkstemp(suffix=".jsonl.zst")
    os.close(fd)
    written_path = tmp_path
    try:
        try:
            summary = export_lists(
                store,
                list_names or None,
                tmp_path,
                all_lists=not list_names,
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as exc:
            # Unknown list name (the only ValueError export_lists raises for input).
            raise ApiError(str(exc), 404) from exc
        written_path = summary.path

        # No message in scope is nothing to download, whether the database is
        # empty, no list has mail, or the date range excludes all of it. Named
        # lists are checked on messages rather than on ``lists`` because a named
        # list always resolves — it is the range that can empty it.
        if summary.lists == 0 or summary.messages == 0:
            raise ApiError("nothing to export", 404)
    except BaseException:
        for path in {tmp_path, written_path}:
            _unlink_quietly(path)
        raise

    filename = (
        f"mlac-export-{_export_slug(list_names)}-{datetime.now().strftime('%Y%m%d')}.jsonl.zst"
    )
    return _download_temp_file(
        written_path,
        {tmp_path, written_path},
        filename=filename,
        mimetype="application/zstd",
    )


@api_bp.get("/export/stats")
def export_stats_download() -> Any:
    """Download the selected messages' scores and metadata as a CSV zip archive.

    The selection params are the full export's: ``list`` (optional, repeatable,
    an unknown name being a 404) and the inclusive ``date_from`` / ``date_to``
    bounds (a malformed one being a 400), with an empty selection again a 404.

    The archive is built via
    :func:`mailing_list_ai_check.stats_export.export_stats` into a temporary
    ``.zip`` and served as an ``application/zip`` attachment named
    ``mlac-stats-<slug>-<YYYYMMDD>.zip``, with the same ``<slug>`` rules as the
    full export and the same streaming and temp-file handling (see
    :func:`_download_temp_file`). It carries no message text and cannot be
    imported. A local database read only — no IMAP or Pangram calls.

    Served only when ``ALLOW_STATS_EXPORT`` is on (the default); a disabled
    instance refuses it with a 403 before any database read.
    """
    if not current_app.config.get("ALLOW_STATS_EXPORT", True):
        raise ApiError("stats export is disabled on this instance", 403)

    store = get_store()
    list_names = _selected_lists(request.args)
    date_from = _validate_iso("date_from", request.args.get("date_from"))
    date_to = _validate_iso("date_to", request.args.get("date_to"))

    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    written_path = tmp_path
    try:
        try:
            summary = export_stats(
                store,
                list_names or None,
                tmp_path,
                all_lists=not list_names,
                date_from=date_from,
                date_to=date_to,
            )
        except ValueError as exc:
            # Unknown list name (the only ValueError export_stats raises for input).
            raise ApiError(str(exc), 404) from exc
        written_path = summary.path
        if summary.lists == 0 or summary.messages == 0:
            raise ApiError("nothing to export", 404)
    except BaseException:
        for path in {tmp_path, written_path}:
            _unlink_quietly(path)
        raise

    filename = f"mlac-stats-{_export_slug(list_names)}-{datetime.now().strftime('%Y%m%d')}.zip"
    return _download_temp_file(
        written_path,
        {tmp_path, written_path},
        filename=filename,
        mimetype="application/zip",
    )


@api_bp.post("/import")
def import_() -> Any:
    """Import an uploaded export file into the store (idempotent, all-or-nothing).

    Expects a multipart upload with the export in the ``file`` field (missing ⇒
    400). ``dry_run`` (query or form param, parsed like the other boolean params)
    validates and reports without writing. The upload is saved to a temporary file
    under a neutral name — the importer detects zstd, gzip and uncompressed input
    from the content, so the uploaded filename is irrelevant — passed to
    :func:`mailing_list_ai_check.export_import.import_file`, and the temp file is
    always removed. Returns the :class:`ImportSummary` fields plus ``"ok": true``;
    a malformed or corrupt file surfaces as a 400.
    """
    upload = request.files.get("file")
    if upload is None:
        raise ApiError("no file uploaded (expected multipart field 'file')")

    dry_run_raw = request.args.get("dry_run")
    if dry_run_raw is None:
        dry_run_raw = request.form.get("dry_run")
    dry_run = bool(_parse_bool("dry_run", dry_run_raw))

    fd, tmp_path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    try:
        upload.save(tmp_path)
        try:
            summary = import_file(get_store(), tmp_path, dry_run=dry_run)
        except ExportImportError as exc:
            raise ApiError(str(exc)) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:  # pragma: no cover - best-effort cleanup
            pass

    return jsonify({**asdict(summary), "ok": True})


# --- documentation ------------------------------------------------------------


def _docs_root() -> Path:
    """The directory the documentation set is read from (the repo root).

    ``DOCS_ROOT`` is set by :func:`mailing_list_ai_check.webapp.create_app`; the
    fallback keeps the blueprint usable when it is registered on a bare app.
    """
    configured = current_app.config.get("DOCS_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3]


def _doc_title(path: Path, fallback: str) -> str:
    """The document's first level-1 ATX heading, or ``fallback`` if it has none."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("# "):
                    return line[2:].strip() or fallback
    except OSError:  # pragma: no cover - unreadable file
        return fallback
    return fallback


def _doc_index() -> list[dict[str, str]]:
    """The servable documents: ``README.md``, ``CHANGELOG.md``, then ``docs/*.md``.

    Only these are exposed, and only files that exist. ``docs`` is read one level
    deep — Markdown in its sub-directories (``docs/design``, ``docs/findings``) is
    deliberately excluded. The returned ``path`` values are the complete allowlist
    :func:`get_doc` matches a request against, so no request path ever reaches the
    filesystem.
    """
    root = _docs_root()
    entries: list[dict[str, str]] = []

    def add(rel: str) -> None:
        full = root / rel
        if full.is_file():
            entries.append({"path": rel, "title": _doc_title(full, rel)})

    add("README.md")
    add("CHANGELOG.md")
    docs_dir = root / "docs"
    if docs_dir.is_dir():
        for child in sorted(docs_dir.iterdir(), key=lambda p: p.name.lower()):
            if child.is_file() and child.suffix.lower() == ".md":
                add(f"docs/{child.name}")
    return entries


@api_bp.get("/docs")
def list_docs() -> Any:
    """List the documentation files the dashboard can display.

    Returns ``{"docs": [{"path": "README.md", "title": "…"}, …]}`` in display
    order, plus ``app_version`` (the running release) and ``extraction_version``
    (the extraction generation) for the drawer's footer. See :func:`_doc_index`
    for which files are included.
    """
    return jsonify(
        {
            "docs": _doc_index(),
            "app_version": __version__,
            "extraction_version": EXTRACTION_VERSION,
        }
    )


@api_bp.get("/docs/<path:doc_path>")
def get_doc(doc_path: str) -> Any:
    """Return one documentation file's raw Markdown.

    ``doc_path`` must equal one of the ``path`` values from :func:`list_docs`
    (anything else is a 404), so the client cannot read arbitrary files. Returns
    ``{"path": …, "title": …, "markdown": …}``; the caller renders the Markdown.
    """
    entry = next((e for e in _doc_index() if e["path"] == doc_path), None)
    if entry is None:
        raise ApiError("document not found", 404)
    try:
        text = (_docs_root() / entry["path"]).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - readable at index time, then not
        raise ApiError("could not read document", 500) from exc
    return jsonify({**entry, "markdown": text})
