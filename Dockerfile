FROM python:3.13-slim-bookworm

# tini for signal handling (signed Debian package)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Never build from / (setuptools would walk /proc forever; keelson-connector-pc's Dockerfile).
WORKDIR /app

COPY pyproject.toml pyproject.toml
COPY README.md README.md
COPY control_authority control_authority
# keelson installs from PyPI at the EXACT version pyproject.toml pins -- never a local checkout.
RUN uv pip install --system --no-cache .

COPY --chmod=555 ./bin/authority-manager2keelson.py /usr/local/bin/authority-manager2keelson

# The store lives on a volume (docker-compose.authority-manager.yml). An untrusted store stops the
# manager rather than guess who holds what -- so it must be a persistent volume, not the image layer.
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app && mkdir -p /state && chown app /state
USER app

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/bin/bash", "-c"]
