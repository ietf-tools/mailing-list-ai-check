"""Request-level hardening of the Flask app.

Covers the Host allow-list (DNS rebinding), the cross-origin write refusal, the
request-body limits, the security response headers, the SPA fallback's handling
of paths that leave ``frontend/dist``, and ``mail-ai-web --debug`` on a
non-loopback host. No test reaches IMAP or Pangram: every write probed here is
either refused before its view runs or is an endpoint that needs neither.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from mailing_list_ai_check import webapp
from mailing_list_ai_check.config import Config
from mailing_list_ai_check.store import Store
from mailing_list_ai_check.webapp import (
    CONTENT_SECURITY_POLICY,
    DEV_CORS_ORIGIN,
    SMALL_BODY_LIMIT,
    create_app,
)

from seed import seed


def _config(db_path: Path, **overrides) -> Config:
    base = Config(
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
    return replace(base, **overrides)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "web.db"
    with Store(path) as store:
        seed(store)
    return path


def _client(db_path, *, frontend_dist=None, **overrides):
    app = create_app(_config(db_path, **overrides), frontend_dist=frontend_dist)
    app.testing = True
    return app.test_client()


@pytest.fixture
def client(db_path):
    return _client(db_path)


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=app></div>")
    (root / "assets" / "app.js").write_text("console.log('app')")
    return root


def _person_post(client, **headers):
    return client.post("/api/persons", json={"canonical_name": "Security Test"}, headers=headers)


# --- Host allow-list ----------------------------------------------------------


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8050", "127.0.0.1:8050", "[::1]:8050", "LOCALHOST"]
)
def test_loopback_host_accepted_by_default(client, host):
    resp = client.get("/api/capabilities", headers={"Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8050", "127.0.0.1.evil.example"])
def test_foreign_host_refused_on_loopback_bind(client, host):
    resp = client.get("/api/capabilities", headers={"Host": host})
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "host not allowed"}


def test_foreign_host_refused_for_writes_too(client):
    resp = client.post("/api/lists/regenerate", headers={"Host": "evil.example"})
    assert resp.status_code == 400


def test_non_loopback_bind_without_allowed_hosts_has_no_host_check(db_path):
    c = _client(db_path, flask_host="0.0.0.0")
    assert c.get("/api/capabilities", headers={"Host": "mlac.example.org"}).status_code == 200
    # Probes and the Dockerfile HEALTHCHECK dial 127.0.0.1.
    assert c.get("/api/capabilities", headers={"Host": "127.0.0.1:8050"}).status_code == 200


def test_allowed_hosts_list_is_enforced_and_ignores_ports(db_path):
    c = _client(db_path, flask_host="0.0.0.0", allowed_hosts=("mlac.example.org", "127.0.0.1"))
    assert c.get("/api/capabilities", headers={"Host": "mlac.example.org"}).status_code == 200
    assert c.get("/api/capabilities", headers={"Host": "MLAC.example.org:443"}).status_code == 200
    assert c.get("/api/capabilities", headers={"Host": "127.0.0.1:8050"}).status_code == 200
    assert c.get("/api/capabilities", headers={"Host": "localhost"}).status_code == 400
    assert c.get("/api/capabilities", headers={"Host": "evil.example"}).status_code == 400


def test_allowed_hosts_entries_with_ports_and_brackets_are_normalised(db_path):
    c = _client(db_path, allowed_hosts=("mlac.example.org:8443", "[::1]"))
    assert c.get("/", headers={"Host": "mlac.example.org"}).status_code == 200
    assert c.get("/", headers={"Host": "[::1]:8050"}).status_code == 200


def test_allowed_hosts_star_disables_the_check(db_path):
    c = _client(db_path, allowed_hosts=("*",))
    assert c.get("/api/capabilities", headers={"Host": "anything.example"}).status_code == 200


def test_host_check_applies_to_the_spa(db_path, dist):
    c = _client(db_path, frontend_dist=dist)
    assert c.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert c.get("/", headers={"Host": "localhost:8050"}).status_code == 200


# --- cross-origin write refusal -------------------------------------------------


def test_cross_origin_post_refused(client):
    resp = _person_post(client, Origin="https://evil.example")
    assert resp.status_code == 403
    assert resp.get_json() == {"error": "cross-origin request refused"}


def test_cross_origin_multipart_import_refused(client):
    resp = client.post(
        "/api/import",
        data={"file": (io.BytesIO(b"{}\n"), "x.jsonl")},
        content_type="multipart/form-data",
        headers={"Origin": "https://evil.example"},
    )
    assert resp.status_code == 403


def test_cross_origin_bodyless_regenerate_refused(client):
    resp = client.post("/api/lists/regenerate", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_cross_origin_text_plain_post_refused(client):
    resp = client.post(
        "/api/extract",
        data='{"limit": 10}',
        content_type="text/plain",
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("origin", ["null", "file://", "not a url", "http://[bad"])
def test_opaque_or_malformed_origin_refused(client, origin):
    assert _person_post(client, Origin=origin).status_code == 403


def test_sec_fetch_site_cross_site_refused_without_origin(client):
    assert _person_post(client, **{"Sec-Fetch-Site": "cross-site"}).status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
def test_sec_fetch_site_other_values_pass(client, site):
    assert _person_post(client, **{"Sec-Fetch-Site": site}).status_code == 201


def test_same_origin_post_allowed(client):
    resp = _person_post(
        client,
        Origin="http://localhost:8050",
        Host="localhost:8050",
        **{"Sec-Fetch-Site": "same-origin"},
    )
    assert resp.status_code == 201


def test_same_origin_behind_tls_ingress_allowed(db_path):
    # TLS terminated upstream: the app sees http, the browser sends https.
    c = _client(db_path, flask_host="0.0.0.0", allowed_hosts=("mlac.example.org",))
    resp = _person_post(c, Origin="https://mlac.example.org", Host="mlac.example.org")
    assert resp.status_code == 201


def test_origin_port_mismatch_refused(db_path):
    c = _client(db_path, flask_host="0.0.0.0")
    resp = _person_post(c, Origin="http://mlac.example.org:9999", Host="mlac.example.org:8050")
    assert resp.status_code == 403


def test_no_origin_post_allowed(client):
    assert _person_post(client).status_code == 201


def test_vite_proxy_origin_allowed(db_path, dist):
    # Vite proxies /api with changeOrigin: Host is the Flask address, Origin is the
    # browser's (any dev-server port), with or without a built frontend.
    for frontend_dist in (None, dist):
        c = _client(db_path, frontend_dist=frontend_dist)
        resp = _person_post(c, Origin="http://localhost:5174", Host="127.0.0.1:8050")
        assert resp.status_code == 201


def test_dev_cors_origin_allowed_in_dev_mode(db_path):
    c = _client(db_path, flask_host="0.0.0.0")
    resp = _person_post(c, Origin=DEV_CORS_ORIGIN, Host="devbox.example:8050")
    assert resp.status_code == 201


def test_loopback_origin_not_allowed_against_a_public_host(db_path, dist):
    c = _client(db_path, frontend_dist=dist, flask_host="0.0.0.0")
    resp = _person_post(c, Origin="http://localhost:5173", Host="mlac.example.org")
    assert resp.status_code == 403


def test_cross_origin_get_is_not_refused(client):
    resp = client.get("/api/lists", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200


def test_readonly_mode_still_refuses_writes(db_path):
    c = _client(db_path, public_readonly=True)
    assert _person_post(c).status_code == 403
    assert _person_post(c, Origin="http://localhost:8050", Host="localhost:8050").status_code == 403
    assert c.post("/api/lists/regenerate").get_json() == {"error": "this instance is read-only"}
    assert c.get("/api/lists").status_code == 200


# --- body limits --------------------------------------------------------------


def test_max_content_length_follows_config(db_path):
    app = create_app(_config(db_path, max_upload_mb=7), frontend_dist=None)
    assert app.config["MAX_CONTENT_LENGTH"] == 7 * 1024 * 1024
    assert app.config["MAX_FORM_MEMORY_SIZE"] is not None
    assert app.config["MAX_FORM_PARTS"] is not None


def test_oversized_json_body_refused(client):
    body = json.dumps({"limit": 10, "pad": "x" * (SMALL_BODY_LIMIT + 1)})
    resp = client.post("/api/extract", data=body, content_type="application/json")
    assert resp.status_code == 413
    assert resp.get_json() == {"error": "request body too large"}


def test_oversized_body_refused_on_bodyless_endpoint(client):
    resp = client.post(
        "/api/lists/regenerate", data=b"x" * (SMALL_BODY_LIMIT + 1), content_type="text/plain"
    )
    assert resp.status_code == 413


def test_chunked_body_refused_on_small_body_paths(client):
    resp = client.post(
        "/api/persons",
        input_stream=io.BytesIO(b'{"name": "x"}'),
        content_type="application/json",
        headers={"Transfer-Encoding": "chunked"},
    )
    assert resp.status_code == 413


def test_small_body_still_accepted(client):
    assert _person_post(client).status_code == 201


def test_import_upload_limit_is_max_upload_mb(db_path):
    c = _client(db_path, max_upload_mb=1)
    over = io.BytesIO(b"x" * (1024 * 1024 + 10))
    resp = c.post(
        "/api/import",
        data={"file": (over, "x.jsonl")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 413
    assert resp.get_json() == {"error": "request body too large"}


def test_import_accepts_bodies_above_the_small_limit(db_path, tmp_path):
    # Larger than SMALL_BODY_LIMIT, within max_upload_mb: reaches the importer,
    # which rejects the junk content with a 400 rather than a 413.
    c = _client(db_path, max_upload_mb=4)
    blob = io.BytesIO(b"not an export\n" * (SMALL_BODY_LIMIT // 10))
    resp = c.post(
        "/api/import?dry_run=1",
        data={"file": (blob, "x.jsonl")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400


# --- security headers ----------------------------------------------------------


_COMMON_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


def _assert_security_headers(resp, *, corp: bool):
    for name, value in _COMMON_HEADERS.items():
        assert resp.headers.get(name) == value, name
    if corp:
        assert resp.headers.get("Cross-Origin-Resource-Policy") == "same-origin"
    else:
        assert "Cross-Origin-Resource-Policy" not in resp.headers


def test_csp_has_the_required_directives():
    for directive in (
        "default-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ):
        assert directive in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY


def test_security_headers_on_production_responses(db_path, dist):
    c = _client(db_path, frontend_dist=dist)
    for path in ("/", "/assets/app.js", "/some/client/route", "/api/lists", "/api/nope"):
        _assert_security_headers(c.get(path), corp=True)
    _assert_security_headers(c.get("/", headers={"Host": "evil.example"}), corp=True)


def test_security_headers_compose_with_dev_cors(client):
    resp = client.get("/api/lists")
    _assert_security_headers(resp, corp=False)
    assert resp.headers["Access-Control-Allow-Origin"] == DEV_CORS_ORIGIN
    _assert_security_headers(client.get("/"), corp=False)


def test_security_headers_on_refusals(db_path):
    c = _client(db_path, public_readonly=True)
    _assert_security_headers(_person_post(c), corp=False)
    resp = c.post("/api/extract", data=b"x" * (SMALL_BODY_LIMIT + 1), content_type="text/plain")
    assert resp.status_code == 403  # read-only is checked before size
    _assert_security_headers(resp, corp=False)


# --- SPA fallback path handling -------------------------------------------------


def test_traversal_answers_like_a_missing_file(db_path, dist, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("outside dist")
    c = _client(db_path, frontend_dist=dist)
    index = c.get("/").data
    existing = c.get("/..%2fsecret.txt")
    missing = c.get("/..%2fno-such-file.txt")
    assert existing.status_code == missing.status_code == 200
    assert existing.data == missing.data == index


def test_etc_passwd_oracle_closed(db_path, dist):
    c = _client(db_path, frontend_dist=dist)
    a = c.get("/..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2fetc/passwd")
    b = c.get("/..%2f..%2f..%2f..%2f..%2f..%2f..%2f..%2fetc/no-such-file-here")
    assert a.status_code == b.status_code == 200
    assert a.data == b.data == c.get("/").data


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlink_out_of_dist_is_not_followed(db_path, dist, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("outside dist")
    (dist / "link.txt").symlink_to(outside)
    c = _client(db_path, frontend_dist=dist)
    resp = c.get("/link.txt")
    assert resp.status_code == 200
    assert resp.data == c.get("/").data


def test_real_static_file_still_served(db_path, dist):
    c = _client(db_path, frontend_dist=dist)
    resp = c.get("/assets/app.js")
    assert resp.status_code == 200
    assert resp.data == b"console.log('app')"


# --- mail-ai-web --debug ------------------------------------------------------


def _patch_main(monkeypatch, db_path, **overrides):
    runs = []
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: _config(db_path, **overrides)))
    monkeypatch.setattr(
        webapp.Flask, "run", lambda self, **kwargs: runs.append((self, kwargs)), raising=True
    )
    return runs


@pytest.mark.parametrize(
    "argv,flask_host",
    [
        (["--debug", "--host", "0.0.0.0"], "127.0.0.1"),
        (["--debug"], "0.0.0.0"),
        (["--debug", "--host", "192.168.1.5"], "127.0.0.1"),
        (["--debug", "--host", "::"], "127.0.0.1"),
    ],
)
def test_debug_refused_on_non_loopback_host(monkeypatch, db_path, capsys, argv, flask_host):
    runs = _patch_main(monkeypatch, db_path, flask_host=flask_host)
    assert webapp.main(argv) == 2
    assert runs == []
    assert "refusing --debug" in capsys.readouterr().err


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2"])
def test_debug_allowed_on_loopback(monkeypatch, db_path, host):
    runs = _patch_main(monkeypatch, db_path)
    assert webapp.main(["--debug", "--host", host]) == 0
    assert len(runs) == 1
    assert runs[0][1]["debug"] is True


def test_non_debug_run_on_any_host_and_host_check_follows_bind(monkeypatch, db_path):
    runs = _patch_main(monkeypatch, db_path)
    assert webapp.main(["--host", "0.0.0.0"]) == 0
    app, kwargs = runs[0]
    assert kwargs["debug"] is False
    assert app.config["ALLOWED_HOSTS"] == ["*"]


# --- configuration --------------------------------------------------------------


def test_config_defaults_for_the_new_keys(monkeypatch):
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("MAX_UPLOAD_MB", raising=False)
    cfg = Config.load()
    assert cfg.allowed_hosts == ()
    assert cfg.max_upload_mb == 1024


def test_config_empty_values_mean_default(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "")
    monkeypatch.setenv("MAX_UPLOAD_MB", "")
    cfg = Config.load()
    assert cfg.allowed_hosts == ()
    assert cfg.max_upload_mb == 1024


def test_config_parses_allowed_hosts_and_upload_limit(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", " mlac.example.org, ,127.0.0.1 ")
    monkeypatch.setenv("MAX_UPLOAD_MB", "300")
    cfg = Config.load()
    assert cfg.allowed_hosts == ("mlac.example.org", "127.0.0.1")
    assert cfg.max_upload_mb == 300


@pytest.mark.parametrize("raw", ["0", "-5", "lots", "1.5"])
def test_config_rejects_a_bad_upload_limit(monkeypatch, raw):
    monkeypatch.setenv("MAX_UPLOAD_MB", raw)
    with pytest.raises(RuntimeError, match="MAX_UPLOAD_MB"):
        Config.load()
