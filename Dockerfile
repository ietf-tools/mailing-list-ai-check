# syntax=docker/dockerfile:1

# mailing-list-ai-check — one image definition with two build targets.
#
#   docker build --target dev  -t mailing-list-ai-check:dev .
#   docker build --target prod -t mailing-list-ai-check:1.15.3 .
#
# dev  — the image .devcontainer/devcontainer.json builds. It carries the
#        toolchain (Python 3.14, Node, make, git, ruff, pytest) and the
#        third-party dependencies, but no application source: the tree is
#        bind-mounted from the developer's disk and installed into the image's
#        virtualenv in editable mode by the devcontainer's postCreateCommand.
#        The database is a file inside that same mounted tree, so it lives on the
#        developer's disk with the code (DATABASE_PATH defaults to
#        ./data/mail.db).
#
# prod — a self-contained deployment image: the source tree at /app, the built
#        dashboard at /app/frontend/dist, and gunicorn serving the Flask app as
#        an unprivileged user. Nothing is mounted from a source tree and the
#        image holds no database; the SQLite file lives on a separate mount
#        (under Kubernetes, a PersistentVolumeClaim at /data, with
#        DATABASE_PATH=/data/mail.db). See docs/deployment.md.
#
# Both targets install the package in editable mode. That is not a development
# convenience: webapp.create_app() finds frontend/dist, and the /api/docs
# endpoints find the documentation set, by walking up from the package's
# __file__ (webapp._repo_root), so the package has to be imported from the tree
# that holds them rather than copied into site-packages.
#
# Python 3.14 is a hard floor, not a preference: the export/import format uses
# compression.zstd, which is standard library only from that release.

ARG PYTHON_IMAGE=python:3.14-slim-trixie
ARG NODE_IMAGE=node:22-trixie-slim


# --- frontend: build the dashboard (what `make build` does) -------------------
FROM ${NODE_IMAGE} AS frontend

WORKDIR /build
# package-lock.json is committed, so `npm ci` installs a pinned tree. Copying
# the manifests alone first keeps the install layer out of the way of ordinary
# source edits.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# `make build` is `npm run build --prefix frontend`; run directly, because make
# and the Python side are not needed to produce frontend/dist.
RUN npm run build


# --- base: the Python runtime and its virtualenv -----------------------------
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv
# Separate from the ENV above: a variable is not reliably expandable in the
# instruction that defines it.
ENV PATH=/opt/venv/bin:$PATH

# A virtualenv outside the workspace, so a bind-mounted tree (which may carry
# the developer's own .venv, built for another platform) can never shadow it.
RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /app


# --- deps: third-party dependencies, from pyproject.toml only ----------------
# Only the metadata and the version module are copied, so an ordinary source
# edit does not invalidate the dependency layer. The package itself is installed
# and immediately removed: that resolves the dependency list without restating
# it here, and the real (editable) install happens against the full tree later.
FROM base AS deps
COPY pyproject.toml ./
COPY src/mailing_list_ai_check/__init__.py src/mailing_list_ai_check/__init__.py

FROM deps AS deps-prod
RUN pip install ".[prod]" && pip uninstall -y mailing-list-ai-check

FROM deps AS deps-dev
RUN pip install ".[dev,prod]" && pip uninstall -y mailing-list-ai-check


# --- prod: the deployment image ----------------------------------------------
FROM deps-prod AS prod

# A fixed, non-root uid/gid, so a Kubernetes securityContext can name it and a
# PersistentVolume can be owned by it (runAsUser/runAsGroup/fsGroup = 1001).
ARG APP_UID=1001
ARG APP_GID=1001
RUN groupadd --gid "${APP_GID}" mlac \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --home-dir /app --no-create-home \
      --shell /usr/sbin/nologin mlac \
 && install -d -o "${APP_UID}" -g "${APP_GID}" /data

# /app stays root-owned: the application only ever writes to the database mount
# and to a temporary directory, so it needs no write access to its own tree.
COPY pyproject.toml README.md CHANGELOG.md ./
COPY src/ src/
# README.md, CHANGELOG.md and docs/*.md are the set /api/docs serves; the
# documentation drawer in the dashboard reads them from this tree.
COPY docs/ docs/
COPY --from=frontend /build/dist/ frontend/dist/
COPY docker/mlac-web /usr/local/bin/mlac-web

RUN pip install --no-deps -e . && chmod 0755 /usr/local/bin/mlac-web

# Defaults for a container: the database on the mounted volume, and a bind
# address reachable from outside the container. Every one of these is an
# ordinary environment variable, so a Deployment overrides it without a rebuild.
ENV DATABASE_PATH=/data/mail.db \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8050

USER mlac
EXPOSE 8050

# /api/capabilities is the cheapest GET in the API: it reads the app config and
# never opens the database. Kubernetes uses its own probes (docs/deployment.md);
# this covers `docker run` and Compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('FLASK_PORT','8050')+'/api/capabilities',timeout=4).read()"]

# The web server is the default. The pipeline commands are on PATH in the same
# image, so a Kubernetes Job overrides this with, e.g.,
# ["mail-ai-import", "/data/export.jsonl.zst"].
CMD ["mlac-web"]


# --- dev: the devcontainer image ---------------------------------------------
FROM deps-dev AS dev

# The toolchain the Makefile and an interactive session expect. zstd is here for
# inspecting export files by hand; libstdc++6 is the Node runtime's dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      less \
      libstdc++6 \
      make \
      openssh-client \
      procps \
      sudo \
      zstd \
 && rm -rf /var/lib/apt/lists/*

# Node comes from the same image that builds frontend/dist, so `npm run dev` and
# the production build always run the same version.
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

# uid/gid 1000 matches the common single-user Linux host, so files created in the
# bind mount belong to the developer. The devcontainer CLI's
# updateRemoteUserUID (on by default) remaps the user when the host uid differs.
ARG DEV_UID=1000
ARG DEV_GID=1000
ARG DEV_USER=vscode
RUN groupadd --gid "${DEV_GID}" "${DEV_USER}" \
 && useradd --uid "${DEV_UID}" --gid "${DEV_GID}" --shell /bin/bash --create-home "${DEV_USER}" \
 && echo "${DEV_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${DEV_USER}" \
 && chmod 0440 "/etc/sudoers.d/${DEV_USER}" \
 && chown -R "${DEV_USER}:${DEV_USER}" /opt/venv

# The Makefile runs the Python tools from ./.venv when it finds one and from
# PATH otherwise. Setting this empty pins the container to PATH (i.e. /opt/venv)
# even when the mounted tree contains a .venv built on the host.
ENV VENV_BIN=""

USER ${DEV_USER}
# The devcontainer runtime keeps the container alive and attaches to it; this is
# only the fallback for a plain `docker run`.
CMD ["sleep", "infinity"]
