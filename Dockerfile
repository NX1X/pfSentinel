# EXPERIMENTAL container image for pfSentinel (pre-1.0, not yet field-tested).
# See docs/docker.md.
#
# Base image: Docker Official Images (Docker Inc., US), Debian 12 slim.
# Pinned by digest. Change the tag and the digest together, after checking:
#   docker buildx imagetools inspect python:<tag>

# ---- build stage ------------------------------------------------------------
FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /src

# 1. Build tools from the hash-locked dev lock file. They stay in this stage.
COPY requirements-dev.lock ./
RUN python -m pip install --only-binary :all: --require-hashes --no-deps \
        -r requirements-dev.lock

# 2. Runtime dependencies from the hash-locked file, into a virtualenv.
COPY requirements.lock ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --only-binary :all: --require-hashes \
        -r requirements.lock

# 3. Build a wheel of pfSentinel from this source tree, then install it
#    without dependencies (they are already installed and verified).
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip wheel --no-deps --no-build-isolation -w /wheels . \
    && /opt/venv/bin/python -m pip install --no-deps /wheels/*.whl

# ---- runtime stage ----------------------------------------------------------
FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72

LABEL org.opencontainers.image.title="pfSentinel (EXPERIMENTAL)" \
      org.opencontainers.image.description="Backup tool for pfSense firewalls. Experimental container image." \
      org.opencontainers.image.source="https://github.com/NX1X/pfSentinel" \
      org.opencontainers.image.licenses="Apache-2.0"

# Fixed UID and GID, so volume ownership is predictable.
# No login shell, no password.
RUN groupadd --gid 10001 pfs \
    && useradd --uid 10001 --gid 10001 --home-dir /home/pfs --create-home \
        --shell /usr/sbin/nologin pfs \
    && mkdir -p /data /home/pfs/.pfsentinel /home/pfs/Documents \
    # pfSentinel saves backups to ~/Documents/pfSentinel unless backup-dir is
    # set. This link makes /data the default, so a fresh volume just works.
    && ln -s /data /home/pfs/Documents/pfSentinel \
    && chown -R 10001:10001 /data /home/pfs \
    && chmod 0700 /home/pfs/.pfsentinel

COPY --from=build /opt/venv /opt/venv

# No bytecode writes: the root filesystem may be read-only.
ENV PATH="/opt/venv/bin:${PATH}" \
    HOME=/home/pfs \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 10001:10001
WORKDIR /data

# /data: backups. /home/pfs/.pfsentinel: config, encrypted secret store,
# trusted host keys. Both must be persistent volumes.
VOLUME ["/data", "/home/pfs/.pfsentinel"]

ENTRYPOINT ["pfs"]
CMD ["--help"]
