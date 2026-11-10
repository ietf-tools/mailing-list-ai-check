"""Hardening tests for the JSON API's input handling, export gate and error text.

Covers:

- integers that would overflow SQLite's signed 64-bit ``INTEGER`` (query params,
  URL ids, JSON-body id lists) — a 400 or 404, never a 500;
- non-finite floats (``nan``, ``inf``) — a 400;
- length caps on the free-text params (``q``, ``address``, ``label``, ``list``,
  ``addresses``);
- the per-process export gate: one build-and-stream of each kind at a time, a
  JSON 429 with ``Retry-After`` while busy, and the slot released on every path;
- IMAP failures reported to the client without exception text, which is logged;
- import errors with the server's temporary path removed.

Every test runs against a seeded temp database through ``app.test_client()``;
``open_client`` and the fetch helpers are monkeypatched, so no test touches IMAP
or Pangram.
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import threading
from pathlib import Path

import pytest

from mailing_list_ai_check import codec
from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import MAX_PER_PAGE, Store
from mailing_list_ai_check.webapp import api as webapp_api, create_app

from seed import seed

#: Larger than any SQLite INTEGER (and than a C ``long long``).
HUGE = "99999999999999999999999"
INT64_MAX = 2**63 - 1


def _config(db_path: Path) -> Config:
    return Config(
        imap_host="imap.example.org",
        imap_port=993,
        imap_username="anonymous",
        imap_password="anonymous@example.com",
        pangram_api_key="",
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
def app(db_path):
    app = create_app(_config(db_path), frontend_dist=None)
    app.testing = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def _error(resp) -> str:
    body = resp.get_json()
    assert isinstance(body, dict), resp.data[:200]
    return body["error"]


# --- 1. integer range --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        f"/api/messages?page={HUGE}",
        f"/api/messages?person={HUGE}",
        f"/api/messages?person=-{HUGE}",
        f"/api/messages?per_page={HUGE}",
        f"/api/summary?person={HUGE}",
        f"/api/messages/timeline?page={HUGE}",
        f"/api/messages/timeline?person={HUGE}",
        f"/api/senders?page={HUGE}",
        f"/api/senders?per_page={HUGE}",
        f"/api/senders/reply-rugs?person={HUGE}",
        f"/api/senders/reply-rugs?person=1&limit={HUGE}",
        f"/api/senders/timelines?persons=1,{HUGE}",
        f"/api/lists/thread-graph?list=announce&start={HUGE}",
        f"/api/lists/thread-graph?list=announce&end={HUGE}",
        "/api/messages?page=" + "9" * 5000,  # past Python's int-string limit
    ],
)
def test_out_of_range_integer_param_is_a_400(client, url):
    resp = client.get(url)
    assert resp.status_code == 400
    assert _error(resp)


def test_page_is_capped_without_overflowing_the_offset(client):
    """``(page - 1) * per_page`` stays inside SQLite's range at the cap."""
    top = webapp_api._MAX_PAGE
    resp = client.get(f"/api/messages?page={top}&per_page={MAX_PER_PAGE}")
    assert resp.status_code == 200
    assert resp.get_json()["messages"] == []
    assert client.get(f"/api/messages?page={top + 1}").status_code == 400
    assert client.get(f"/api/senders?page={top}").status_code == 200
    assert client.get(f"/api/senders?page={top + 1}").status_code == 400


def test_in_range_integers_still_work(client):
    assert client.get("/api/messages?page=2&per_page=5").get_json()["page"] == 2
    # A very large but representable id simply matches nothing.
    resp = client.get(f"/api/messages?person={INT64_MAX}")
    assert resp.status_code == 200
    assert resp.get_json()["total"] == 0
    # An end rank beyond the list is clamped, as documented.
    resp = client.get(f"/api/lists/thread-graph?list=announce&end={INT64_MAX}")
    assert resp.status_code == 200
    assert client.get(f"/api/senders/reply-rugs?person=1&limit={INT64_MAX}").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        f"/api/messages/{HUGE}",
        f"/api/messages/{INT64_MAX + 1}",
        "/api/messages/" + "9" * 5000,
        "/api/messages/-1",
    ],
)
def test_message_id_that_cannot_exist_is_a_json_404(client, path):
    resp = client.get(path)
    assert resp.status_code == 404
    assert _error(resp)


def test_message_detail_still_routes_valid_ids(client):
    first = client.get("/api/messages?per_page=1").get_json()["messages"][0]
    resp = client.get(f"/api/messages/{first['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == first["id"]
    assert client.get(f"/api/messages/{INT64_MAX}").status_code == 404


@pytest.mark.parametrize("method", ["put", "delete"])
def test_person_id_that_cannot_exist_is_a_404(client, method):
    resp = getattr(client, method)(f"/api/persons/{HUGE}", json={"canonical_name": "x"})
    assert resp.status_code == 404


def test_person_routes_still_work(client):
    created = client.post("/api/persons", json={"canonical_name": "Temp"})
    assert created.status_code == 201
    pid = created.get_json()["id"]
    assert client.put(f"/api/persons/{pid}", json={"canonical_name": "T2"}).status_code == 200
    assert client.delete(f"/api/persons/{pid}").status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/persons", {"canonical_name": "x", "address_ids": [2**70]}),
        ("post", "/api/persons", {"canonical_name": "x", "address_ids": [-(2**64)]}),
        ("put", "/api/persons/1", {"add_address_ids": [2**63]}),
        ("put", "/api/persons/1", {"remove_address_ids": [2**63]}),
        ("post", "/api/staleness/reextract", {"ids": [1, 2**64]}),
        ("post", "/api/staleness/rescore", {"ids": [2**64]}),
    ],
)
def test_out_of_range_json_ids_are_a_400(client, method, path, body):
    resp = getattr(client, method)(path, json=body)
    assert resp.status_code == 400
    assert "out-of-range" in _error(resp)


def test_in_range_json_address_ids_still_resolve(client):
    resp = client.post("/api/persons", json={"canonical_name": "x", "address_ids": [INT64_MAX]})
    assert resp.status_code == 404  # representable, just absent


# --- 2. non-finite floats ----------------------------------------------------


@pytest.mark.parametrize("param", ["cpm_min", "cpm_max", "min_likelihood", "max_likelihood"])
@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity", "1e999"])
def test_non_finite_float_is_a_400(client, param, value):
    resp = client.get(f"/api/messages?{param}={value}")
    assert resp.status_code == 400
    assert "finite" in _error(resp)


def test_finite_floats_still_filter(client):
    assert client.get("/api/messages?cpm_min=1.5&cpm_max=1e6").status_code == 200
    assert client.get("/api/summary?min_likelihood=0&max_likelihood=1").status_code == 200


# --- 3. free-text length caps ------------------------------------------------


@pytest.mark.parametrize(
    ("template", "limit"),
    [
        ("/api/messages?q={}", webapp_api._MAX_Q_LEN),
        ("/api/summary?q={}", webapp_api._MAX_Q_LEN),
        ("/api/messages/timeline?q={}", webapp_api._MAX_Q_LEN),
        ("/api/addresses?q={}", webapp_api._MAX_Q_LEN),
        ("/api/senders?q={}", webapp_api._MAX_Q_LEN),
        ("/api/messages?address={}", webapp_api._MAX_EMAIL_LEN),
        ("/api/senders/reply-rugs?address={}", webapp_api._MAX_EMAIL_LEN),
        ("/api/senders/timelines?addresses=a@x.org,{}", webapp_api._MAX_EMAIL_LEN),
        ("/api/messages?label={}", webapp_api._MAX_LABEL_LEN),
        ("/api/messages?list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/senders?list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/senders/timelines?persons=1&list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/lists/timelines?list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/lists/thread-graph?list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/export?list={}", webapp_api._MAX_LIST_NAME_LEN),
        ("/api/export/stats?list={}", webapp_api._MAX_LIST_NAME_LEN),
    ],
)
def test_free_text_over_the_cap_is_a_400_and_at_the_cap_is_not(client, template, limit):
    over = client.get(template.format("a" * (limit + 1)))
    assert over.status_code == 400
    assert f"at most {limit} characters" in _error(over)
    at = client.get(template.format("a" * limit))
    assert at.status_code != 400  # 200, or a 404 for an unknown list/export


def test_search_and_filters_still_match(client):
    assert client.get("/api/messages?q=a").get_json()["total"] > 0
    assert client.get("/api/messages?list=announce").get_json()["total"] > 0
    assert client.get("/api/senders?q=alice").get_json()["total"] >= 1


# --- 4. export gate ----------------------------------------------------------


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    """An empty directory that :func:`tempfile.mkstemp` writes into."""
    path = tmp_path / "tempfiles"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _gate(app, kind="full"):
    with app.app_context():
        return webapp_api._export_gate(kind)


EXPORT_URLS = [("/api/export", "full"), ("/api/export/stats", "stats")]


@pytest.mark.parametrize(("url", "kind"), EXPORT_URLS)
def test_second_export_while_one_streams_is_a_429(app, client, temp_dir, url, kind):
    first = client.get(url)
    assert first.status_code == 200
    assert _gate(app, kind).busy  # held until the response is closed

    busy = client.get(url)
    assert busy.status_code == 429
    assert busy.headers["Retry-After"] == str(webapp_api._EXPORT_RETRY_AFTER_SECONDS)
    assert "in progress" in _error(busy)
    assert os.listdir(temp_dir) == []  # the refused request built nothing

    first.close()
    assert not _gate(app, kind).busy
    again = client.get(url)
    assert again.status_code == 200
    again.close()


def test_the_two_export_kinds_are_gated_separately(app, client):
    full = client.get("/api/export")
    stats = client.get("/api/export/stats")
    assert (full.status_code, stats.status_code) == (200, 200)
    full.close()
    stats.close()


def test_concurrent_builds_are_refused_while_one_is_building(app, client, monkeypatch):
    """Two threads: the second request arrives while the first is mid-build."""
    started, proceed = threading.Event(), threading.Event()
    real_export_lists = webapp_api.export_lists

    def slow_export_lists(*args, **kwargs):
        started.set()
        assert proceed.wait(10)
        return real_export_lists(*args, **kwargs)

    monkeypatch.setattr(webapp_api, "export_lists", slow_export_lists)
    result = {}

    def first_request():
        resp = client.get("/api/export")
        result["status"] = resp.status_code
        result["magic"] = resp.data[:4]

    thread = threading.Thread(target=first_request)
    thread.start()
    try:
        assert started.wait(10)
        assert client.get("/api/export").status_code == 429
    finally:
        proceed.set()
        thread.join(10)
    assert result == {"status": 200, "magic": codec.ZSTD_MAGIC}
    assert not _gate(app).busy


@pytest.mark.parametrize(("url", "kind"), EXPORT_URLS)
def test_gate_released_after_the_body_is_read(app, client, url, kind):
    assert client.get(url).data  # exhausting the stream runs its finally
    assert not _gate(app, kind).busy


@pytest.mark.parametrize(("url", "kind"), EXPORT_URLS)
def test_gate_released_on_head_request(app, client, url, kind):
    resp = client.head(url)
    assert resp.status_code == 200
    assert _gate(app, kind).busy
    resp.close()  # the body never starts; only call_on_close runs
    assert not _gate(app, kind).busy


def test_gate_released_when_the_client_disconnects(app, client, monkeypatch):
    monkeypatch.setattr(webapp_api, "_EXPORT_CHUNK_BYTES", 16)
    resp = client.get("/api/export")
    next(iter(resp.response))  # mid-stream
    assert _gate(app).busy
    resp.close()
    assert not _gate(app).busy


def test_gate_released_when_the_body_is_buffered(app):
    with app.test_request_context("/api/export"):
        resp = webapp_api.export()
        assert resp.get_data()[:4] == codec.ZSTD_MAGIC
        assert not webapp_api._export_gate("full").busy


@pytest.mark.parametrize(
    ("url", "builder", "kind"),
    [("/api/export", "export_lists", "full"), ("/api/export/stats", "export_stats", "stats")],
)
def test_gate_released_when_the_build_fails(app, client, temp_dir, monkeypatch, url, builder, kind):
    def boom(*args, **kwargs):
        raise RuntimeError("export exploded")

    monkeypatch.setattr(webapp_api, builder, boom)
    with pytest.raises(RuntimeError):
        client.get(url)
    assert not _gate(app, kind).busy
    assert os.listdir(temp_dir) == []


def test_gate_released_when_an_internal_value_error_escapes(app, client, monkeypatch):
    """Only the unknown-list ValueError becomes a 404; others stay internal (500)."""

    def boom(*args, **kwargs):
        raise ValueError("internal detail /var/secret")

    monkeypatch.setattr(webapp_api, "export_lists", boom)
    with pytest.raises(ValueError):
        client.get("/api/export")
    assert not _gate(app).busy


def test_gate_released_when_a_list_vanishes_mid_request(app, client, monkeypatch):
    def vanished(*args, **kwargs):
        raise ValueError("unknown list name: 'announce'")

    monkeypatch.setattr(webapp_api, "export_lists", vanished)
    resp = client.get("/api/export?list=announce")
    assert resp.status_code == 404
    assert _error(resp) == "unknown list name: 'announce'"
    assert not _gate(app).busy


@pytest.mark.parametrize(("url", "kind"), EXPORT_URLS)
def test_gate_released_when_there_is_nothing_to_export(app, client, temp_dir, url, kind):
    resp = client.get(f"{url}?date_from=1900-01-01&date_to=1900-01-02")
    assert resp.status_code == 404
    assert _error(resp) == "nothing to export"
    assert not _gate(app, kind).busy
    assert os.listdir(temp_dir) == []


@pytest.mark.parametrize(("url", "kind"), EXPORT_URLS)
def test_unknown_list_is_a_404_without_taking_the_slot(app, client, temp_dir, url, kind):
    held = client.get(url)  # occupy the slot: an unknown list must still be a 404
    resp = client.get(f"{url}?list=does-not-exist")
    assert resp.status_code == 404
    assert _error(resp) == "unknown list name: 'does-not-exist'"
    held.close()
    assert not _gate(app, kind).busy


def test_gate_released_when_the_finished_file_cannot_be_opened(app, client, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("cannot open")

    monkeypatch.setattr(webapp_api, "open", refuse, raising=False)
    with pytest.raises(OSError):
        client.get("/api/export")
    assert not _gate(app).busy


def test_validation_errors_and_disabled_exports_never_touch_the_gate(app, client, db_path):
    assert client.get("/api/export?date_from=not-a-date").status_code == 400
    assert not _gate(app).busy
    disabled = create_app(_config(db_path), frontend_dist=None)
    disabled.config["ALLOW_EXPORT"] = False
    assert disabled.test_client().get("/api/export").status_code == 403


def test_gate_release_is_idempotent_and_bound_to_its_acquisition():
    gate = webapp_api._ExportGate(stale_after=3600)
    release = gate.try_acquire()
    assert release is not None and gate.busy
    assert gate.try_acquire() is None
    release()
    release()  # a second call is harmless
    assert not gate.busy

    second = gate.try_acquire()
    release()  # the first holder's late release must not free the second's slot
    assert gate.busy
    second()
    assert not gate.busy


def test_a_stale_slot_is_taken_over(monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(webapp_api.time, "monotonic", lambda: clock[0])
    gate = webapp_api._ExportGate(stale_after=60)
    leaked = gate.try_acquire()
    assert leaked is not None
    clock[0] += 59
    assert gate.try_acquire() is None
    clock[0] += 2
    with caplog.at_level(logging.WARNING, logger=webapp_api.__name__):
        fresh = gate.try_acquire()
    assert fresh is not None
    assert "presuming it leaked" in caplog.text
    leaked()  # the leaked holder releasing late does not free the new holder
    assert gate.busy
    fresh()
    assert not gate.busy


# --- 5. error text -----------------------------------------------------------

#: Text a real socket/IMAP error could carry: host, port, a login name.
SENSITIVE = "imap.example.org:993 NO [AUTHENTICATIONFAILED] user secret-login refused"


class _FakeClient:
    def close(self) -> None:
        pass

    def logout(self) -> None:
        pass

    def examine(self, folder):
        raise RuntimeError(SENSITIVE)


def _boom(*args, **kwargs):
    raise RuntimeError(SENSITIVE)


def _range_db(tmp_path):
    db = tmp_path / "range.db"
    with Store(db) as store:
        lst = store.upsert_list("announce", "Shared Folders/announce")
        addr = store.upsert_address("stored@example.org", "Stored")
        store.upsert_message(
            message_id="<stored5@x>",
            list_id=lst.id,
            address_id=addr.id,
            subject="stored 5",
            date="2025-01-01T00:00:00+00:00",
            in_reply_to=None,
            raw_body="stored body",
            uid=5,
        )
    return db


def _assert_generic(resp, caplog, prefix):
    assert resp.status_code == 502
    error = _error(resp)
    assert error.startswith(prefix)
    for leaked in ("imap.example.org", "993", "secret-login", "AUTHENTICATIONFAILED"):
        assert leaked not in error
    assert SENSITIVE in caplog.text  # the detail is logged server-side
    assert "anonymous@example.com" not in caplog.text  # never the password


CONNECT_CASES = [
    ("/api/pull", {"list": "newlist", "count": 5}),
    ("/api/pull/fetch", {"list": "newlist", "count": 5}),
    ("/api/lists/regenerate", None),
    ("/api/lists/preview", {"list": "announce", "mode": "new"}),
    ("/api/pull/range", {"list": "announce", "mode": "new"}),
    ("/api/pull/range/fetch", {"list": "announce", "mode": "new"}),
]


@pytest.mark.parametrize(("path", "body"), CONNECT_CASES)
def test_imap_connect_failure_hides_the_exception_text(tmp_path, monkeypatch, caplog, path, body):
    monkeypatch.setattr(webapp_api, "open_client", _boom)
    app = create_app(_config(_range_db(tmp_path)), frontend_dist=None)
    app.testing = True
    with caplog.at_level(logging.WARNING, logger=webapp_api.__name__):
        resp = app.test_client().post(path, json=body)
    _assert_generic(resp, caplog, "could not connect to the IMAP server")


@pytest.mark.parametrize(
    ("path", "body", "prefix"),
    [
        ("/api/pull", {"list": "newlist", "count": 5}, "IMAP fetch failed for list 'newlist'"),
        ("/api/lists/regenerate", None, "IMAP list enumeration failed"),
        (
            "/api/lists/preview",
            {"list": "announce", "mode": "new"},
            "IMAP preview failed for list 'announce'",
        ),
        (
            "/api/pull/range",
            {"list": "announce", "mode": "new"},
            "IMAP ranged pull failed for list 'announce'",
        ),
        (
            "/api/pull/range/fetch",
            {"list": "announce", "mode": "before", "count": 3},
            "IMAP ranged pull failed for list 'announce'",
        ),
    ],
)
def test_imap_operation_failure_hides_the_exception_text(
    tmp_path, monkeypatch, caplog, path, body, prefix
):
    monkeypatch.setattr(webapp_api, "open_client", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(webapp_api, "resolve_folders", _boom)
    monkeypatch.setattr(webapp_api, "refresh_lists_index", _boom)
    app = create_app(_config(_range_db(tmp_path)), frontend_dist=None)
    app.testing = True
    with caplog.at_level(logging.WARNING, logger=webapp_api.__name__):
        resp = app.test_client().post(path, json=body)
    _assert_generic(resp, caplog, prefix)


def _empty_client(tmp_path):
    path = tmp_path / "empty.db"
    with Store(path):
        pass
    app = create_app(_config(path), frontend_dist=None)
    app.testing = True
    return app.test_client()


def _upload(client, payload: bytes):
    return client.post(
        "/api/import",
        data={"file": (io.BytesIO(payload), "upload.jsonl.zst")},
        content_type="multipart/form-data",
    )


def test_import_error_omits_the_temporary_path(tmp_path, temp_dir):
    resp = _upload(_empty_client(tmp_path), codec.ZSTD_MAGIC + b"\x00garbage" * 8)
    assert resp.status_code == 400
    error = _error(resp)
    assert error.startswith("cannot read the uploaded file")
    assert str(temp_dir) not in error
    assert os.path.realpath(temp_dir) not in error


def test_import_error_keeps_the_line_number_and_reason(tmp_path):
    resp = _upload(_empty_client(tmp_path), b"{not json}\n")
    assert resp.status_code == 400
    assert _error(resp).startswith("line 1: invalid JSON")


def test_import_non_utf8_reason_is_kept(tmp_path):
    resp = _upload(_empty_client(tmp_path), b"\xff\xfe\x00binary")
    assert resp.status_code == 400
    error = _error(resp)
    assert "not valid UTF-8" in error
    assert tempfile.gettempdir() not in error


def test_without_path_replaces_every_spelling():
    path = "relative/tmpabc.jsonl"
    msg = f"cannot read {os.path.realpath(path)}: corrupt stream: {path}: bad"
    assert webapp_api._without_path(msg, path) == (
        "cannot read the uploaded file: corrupt stream: the uploaded file: bad"
    )


def test_export_404_message_echoes_only_the_requested_name(client):
    resp = client.get("/api/export?list=no-such-list")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "unknown list name: 'no-such-list'"}
