#!/bin/sh
# Save every test investigation (text report + page data) into a folder,
# to compare before and after a change:
#   scripts/snapshot.sh /tmp/before     (before the change)
#   scripts/snapshot.sh /tmp/after      (after it)
#   diff -r /tmp/before /tmp/after      (every line that changed)
# Runs the tests in the runtime image, like make-bundle.sh.
set -e
out=${1:?usage: snapshot.sh FOLDER}
cd "$(dirname "$0")/.."
rm -rf "$out" && mkdir -p "$out"
image=$(sed -n 's/^x-image: *&image *//p' compose.yaml)
# the runtime image has no pytest: put it in a throwaway folder (as make-bundle.sh)
tools=$(mktemp -d)
trap 'rm -rf "$tools"' EXIT
docker run --rm -u "$(id -u):$(id -g)" -v "$tools":/out python:3.13-alpine \
  pip install -q --no-cache-dir --target /out pytest >/dev/null 2>&1
docker run --rm --entrypoint python -u "$(id -u):$(id -g)" -v "$PWD":/app:ro -v "$tools":/tools:ro \
  -v "$(realpath "$out")":/out -w /app -e PYTHONPATH=src:tests:/tools -e SNAPSHOT_DIR=/out "$image" \
  -m pytest -q -p no:cacheprovider tests | tail -1
echo "$(ls "$out" | wc -l) investigations saved in $out"
