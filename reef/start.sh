#!/usr/bin/env bash
# One-step setup for Mac/Linux: creates .env, lets you paste the keys, checks them, starts Reef in Docker.
set -euo pipefail
cd "$(dirname "$0")"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker (Docker Desktop on Mac), then run ./start.sh again."
  exit 1
fi

has_keys() {
  grep -Eq '^APIFY_TOKEN=[^[:space:]]' .env && grep -Eq '^OPENROUTER_API_KEY=[^[:space:]]' .env
}

edit_env() {
  echo
  echo "Paste your keys right after APIFY_TOKEN= and OPENROUTER_API_KEY= in .env and save."
  if [ "$(uname)" = "Darwin" ]; then
    open -e .env
    read -r -p "Press Enter here after you saved the file... "
  else
    "${EDITOR:-nano}" .env
  fi
}

if ! grep -q "from .cli import main" reef/__main__.py 2>/dev/null; then
  echo "The files in this folder are mixed up (probably copied over an older version)."
  echo "Keep your .env, delete this folder, extract the zip into an empty folder and run ./start.sh again."
  exit 1
fi

# An older setup could leave a FOLDER named .env behind (Docker creates it when the file is missing).
if [ -d .env ]; then rm -rf .env; fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env in $(pwd)"
  edit_env
fi
if ! has_keys; then
  echo "APIFY_TOKEN or OPENROUTER_API_KEY is still empty."
  edit_env
fi
if ! has_keys; then
  echo "Keys are still missing in .env - paste them and run ./start.sh again."
  exit 1
fi

echo; echo "=== Building Reef (the first time takes a few minutes) ==="
docker compose build

echo; echo "=== Checking your keys ==="
if ! docker compose run --rm reef python -m reef doctor --notify; then
  echo; echo "Some checks failed (lines marked [x] above). Fix .env and run ./start.sh again."
  exit 1
fi

echo; echo "=== Starting Reef in the background ==="
docker compose up -d
cat <<'EOF'

Reef is running. It starts again by itself whenever Docker starts.
  docker compose logs -f --tail 100                    see what it is doing
  docker compose exec reef python -m reef status       fleet overview
  docker compose exec reef python -m reef pause        stop switch
  docker compose exec reef python -m reef resume       start again
EOF
