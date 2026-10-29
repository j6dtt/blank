#!/bin/sh
# Pack everything the air-gapped live host needs into one file:
# dist/uxcorrelate-<version>.tar
#   image.tar.xz  runtime image, Python + libraries only (docker load)
#   src/          the app code, mounted into the container
#   compose.yaml, config.example.yaml, README.md
#   INSTALL.txt   steps to run on the live host
set -eu
cd "$(dirname "$0")/.."

image=$(docker compose config --images)
version=$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])")
name="uxcorrelate-$version"
out="dist/$name"

echo "Building runtime image $image ..."
docker compose build --quiet

# No bundle while the code disagrees with Riverbed's reference data
# (tests/test_contract.py) or any other test fails.
# The runtime image has no pip, so pytest is installed into a throwaway folder
# with the plain base image and mounted in.
echo "Running tests in the runtime image ..."
tools=$(mktemp -d)
trap 'rm -rf "$tools"' EXIT
docker run --rm -u "$(id -u):$(id -g)" -v "$tools":/out python:3.13-alpine \
  pip install -q --no-cache-dir --target /out pytest >/dev/null 2>&1
docker run --rm --entrypoint python -v "$PWD":/app:ro -v "$tools":/tools:ro -w /app \
  -e PYTHONPATH=src:/tools "$image" -m pytest -q -p no:cacheprovider \
  || { echo "Tests failed: no bundle made."; exit 1; }

echo "Packing $name ..."
rm -rf "$out" && mkdir -p "$out"
docker save "$image" | xz -9 -T0 > "$out/image.tar.xz"
sed "/^    build: \.$/d" compose.yaml > "$out/compose.yaml"   # live never builds
cp config.example.yaml README.md "$out/"
tar -cf - --exclude=__pycache__ src | tar -C "$out" -xf -

cat > "$out/INSTALL.txt" <<EOF
UX Correlator $version - install on the live host (no internet needed)
Runtime image: $image (Python + libraries; the code is in ./src)

First install:
  tar xf $name.tar && cd $name
  docker load < image.tar.xz            # loads $image
  cp config.example.yaml config.yaml    # fill every <REQUIRED> value
  chmod -R o+rX src config.yaml         # the container user (uid 1000) must read them
  vi .env                               # ATERNITY_PASSWORD=...  NETIM_PASSWORD=...
  chmod 600 .env
  docker compose up -d
  docker compose exec uxcorrelate uxcorrelate verify

Fix code on this host:
  vi src/uxcorrelate/<file>.py
  docker compose restart
  (copy the fix back to the dev repo afterwards)

Upgrade to a new bundle (keeps config.yaml and .env):
  tar xf $name.tar
  rm -rf <install dir>/src && cp -r $name/src <install dir>/
  chmod -R o+rX <install dir>/src
  docker load < $name/image.tar.xz      # only if the image tag changed
  cp $name/compose.yaml <install dir>/  # only if you have not edited it
  cd <install dir> && docker compose up -d

Never run "docker compose up --build" here: there is no internet to build.
EOF

tar -C dist -cf "dist/$name.tar" "$name"
rm -rf "$out"

limit=$((25 * 1000 * 1000))   # prod transfer limit
size=$(stat -c %s "dist/$name.tar")
if [ "$size" -ge "$limit" ]; then
  rm -f "dist/$name.tar"
  echo "Bundle is $size bytes, over the 25 MB prod limit: no bundle made."
  exit 1
fi
echo "Bundle: dist/$name.tar ($(du -h "dist/$name.tar" | cut -f1))"
echo "Image platform: $(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')"
