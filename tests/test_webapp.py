"""Tests for the Flask JSON API (Phase 5).

Every test drives ``app.test_client()`` against a seeded temp database (see
:mod:`tests.seed`). Covers each endpoint, every filter (alone and combined),
pagination edges, sort orders, free-text search, summary correctness, person
CRUD + suggestions, input-validation 400s, 404s, the no-frontend JSON notice,
and CORS headers in dev mode.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import tracemalloc
import zipfile
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeFolder, FakeImapConn, make_raw

from mailing_list_ai_check import __version__, codec
from mailing_list_ai_check.cli import ScoreSummary
from mailing_list_ai_check.config import Config
from email_reply_extractor.extraction import EXTRACTION_VERSION
from mailing_list_ai_check.fetcher import FetchSummary
from mailing_list_ai_check.imap_client import ImapClient
from mailing_list_ai_check.staleness import ExtractionDiff
from mailing_list_ai_check.store import MULTI_NAME_ADDRESS_THRESHOLD, Store
from mailing_list_ai_check.webapp import DEV_CORS_ORIGIN, api as webapp_api, create_app

from seed import seed


def _config(db_path: Path) -> Config:
    return Config(
        imap_host="imap.example.org",
        imap_port=993,
        imap_username="anonymous",
        imap_password="anonymous@example.com",
        pangram_api_key="test-key",
        database_path=str(db_path),
        log_level="INFO",
        flask_host="127.0.0.1",
        flask_port=8050,
    )


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with Store(path) as store:
        seed(store)
    return path


@pytest.fixture
def client(db_path):
    """A test client in dev mode (no built frontend)."""
    app = create_app(_config(db_path), frontend_dist=None)
    app.testing = True
    return app.test_client()


def _ids(messages):
    return {m["message_id"] for m in messages}


# --- /api/messages ------------------------------------------------------------


def test_messages_default(client):
    resp = client.get("/api/messages")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 15
    assert body["page"] == 1
    assert body["per_page"] == 50
    assert body["pages"] == 1
    assert len(body["messages"]) == 15


def test_message_from_name_overrides_the_address_name(db_path, client):
    # A message whose header carried its own display name reports that name, in
    # the list row and in the detail, rather than the address's stored one.
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("alice@example.org")
        up = store.upsert_message(
            message_id="<own-name@test>",
            list_id=lst.id,
            address_id=addr.id,
            subject="Notification",
            date="2026-04-01T10:00:00",
            in_reply_to=None,
            raw_body="body",
            uid=901,
            from_name="Someone Else",
        )

    row = next(
        m
        for m in client.get("/api/messages?q=Notification").get_json()["messages"]
        if m["message_id"] == "<own-name@test>"
    )
    assert row["from"] == {"address": "alice@example.org", "display_name": "Someone Else"}

    detail = client.get(f"/api/messages/{up.message.id}").get_json()
    assert detail["from"] == {"address": "alice@example.org", "display_name": "Someone Else"}


def test_message_row_shape(client):
    body = client.get("/api/messages?list=announce&label=AI").get_json()
    assert body["total"] == 1
    row = body["messages"][0]
    assert row["message_id"] == "<m1@test>"
    # Seeded rows carry no per-message from_name, so the address name is served.
    assert row["from"] == {"address": "alice@example.org", "display_name": "Alice Smith"}
    assert row["person"]["name"] == "Alice Smith"
    assert row["extraction"] == {
        "status": "ok",
        "method": "email-reply-parser",
        "char_count": len("Body of Intro to draft"),
    }
    assert row["score"]["label"] == "AI"
    assert row["score"]["fraction_ai"] == pytest.approx(0.95)


def test_row_without_extraction_or_score(client):
    body = client.get("/api/messages?q=No extraction yet").get_json()
    row = body["messages"][0]
    assert row["extraction"] is None
    assert row["score"] is None


@pytest.mark.parametrize(
    "query,expected_total",
    [
        ("list=announce", 7),
        ("address=bob@example.org", 3),
        ("label=AI", 3),
        ("label=Human", 3),
        ("date_from=2026-02-01&date_to=2026-02-28", 5),
        ("min_likelihood=0.5", 5),
        ("max_likelihood=0.1", 3),
        ("has_score=true", 9),
        ("has_score=false", 6),
        ("q=QUIC", 3),
        ("list=announce&label=AI", 1),  # combined
        ("list=last-call&has_score=true", 3),  # combined
    ],
)
def test_message_filters(client, query, expected_total):
    body = client.get(f"/api/messages?{query}").get_json()
    assert body["total"] == expected_total


def test_filter_by_person(client):
    body = client.get("/api/messages?person=1&per_page=200").get_json()
    assert body["total"] == 5
    assert _ids(body["messages"]) == {
        "<m1@test>",
        "<m2@test>",
        "<m7@test>",
        "<m11@test>",
        "<m15@test>",
    }


def test_pagination_edges(client):
    p1 = client.get("/api/messages?page=1&per_page=10").get_json()
    p2 = client.get("/api/messages?page=2&per_page=10").get_json()
    p3 = client.get("/api/messages?page=3&per_page=10").get_json()
    assert len(p1["messages"]) == 10
    assert p1["pages"] == 2
    assert len(p2["messages"]) == 5
    assert p3["messages"] == []  # beyond the end


def test_per_page_cap(client):
    body = client.get("/api/messages?per_page=100000").get_json()
    assert body["per_page"] == 200  # capped
    assert len(body["messages"]) == 15


def test_sort_orders(client):
    asc = client.get("/api/messages?sort=date&order=asc&per_page=200").get_json()
    desc = client.get("/api/messages?sort=date&order=desc&per_page=200").get_json()
    assert asc["messages"][0]["message_id"] == "<m1@test>"
    assert desc["messages"][0]["message_id"] == "<m15@test>"
    frac = client.get("/api/messages?sort=fraction_ai&order=desc&per_page=200").get_json()
    assert frac["messages"][0]["message_id"] == "<m14@test>"


def test_sort_by_the_writing_rate_keeps_rateless_rows_last(client, db_path):
    """``sort=timing_cpm`` orders by the stored rate, rows without one last."""
    rated = {"<m1@test>": 5.0, "<m3@test>": 50.0, "<m7@test>": 900.0}
    with Store(db_path) as store:
        for message_id, cpm in rated.items():
            store.conn.execute(
                "UPDATE messages SET timing_cpm = ? WHERE message_id = ?", (cpm, message_id)
            )
        store.conn.commit()

    desc = client.get("/api/messages?sort=timing_cpm&order=desc&per_page=200").get_json()
    assert [m["message_id"] for m in desc["messages"][:3]] == [
        "<m7@test>",
        "<m3@test>",
        "<m1@test>",
    ]
    # The rateless rows follow the rated ones in this direction and the other:
    # the NULL group is pinned to the bottom, not reversed to the top.
    assert all(m["timing_cpm"] is None for m in desc["messages"][3:])

    asc = client.get("/api/messages?sort=timing_cpm&order=asc&per_page=200").get_json()
    assert [m["message_id"] for m in asc["messages"][:3]] == [
        "<m1@test>",
        "<m3@test>",
        "<m7@test>",
    ]
    assert all(m["timing_cpm"] is None for m in asc["messages"][3:])


@pytest.mark.parametrize(
    "query",
    [
        "page=abc",
        "page=0",
        "per_page=xyz",
        "per_page=0",
        "sort=bogus",
        "order=sideways",
        "min_likelihood=high",
        "min_likelihood=2",
        "max_likelihood=-1",
        "person=notanint",
        "date_from=not-a-date",
        "has_score=maybe",
    ],
)
def test_bad_query_params_return_400(client, query):
    resp = client.get(f"/api/messages?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/messages/<id> -------------------------------------------------------


def test_message_detail(client, db_path):
    # Find m2's db id via its message_id.
    with Store(db_path) as store:
        m2_id = store.find_message_by_message_id("<m2@test>").id
        m1_id = store.find_message_by_message_id("<m1@test>").id
    body = client.get(f"/api/messages/{m2_id}").get_json()
    assert body["message_id"] == "<m2@test>"
    assert body["raw_body"] == "RAW Re: Intro to draft"
    assert body["in_reply_to"] == "<m1@test>"
    assert body["thread_parent_id"] == m1_id  # resolved to stored parent
    assert body["extraction"]["extracted_text"] == "Body of Re: Intro to draft"
    assert body["score"]["label"] == "Human"
    assert body["score"]["prediction_short"] == "Human"
    # The stored Pangram response is not part of the detail payload.
    assert "raw_response" not in body["score"]


def test_message_detail_reports_ignored_lines_and_scored_word_count(client, db_path):
    # Seed a message whose extraction contains a greeting + sign-off + signature,
    # then assert message_detail reports which lines scoring would drop and the
    # word count of what would actually be sent to the detector.
    text = (
        "Hi all,\n"  # 0: greeting (dropped)
        "This is the substantive content of my message.\n"  # 1: kept
        "It has a couple of lines worth scoring here.\n"  # 2: kept
        "\n"  # 3: blank (never reported)
        "Best,\n"  # 4: sign-off (dropped)
        "Alice\n"  # 5: sign-off name (dropped)
        "-- \n"  # 6: signature delimiter (dropped)
        "Alice Example\n"  # 7: after delimiter (dropped)
        "ORCID: 0000-0002"  # 8: after delimiter (dropped)
    )
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce").id
        addr = store.upsert_address("frank@example.org", "Frank").id
        msg = store.upsert_message(
            message_id="<furniture@test>",
            list_id=lst,
            address_id=addr,
            subject="Furniture",
            date="2026-03-30T10:00:00",
            in_reply_to=None,
            raw_body="RAW Furniture",
            uid=None,
        ).message
        store.insert_extraction(message_id=msg.id, extracted_text=text, method="erp", status="ok")
        msg_id = msg.id

    body = client.get(f"/api/messages/{msg_id}").get_json()
    extraction = body["extraction"]
    # The full stage-1 text (furniture included) is still returned verbatim.
    assert extraction["extracted_text"] == text
    # Only non-blank furniture lines are reported; the blank line 3 is not.
    assert extraction["ignored_lines"] == [0, 4, 5, 6, 7, 8]
    # scored_word_count is the two surviving content lines (8 + 9 words).
    assert extraction["scored_word_count"] == 17


def test_message_detail_ignored_lines_reflect_html_signature_hint(client, db_path):
    # A signature line with no "-- " delimiter and no recognizable contact shape
    # would survive ordinary cleaning, but the message's HTML marks it as a
    # signature. message_detail must apply that hint so ignored_lines/scored count
    # reflect exactly what scoring drops.
    sig_line = "Frank Q Example Distinguished Engineer at Example Systems Group"
    text = (
        "This is the substantive content of my message for the group today.\n"  # 0 kept
        "It carries a second line of genuine review commentary to score.\n"  # 1 kept
        f"{sig_line}"  # 2 dropped only via the HTML signature hint
    )
    raw_html = (
        "<div>This is the substantive content of my message for the group today.</div>"
        "<div>It carries a second line of genuine review commentary to score.</div>"
        f'<div class="gmail_signature">{sig_line}</div>'
    )
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce").id
        addr = store.upsert_address("frank@example.org", "Frank").id
        msg = store.upsert_message(
            message_id="<htmlsig@test>",
            list_id=lst,
            address_id=addr,
            subject="HTML sig",
            date="2026-03-31T10:00:00",
            in_reply_to=None,
            raw_body="RAW",
            uid=None,
            raw_html=raw_html,
        ).message
        store.insert_extraction(message_id=msg.id, extracted_text=text, method="erp", status="ok")
        msg_id = msg.id

    extraction = client.get(f"/api/messages/{msg_id}").get_json()["extraction"]
    assert extraction["ignored_lines"] == [2]  # the signature line, via the hint
    assert extraction["scored_word_count"] == len(
        (
            "This is the substantive content of my message for the group today. "
            "It carries a second line of genuine review commentary to score."
        ).split()
    )


def test_message_detail_no_thread_parent(client, db_path):
    with Store(db_path) as store:
        m1_id = store.find_message_by_message_id("<m1@test>").id
    body = client.get(f"/api/messages/{m1_id}").get_json()
    assert body["thread_parent_id"] is None


def test_message_detail_404(client):
    resp = client.get("/api/messages/99999")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --- per-window details -------------------------------------------------------


def test_window_details_positions_windows_in_the_extracted_text():
    # Scoring sends the extracted text minus its furniture lines, so window
    # offsets index a text whose lines are a subsequence of the extracted ones.
    # The reported positions must point back at the extracted-text lines.
    extracted = "Hi all,\nFirst body line.\nSecond body line.\nBest,\nAlice\n"
    analysed = "First body line.\nSecond body line."
    raw = {
        "text": analysed,
        "windows": [
            {
                "text": "First body line.",
                "start_index": 0,
                "end_index": 16,
                "label": "Human Written",
                "ai_assistance_score": 0.02,
                "confidence": "High",
                "word_count": 3,
                "is_humanized": False,
                "humanizer_score": 0.0,
            },
            {
                # Leading whitespace is trimmed off, so the marker lands on the
                # first real character rather than the end of the line before.
                "text": "\nSecond body line.",
                "start_index": 16,
                "end_index": 34,
                "label": "AI-Generated",
                "ai_assistance_score": 0.91,
                "confidence": "Medium",
                "word_count": 3,
                "is_humanized": True,
                "humanizer_score": 0.87,
            },
        ],
    }
    first, second = webapp_api._window_details(raw, extracted)

    assert first["index"] == 1
    assert first["start"] == {"line": 1, "col": 0}
    assert first["end"] == {"line": 1, "col": 16}
    assert first["chars"] == 16
    assert first["ai_assistance_score"] == 0.02
    assert first["confidence"] == "High"
    assert first["label"] == "Human Written"
    assert first["is_humanized"] is False
    assert first["humanizer_score"] == 0.0

    assert second["index"] == 2
    assert second["start"] == {"line": 2, "col": 0}
    assert second["end"] == {"line": 2, "col": 17}
    assert second["chars"] == 17
    assert second["is_humanized"] is True
    assert second["humanizer_score"] == 0.87


def test_window_details_reports_scores_when_a_window_cannot_be_located():
    # A message re-extracted after it was scored no longer contains the analysed
    # lines. The window is still reported, without a position.
    raw = {
        "text": "A line that is gone now.",
        "windows": [
            {
                "text": "A line that is gone now.",
                "start_index": 0,
                "end_index": 24,
                "label": "AI-Generated",
                "ai_assistance_score": 0.88,
                "confidence": "High",
            }
        ],
    }
    (window,) = webapp_api._window_details(raw, "Completely different text.\n")
    assert window["start"] is None
    assert window["end"] is None
    assert window["ai_assistance_score"] == 0.88


def test_window_details_without_windows():
    assert webapp_api._window_details(None, "text") == []
    assert webapp_api._window_details({}, "text") == []


def test_message_row_carries_per_window_scores(client, db_path):
    with Store(db_path) as store:
        m2_id = store.find_message_by_message_id("<m2@test>").id
    body = client.get(f"/api/messages/{m2_id}").get_json()
    (window,) = body["score"]["windows"]
    assert window["confidence"] == "High"
    assert window["ai_assistance_score"] == pytest.approx(0.02)
    # The seeded window covers the whole single-line extracted text.
    assert window["start"] == {"line": 0, "col": 0}
    assert window["end"] == {"line": 0, "col": len("Body of Re: Intro to draft")}

    row = next(r for r in client.get("/api/messages").get_json()["messages"] if r["id"] == m2_id)
    # The list endpoint carries the numbers and the verdict label, not the
    # positions. The seeded response predates the humanizer fields, so
    # is_humanized is null.
    assert row["score"]["windows"] == [
        {
            "ai_assistance_score": pytest.approx(0.02),
            "confidence": "High",
            "label": "Human Written",
            "is_humanized": None,
        }
    ]


# --- /api/summary -------------------------------------------------------------


def test_summary(client):
    body = client.get("/api/summary").get_json()
    assert body["total"] == 15
    assert body["extracted"] == 10
    assert body["scored"] == 9
    assert body["too_short"] == 1
    assert body["label_distribution"] == {"AI": 3, "Human": 3, "Mixed": 3}
    assert body["avg_fraction_ai"] == pytest.approx(4.52 / 9)
    assert len(body["by_month"]) == 3


def test_summary_respects_filters(client):
    body = client.get("/api/summary?list=quic").get_json()
    assert body["total"] == 3
    assert body["scored"] == 1
    assert body["label_distribution"] == {"AI": 1}


def test_summary_db_size_bytes(client):
    # The test client is file-backed (see the db_path fixture), so the SQLite
    # file exists and has a positive size.
    body = client.get("/api/summary").get_json()
    assert body["db_size_bytes"] > 0


# --- /api/lists, /api/addresses, /api/persons ---------------------------------


def test_lists_endpoint(client):
    lists = client.get("/api/lists").get_json()["lists"]
    counts = {row["name"]: row["message_count"] for row in lists}
    assert counts == {"announce": 7, "last-call": 5, "quic": 3}


def test_addresses_endpoint_and_q(client):
    assert len(client.get("/api/addresses").get_json()["addresses"]) == 6
    filtered = client.get("/api/addresses?q=alice").get_json()["addresses"]
    assert {a["email"] for a in filtered} == {"alice@example.org", "alice@work.example"}


def test_persons_endpoint(client):
    persons = {p["canonical_name"]: p for p in client.get("/api/persons").get_json()["persons"]}
    assert set(persons) == {"Alice Smith", "Bob Jones"}
    assert persons["Alice Smith"]["message_count"] == 5


def test_lists_endpoint_label_mix(client):
    rows = {row["name"]: row for row in client.get("/api/lists").get_json()["lists"]}
    assert rows["announce"]["scored_count"] == 5
    assert rows["announce"]["label_counts"] == {
        "AI": 1,
        "Human": 2,
        "Mixed": 2,
    }
    assert rows["quic"]["label_counts"] == {"AI": 1}
    # m5 (announce) is the only extraction gated under the reliability floor.
    assert rows["announce"]["too_short_count"] == 1
    assert rows["quic"]["too_short_count"] == 0


def test_summary_by_list_carries_too_short_count(client):
    by_list = {row["list"]: row for row in client.get("/api/summary").get_json()["by_list"]}
    assert by_list["announce"]["too_short_count"] == 1
    assert by_list["last-call"]["too_short_count"] == 0


def test_lists_endpoint_earliest_message_at(client):
    rows = {row["name"]: row for row in client.get("/api/lists").get_json()["lists"]}
    # The oldest stored message date per list, as an ISO-8601 string.
    assert rows["announce"]["earliest_message_at"] == "2026-01-05T10:00:00"
    assert rows["last-call"]["earliest_message_at"] == "2026-01-08T10:00:00"
    assert rows["quic"]["earliest_message_at"] == "2026-01-25T10:00:00"


# --- /api/senders -------------------------------------------------------------


def _senders_by_name(body):
    return {row["name"]: row for row in body["senders"]}


def test_senders_default(client):
    body = client.get("/api/senders").get_json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["per_page"] == 60
    assert body["sort"] == "count"
    assert body["order"] == "desc"  # natural default for count
    # Default sort is count desc, ties broken by name asc.
    assert [row["name"] for row in body["senders"]] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def _seed_robot_sender(db_path, email="noreply@github.com", count=2):
    """Add ``count`` auto-generated messages from ``email`` to the announce list."""
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        address = store.upsert_address(email, "Robot")
        for i in range(count):
            store.upsert_message(
                message_id=f"<robot{i}@test>",
                list_id=lst.id,
                address_id=address.id,
                subject="notification",
                date="2026-04-01T10:00:00",
                in_reply_to=None,
                raw_body="body",
                uid=None,
                auto_generated="robot-sender",
            )


def test_senders_names_a_many_named_address_by_its_address(db_path, client):
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        address = store.upsert_address("noreply@example.org", "First Seen Name")
        for i in range(MULTI_NAME_ADDRESS_THRESHOLD):
            store.upsert_message(
                message_id=f"<many{i}@test>",
                list_id=lst.id,
                address_id=address.id,
                subject="notification",
                date="2026-04-01T10:00:00",
                in_reply_to=None,
                raw_body="body",
                uid=None,
                from_name=f"Person {i}",
            )

    senders = _senders_by_name(client.get("/api/senders").get_json())
    assert "First Seen Name" not in senders
    entry = senders["noreply@example.org"]
    assert entry["distinct_from_names"] == MULTI_NAME_ADDRESS_THRESHOLD
    assert entry["named_by_address"] is True
    assert entry["emails"] == ["noreply@example.org"]


def test_senders_omits_never_scored_senders_by_default(db_path, client):
    _seed_robot_sender(db_path)
    body = client.get("/api/senders").get_json()
    assert "Robot" not in [row["name"] for row in body["senders"]]
    assert body["total"] == 5  # unchanged by the robot sender
    assert body["include_excluded"] is False


def test_senders_include_excluded_shows_them_flagged(db_path, client):
    _seed_robot_sender(db_path)
    body = client.get("/api/senders?include_excluded=true").get_json()
    assert body["include_excluded"] is True
    assert body["total"] == 6
    robot = _senders_by_name(body)["Robot"]
    assert robot["excluded_from_scoring"] is True
    assert (robot["excluded_count"], robot["message_count"]) == (2, 2)
    # A sender with scoreable mail is reported either way, and never flagged.
    assert _senders_by_name(body)["Carol"]["excluded_from_scoring"] is False


def test_senders_omits_a_shared_address_by_default(db_path, client):
    with Store(db_path) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        address = store.upsert_address("noreply@ietf.org", "Datatracker")
        store.upsert_message(
            message_id="<dt1@test>",
            list_id=lst.id,
            address_id=address.id,
            subject="Ballot position",
            date="2026-04-01T10:00:00",
            in_reply_to=None,
            raw_body="body",
            uid=None,
        )

    body = client.get("/api/senders").get_json()
    assert "Datatracker" not in _senders_by_name(body)
    assert body["total"] == 5

    body = client.get("/api/senders?include_excluded=true").get_json()
    entry = _senders_by_name(body)["Datatracker"]
    assert entry["shared_address"] is True
    assert entry["excluded_from_scoring"] is False


def test_senders_rejects_a_bad_include_excluded(client):
    resp = client.get("/api/senders?include_excluded=maybe")
    assert resp.status_code == 400


def test_senders_person_and_unlinked_shape(client):
    senders = _senders_by_name(client.get("/api/senders").get_json())

    alice = senders["Alice Smith"]
    assert alice["type"] == "person"
    assert "person_id" in alice
    assert alice["emails"] == ["alice@example.org", "alice@work.example"]
    assert alice["message_count"] == 5
    assert alice["label_counts"] == {"AI": 1, "Human": 1, "Mixed": 2}

    assert alice["too_short_count"] == 0

    carol = senders["Carol"]
    assert carol["type"] == "address"
    assert "address_id" in carol
    assert carol["emails"] == ["carol@example.org"]
    assert carol["label_counts"] == {"Human": 2, "AI": 1}

    # Dave sent m5, the only extraction gated under the reliability floor.
    assert senders["Dave"]["too_short_count"] == 1

    # Linked addresses never surface as their own entry.
    assert "bob@example.org" not in senders


def test_senders_q_over_name_and_email(client):
    by_name = client.get("/api/senders?q=alice").get_json()
    assert {r["name"] for r in by_name["senders"]} == {"Alice Smith"}
    assert by_name["total"] == 1
    by_email = client.get("/api/senders?q=work.example").get_json()
    assert {r["name"] for r in by_email["senders"]} == {"Alice Smith"}
    assert client.get("/api/senders?q=nobody").get_json()["total"] == 0


def test_senders_sort_name_default_order(client):
    body = client.get("/api/senders?sort=name").get_json()
    assert body["order"] == "asc"  # natural default for name
    assert [r["name"] for r in body["senders"]] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_senders_sort_count_asc_explicit(client):
    body = client.get("/api/senders?sort=count&order=asc").get_json()
    assert [r["name"] for r in body["senders"]] == [
        "Dave",
        "Eve",
        "Bob Jones",
        "Carol",
        "Alice Smith",
    ]


def test_senders_sort_ai_default_order(client):
    body = client.get("/api/senders?sort=ai").get_json()
    assert body["order"] == "desc"  # natural default for ai
    # AI + Mixed counts: Alice 3, Bob 2, Carol 1, Dave 0, Eve 0 (level on
    # message count too, so name asc decides).
    assert [r["name"] for r in body["senders"]] == [
        "Alice Smith",
        "Bob Jones",
        "Carol",
        "Dave",
        "Eve",
    ]


def test_senders_sort_ai_asc_explicit(client):
    body = client.get("/api/senders?sort=ai&order=asc").get_json()
    assert [r["name"] for r in body["senders"]] == [
        "Dave",
        "Eve",
        "Carol",
        "Bob Jones",
        "Alice Smith",
    ]


def test_senders_ai_ties_break_on_count_in_both_directions(client, db_path):
    """Senders level on AI + Mixed count order by volume, most messages first, either way."""
    # Dave and Eve sit at an AI + Mixed count of 0 on 2 messages each. Frank
    # sits at the same count on 3, so only the tie-break decides where he lands
    # among them, and the volume key runs the same direction whichever way the
    # AI count is sorted.
    with Store(db_path) as store:
        announce = store.upsert_list("announce", "Shared Folders/announce")
        frank = store.upsert_address("frank@example.org", display_name="Frank")
        for n in range(3):
            store.upsert_message(
                message_id=f"<frank{n}@test>",
                list_id=announce.id,
                address_id=frank.id,
                subject="Notes",
                date=f"2026-04-0{n + 1}T10:00:00",
                in_reply_to=None,
                raw_body="body",
                uid=910 + n,
            )

    for order in ("desc", "asc"):
        body = client.get(f"/api/senders?sort=ai&order={order}").get_json()
        by_name = {r["name"]: r for r in body["senders"]}
        assert by_name["Frank"]["message_count"] == 3
        zero_count = [r["name"] for r in body["senders"] if r["name"] in ("Frank", "Dave", "Eve")]
        # Frank leads the tied group on volume; Dave and Eve, level on both
        # keys, keep the name-ascending order behind him.
        assert zero_count == ["Frank", "Dave", "Eve"]


def test_senders_pagination_and_total(client):
    p1 = client.get("/api/senders?sort=name&order=asc&page=1&per_page=2").get_json()
    p2 = client.get("/api/senders?sort=name&order=asc&page=2&per_page=2").get_json()
    p3 = client.get("/api/senders?sort=name&order=asc&page=3&per_page=2").get_json()
    assert p1["total"] == p2["total"] == p3["total"] == 5
    assert [r["name"] for r in p1["senders"]] == ["Alice Smith", "Bob Jones"]
    assert [r["name"] for r in p2["senders"]] == ["Carol", "Dave"]
    assert [r["name"] for r in p3["senders"]] == ["Eve"]


def test_senders_per_page_cap(client):
    body = client.get("/api/senders?per_page=100000").get_json()
    assert body["per_page"] == 200  # clamped to MAX_PER_PAGE


def test_senders_list_param_filters_and_echoes(client):
    body = client.get("/api/senders?list=quic").get_json()
    assert body["list"] == "quic"
    # quic messages: m13(a3) m14(a4) m15(a2) -> Alice, Bob, Carol only.
    assert body["total"] == 3
    senders = _senders_by_name(body)
    assert set(senders) == {"Alice Smith", "Bob Jones", "Carol"}
    assert senders["Carol"]["message_count"] == 1
    assert senders["Carol"]["label_counts"] == {"AI": 1}


def test_senders_list_param_default_is_null(client):
    body = client.get("/api/senders").get_json()
    assert body["list"] is None


def test_senders_unknown_list_returns_empty(client):
    body = client.get("/api/senders?list=does-not-exist").get_json()
    assert body["list"] == "does-not-exist"
    assert body["total"] == 0
    assert body["senders"] == []


@pytest.mark.parametrize(
    "query",
    [
        "sort=bogus",
        "order=sideways",
        "page=abc",
        "page=0",
        "per_page=xyz",
        "per_page=0",
    ],
)
def test_senders_bad_query_params_return_400(client, query):
    resp = client.get(f"/api/senders?{query}")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_person_suggestions(client):
    body = client.get("/api/persons/suggestions").get_json()
    suggestions = {s["display_name"]: s for s in body["suggestions"]}
    # "Alice Smith" is the only display name shared by 2+ distinct emails.
    assert "Alice Smith" in suggestions
    assert set(suggestions["Alice Smith"]["emails"]) == {
        "alice@example.org",
        "alice@work.example",
    }


# --- person CRUD --------------------------------------------------------------


def test_person_crud_roundtrip(client, db_path):
    with Store(db_path) as store:
        carol_id = store.address_rows("carol")[0]["id"]
        dave_id = store.address_rows("dave")[0]["id"]

    # Create with one address assigned.
    resp = client.post(
        "/api/persons",
        json={"canonical_name": "Carol Danvers", "address_ids": [carol_id]},
    )
    assert resp.status_code == 201
    person = resp.get_json()
    pid = person["id"]
    assert {a["id"] for a in person["addresses"]} == {carol_id}

    # Rename + add another address.
    resp = client.put(
        f"/api/persons/{pid}",
        json={"canonical_name": "Carol D.", "add_address_ids": [dave_id]},
    )
    assert resp.status_code == 200
    updated = resp.get_json()
    assert updated["canonical_name"] == "Carol D."
    assert {a["id"] for a in updated["addresses"]} == {carol_id, dave_id}

    # Detach one.
    resp = client.put(f"/api/persons/{pid}", json={"remove_address_ids": [carol_id]})
    assert {a["id"] for a in resp.get_json()["addresses"]} == {dave_id}

    # Delete detaches remaining address, not deletes it.
    assert client.delete(f"/api/persons/{pid}").status_code == 200
    assert client.get(f"/api/messages?person={pid}").get_json()["total"] == 0
    with Store(db_path) as store:
        assert store.get_address(dave_id).person_id is None


def test_person_create_validation(client):
    assert client.post("/api/persons", json={}).status_code == 400
    assert client.post("/api/persons", json={"canonical_name": ""}).status_code == 400
    # Non-existent address id.
    resp = client.post("/api/persons", json={"canonical_name": "X", "address_ids": [99999]})
    assert resp.status_code == 404
    # Bad address_ids type.
    resp = client.post("/api/persons", json={"canonical_name": "X", "address_ids": "nope"})
    assert resp.status_code == 400


def test_person_update_and_delete_404(client):
    assert client.put("/api/persons/99999", json={"canonical_name": "X"}).status_code == 404
    assert client.delete("/api/persons/99999").status_code == 404


# --- frontend / CORS / errors -------------------------------------------------


def test_no_frontend_json_notice(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["api_base"] == "/api"
    assert "not been built" in body["message"]


def test_cors_headers_in_dev_mode(client):
    resp = client.get("/api/lists")
    assert resp.headers["Access-Control-Allow-Origin"] == DEV_CORS_ORIGIN
    assert "GET" in resp.headers["Access-Control-Allow-Methods"]


def test_production_serves_frontend(tmp_path, db_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>dashboard</title>")
    app = create_app(_config(db_path), frontend_dist=dist)
    app.testing = True
    c = app.test_client()

    # Root serves the SPA shell.
    root = c.get("/")
    assert root.status_code == 200
    assert b"dashboard" in root.data
    # Unknown client route also falls back to index.html.
    assert b"dashboard" in c.get("/explorer").data
    # API still works and is not shadowed.
    assert c.get("/api/lists").status_code == 200
    # Unknown API route still returns JSON 404, not the SPA shell.
    missing = c.get("/api/nope")
    assert missing.status_code == 404
    assert "error" in missing.get_json()
    # No CORS header in production mode.
    assert "Access-Control-Allow-Origin" not in c.get("/api/lists").headers


def test_unknown_api_route_404_dev(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --- /api/pull (fetch + extract + score) --------------------------------------
#
# The pipeline is mocked at the api-module boundary (open_client / resolve_folders
# / run_fetch / run_extract / run_score / PangramClient) so no test ever touches
# the network or the paid Pangram API.


class _FakeImapClient:
    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass


def _pull_client(db_path: Path, *, pangram_key: str = "test-key"):
    config = replace(_config(db_path), pangram_api_key=pangram_key)
    app = create_app(config, frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_pull_happy_path_with_scoring(db_path, monkeypatch):
    calls: dict = {}

    def fake_resolve_folders(client, names, all_lists=False):
        calls["names"] = list(names)
        return [f"Shared Folders/{names[0]}"]

    def fake_run_fetch(client, store, request):
        calls["request"] = request
        return FetchSummary(fetched=5, duplicates=1, parse_errors=0)

    def fake_run_extract(store, limit=None):
        calls["extract_limit"] = limit
        return Counter({"ok": 4, "empty": 1}), Counter({"email-reply-parser": 5})

    def fake_run_score(store, client, *, limit=None, **kwargs):
        calls["score_limit"] = limit
        return ScoreSummary(scored=3, cache_hits=1, too_short=1, api_calls=3)

    class FakePangram:
        def __init__(self, key, *, model=None):
            calls["pangram_key"] = key
            calls["pangram_model"] = model

    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(webapp_api, "resolve_folders", fake_resolve_folders)
    monkeypatch.setattr(webapp_api, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(webapp_api, "run_extract", fake_run_extract)
    monkeypatch.setattr(webapp_api, "run_score", fake_run_score)
    monkeypatch.setattr(webapp_api, "PangramClient", FakePangram)

    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 25})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "fetched": 5,
        "duplicates": 1,
        "parse_errors": 0,
        "extracted": 4,
        "empty": 1,
        "too_short": 1,
        "scored": 3,
        "cache_hits": 1,
        "api_calls": 3,
        "scoring_skipped": False,
    }
    # count drives depth, the fetch cap, extraction and scoring limits.
    assert calls["names"] == ["newlist"]
    assert calls["request"].depth.count == 25
    assert calls["request"].limit == 25
    assert calls["extract_limit"] == 25
    assert calls["score_limit"] == 25
    assert calls["pangram_key"] == "test-key"


def test_pull_skips_scoring_without_api_key(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(
        webapp_api, "resolve_folders", lambda c, names, all_lists=False: ["Shared Folders/x"]
    )
    monkeypatch.setattr(webapp_api, "run_fetch", lambda c, s, r: FetchSummary(fetched=2))
    monkeypatch.setattr(
        webapp_api, "run_extract", lambda s, limit=None: (Counter({"ok": 2}), Counter())
    )

    def _must_not_call(*a, **k):
        raise AssertionError("run_score must not run without an API key")

    monkeypatch.setattr(webapp_api, "run_score", _must_not_call)

    resp = _pull_client(db_path, pangram_key="").post(
        "/api/pull", json={"list": "newlist", "count": 5}
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scoring_skipped"] is True
    assert body["scored"] == 0
    assert body["cache_hits"] == 0
    assert body["api_calls"] == 0
    assert body["too_short"] == 0
    assert body["fetched"] == 2
    assert body["extracted"] == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 10},  # missing list
        {"list": "", "count": 10},  # empty list
        {"list": "   ", "count": 10},  # whitespace-only list
        {"list": "bad name", "count": 10},  # space is not allowed
        {"list": "bad/name", "count": 10},  # slash not allowed
        {"list": "ok"},  # missing count
        {"list": "ok", "count": 0},  # below min
        {"list": "ok", "count": 1001},  # above max
        {"list": "ok", "count": "ten"},  # non-int
        {"list": "ok", "count": 1.5},  # float is not an int
        {"list": "ok", "count": True},  # bool is not a valid int here
    ],
)
def test_pull_validation_400(client, payload):
    resp = client.post("/api/pull", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/lists/regenerate ------------------------------------------------------


def test_regenerate_lists_reconciles_and_returns_counts(db_path, monkeypatch):
    # The seed has announce/last-call/quic, all with messages. The fake server
    # enumeration drops last-call and adds wimse: last-call must survive with a
    # removed_from_server_at stamp, wimse must appear.
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())

    def fake_refresh(client, store):
        # Mirror the real fetcher.refresh_lists_index contract: the store
        # reconciliation counts plus the two activity-check keys it appends.
        counts = store.refresh_lists_index(
            [
                ("announce", "Shared Folders/announce"),
                ("quic", "Shared Folders/quic"),
                ("wimse", "Shared Folders/wimse"),
            ]
        )
        counts["activity_checked"] = 0
        counts["activity_failed"] = 0
        return counts

    monkeypatch.setattr(webapp_api, "refresh_lists_index", fake_refresh)

    c = _pull_client(db_path)
    resp = c.post("/api/lists/regenerate")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "added": 1,
        "restored": 0,
        "deleted": 0,
        "kept_missing": 1,
        "total": 4,
        "activity_checked": 0,
        "activity_failed": 0,
    }
    rows = {row["name"]: row for row in c.get("/api/lists").get_json()["lists"]}
    assert set(rows) == {"announce", "last-call", "quic", "wimse"}
    assert rows["last-call"]["removed_from_server_at"] is not None
    assert rows["announce"]["removed_from_server_at"] is None
    # /api/lists now exposes the new column on every row.
    assert all("last_message_at" in row for row in rows.values())


def test_regenerate_lists_imap_connect_failure_502(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    resp = _pull_client(db_path).post("/api/lists/regenerate")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_regenerate_lists_enumeration_failure_502(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())

    def _boom(client, store):
        raise RuntimeError("LIST failed")

    monkeypatch.setattr(webapp_api, "refresh_lists_index", _boom)
    resp = _pull_client(db_path).post("/api/lists/regenerate")
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_pull_imap_connect_failure_502(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 5})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_pull_fetch_failure_502(db_path, monkeypatch):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(
        webapp_api, "resolve_folders", lambda c, names, all_lists=False: ["Shared Folders/x"]
    )

    def _boom(*a, **k):
        raise RuntimeError("EXAMINE failed")

    monkeypatch.setattr(webapp_api, "run_fetch", _boom)
    resp = _pull_client(db_path).post("/api/pull", json={"list": "newlist", "count": 5})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- /api/export, /api/import -------------------------------------------------
#
# The seed carries 15 messages across 3 lists, 13 extraction rows (every status),
# and 9 scores; export/import counts below are read against those totals.


def _empty_client(tmp_path, name="empty.db"):
    """A dev-mode client over a fresh, schema-initialised but empty database."""
    path = tmp_path / name
    with Store(path):
        pass  # opening a new path runs the migrations, creating an empty schema
    app = create_app(_config(path), frontend_dist=None)
    app.testing = True
    return path, app.test_client()


def _records(body):
    """Decode an export response body into its list of records.

    :func:`codec.open_read_text` classifies the container from its content and
    works on a path, so the response bytes go through a temporary file; the
    decoding therefore makes no assumption about which container was served.
    """
    with tempfile.NamedTemporaryFile(suffix=".export") as fh:
        fh.write(body)
        fh.flush()
        with codec.open_read_text(fh.name) as text:
            return [json.loads(line) for line in text if line.strip()]


def _multipart(data_bytes, filename="mlac-export.jsonl.zst"):
    return {"file": (io.BytesIO(data_bytes), filename)}


def test_export_all_lists(client):
    resp = client.get("/api/export")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zstd"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert disposition.endswith('.jsonl.zst"')

    records = _records(resp.data)
    assert records[0]["type"] == "header"
    assert records[0]["format"] == "mlac-export"
    assert records[-1]["type"] == "trailer"
    assert records[-1]["messages"] == 15


def test_export_body_is_really_zstd(client):
    """The download is a zstd stream, not JSON Lines under a compressed name."""
    resp = client.get("/api/export")
    assert resp.status_code == 200
    assert resp.data[:4] == codec.ZSTD_MAGIC


def test_export_declares_the_exact_body_length(client):
    """``Content-Length`` is the finished file's size, so it can never be wrong."""
    resp = client.get("/api/export")
    assert resp.status_code == 200
    assert int(resp.headers["Content-Length"]) == len(resp.data)


def test_export_single_list(client):
    resp = client.get("/api/export?list=announce")
    assert resp.status_code == 200
    header = _records(resp.data)[0]
    # Only the requested list's folder is present.
    assert header["folders"] == ["Shared Folders/announce"]


def test_export_several_lists(client):
    """``list`` repeats: each one selects a list, and the file holds them all."""
    resp = client.get("/api/export?list=announce&list=quic")
    assert resp.status_code == 200
    records = _records(resp.data)
    assert records[0]["folders"] == ["Shared Folders/announce", "Shared Folders/quic"]
    # 7 announce + 3 quic, and nothing from last-call.
    assert records[-1]["messages"] == 10
    assert 'filename="mlac-export-2-lists-' in resp.headers["Content-Disposition"]


def test_export_repeated_list_name_is_not_exported_twice(client):
    resp = client.get("/api/export?list=announce&list=announce")
    assert resp.status_code == 200
    records = _records(resp.data)
    assert records[0]["folders"] == ["Shared Folders/announce"]
    assert 'filename="mlac-export-announce-' in resp.headers["Content-Disposition"]


def test_export_date_range_narrows_the_messages(client):
    """The seed's January messages, across every list (m1, m8, m2, m3, m13)."""
    resp = client.get("/api/export?date_from=2026-01-01&date_to=2026-01-31")
    assert resp.status_code == 200
    records = _records(resp.data)
    assert records[-1]["messages"] == 5
    header = records[0]
    assert (header["date_from"], header["date_to"]) == ("2026-01-01", "2026-01-31")


def test_export_date_range_combines_with_selected_lists(client):
    """announce in February: m4, m5, m6 — quic contributes nothing in that month."""
    resp = client.get("/api/export?list=announce&list=quic&date_from=2026-02-01&date_to=2026-02-28")
    assert resp.status_code == 200
    records = _records(resp.data)
    assert records[-1]["messages"] == 3
    # quic is still named in the file: an explicitly requested list is exported
    # whether or not the range leaves it any message.
    assert records[0]["folders"] == ["Shared Folders/announce", "Shared Folders/quic"]


def test_export_open_ended_range_needs_only_one_bound(client):
    resp = client.get("/api/export?date_from=2026-03-01")
    assert resp.status_code == 200
    records = _records(resp.data)
    assert records[-1]["messages"] == 5  # m7, m11, m12, m14, m15
    assert "date_to" not in records[0]


def test_export_empty_date_range_404(client):
    """A range no message falls in is nothing to download, not an empty file."""
    resp = client.get("/api/export?date_from=2020-01-01&date_to=2020-12-31")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.parametrize("param", ["date_from", "date_to"], ids=["from", "to"])
def test_export_rejects_a_malformed_date_400(client, param):
    resp = client.get(f"/api/export?{param}=last-tuesday")
    assert resp.status_code == 400
    assert param in resp.get_json()["error"]


def test_export_unknown_list_404(client):
    resp = client.get("/api/export?list=does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_export_empty_db_404(tmp_path):
    _, c = _empty_client(tmp_path)
    resp = c.get("/api/export")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


# --- /api/export/stats ---------------------------------------------------------
#
# The same selection as /api/export over the same seed. The archive's own
# contents are covered by tests/test_stats_export.py; what is asserted here is
# the HTTP surface.


def _archive(body):
    """The zip response body as a :class:`zipfile.ZipFile` over its bytes."""
    return zipfile.ZipFile(io.BytesIO(body))


def _stats_datapackage(body):
    """The archive's Frictionless descriptor, parsed."""
    with _archive(body) as archive:
        return json.loads(archive.read("datapackage.json").decode("utf-8"))


def _stats_mlac(body):
    """The descriptor's custom provenance object."""
    return _stats_datapackage(body)["mlac"]


def _stats_columns(body, member="messages.csv"):
    with _archive(body) as archive:
        return archive.read(member).decode("utf-8").splitlines()[0].split(",")


def test_export_stats_all_lists(client):
    resp = client.get("/api/export/stats")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    disposition = resp.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert disposition.endswith('.zip"')
    assert 'filename="mlac-stats-all-' in disposition

    with _archive(resp.data) as archive:
        assert set(archive.namelist()) == {
            "messages.csv",
            "lists.csv",
            "senders.csv",
            "datapackage.json",
            "README.md",
        }
    # The whole seed: 15 messages over 3 lists, from 6 addresses (a1+a2 and a3
    # grouped into 2 persons, a4/a5/a6 unlinked) — senders counts the rows of
    # senders.csv, which is one per address.
    assert _stats_mlac(resp.data)["rows"] == {"messages": 15, "lists": 3, "senders": 6}


def test_export_stats_declares_the_exact_body_length(client):
    resp = client.get("/api/export/stats")
    assert int(resp.headers["Content-Length"]) == len(resp.data)


def test_export_stats_single_list(client):
    resp = client.get("/api/export/stats?list=announce")
    assert resp.status_code == 200
    assert 'filename="mlac-stats-announce-' in resp.headers["Content-Disposition"]
    mlac = _stats_mlac(resp.data)
    assert mlac["folders"] == ["Shared Folders/announce"]
    assert mlac["rows"]["messages"] == 7


def test_export_stats_several_lists(client):
    resp = client.get("/api/export/stats?list=announce&list=quic")
    assert resp.status_code == 200
    assert 'filename="mlac-stats-2-lists-' in resp.headers["Content-Disposition"]
    assert _stats_mlac(resp.data)["rows"]["messages"] == 10


def test_export_stats_date_range_narrows_the_messages(client):
    resp = client.get("/api/export/stats?date_from=2026-01-01&date_to=2026-01-31")
    assert resp.status_code == 200
    mlac = _stats_mlac(resp.data)
    assert mlac["rows"]["messages"] == 5  # m1, m8, m2, m3, m13
    assert (mlac["date_from"], mlac["date_to"]) == ("2026-01-01", "2026-01-31")


def test_export_stats_names_senders_by_address(client):
    """One variant only: the addresses are present and senders.csv groups them."""
    mlac = _stats_mlac(client.get("/api/export/stats").data)
    assert mlac["stats_format_version"] == 2
    assert "identified" not in mlac


def test_export_stats_members_carry_the_version_2_columns(client):
    resp = client.get("/api/export/stats")
    assert _stats_columns(resp.data)[:5] == ["message_id", "list", "folder", "date", "email"]
    assert _stats_columns(resp.data, "senders.csv") == ["sender_key", "email"]


def test_export_stats_descriptor_describes_the_served_members(client):
    """The download is a data package, not only a zip of files."""
    descriptor = _stats_datapackage(client.get("/api/export/stats").data)
    assert descriptor["$schema"] == "https://datapackage.org/profiles/2.0/datapackage.json"
    assert descriptor["name"] == "mlac-stats"
    assert [resource["path"] for resource in descriptor["resources"]] == [
        "messages.csv",
        "lists.csv",
        "senders.csv",
    ]
    messages = descriptor["resources"][0]
    assert [field["name"] for field in messages["schema"]["fields"]] == _stats_columns(
        client.get("/api/export/stats").data
    )
    assert messages["schema"]["primaryKey"] == ["folder", "message_id"]


def test_export_stats_ignores_an_unknown_query_param(client):
    """The removed ``pseudonymous`` param is an unknown one, not an error."""
    resp = client.get("/api/export/stats?pseudonymous=true")
    assert resp.status_code == 200
    assert "email" in _stats_columns(resp.data)


@pytest.mark.parametrize("param", ["date_from", "date_to"], ids=["from", "to"])
def test_export_stats_rejects_a_malformed_date_400(client, param):
    resp = client.get(f"/api/export/stats?{param}=last-tuesday")
    assert resp.status_code == 400
    assert param in resp.get_json()["error"]


def test_export_stats_unknown_list_404(client):
    resp = client.get("/api/export/stats?list=does-not-exist")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_export_stats_empty_date_range_404(client):
    resp = client.get("/api/export/stats?date_from=2020-01-01&date_to=2020-12-31")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_export_stats_empty_db_404(tmp_path):
    _, c = _empty_client(tmp_path)
    resp = c.get("/api/export/stats")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_import_roundtrip(client, tmp_path):
    export_bytes = client.get("/api/export").data
    _, c2 = _empty_client(tmp_path)

    resp = c2.post("/api/import", data=_multipart(export_bytes), content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["dry_run"] is False
    assert body["messages_inserted"] == 15
    assert body["extractions_inserted"] == 13
    assert body["scores_inserted"] == 9
    # The imported data is now queryable in the target.
    assert c2.get("/api/messages").get_json()["total"] == 15

    # Re-importing the same file is a no-op: every message is skipped, nothing new.
    again = c2.post(
        "/api/import", data=_multipart(export_bytes), content_type="multipart/form-data"
    ).get_json()
    assert again["messages_skipped"] == 15
    assert again["messages_inserted"] == 0
    assert again["extractions_inserted"] == 0
    assert again["scores_inserted"] == 0


def test_import_dry_run_leaves_target_unchanged(client, tmp_path):
    export_bytes = client.get("/api/export").data
    _, c2 = _empty_client(tmp_path)

    resp = c2.post(
        "/api/import?dry_run=true",
        data=_multipart(export_bytes),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["dry_run"] is True
    assert body["messages_inserted"] == 15  # reported...
    assert c2.get("/api/messages").get_json()["total"] == 0  # ...but nothing written


def test_import_no_file_400(tmp_path):
    _, c2 = _empty_client(tmp_path)
    resp = c2.post("/api/import", data={}, content_type="multipart/form-data")
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_import_corrupt_file_400(tmp_path):
    _, c2 = _empty_client(tmp_path)
    resp = c2.post(
        "/api/import",
        data=_multipart(b"this is not a valid export\n", filename="x.jsonl"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    # All-or-nothing: the failed import left the target empty.
    assert c2.get("/api/messages").get_json()["total"] == 0


def test_import_corrupt_compressed_upload_400(tmp_path):
    """Content that only looks like zstd is a bad request, not a 500."""
    _, c2 = _empty_client(tmp_path)
    resp = c2.post(
        "/api/import",
        data=_multipart(codec.ZSTD_MAGIC + b"not a zstd frame" * 8),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert c2.get("/api/messages").get_json()["total"] == 0


def test_import_non_utf8_binary_upload_400(tmp_path):
    """A binary upload is a bad request, not a 500.

    Only zstd and gzip magic mark an upload as compressed, so binary content
    without either is read as plain text and fails in the UTF-8 decoder rather
    than the codec layer; that path has to reach the client as a 400 too.
    """
    _, c2 = _empty_client(tmp_path)
    junk = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x02\x03\x0a\xfe\xff\x80\x81" * 8
    assert not junk.startswith(codec.ZSTD_MAGIC)
    assert not junk.startswith(codec.GZIP_MAGIC)

    resp = c2.post(
        "/api/import",
        data=_multipart(junk, filename="photo.jpg"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    # The decode branch, not a broken container.
    assert "not valid UTF-8" in resp.get_json()["error"]
    assert c2.get("/api/messages").get_json()["total"] == 0


def test_import_truncated_export_upload_400(client, tmp_path):
    """A download cut short mid-transfer fails as a bad request and writes nothing."""
    export_bytes = client.get("/api/export").data
    _, c2 = _empty_client(tmp_path)

    resp = c2.post(
        "/api/import",
        data=_multipart(export_bytes[: len(export_bytes) // 2]),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert c2.get("/api/messages").get_json()["total"] == 0


def test_import_accepts_an_uncompressed_upload(client, tmp_path):
    """The uploaded name is irrelevant: plain JSON Lines under a .zst name imports."""
    records = _records(client.get("/api/export").data)
    plain = "".join(json.dumps(rec) + "\n" for rec in records).encode("utf-8")

    _, c2 = _empty_client(tmp_path)
    resp = c2.post(
        "/api/import",
        data=_multipart(plain, filename="mlac-export.jsonl.zst"),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert resp.get_json()["messages_inserted"] == 15


# --- /api/export streaming and temp-file hygiene ------------------------------
#
# The endpoint builds the export in a temporary file and streams it back in
# chunks, and must leave behind neither the file nor an open descriptor on any
# exit path. Redirecting :mod:`tempfile` at an empty directory makes a leftover
# file directly observable; shadowing ``open`` in the endpoint's module makes a
# leftover descriptor observable.


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    """An empty directory that :func:`tempfile.mkstemp` writes into."""
    path = tmp_path / "tempfiles"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


@pytest.fixture
def opened_files(monkeypatch):
    """Collects the file objects the endpoint opens, so closure can be asserted.

    ``api.py`` resolves the builtin through its own module globals, so binding the
    name there intercepts the single ``open`` call the export makes.
    """
    handles = []
    real_open = open

    def _spy(*args, **kwargs):
        fh = real_open(*args, **kwargs)
        handles.append(fh)
        return fh

    monkeypatch.setattr(webapp_api, "open", _spy, raising=False)
    return handles


@pytest.fixture
def small_chunks(monkeypatch):
    """Shrink the streamed chunk to 16 bytes, so the seeded export spans many."""
    monkeypatch.setattr(webapp_api, "_EXPORT_CHUNK_BYTES", 16)
    return 16


def test_export_leaves_no_temp_file_on_success(client, temp_dir):
    assert client.get("/api/export").status_code == 200
    assert os.listdir(temp_dir) == []


def test_export_leaves_no_temp_file_on_unknown_list_404(client, temp_dir):
    assert client.get("/api/export?list=does-not-exist").status_code == 404
    assert os.listdir(temp_dir) == []


def test_export_leaves_no_temp_file_when_there_is_nothing_to_export(tmp_path, temp_dir):
    _, c = _empty_client(tmp_path)
    assert c.get("/api/export").status_code == 404
    assert os.listdir(temp_dir) == []


def test_export_stats_leaves_no_temp_file_on_success(client, temp_dir):
    """The stats archive is built and served through the same temp-file handling."""
    assert client.get("/api/export/stats").status_code == 200
    assert os.listdir(temp_dir) == []


def test_export_stats_leaves_no_temp_file_on_unknown_list_404(client, temp_dir):
    assert client.get("/api/export/stats?list=does-not-exist").status_code == 404
    assert os.listdir(temp_dir) == []


def test_export_stats_streams_the_body_in_chunks(client, temp_dir, small_chunks):
    resp = client.get("/api/export/stats")
    chunks = list(resp.response)
    resp.close()
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= small_chunks
    assert b"".join(chunks)[:2] == b"PK"  # a zip local file header
    assert os.listdir(temp_dir) == []


def test_export_leaves_no_temp_file_when_the_export_fails(client, temp_dir, monkeypatch):
    """An unexpected failure mid-export still removes the temporary file."""

    def _boom(*args, **kwargs):
        raise RuntimeError("export exploded")

    monkeypatch.setattr(webapp_api, "export_lists", _boom)
    with pytest.raises(RuntimeError):
        client.get("/api/export")
    assert os.listdir(temp_dir) == []


def test_export_unlinks_the_file_before_the_body_is_streamed(client, temp_dir, small_chunks):
    """The name is gone as soon as the view returns; the open descriptor serves it.

    Nothing later in the download can therefore leak a file, whatever the client
    or the server does with the response.
    """
    resp = client.get("/api/export")
    assert os.listdir(temp_dir) == []  # already unlinked, body not read yet
    assert resp.data[:4] == codec.ZSTD_MAGIC
    assert os.listdir(temp_dir) == []


def test_export_streams_the_body_in_chunks(client, temp_dir, small_chunks):
    """The body arrives as many bounded chunks, not one buffer of the whole file."""
    resp = client.get("/api/export")
    chunks = list(resp.response)
    resp.close()
    assert len(chunks) > 1
    assert max(len(chunk) for chunk in chunks) <= small_chunks
    assert b"".join(chunks)[:4] == codec.ZSTD_MAGIC
    assert os.listdir(temp_dir) == []


def test_export_releases_the_file_when_the_client_disconnects(
    client, temp_dir, opened_files, small_chunks
):
    """Abandoning the download part-way still closes the descriptor.

    Closing the response iterable without exhausting it is what a WSGI server
    does on an early disconnect (PEP 3333), and is what the generator's
    ``finally`` exists for.
    """
    resp = client.get("/api/export")
    first = next(iter(resp.response))
    assert len(first) == small_chunks  # genuinely mid-stream, not buffered
    (handle,) = opened_files
    assert not handle.closed

    resp.close()  # the disconnect
    assert handle.closed
    assert os.listdir(temp_dir) == []


def test_export_releases_the_file_when_the_body_is_never_read(client, temp_dir, opened_files):
    """A response closed without the body ever starting still releases everything.

    Werkzeug serves a HEAD request by dropping the body iterable unstarted, so the
    generator's ``finally`` never runs and only the ``call_on_close`` callback can
    close the descriptor.
    """
    resp = client.head("/api/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Length"] != "0"
    (handle,) = opened_files

    resp.close()
    assert handle.closed
    assert os.listdir(temp_dir) == []


def test_export_releases_the_file_when_the_body_is_buffered(client, temp_dir, opened_files):
    """Reading the body through the response object also releases the descriptor.

    :meth:`werkzeug.wrappers.Response.get_data` — reachable from any hook that
    inspects a response body — collapses the iterable and closes it directly,
    without running the ``call_on_close`` callbacks; the generator's ``finally``
    is what covers that path. The view is called inside a request context because
    the WSGI layer never hands the inner response object to a client.
    """
    with client.application.test_request_context("/api/export"):
        resp = webapp_api.export()
        assert resp.get_data()[:4] == codec.ZSTD_MAGIC

    (handle,) = opened_files
    assert handle.closed
    assert os.listdir(temp_dir) == []


def test_export_peak_memory_does_not_track_the_export_size(client, temp_dir, monkeypatch):
    """Streaming a 32 MB export allocates chunks, not 32 MB.

    ``export_lists`` is replaced by one that writes a large file, because the
    property under test is about size and the seeded database is a few kilobytes.
    The endpoint reads only ``.lists``, ``.messages`` and ``.path`` off the
    summary. Peak Python
    allocation is measured, not sampled RSS, so the result is exact and stable;
    the whole request is inside the traced window because the test client pulls
    the first chunk eagerly, and the stand-in exporter writes in small blocks so
    that building the file cannot dominate the measurement.
    """
    size = 32 * 1024 * 1024
    block_size = 64 * 1024

    def _big_export(store, lists, path, **kwargs):
        with open(path, "wb") as fh:
            for _ in range(size // block_size):
                fh.write(b"\0" * block_size)
        return SimpleNamespace(lists=1, messages=1, path=path)

    monkeypatch.setattr(webapp_api, "export_lists", _big_export)

    tracemalloc.start()
    try:
        resp = client.get("/api/export")
        streamed = sum(len(chunk) for chunk in resp.response)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    resp.close()

    assert streamed == size
    assert int(resp.headers["Content-Length"]) == size
    # Measured at ~0.35 MB; the bound is loose enough not to be a tripwire on
    # unrelated per-request allocation, and still far below a buffered body.
    assert peak < size // 16
    assert os.listdir(temp_dir) == []


# --- /api/lists/preview + /api/pull/range -------------------------------------
#
# Both endpoints back the dashboard's "Add messages" popover. open_client is
# monkeypatched to return a real ImapClient over the network-free FakeImapConn
# (so EXAMINE / UID SEARCH / header + body FETCH all run against an in-memory
# folder), while run_extract / run_score / PangramClient are faked at the api
# boundary so no test touches the network or the paid Pangram API.


def _server_folder(uids, *, uidvalidity=1000, name="announce"):
    """A FakeFolder of real messages at ``uids`` (distinct sender/subject each)."""
    fd = FakeFolder(uidvalidity=uidvalidity, uidnext=(max(uids) + 1 if uids else 1))
    fd.exists = len(uids)
    for uid in uids:
        fd.messages[uid] = make_raw(
            message_id=f"<msg{uid}@x>",
            from_header=f"User{uid} <user{uid}@example.org>",
            subject=f"Subject {uid}",
            date="Mon, 06 Jan 2025 10:00:00 +0000",
        )
        fd.dates[uid] = datetime(2025, 1, 6, 10, 0, 0)
        fd.froms[uid] = f"user{uid}@example.org"
    return fd


def _range_db(tmp_path, *, stored_uids, cursor=None, name="announce"):
    """A fresh db with list ``name``, messages at ``stored_uids`` and opt. cursor."""
    db = tmp_path / "range.db"
    with Store(db) as store:
        lst = store.upsert_list(name, f"Shared Folders/{name}")
        addr = store.upsert_address("stored@example.org", "Stored")
        for uid in stored_uids:
            store.upsert_message(
                message_id=f"<stored{uid}@x>",
                list_id=lst.id,
                address_id=addr.id,
                subject=f"stored {uid}",
                date="2025-01-01T00:00:00+00:00",
                in_reply_to=None,
                raw_body="stored body",
                uid=uid,
            )
        if cursor is not None:
            store.set_pull_state(lst.id, cursor[0], cursor[1])
    return db


def _client_over(db, conn, monkeypatch, *, pangram_key=""):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: ImapClient(conn))
    config = replace(_config(db), pangram_api_key=pangram_key)
    app = create_app(config, frontend_dist=None)
    app.testing = True
    return app.test_client()


# --- preview ------------------------------------------------------------------


def test_preview_new_lists_first_25_ascending(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[])  # nothing stored -> baseline 0
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 31))})
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/lists/preview", json={"list": "announce", "mode": "new"}).get_json()
    assert body["mode"] == "new"
    assert body["list"] == "announce"
    assert body["total"] == 30
    assert body["shown"] == 25  # only the first 25 are previewed
    assert body["more"] == 5
    # First 25 ascending: uids 1..25, oldest first.
    assert [m["from_email"] for m in body["messages"][:2]] == [
        "user1@example.org",
        "user2@example.org",
    ]
    assert body["messages"][-1]["from_email"] == "user25@example.org"
    assert body["messages"][0]["subject"] == "Subject 1"
    assert body["messages"][0]["date"] == "2025-01-06T10:00:00+00:00"


def test_preview_before_lists_last_count_ascending(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])  # earliest stored uid is 5
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 11))})
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/lists/preview", json={"list": "announce", "mode": "before", "count": 2}
    ).get_json()
    assert body["mode"] == "before"
    # Older-than-5 uids on the server are 1..4.
    assert body["total"] == 4
    assert body["shown"] == 2
    assert body["more"] == 2
    # The LAST 2 (immediately preceding uid 5), ascending: uids 3, 4.
    assert [m["from_email"] for m in body["messages"]] == [
        "user3@example.org",
        "user4@example.org",
    ]


def test_preview_before_no_stored_uids_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[])  # nothing to anchor "before"
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 5))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json={"list": "announce", "mode": "before"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_preview_unknown_list_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[1])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder([1])})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json={"list": "nope", "mode": "new"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "new"},  # missing list
        {"list": "bad name", "mode": "new"},  # space not allowed
        {"list": "announce"},  # missing mode
        {"list": "announce", "mode": "sideways"},  # bad mode
        {"list": "announce", "mode": "before", "count": "ten"},  # non-int count
        {"list": "announce", "mode": "before", "count": True},  # bool not an int
    ],
)
def test_preview_validation_400(tmp_path, monkeypatch, payload):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/lists/preview", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_preview_imap_connect_failure_502(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    config = replace(_config(db), pangram_api_key="")
    app = create_app(config, frontend_dist=None)
    app.testing = True
    resp = app.test_client().post("/api/lists/preview", json={"list": "announce", "mode": "new"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- ranged pull --------------------------------------------------------------


def _fake_pipeline(monkeypatch, calls, *, scored=False):
    def fake_extract(store, limit=None):
        calls["extract_limit"] = limit
        return Counter({"ok": 1}), Counter({"email-reply-parser": 1})

    monkeypatch.setattr(webapp_api, "run_extract", fake_extract)

    def fake_score(store, client, *, limit=None, **kw):
        calls["score_limit"] = limit
        return ScoreSummary(scored=2, cache_hits=1, too_short=0, api_calls=2)

    monkeypatch.setattr(webapp_api, "run_score", fake_score)
    monkeypatch.setattr(webapp_api, "PangramClient", lambda key, *, model=None: None)


def test_pull_range_new_with_cursor_advances_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5], cursor=(1000, 5))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "new", "count": 2}
    ).get_json()
    # Baseline is the cursor's last_uid 5, so new uids are 6,7,8; first 2 -> 6,7.
    assert body["mode"] == "new"
    assert body["matched"] == 3
    assert body["capped"] is False
    assert body["fetched"] == 2
    assert body["scoring_skipped"] is True  # no api key on this client
    assert calls["extract_limit"] == 2
    # Cursor advanced to the max fetched uid (7), same UIDVALIDITY.
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 7)


def test_pull_range_new_without_cursor_falls_back_to_max_stored_uid(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])  # no cursor
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    # Baseline = max stored uid (5); new = 6,7,8; count omitted -> all.
    assert body["matched"] == 3
    assert body["capped"] is False
    assert body["fetched"] == 3
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        assert store.get_pull_state(lst.id).last_uid == 8


def test_pull_range_new_uidvalidity_mismatch_ignores_stale_cursor(tmp_path, monkeypatch):
    # Cursor is from a DIFFERENT UIDVALIDITY with a high last_uid; it must be
    # ignored in favour of the max stored uid, and then rewritten.
    db = _range_db(tmp_path, stored_uids=[5], cursor=(999, 100))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    assert body["matched"] == 3  # 6,7,8 (baseline fell back to stored max 5)
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 8)  # rewritten to server's


def test_pull_range_before_never_touches_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5, 6], cursor=(1000, 6))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 7))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "before", "count": 2}
    ).get_json()
    # Older-than-5 uids are 1..4; last 2 -> 3,4.
    assert body["mode"] == "before"
    assert body["matched"] == 4
    assert body["capped"] is False
    assert body["fetched"] == 2
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 6)  # unchanged


def test_pull_range_new_all_caps_at_max(tmp_path, monkeypatch):
    # 1001 new uids with count omitted ("all") must be capped to _MAX_PULL_COUNT.
    db = _range_db(tmp_path, stored_uids=[])
    fd = FakeFolder(uidvalidity=1000, uidnext=1003)
    fd.exists = 1001
    for uid in range(1, 1002):
        fd.messages[uid] = b""  # search only needs the keys; run_fetch_uids is faked
    conn = FakeImapConn(folders={"Shared Folders/announce": fd})

    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)

    def fake_fetch_uids(client, store, folder, uids, *, batch_size=200):
        calls["n_uids"] = len(uids)
        return FetchSummary(fetched=len(uids))

    monkeypatch.setattr(webapp_api, "run_fetch_uids", fake_fetch_uids)
    c = _client_over(db, conn, monkeypatch)

    body = c.post("/api/pull/range", json={"list": "announce", "mode": "new"}).get_json()
    assert body["matched"] == 1001
    assert body["capped"] is True
    assert calls["n_uids"] == 1000  # trimmed to the cap
    assert body["fetched"] == 1000


def test_pull_range_scores_when_api_key_present(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5], cursor=(1000, 5))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    calls: dict = {}
    _fake_pipeline(monkeypatch, calls)
    c = _client_over(db, conn, monkeypatch, pangram_key="test-key")

    body = c.post(
        "/api/pull/range", json={"list": "announce", "mode": "new", "count": 3}
    ).get_json()
    assert body["scoring_skipped"] is False
    assert body["scored"] == 2
    assert body["cache_hits"] == 1
    assert body["api_calls"] == 2
    assert calls["score_limit"] == 3  # limit = number of uids fetched


@pytest.mark.parametrize(
    "payload",
    [
        {"list": "announce", "mode": "before"},  # before requires count
        {"list": "announce", "mode": "before", "count": 0},  # below min
        {"list": "announce", "mode": "before", "count": 1001},  # above max
        {"list": "announce", "mode": "before", "count": "x"},  # non-int
        {"list": "announce", "mode": "before", "count": True},  # bool not an int
        {"list": "announce", "mode": "new", "count": 0},  # below min
        {"list": "announce", "mode": "new", "count": 1001},  # above max
        {"list": "announce", "mode": "sideways", "count": 5},  # bad mode
    ],
)
def test_pull_range_validation_400(tmp_path, monkeypatch, payload):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_pull_range_unknown_list_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range", json={"list": "nope", "mode": "new"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_pull_range_imap_connect_failure_502(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    config = replace(_config(db), pangram_api_key="")
    app = create_app(config, frontend_dist=None)
    app.testing = True
    resp = app.test_client().post("/api/pull/range", json={"list": "announce", "mode": "new"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- stage endpoints: /api/pull/fetch, /api/extract, /api/score ----------------
#
# These split /pull's three stages into separate sequential calls. The pipeline
# is mocked at the api-module boundary exactly as the /pull tests above, so no
# test touches the network or the paid Pangram API.


def test_pull_fetch_happy_path(db_path, monkeypatch):
    calls: dict = {}

    def fake_resolve_folders(client, names, all_lists=False):
        calls["names"] = list(names)
        return [f"Shared Folders/{names[0]}"]

    def fake_run_fetch(client, store, request):
        calls["request"] = request
        return FetchSummary(fetched=5, duplicates=1, parse_errors=0)

    def _must_not_extract(*a, **k):
        raise AssertionError("run_extract must not run for the fetch-only stage")

    def _must_not_score(*a, **k):
        raise AssertionError("run_score must not run for the fetch-only stage")

    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeImapClient())
    monkeypatch.setattr(webapp_api, "resolve_folders", fake_resolve_folders)
    monkeypatch.setattr(webapp_api, "run_fetch", fake_run_fetch)
    monkeypatch.setattr(webapp_api, "run_extract", _must_not_extract)
    monkeypatch.setattr(webapp_api, "run_score", _must_not_score)

    resp = _pull_client(db_path).post("/api/pull/fetch", json={"list": "newlist", "count": 25})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "fetched": 5,
        "duplicates": 1,
        "parse_errors": 0,
        "limit": 25,  # echoes count for the extract/score stages
    }
    assert calls["names"] == ["newlist"]
    assert calls["request"].depth.count == 25
    assert calls["request"].limit == 25


@pytest.mark.parametrize(
    "payload",
    [
        {"count": 10},  # missing list
        {"list": "", "count": 10},  # empty list
        {"list": "   ", "count": 10},  # whitespace-only list
        {"list": "bad name", "count": 10},  # space is not allowed
        {"list": "bad/name", "count": 10},  # slash not allowed
        {"list": "ok"},  # missing count
        {"list": "ok", "count": 0},  # below min
        {"list": "ok", "count": 1001},  # above max
        {"list": "ok", "count": "ten"},  # non-int
        {"list": "ok", "count": 1.5},  # float is not an int
        {"list": "ok", "count": True},  # bool is not a valid int here
    ],
)
def test_pull_fetch_validation_400(client, payload):
    resp = client.post("/api/pull/fetch", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_pull_fetch_imap_connect_failure_502(db_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    resp = _pull_client(db_path).post("/api/pull/fetch", json={"list": "newlist", "count": 5})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


def test_extract_happy_path(db_path, monkeypatch):
    calls: dict = {}

    def fake_run_extract(store, limit=None):
        calls["limit"] = limit
        return Counter({"ok": 4, "empty": 1}), Counter({"email-reply-parser": 5})

    monkeypatch.setattr(webapp_api, "run_extract", fake_run_extract)

    resp = _pull_client(db_path).post("/api/extract", json={"limit": 25})
    assert resp.status_code == 200
    assert resp.get_json() == {"extracted": 4, "empty": 1}
    assert calls["limit"] == 25


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing limit
        {"limit": 0},  # below min
        {"limit": 1001},  # above max
        {"limit": "ten"},  # non-int
        {"limit": 1.5},  # float is not an int
        {"limit": True},  # bool is not a valid int here
    ],
)
def test_extract_validation_400(client, payload):
    resp = client.post("/api/extract", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_score_happy_path_with_key(db_path, monkeypatch):
    calls: dict = {}

    def fake_run_score(store, client, *, limit=None, **kwargs):
        calls["limit"] = limit
        return ScoreSummary(scored=3, cache_hits=1, too_short=1, api_calls=3)

    class FakePangram:
        def __init__(self, key, *, model=None):
            calls["pangram_key"] = key
            calls["pangram_model"] = model

    monkeypatch.setattr(webapp_api, "run_score", fake_run_score)
    monkeypatch.setattr(webapp_api, "PangramClient", FakePangram)

    resp = _pull_client(db_path).post("/api/score", json={"limit": 25})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "scored": 3,
        "cache_hits": 1,
        "api_calls": 3,
        "too_short": 1,
        "scoring_skipped": False,
    }
    assert calls["limit"] == 25
    assert calls["pangram_key"] == "test-key"


def test_score_skips_without_api_key(db_path, monkeypatch):
    def _must_not_call(*a, **k):
        raise AssertionError("run_score must not run without an API key")

    monkeypatch.setattr(webapp_api, "run_score", _must_not_call)

    resp = _pull_client(db_path, pangram_key="").post("/api/score", json={"limit": 5})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "scored": 0,
        "cache_hits": 0,
        "api_calls": 0,
        "too_short": 0,
        "scoring_skipped": True,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing limit
        {"limit": 0},  # below min
        {"limit": 1001},  # above max
        {"limit": "ten"},  # non-int
        {"limit": 1.5},  # float is not an int
        {"limit": True},  # bool is not a valid int here
    ],
)
def test_score_validation_400(client, payload):
    resp = client.post("/api/score", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/settings + the Pangram detector generation --------------------------
#
# The stored ``pangram_model`` setting decides which detector every scoring run
# selects, and which stored verdicts the upgrade notice counts as out of date.
# Scoring is faked at the api boundary throughout, so no test calls Pangram.


def _set_detector_version(db_path: Path, message_key: str, version: str, seeded) -> int:
    """Stamp the score of ``message_key``'s extraction with ``version``.

    Returns the message primary key, so a test can name it in a request.
    """
    message_pk = seeded.messages[message_key]
    with Store(db_path) as store:
        store.conn.execute(
            "UPDATE scores SET detector_version = ? WHERE extraction_id = "
            "(SELECT id FROM extractions WHERE message_id = ?)",
            (version, message_pk),
        )
        store.conn.commit()
    return message_pk


@pytest.fixture
def seeded(db_path):
    """The seeded message primary keys, by their spec key (``m1`` … ``m15``)."""
    with Store(db_path) as store:
        messages = {
            f"m{i}": store.find_message_by_message_id(f"<m{i}@test>").id for i in range(1, 16)
        }
    return SimpleNamespace(messages=messages)


def test_settings_default_to_pangram_4(client):
    assert client.get("/api/settings").get_json() == {"pangram_model": "pangram-4"}


def test_settings_put_persists_the_model(client):
    resp = client.put("/api/settings", json={"pangram_model": "default"})
    assert resp.status_code == 200
    assert resp.get_json() == {"pangram_model": "default"}
    # A later request reads the stored value.
    assert client.get("/api/settings").get_json() == {"pangram_model": "default"}


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing key
        {"pangram_model": "pangram-5"},  # not an accepted selector
        {"pangram_model": None},
        {"pangram_model": 4},
        {"detector": "pangram-4"},  # unknown key
        {"pangram_model": "default", "extra": 1},  # unknown key alongside a valid one
    ],
)
def test_settings_put_validation_400(client, payload):
    resp = client.put("/api/settings", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    # Nothing was written.
    assert client.get("/api/settings").get_json() == {"pangram_model": "pangram-4"}


def test_score_stage_uses_the_stored_model(db_path, monkeypatch):
    calls: dict = {}

    def fake_run_score(store, client, *, limit=None, **kwargs):
        return ScoreSummary(scored=1, cache_hits=0, too_short=0, api_calls=1)

    class FakePangram:
        def __init__(self, key, *, model=None):
            calls["model"] = model

    monkeypatch.setattr(webapp_api, "run_score", fake_run_score)
    monkeypatch.setattr(webapp_api, "PangramClient", FakePangram)

    c = _pull_client(db_path)
    assert c.put("/api/settings", json={"pangram_model": "default"}).status_code == 200
    assert c.post("/api/score", json={"limit": 5}).status_code == 200
    assert calls["model"] == "default"

    # Switching back selects Pangram 4 for the next run.
    assert c.put("/api/settings", json={"pangram_model": "pangram-4"}).status_code == 200
    assert c.post("/api/score", json={"limit": 5}).status_code == 200
    assert calls["model"] == "pangram-4"


# --- /api/pangram/notice ------------------------------------------------------


def test_notice_pending_when_old_generation_scores_exist(client, db_path):
    body = client.get("/api/pangram/notice").get_json()
    # Every seeded score carries a pre-4 detector version.
    assert body["state"] == "pending"
    assert body["old_scores"] == 9
    with Store(db_path) as store:
        expected = [message_id for message_id, _ in store.scores_outside_generation("4")]
    assert body["message_ids"] == expected
    # The scored extractions hold 39 words between them, at $0.05 per 100.
    assert body["estimated_words"] == 39
    assert body["estimated_cost_v4"] == 0.02


def test_notice_dismissed_on_a_database_with_nothing_to_retest(tmp_path):
    db = tmp_path / "fresh.db"
    with Store(db):
        pass
    app = create_app(_config(db), frontend_dist=None)
    app.testing = True
    body = app.test_client().get("/api/pangram/notice").get_json()
    assert body == {
        "state": "dismissed",
        "old_scores": 0,
        "message_ids": [],
        "estimated_words": 0,
        "estimated_cost_v4": 0.0,
    }


def test_notice_counts_only_scores_outside_the_selected_generation(client, db_path, seeded):
    _set_detector_version(db_path, "m1", "4.0", seeded)
    body = client.get("/api/pangram/notice").get_json()
    assert body["old_scores"] == 8
    assert seeded.messages["m1"] not in body["message_ids"]

    # Selecting Pangram 3 turns the same row into the out-of-date one.
    client.put("/api/settings", json={"pangram_model": "default"})
    ids = client.get("/api/pangram/notice").get_json()["message_ids"]
    assert seeded.messages["m1"] in ids


@pytest.mark.parametrize("state", ["later", "dismissed"])
def test_notice_put_persists_the_state(client, state):
    resp = client.put("/api/pangram/notice", json={"state": state})
    assert resp.status_code == 200
    assert resp.get_json()["state"] == state
    # The counts come back with it, and the state survives the next read.
    assert resp.get_json()["old_scores"] == 9
    assert client.get("/api/pangram/notice").get_json()["state"] == state


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing state
        {"state": "pending"},  # resolved, never settable
        {"state": "nope"},
        {"state": None},
    ],
)
def test_notice_put_validation_400(client, payload):
    resp = client.put("/api/pangram/notice", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert client.get("/api/pangram/notice").get_json()["state"] == "pending"


# --- /api/pangram/retest ------------------------------------------------------


def test_retest_drops_only_old_generation_scores_and_rescores(db_path, seeded, monkeypatch):
    current = _set_detector_version(db_path, "m1", "4.0", seeded)
    old = seeded.messages["m3"]

    calls: dict = {}

    def fake_run_score(store, client, *, limit=None, **kwargs):
        calls["limit"] = limit
        calls["message_ids"] = kwargs.get("message_ids")
        return ScoreSummary(scored=1, cache_hits=0, too_short=0, api_calls=1)

    class FakePangram:
        def __init__(self, key, *, model=None):
            calls["model"] = model

    monkeypatch.setattr(webapp_api, "run_score", fake_run_score)
    monkeypatch.setattr(webapp_api, "PangramClient", FakePangram)

    resp = _pull_client(db_path).post("/api/pangram/retest", json={"ids": [current, old]})
    assert resp.status_code == 200
    assert resp.get_json() == {
        "scored": 1,
        "cache_hits": 0,
        "api_calls": 1,
        "too_short": 0,
        "scoring_skipped": False,
        "invalidated": 1,
    }
    assert calls["limit"] == 2
    assert calls["message_ids"] == {current, old}
    assert calls["model"] == "pangram-4"

    with Store(db_path) as store:
        # The Pangram 4 verdict survived; the older one was dropped, which puts
        # its extraction back in the scoring queue.
        assert store.score_for_extraction(store.extraction_for_message(current).id) is not None
        assert store.score_for_extraction(store.extraction_for_message(old).id) is None


def test_retest_ignores_messages_without_a_score(db_path, seeded, monkeypatch):
    monkeypatch.setattr(
        webapp_api,
        "run_score",
        lambda store, client, **kwargs: ScoreSummary(scored=0, cache_hits=0, api_calls=0),
    )
    monkeypatch.setattr(webapp_api, "PangramClient", lambda key, *, model=None: None)

    # m13 is extracted but unscored; m12 has no extraction row at all.
    ids = [seeded.messages["m13"], seeded.messages["m12"]]
    body = _pull_client(db_path).post("/api/pangram/retest", json={"ids": ids}).get_json()
    assert body["invalidated"] == 0


def test_retest_skips_scoring_without_an_api_key(db_path, seeded, monkeypatch):
    def _must_not_call(*a, **k):
        raise AssertionError("run_score must not run without an API key")

    monkeypatch.setattr(webapp_api, "run_score", _must_not_call)

    ids = [seeded.messages["m3"]]
    resp = _pull_client(db_path, pangram_key="").post("/api/pangram/retest", json={"ids": ids})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["scoring_skipped"] is True
    # The out-of-date verdict is still dropped, so a later run re-scores it.
    assert body["invalidated"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing ids
        {"ids": []},  # empty
        {"ids": [1, "two"]},  # non-int
        {"ids": list(range(1001))},  # above the cap
    ],
)
def test_retest_validation_400(client, payload):
    resp = client.post("/api/pangram/retest", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- /api/pull/range/fetch ----------------------------------------------------
#
# The fetch-only stage of /pull/range. Driven over the network-free FakeImapConn
# (real EXAMINE / UID SEARCH / body FETCH), with run_extract / run_score faked so
# they can be asserted never to run.


def _guard_no_pipeline(monkeypatch):
    """Assert the extract/score pipeline never runs for a fetch-only stage."""

    def _must_not_extract(*a, **k):
        raise AssertionError("run_extract must not run for the fetch-only stage")

    def _must_not_score(*a, **k):
        raise AssertionError("run_score must not run for the fetch-only stage")

    monkeypatch.setattr(webapp_api, "run_extract", _must_not_extract)
    monkeypatch.setattr(webapp_api, "run_score", _must_not_score)


def test_pull_range_fetch_new_advances_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5], cursor=(1000, 5))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 9))})
    _guard_no_pipeline(monkeypatch)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range/fetch", json={"list": "announce", "mode": "new", "count": 2}
    ).get_json()
    # Baseline is the cursor's last_uid 5, so new uids are 6,7,8; first 2 -> 6,7.
    assert body["mode"] == "new"
    assert body["matched"] == 3
    assert body["capped"] is False
    assert body["fetched"] == 2
    assert body["limit"] == 2  # number of messages chosen, for later stages
    assert "extracted" not in body  # fetch stage never runs extract/score
    assert "scored" not in body
    # Cursor advanced to the max fetched uid (7), same UIDVALIDITY.
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 7)


def test_pull_range_fetch_before_never_touches_pull_state(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5, 6], cursor=(1000, 6))
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 7))})
    _guard_no_pipeline(monkeypatch)
    c = _client_over(db, conn, monkeypatch)

    body = c.post(
        "/api/pull/range/fetch", json={"list": "announce", "mode": "before", "count": 2}
    ).get_json()
    # Older-than-5 uids are 1..4; last 2 -> 3,4.
    assert body["mode"] == "before"
    assert body["matched"] == 4
    assert body["fetched"] == 2
    assert body["limit"] == 2
    with Store(db) as store:
        lst = store.get_list_by_name("announce")
        ps = store.get_pull_state(lst.id)
        assert (ps.uidvalidity, ps.last_uid) == (1000, 6)  # unchanged


@pytest.mark.parametrize(
    "payload",
    [
        {"list": "announce", "mode": "before"},  # before requires count
        {"list": "announce", "mode": "before", "count": 0},  # below min
        {"list": "announce", "mode": "before", "count": 1001},  # above max
        {"list": "announce", "mode": "before", "count": "x"},  # non-int
        {"list": "announce", "mode": "before", "count": True},  # bool not an int
        {"list": "announce", "mode": "new", "count": 0},  # below min
        {"list": "announce", "mode": "new", "count": 1001},  # above max
        {"list": "announce", "mode": "sideways", "count": 5},  # bad mode
        {"mode": "new"},  # missing list
        {"list": "bad name", "mode": "new"},  # space not allowed
    ],
)
def test_pull_range_fetch_validation_400(tmp_path, monkeypatch, payload):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range/fetch", json=payload)
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_pull_range_fetch_unknown_list_404(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])
    conn = FakeImapConn(folders={"Shared Folders/announce": _server_folder(range(1, 6))})
    c = _client_over(db, conn, monkeypatch)
    resp = c.post("/api/pull/range/fetch", json={"list": "nope", "mode": "new"})
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_pull_range_fetch_imap_connect_failure_502(tmp_path, monkeypatch):
    db = _range_db(tmp_path, stored_uids=[5])

    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(webapp_api, "open_client", _boom)
    config = replace(_config(db), pangram_api_key="")
    app = create_app(config, frontend_dist=None)
    app.testing = True
    resp = app.test_client().post("/api/pull/range/fetch", json={"list": "announce", "mode": "new"})
    assert resp.status_code == 502
    assert "error" in resp.get_json()


# --- documentation ------------------------------------------------------------


@pytest.fixture
def docs_client(db_path, tmp_path):
    """A test client whose documentation root is a small synthetic repo tree."""
    root = tmp_path / "repo"
    (root / "docs" / "findings").mkdir(parents=True)
    (root / "README.md").write_text("# Mailing List AI Check\n\nintro\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "docs" / "zeta.md").write_text("# Zeta\n", encoding="utf-8")
    (root / "docs" / "alpha.md").write_text("no heading here\n", encoding="utf-8")
    (root / "docs" / "notes.txt").write_text("not markdown\n", encoding="utf-8")
    (root / "docs" / "findings" / "imap.md").write_text("# Findings\n", encoding="utf-8")
    (root / "secret.md").write_text("not in the set\n", encoding="utf-8")

    app = create_app(_config(db_path), frontend_dist=None, docs_root=root)
    app.testing = True
    return app.test_client()


def test_docs_index_order_and_titles(docs_client):
    body = docs_client.get("/api/docs").get_json()
    paths = [d["path"] for d in body["docs"]]
    # README and CHANGELOG lead; docs/ follows, sorted by file name.
    assert paths == ["README.md", "CHANGELOG.md", "docs/alpha.md", "docs/zeta.md"]
    titles = {d["path"]: d["title"] for d in body["docs"]}
    assert titles["README.md"] == "Mailing List AI Check"
    assert titles["docs/zeta.md"] == "Zeta"
    # No level-1 heading → the path is the title.
    assert titles["docs/alpha.md"] == "docs/alpha.md"
    # The index carries the version stamps for the drawer's footer.
    assert body["app_version"] == __version__
    assert body["extraction_version"] == EXTRACTION_VERSION


def test_docs_index_excludes_subdirectories_and_non_markdown(docs_client):
    paths = [d["path"] for d in docs_client.get("/api/docs").get_json()["docs"]]
    assert "docs/findings/imap.md" not in paths
    assert "docs/notes.txt" not in paths
    assert "secret.md" not in paths


def test_docs_get_returns_markdown(docs_client):
    body = docs_client.get("/api/docs/README.md").get_json()
    assert body["path"] == "README.md"
    assert body["title"] == "Mailing List AI Check"
    assert body["markdown"] == "# Mailing List AI Check\n\nintro\n"
    nested = docs_client.get("/api/docs/docs/zeta.md").get_json()
    assert nested["markdown"] == "# Zeta\n"


def test_docs_get_rejects_anything_outside_the_index(docs_client):
    for path in (
        "secret.md",
        "docs/notes.txt",
        "docs/findings/imap.md",
        "../README.md",
        "docs/../secret.md",
        "nope.md",
    ):
        resp = docs_client.get(f"/api/docs/{path}")
        assert resp.status_code == 404, path
        assert "error" in resp.get_json()


def test_docs_index_tolerates_missing_files(db_path, tmp_path):
    empty = tmp_path / "bare"
    empty.mkdir()
    app = create_app(_config(db_path), frontend_dist=None, docs_root=empty)
    app.testing = True
    body = app.test_client().get("/api/docs").get_json()
    assert body["docs"] == []
    assert body["app_version"] == __version__
    assert body["extraction_version"] == EXTRACTION_VERSION


def test_docs_index_covers_the_real_repository(db_path):
    """The default docs root is the repo, and it exposes the shipped files."""
    app = create_app(_config(db_path), frontend_dist=None)
    app.testing = True
    paths = [d["path"] for d in app.test_client().get("/api/docs").get_json()["docs"]]
    assert paths[:2] == ["README.md", "CHANGELOG.md"]
    assert "docs/export-import.md" in paths
    assert not any(p.startswith("docs/findings/") for p in paths)


# --- reply timing ---------------------------------------------------------------


def _recompute_timing(db_path):
    """Classify the seeded replies (the pipeline stages normally do this)."""
    with Store(db_path) as store:
        store.recompute_timing()


def test_messages_include_timing_and_filter_by_the_rate(client, db_path):
    _recompute_timing(db_path)
    # m2 is the only seeded reply whose parent is stored: 26 chars of new text
    # over a 10-day gap, a small fraction of a char/minute (band: normal).
    body = client.get("/api/messages?cpm_max=1").get_json()
    assert body["total"] == 1
    assert body["messages"][0]["message_id"] == "<m2@test>"
    assert body["messages"][0]["timing"] == "normal"
    # Every other seeded message has no rate, so any bound excludes them all.
    assert client.get("/api/messages?cpm_min=1").get_json()["total"] == 0


def test_messages_timing_is_null_before_classification(client):
    body = client.get("/api/messages?q=Re: Intro").get_json()
    assert body["messages"][0]["timing"] is None
    assert body["messages"][0]["timing_cpm"] is None


def test_messages_include_the_rate_behind_the_timing_band(client, db_path):
    _recompute_timing(db_path)
    row = client.get("/api/messages?cpm_max=1").get_json()["messages"][0]
    # m2's 26 characters of new text over a 10-day gap: a fraction of a
    # char/minute, rounded to one decimal place.
    assert row["timing_cpm"] is not None
    assert 0 <= row["timing_cpm"] < 1
    # A message with no band (not a reply) carries no rate either.
    unbanded = client.get("/api/messages?list=announce&label=AI").get_json()["messages"][0]
    assert unbanded["timing"] is None
    assert unbanded["timing_cpm"] is None


@pytest.mark.parametrize("param", ["cpm_min", "cpm_max"])
def test_messages_rejects_non_numeric_rate_bounds(client, param):
    resp = client.get(f"/api/messages?{param}=bogus")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == f"{param} must be a number"


@pytest.mark.parametrize("param", ["cpm_min", "cpm_max"])
def test_messages_rejects_negative_rate_bounds(client, param):
    resp = client.get(f"/api/messages?{param}=-1")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == f"{param} must be >= 0"


def test_messages_ignores_the_withdrawn_timing_param(client, db_path):
    """The band filter is gone: an old link's ``timing`` param selects nothing.

    It is an unknown query param now, so it is ignored rather than rejected —
    the request returns the unfiltered page instead of a 400.
    """
    _recompute_timing(db_path)
    unfiltered = client.get("/api/messages").get_json()["total"]
    body = client.get("/api/messages?timing=implausible").get_json()
    assert body["total"] == unfiltered


def test_message_detail_includes_timing(client, db_path):
    _recompute_timing(db_path)
    with Store(db_path) as store:
        message = store.find_message_by_message_id("<m2@test>")
    body = client.get(f"/api/messages/{message.id}").get_json()
    assert body["timing"] == "normal"


def test_summary_timing_distribution(client, db_path):
    _recompute_timing(db_path)
    body = client.get("/api/summary").get_json()
    assert body["timing_distribution"] == {"normal": 1}


# --- /api/staleness shape ------------------------------------------------------
#
# The dashboard reads the extraction generation, not the app version: the report
# carries a top-level ``extraction_version`` and keys each entry of ``versions``
# by ``extraction_version``. The old ``version`` key is gone, and a client that
# still looked for it would silently render every group as unrecorded.


def test_staleness_report_is_keyed_by_extraction_generation(client):
    body = client.get("/api/staleness").get_json()
    assert body["app_version"] == __version__
    assert body["extraction_version"] == EXTRACTION_VERSION
    # The seeded extractions are written by the running routine.
    assert body["stale"] is False
    assert body["versions"] == [
        {"extraction_version": EXTRACTION_VERSION, "count": body["total"], "stale": False}
    ]


def test_staleness_report_groups_old_and_unrecorded_generations(client, db_path):
    """Every group is identified by its generation stamp, NULL included."""
    with Store(db_path) as store:
        ids = [
            store.extraction_for_message(message_id).id
            for message_id in store.extracted_message_ids()
        ]
        assert len(ids) >= 2
        store.set_extraction_version(ids[0], extraction_version=EXTRACTION_VERSION - 1)
        store.conn.execute(
            "UPDATE extractions SET extraction_version = NULL WHERE id = ?", (ids[1],)
        )
        store.conn.commit()

    body = client.get("/api/staleness").get_json()
    assert body["stale"] is True
    assert body["stale_count"] == 2
    groups = {entry["extraction_version"]: entry for entry in body["versions"]}
    assert groups[None] == {"extraction_version": None, "count": 1, "stale": True}
    assert groups[EXTRACTION_VERSION - 1] == {
        "extraction_version": EXTRACTION_VERSION - 1,
        "count": 1,
        "stale": True,
    }
    assert groups[EXTRACTION_VERSION]["stale"] is False
    # No entry carries the withdrawn key.
    assert all("version" not in entry for entry in body["versions"])


def test_serialize_diff_carries_both_version_stamps():
    """An affected-message row reports the generation and the app version."""
    diff = ExtractionDiff(
        message_id=7,
        extraction_id=9,
        list_name="announce",
        date="2026-01-01T00:00:00+00:00",
        subject="Intro to draft",
        from_address="alice@example.org",
        from_display_name="Alice",
        pipeline_version="1.0.5",
        extraction_version=1,
        old_chars=100,
        new_chars=120,
        old_status="ok",
        new_status="ok",
        text_changed=True,
        scored_text_changed=False,
        scored_text_changed_in_substance=False,
        scored=True,
    )
    row = webapp_api._serialize_diff(diff)
    assert row["pipeline_version"] == "1.0.5"
    assert row["extraction_version"] == 1
    assert row["id"] == 7

    # An extraction written before the stamp existed reports it as null.
    unrecorded = webapp_api._serialize_diff(replace(diff, extraction_version=None))
    assert unrecorded["extraction_version"] is None


def test_staleness_check_rows_report_the_stored_generation(client, db_path):
    """The rows from /staleness/check carry the same two stamps."""
    with Store(db_path) as store:
        extraction_id = store.extraction_for_message(store.extracted_message_ids()[0]).id
        store.replace_extraction(
            extraction_id,
            extracted_text="text the current routine would not produce",
            method="manual",
            status="ok",
            extraction_version=EXTRACTION_VERSION - 1,
            pipeline_version="1.0.5",
        )

    body = client.post("/api/staleness/check").get_json()
    assert body["differing"] >= 1
    row = next(m for m in body["messages"] if m["extraction_version"] is not None)
    assert row["extraction_version"] == EXTRACTION_VERSION - 1
    assert row["pipeline_version"] == "1.0.5"


# --- read-only mode (PUBLIC_READONLY) -----------------------------------------


@pytest.fixture
def readonly_client(db_path):
    """A test client for an instance with PUBLIC_READONLY enabled."""
    app = create_app(replace(_config(db_path), public_readonly=True), frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_readonly_allows_get_data(readonly_client):
    """GET data endpoints keep working under read-only mode."""
    for path in ("/api/messages", "/api/summary", "/api/lists", "/api/senders"):
        resp = readonly_client.get(path)
        assert resp.status_code == 200, path


def test_readonly_rejects_every_write_method(readonly_client):
    """POST/PUT/DELETE are refused with a 403 before reaching a view."""
    cases = [
        ("post", "/api/pull", {"list": "announce", "count": 1}),
        ("post", "/api/extract", {"limit": 1}),
        ("post", "/api/score", {"limit": 1}),
        ("post", "/api/import", None),
        ("put", "/api/settings", {"pangram_model": "default"}),
        ("put", "/api/pangram/notice", {"state": "later"}),
        ("post", "/api/persons", {"canonical_name": "X", "address_ids": []}),
        ("delete", "/api/persons/1", None),
    ]
    for method, path, body in cases:
        resp = getattr(readonly_client, method)(path, json=body)
        assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}"
        assert resp.get_json() == {"error": "this instance is read-only"}


def test_readonly_write_leaves_the_store_untouched(readonly_client, db_path):
    """A refused write changes nothing: the setting keeps its prior value."""
    before = readonly_client.get("/api/settings").get_json()
    resp = readonly_client.put("/api/settings", json={"pangram_model": "default"})
    assert resp.status_code == 403
    after = readonly_client.get("/api/settings").get_json()
    assert before == after


def test_writes_allowed_when_readonly_off(client):
    """The default instance is unchanged: writes still reach their view."""
    resp = client.put("/api/settings", json={"pangram_model": "default"})
    assert resp.status_code == 200


# --- export switches (ALLOW_EXPORT / ALLOW_STATS_EXPORT) ----------------------


def _export_client(db_path, **flags):
    app = create_app(replace(_config(db_path), **flags), frontend_dist=None)
    app.testing = True
    return app.test_client()


def test_exports_served_by_default(client):
    """With both switches at their default, both exports download."""
    assert client.get("/api/export").status_code == 200
    assert client.get("/api/export/stats").status_code == 200


def test_allow_export_off_refuses_full_export_only(db_path):
    """Disabling ALLOW_EXPORT 403s the full export but leaves stats untouched."""
    c = _export_client(db_path, allow_export=False)
    resp = c.get("/api/export")
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "data export is disabled on this instance"}
    assert c.get("/api/export/stats").status_code == 200


def test_allow_stats_export_off_refuses_stats_export_only(db_path):
    """Disabling ALLOW_STATS_EXPORT 403s the stats export but leaves the full export."""
    c = _export_client(db_path, allow_stats_export=False)
    resp = c.get("/api/export/stats")
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "stats export is disabled on this instance"}
    assert c.get("/api/export").status_code == 200


def test_both_export_switches_off(db_path):
    """Both switches off 403 both exports; other GET data is unaffected."""
    c = _export_client(db_path, allow_export=False, allow_stats_export=False)
    assert c.get("/api/export").status_code == 403
    assert c.get("/api/export/stats").status_code == 403
    assert c.get("/api/summary").status_code == 200


# --- capabilities endpoint ----------------------------------------------------


def test_capabilities_defaults(client):
    """The default instance reports full capability."""
    body = client.get("/api/capabilities").get_json()
    assert body == {
        "public_readonly": False,
        "allow_export": True,
        "allow_stats_export": True,
    }


def test_capabilities_reflect_config(db_path):
    """Each flag is echoed from the instance config."""
    c = _export_client(db_path, public_readonly=True, allow_export=False, allow_stats_export=True)
    body = c.get("/api/capabilities").get_json()
    assert body == {
        "public_readonly": True,
        "allow_export": False,
        "allow_stats_export": True,
    }


def test_capabilities_readable_under_readonly(readonly_client):
    """The capabilities GET is not blocked by read-only mode."""
    assert readonly_client.get("/api/capabilities").status_code == 200
