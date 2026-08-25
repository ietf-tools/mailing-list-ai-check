# mailing-list-ai-check — developer tasks.
#
# Dev workflow is two terminals (the Vite dev server proxies /api to Flask):
#   terminal 1:  mail-ai-web                      # Flask API on :8050
#   terminal 2:  npm run dev --prefix frontend      # Vite on :5173 -> open this
# `make dev` prints this reminder. Use `make build` to produce frontend/dist,
# which `mail-ai-web` then serves directly (no Vite needed).
#
# The Python tools run from ./.venv when the tree has one (the plain host
# workflow) and from PATH otherwise (the container workflow, where the virtualenv
# is at /opt/venv and the dev image sets VENV_BIN empty). Force either with
# `make test VENV_BIN=` or `make test VENV_BIN=.venv/bin/`.
VENV_BIN ?= $(if $(wildcard .venv/bin/pytest),.venv/bin/,)

# Container image name and tag; the tag tracks the package version, which is the
# single source of truth in src/mailing_list_ai_check/__init__.py.
IMAGE ?= mailing-list-ai-check
TAG ?= $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/mailing_list_ai_check/__init__.py)

.PHONY: dev build test lint install-frontend image image-dev

dev:
	@echo "Two-terminal dev workflow:"
	@echo "  terminal 1:  mail-ai-web                    # Flask API on http://127.0.0.1:8050"
	@echo "  terminal 2:  npm run dev --prefix frontend    # Vite dev server on http://localhost:5173"
	@echo ""
	@echo "Open http://localhost:5173 — it proxies /api to Flask, so no CORS setup is needed."

install-frontend:
	npm install --prefix frontend

build:
	npm run build --prefix frontend

test:
	$(VENV_BIN)pytest -q

lint:
	$(VENV_BIN)ruff check .
	$(VENV_BIN)ruff format --check .

# The deployment image: the frontend build, the package and the documentation
# set, served by gunicorn. See docs/deployment.md.
image:
	docker build --target prod -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

# The dev-container image, built by hand. Normally the devcontainer runtime
# builds this from .devcontainer/devcontainer.json instead.
image-dev:
	docker build --target dev -t $(IMAGE):dev .
