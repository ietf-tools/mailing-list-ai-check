"""Flask app for the dashboard: a JSON API plus (in production) the built Vue SPA.

The API lives in :mod:`mailing_list_ai_check.webapp.api`. This module wires it into
an application via :func:`create_app` and exposes the ``mail-ai-web`` dev-server
entry point (:func:`main`).

Frontend serving
----------------
If ``frontend/dist`` exists (``npm run build`` has run) the app serves it as a
single-page app: static files are returned directly and every other non-``/api``
path falls back to ``index.html`` so client-side routing works. If it does not
exist (the common state during development), the app is in **dev mode**: ``/``
returns a small JSON notice and CORS headers are emitted for the Vite dev server
origin (``http://localhost:5173``) so the separately served frontend can call the
API.

Connection handling
--------------------
One ``sqlite3``-backed :class:`~mailing_list_ai_check.store.Store` is opened per
request (lazily, in :func:`~mailing_list_ai_check.webapp.api.get_store`) and closed
on app-context teardown. Per-request connections keep each connection on a single
thread, so the stdlib default ``check_same_thread=True`` holds under the threaded
dev server without sharing a connection across threads.

Request hardening
-----------------
The app has no authentication, so every request passes these guards, in order,
before it reaches a view:

1. **Host allow-list** (:func:`_register_host_check`) — a ``Host`` header outside
   ``Config.allowed_hosts`` gets a 400. Defeats DNS rebinding against a local
   instance.
2. **Read-only mode** (:func:`_register_readonly`) — with ``PUBLIC_READONLY`` every
   non-GET/HEAD/OPTIONS request gets a 403.
3. **Cross-origin writes** (:func:`_register_origin_check`) — a state-changing
   request whose ``Origin`` is another site, or whose ``Sec-Fetch-Site`` is
   ``cross-site``, gets a 403. Requests with neither header (curl, scripts) pass.
4. **Body size** (:func:`_register_body_limits`) — 1 MiB for every path except
   ``/api/import``, which takes up to ``Config.max_upload_mb``; larger bodies get
   a 413.

Every response also carries a fixed set of security headers
(:func:`_register_security_headers`), including a Content-Security-Policy that
the built dashboard satisfies.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.security import safe_join

from ..config import Config
from .api import ApiError, api_bp

log = logging.getLogger("mailing_list_ai_check.web")

#: The Vite dev server origin allowed via CORS when no built frontend is present.
DEV_CORS_ORIGIN = "http://localhost:5173"

#: Hostnames that name the local machine. A loopback-bound instance answers to
#: these alone unless ``ALLOWED_HOSTS`` says otherwise.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: The one path whose body may exceed :data:`SMALL_BODY_LIMIT`.
UPLOAD_PATH = "/api/import"

#: Largest body accepted by every path other than :data:`UPLOAD_PATH`. The
#: largest legitimate JSON body (1000 message ids) is a few KiB.
SMALL_BODY_LIMIT = 1024 * 1024

#: Largest non-file multipart/urlencoded field (the import form's ``dry_run``).
#: Werkzeug's multipart decoder also applies this to its read buffer, which takes
#: 64 KiB reads of the file part, so it must stay well above that; this is
#: Werkzeug's own default, set explicitly so a framework change cannot lift it.
MAX_FORM_MEMORY_SIZE = 500_000

#: Most multipart/urlencoded parts parsed from one request.
MAX_FORM_PARTS = 100

#: Content-Security-Policy for every response. The built SPA (``frontend/dist``)
#: loads one module script and one stylesheet from ``/assets``, images and icons
#: from its own origin, and calls only ``/api``; it has no inline script, inline
#: ``style`` attribute, ``data:`` URI or external resource. Vue applies bound
#: styles through the CSSOM, which ``style-src`` does not restrict.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)

#: Sentinel: auto-detect ``frontend/dist`` from the repo root.
_AUTODETECT = object()


def _repo_root() -> Path:
    """The repo root, relative to this file (…/src/pkg/webapp)."""
    return Path(__file__).resolve().parents[3]


def _default_frontend_dist() -> Path:
    """Locate ``frontend/dist`` relative to the repo root (…/src/pkg/webapp)."""
    return _repo_root() / "frontend" / "dist"


def create_app(
    config: Config | None = None,
    *,
    frontend_dist: Any = _AUTODETECT,
    docs_root: Any = _AUTODETECT,
) -> Flask:
    """Build the Flask application.

    ``config`` defaults to :meth:`Config.load`. ``frontend_dist`` defaults to
    auto-detecting ``frontend/dist``; pass an explicit path (or ``None``) to force
    production or dev behaviour — used by tests. ``docs_root`` is the directory the
    ``/api/docs`` endpoints read the documentation set from (default: the repo
    root).
    """
    if config is None:
        config = Config.load()

    if frontend_dist is _AUTODETECT:
        dist: Path | None = _default_frontend_dist()
    elif frontend_dist is None:
        dist = None
    else:
        dist = Path(frontend_dist)

    dev_mode = dist is None or not dist.exists()

    app = Flask(__name__, static_folder=None)
    app.config["STORE_PATH"] = config.database_path
    # The full Config is needed by the /api/pull endpoint (IMAP credentials and
    # the optional Pangram key). Stashed here so tests can inject a config with,
    # e.g., an empty pangram_api_key without touching the environment.
    app.config["APP_CONFIG"] = config
    app.config["FRONTEND_DIST"] = str(dist) if dist is not None else None
    app.config["DEV_MODE"] = dev_mode
    app.config["DOCS_ROOT"] = str(_repo_root() if docs_root is _AUTODETECT else docs_root)
    app.config["PUBLIC_READONLY"] = config.public_readonly
    app.config["ALLOW_EXPORT"] = config.allow_export
    app.config["ALLOW_STATS_EXPORT"] = config.allow_stats_export
    # The app-wide ceiling, which is the import upload's limit; every other path
    # is lowered to SMALL_BODY_LIMIT per request in _register_body_limits.
    app.config["MAX_CONTENT_LENGTH"] = config.max_upload_mb * 1024 * 1024
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_FORM_MEMORY_SIZE
    app.config["MAX_FORM_PARTS"] = MAX_FORM_PARTS

    _register_teardown(app)
    # before_request hooks run in registration order.
    _register_host_check(app, config)
    if config.public_readonly:
        _register_readonly(app)
    _register_origin_check(app, dev_mode=dev_mode)
    _register_body_limits(app)
    # after_request hooks run in reverse registration order; the two set
    # disjoint headers, so either order composes.
    _register_security_headers(app, dev_mode=dev_mode)
    if dev_mode:
        _register_cors(app)
    app.register_blueprint(api_bp)
    _register_frontend(app, None if dev_mode else dist)
    _register_errors(app)
    return app


def _register_teardown(app: Flask) -> None:
    @app.teardown_appcontext
    def _close_store(_exc: BaseException | None) -> None:
        store = g.pop("store", None)
        if store is not None:
            store.close()


#: HTTP methods that never change server state; everything else is refused when
#: the app runs read-only. HEAD/OPTIONS are the safe non-GET methods Flask may
#: dispatch itself (auto-HEAD, CORS preflight).
_READONLY_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _register_readonly(app: Flask) -> None:
    """Refuse every state-changing request with a 403 (PUBLIC_READONLY mode).

    The guard keys off the HTTP method alone, so it needs no per-route list and
    cannot be out-run by a new endpoint: only GET/HEAD/OPTIONS pass, and every
    POST/PUT/DELETE/PATCH — pull, extract, score, import, settings, the person
    edits — is refused before it reaches its view, so no store write or outbound
    IMAP/Pangram call happens. GET data and the dashboard are unaffected.
    """

    @app.before_request
    def _reject_writes() -> Any:
        if request.method not in _READONLY_SAFE_METHODS:
            return jsonify({"error": "this instance is read-only"}), 403
        return None


def _hostname(host: str) -> str:
    """The hostname part of a ``Host`` value: lower-cased, port and brackets removed.

    ``"[::1]:8050"`` and ``"::1"`` both give ``"::1"``; ``"Example.org:443."`` gives
    ``"example.org"``. A bare IPv6 address (several colons, no brackets) is kept
    whole.
    """
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        host = host[1:end] if end != -1 else host[1:]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


def _host_port(host: str) -> int | None:
    """The explicit port of a ``Host`` value, or None (also for a malformed one)."""
    host = host.strip()
    if host.startswith("["):
        rest = host[host.find("]") + 1 :] if "]" in host else ""
    elif host.count(":") == 1:
        rest = host[host.find(":") :]
    else:
        return None
    if not rest.startswith(":"):
        return None
    try:
        return int(rest[1:])
    except ValueError:
        return None


def is_loopback_host(host: str | None) -> bool:
    """Whether a bind or Host name refers to the local machine only.

    An empty host counts as loopback: Flask's ``app.run`` binds ``127.0.0.1``
    when given none.
    """
    if not host:
        return True
    name = _hostname(host)
    if name in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def resolve_allowed_hosts(config: Config) -> frozenset[str] | None:
    """The hostnames a request's ``Host`` must name, or None for no check.

    ``ALLOWED_HOSTS`` wins when set (``*`` anywhere in it disables the check).
    Unset, a loopback-bound instance answers to the loopback names only, and any
    other bind address (``0.0.0.0`` in the container, behind an ingress with a
    hostname the app cannot know) has no check.
    """
    entries: Iterable[str] = config.allowed_hosts
    if entries:
        if any(entry.strip() == "*" for entry in entries):
            return None
        return frozenset(_hostname(entry) for entry in entries if entry.strip())
    if is_loopback_host(config.flask_host):
        return LOOPBACK_HOSTS
    return None


def _register_host_check(app: Flask, config: Config) -> None:
    """Refuse a request whose ``Host`` header is not an allowed hostname (400).

    A DNS-rebinding page reaches the server under the attacker's hostname, so its
    requests carry that name in ``Host``; refusing it denies the page both the
    writes and the responses.
    """
    allowed = resolve_allowed_hosts(config)
    app.config["ALLOWED_HOSTS"] = sorted(allowed) if allowed is not None else ["*"]
    if allowed is None:
        return

    @app.before_request
    def _check_host() -> Any:
        if _hostname(request.host) not in allowed:
            return jsonify({"error": "host not allowed"}), 400
        return None


def _normalised_port(port: int | None) -> int | None:
    """Treat the default HTTP and HTTPS ports as "no explicit port".

    TLS is usually terminated by an ingress, so the scheme the app sees need not
    match the browser's; dropping 80 and 443 on both sides compares the
    hostname and any non-default port only.
    """
    return None if port in (80, 443) else port


def _origin_allowed(origin: str, host: str, *, dev_mode: bool) -> bool:
    """Whether a browser ``Origin`` may issue a state-changing request to ``host``.

    Allowed: the request's own host (hostname and non-default port equal);
    in dev mode, :data:`DEV_CORS_ORIGIN`; and any loopback origin when the request
    itself names a loopback host — the Vite dev server (on any port) proxies
    ``/api`` with ``changeOrigin``, so its requests arrive with ``Host:
    127.0.0.1:8050`` and the browser's ``Origin: http://localhost:5173``. A page
    on another site cannot claim a loopback origin, and a rebinding page's
    ``Host`` is refused before this check.
    """
    if dev_mode and origin == DEV_CORS_ORIGIN:
        return True
    try:
        parts = urlsplit(origin)
        origin_name = (parts.hostname or "").rstrip(".")
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not origin_name:
        return False  # includes the opaque origin "null"
    request_name = _hostname(host)
    if origin_name == request_name and _normalised_port(origin_port) == _normalised_port(
        _host_port(host)
    ):
        return True
    return is_loopback_host(origin_name) and is_loopback_host(request_name)


def _register_origin_check(app: Flask, *, dev_mode: bool) -> None:
    """Refuse a cross-site state-changing request with a 403.

    Any web page the operator visits can submit a form or a ``text/plain`` fetch
    to the local instance without a CORS preflight; the browser marks such a
    request with ``Origin`` (and ``Sec-Fetch-Site``). When ``Origin`` is present
    it decides; otherwise ``Sec-Fetch-Site: cross-site`` is refused. A request
    with neither header — curl, the CLI, the test client — is not a browser
    request a third-party page could have caused, and passes.
    """

    @app.before_request
    def _check_origin() -> Any:
        if request.method in _READONLY_SAFE_METHODS:
            return None
        origin = request.headers.get("Origin")
        if origin is not None:
            if _origin_allowed(origin, request.host, dev_mode=dev_mode):
                return None
            return jsonify({"error": "cross-origin request refused"}), 403
        if request.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
            return jsonify({"error": "cross-origin request refused"}), 403
        return None


def _register_body_limits(app: Flask) -> None:
    """Bound every request body: 1 MiB, except the import upload (413 beyond).

    ``MAX_CONTENT_LENGTH`` (the import limit) is the app-wide ceiling; this hook
    lowers the per-request limit on every other path, which also caps a body
    streamed without a ``Content-Length``. A declared length over the limit, or a
    chunked body on a small-body path, is refused before the view runs.
    """

    @app.before_request
    def _limit_body() -> Any:
        if request.path == UPLOAD_PATH:
            limit = app.config["MAX_CONTENT_LENGTH"]
        else:
            limit = SMALL_BODY_LIMIT
            request.max_content_length = limit
            if "chunked" in request.headers.get("Transfer-Encoding", "").lower():
                return jsonify({"error": "request body must declare a Content-Length"}), 413
        length = request.content_length
        if length is not None and limit is not None and length > limit:
            return jsonify({"error": "request body too large"}), 413
        return None


def _register_security_headers(app: Flask, *, dev_mode: bool) -> None:
    """Set the browser security headers on every response.

    ``X-Frame-Options`` and ``frame-ancestors`` stop the dashboard — whose buttons
    start paid scoring runs — from being framed for clickjacking. In dev mode the
    frontend is served by Vite from another origin, so
    ``Cross-Origin-Resource-Policy`` is left off there.
    """

    @app.after_request
    def _add_security_headers(response: Any) -> Any:
        headers = response.headers
        headers["X-Content-Type-Options"] = "nosniff"
        headers["X-Frame-Options"] = "DENY"
        headers["Referrer-Policy"] = "no-referrer"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        if not dev_mode:
            headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        return response


def _register_cors(app: Flask) -> None:
    @app.after_request
    def _add_cors_headers(response: Any) -> Any:
        response.headers["Access-Control-Allow-Origin"] = DEV_CORS_ORIGIN
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response


def _register_frontend(app: Flask, dist: Path | None) -> None:
    if dist is not None:
        index = "index.html"
        dist_root = dist.resolve()

        def _static_file(path: str) -> str | None:
            """``path`` if it names a regular file inside dist, else None.

            ``safe_join`` refuses ``..`` segments and absolute paths, and the
            resolved path must stay inside dist, so a symlink leading out of it is
            not followed. Nothing outside dist is ever stat'ed: a path that leaves
            dist answers exactly like an unknown one (the index), so responses do
            not reveal which files exist elsewhere on the disk.
            """
            if not path:
                return None
            joined = safe_join(str(dist_root), path)
            if joined is None:
                return None
            resolved = Path(joined).resolve()
            if not resolved.is_relative_to(dist_root) or not resolved.is_file():
                return None
            return path

        @app.route("/", defaults={"path": ""})
        @app.route("/<path:path>")
        def spa(path: str) -> Any:
            # Never let the SPA fallback shadow the API; unknown API routes must
            # still 404 as JSON.
            if path.startswith("api/"):
                raise ApiError("not found", 404)
            static = _static_file(path)
            if static is not None:
                return send_from_directory(dist_root, static)
            return send_from_directory(dist_root, index)
    else:

        @app.route("/")
        def notice() -> Any:
            return jsonify(
                {
                    "app": "mailing-list-ai-check",
                    "message": (
                        "API is running. The Vue frontend has not been built "
                        "(frontend/dist is missing). Run the Vite dev server, or "
                        "'npm run build' to have this app serve the bundle."
                    ),
                    "api_base": "/api",
                }
            )


def _register_errors(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _handle_api_error(exc: ApiError) -> Any:
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(400)
    def _bad_request(_exc: Any) -> Any:
        return jsonify({"error": "bad request"}), 400

    @app.errorhandler(404)
    def _not_found(_exc: Any) -> Any:
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_exc: Any) -> Any:
        return jsonify({"error": "method not allowed"}), 405

    @app.errorhandler(413)
    def _too_large(_exc: Any) -> Any:
        return jsonify({"error": "request body too large"}), 413


# --- dev-server entry point ---------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mail-ai-web",
        description="Run the dashboard JSON API (and built frontend, if present).",
    )
    parser.add_argument("--host", help="bind host (default from FLASK_HOST)")
    parser.add_argument("--port", type=int, help="bind port (default from FLASK_PORT)")
    parser.add_argument("--db", metavar="PATH", help="override the database path")
    parser.add_argument("--debug", action="store_true", help="run Flask in debug mode")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load()
    if args.db:
        config = replace(config, database_path=args.db)

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    host = args.host or config.flask_host
    port = args.port or config.flask_port
    if args.debug and not is_loopback_host(host):
        # The Werkzeug debugger runs arbitrary Python from the browser; on a
        # non-loopback address that is remote code execution for anyone who can
        # reach the port.
        print(
            f"mail-ai-web: refusing --debug on non-loopback host {host!r}; the "
            "interactive debugger executes arbitrary code. Bind 127.0.0.1 or drop "
            "--debug.",
            file=sys.stderr,
        )
        return 2
    # The Host allow-list default follows the address actually bound.
    config = replace(config, flask_host=host)
    app = create_app(config)
    mode = "dev (no built frontend)" if app.config["DEV_MODE"] else "serving frontend/dist"
    log.info("starting mail-ai-web on http://%s:%d [%s]", host, port, mode)
    # threaded=True so a long-running /api/pull (network + paid scoring) does not
    # block the SPA's other API requests on the single-process dev server.
    app.run(host=host, port=port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
