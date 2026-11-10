"""Configuration loading.

Secrets come from environment variables, loaded from a gitignored ``.env`` file
in local development. Never hard-code credentials in source.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var. ``1/true/yes/on`` (any case) is true; unset is default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    """Read a comma-separated env var as a tuple of non-empty, trimmed items."""
    raw = os.environ.get(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive integer env var; unset or empty is ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from None
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}")
    return value


@dataclass(frozen=True)
class Config:
    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    pangram_api_key: str
    database_path: str
    log_level: str
    flask_host: str
    flask_port: int
    #: When true, the web app rejects every state-changing request (any method
    #: other than GET/HEAD/OPTIONS) with a 403. Intended for a read-only instance
    #: exposed on an untrusted network: the dashboard and all its GET data stay
    #: fully usable, while pull/extract/score, import, settings and the person
    #: edits are refused. Off by default, so a normal local instance is unchanged.
    public_readonly: bool = False
    #: Whether ``GET /api/export`` (the full corpus download, message bodies
    #: included) is served. These two export switches are GET endpoints that the
    #: read-only guard does not cover; set this false to refuse the full export
    #: with a 403 on an instance whose message text should not be downloadable
    #: in bulk. It does not withhold text from the dashboard: the message detail
    #: endpoint still returns each message's body. On by default, so a normal
    #: instance is unchanged.
    allow_export: bool = True
    #: Whether ``GET /api/export/stats`` (the scores/metadata CSV archive, which
    #: carries no message text) is served. Set false to refuse it with a 403. On
    #: by default.
    allow_stats_export: bool = True
    #: Hostnames the web app answers to (the ``Host`` header, port ignored). A
    #: request naming any other host is refused with a 400, which defeats DNS
    #: rebinding. ``("*",)`` disables the check. Empty (the default) means: only
    #: the loopback names when ``flask_host`` is a loopback address, and no check
    #: otherwise (a container bound to 0.0.0.0 behind an ingress, whose hostname
    #: the app cannot know; such an instance should set ``ALLOWED_HOSTS``).
    allowed_hosts: tuple[str, ...] = ()
    #: Largest request body accepted by ``POST /api/import``, in MiB. Every
    #: other endpoint is limited to 1 MiB regardless. Larger requests get a 413.
    max_upload_mb: int = 1024

    @classmethod
    def load(cls) -> "Config":
        # IMAP settings are deployment-specific and have no baked-in defaults.
        # Pulling requires IMAP_HOST; some archives accept an anonymous login,
        # in which case set IMAP_USERNAME/IMAP_PASSWORD to that documented
        # login rather than real credentials.
        return cls(
            imap_host=os.environ.get("IMAP_HOST", ""),
            imap_port=int(os.environ.get("IMAP_PORT", "993")),
            imap_username=os.environ.get("IMAP_USERNAME", ""),
            imap_password=os.environ.get("IMAP_PASSWORD", ""),
            pangram_api_key=os.environ.get("PANGRAM_API_KEY", ""),
            database_path=os.environ.get("DATABASE_PATH", "./data/mail.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            flask_host=os.environ.get("FLASK_HOST", "127.0.0.1"),
            flask_port=int(os.environ.get("FLASK_PORT", "8050")),
            public_readonly=_env_bool("PUBLIC_READONLY", False),
            allow_export=_env_bool("ALLOW_EXPORT", True),
            allow_stats_export=_env_bool("ALLOW_STATS_EXPORT", True),
            allowed_hosts=_env_list("ALLOWED_HOSTS"),
            max_upload_mb=_env_positive_int("MAX_UPLOAD_MB", 1024),
        )
