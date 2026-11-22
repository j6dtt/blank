#!/bin/sh
# The page check (dev only): a real Chrome opens the pages and counts what is drawn against every
# number that stands for it. Run it before every copy to stage that touches a page; it must end with
# "0 do not". What it looks at and why: the top of dev/page_check.py.
#
#   dev/page_check.sh
#
# Needs the dev app up with its sample data (docker compose -f dev/compose.yaml up -d, then the app)
# and the two images already on the dev host: the runtime image and uitest-chrome.
set -eu
cd "$(dirname "$0")/.."
RUNTIME="$(sed -n 's/^x-image: *&image *//p' compose.yaml | head -1)"
[ -n "$RUNTIME" ] || RUNTIME=uxcorrelate-runtime:4
OUT="$(mktemp -d)"
chmod 755 "$OUT"
trap 'rm -rf "$OUT"' EXIT
docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint python \
  -v "$PWD":/app:ro -v "$OUT":/out -w /app -e PYTHONPATH=src "$RUNTIME" dev/page_check.py data /out
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp --entrypoint python3 \
  -v "$PWD/dev":/dev-check:ro -v "$OUT":/out uitest-chrome:latest /dev-check/page_check.py look /out
