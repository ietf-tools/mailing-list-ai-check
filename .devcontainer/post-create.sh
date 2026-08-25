#!/usr/bin/env bash
# Runs once, after the dev container is created (devcontainer.json's
# postCreateCommand). Everything here operates on the bind-mounted tree, which
# does not exist at image build time.
set -euo pipefail

cd "$(dirname "$0")/.."

# The image chowns /opt/venv to uid 1000. When the host developer's uid differs,
# the devcontainer runtime's updateRemoteUserUID remaps the container user to it,
# which leaves the virtualenv owned by the old uid.
if [ ! -w /opt/venv/bin ]; then
	echo "==> taking ownership of /opt/venv (remapped user id)"
	sudo chown -R "$(id -u):$(id -g)" /opt/venv
fi

# The mounted tree is the package source. Install it editable into the image's
# virtualenv so `mail-ai-*`, pytest and the web app all import the mounted code.
# --no-deps because the dependencies are already in the image; this only writes
# the path hook and the console scripts, and needs no network.
echo "==> installing the package (editable) into /opt/venv"
pip install --no-deps -e .

# frontend/node_modules is a named volume (see devcontainer.json) and Docker
# creates it root-owned on first use.
if [ ! -w frontend/node_modules ]; then
	echo "==> taking ownership of the frontend/node_modules volume"
	sudo chown "$(id -u):$(id -g)" frontend/node_modules
fi

echo "==> installing the frontend dependencies"
make install-frontend

cat <<'NOTE'

Dev container ready.

  make test                      pytest
  make lint                      ruff check + format --check
  make build                     build frontend/dist (mail-ai-web then serves it)
  make dev                       print the two-terminal dev workflow

  terminal 1: mail-ai-web                          Flask API on :8050
  terminal 2: npm run dev --prefix frontend        Vite on :5173 (open this)

Configuration: copy .env.example to .env and fill in what you need. IMAP_HOST is
required to pull mail and PANGRAM_API_KEY to score it; the dashboard and the
extraction stage need neither. The database defaults to ./data/mail.db, inside
this mounted tree.
NOTE
