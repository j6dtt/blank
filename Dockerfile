# Runtime only: Python + the libraries from pyproject.toml. The app code is
# NOT in the image; compose.yaml mounts ./src at /app/src, so a code fix is
# "edit the file, docker compose restart". Rebuild only when dependencies
# in pyproject.toml change (then bump the image tag in compose.yaml).
#
# Kept small for the air-gapped bundle (limit 25 MB): the libraries are
# installed in a build stage, then pip, compiled caches and Python's desktop
# extras are deleted and the result is copied into one fresh layer.
FROM python:3.13-alpine AS build

COPY pyproject.toml /tmp/pyproject.toml
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('/tmp/pyproject.toml', 'rb'))['project']['dependencies']))" \
      > /tmp/requirements.txt \
 && pip install --no-cache-dir --root-user-action=ignore -r /tmp/requirements.txt \
 && pip uninstall -y --root-user-action=ignore pip \
 && cd /usr/local/lib/python3.13 \
 && rm -rf ensurepip idlelib tkinter turtledemo turtle.py pydoc_data \
 && find / -xdev -name __pycache__ -type d -prune -exec rm -rf {} + \
 && rm -rf /tmp/* /root/.cache \
 && printf '#!/bin/sh\nexec python -m uxcorrelate.cli "$@"\n' > /usr/local/bin/uxcorrelate \
 && chmod 755 /usr/local/bin/uxcorrelate \
 && adduser -D -H uxc

FROM scratch
COPY --from=build / /

ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    LANG=C.UTF-8 PYTHONPATH=/app/src PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER uxc
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4)"

ENTRYPOINT ["uxcorrelate"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8080"]
