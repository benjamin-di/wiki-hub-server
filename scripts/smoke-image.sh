#!/usr/bin/env bash
# Smoke the exact candidate without network access or persistent application state.
set -euo pipefail
image="${1:?image required}"
docker run --rm --network none --read-only --tmpfs /data:uid=1001 \
  --entrypoint python "$image" \
  -c 'from wiki_hub.server import create_app; from wiki_hub import __version__; print(__version__)'
docker run --rm --network none --read-only --entrypoint git "$image" lfs version
