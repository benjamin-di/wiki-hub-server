# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b AS uv
FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

RUN apt-get update \
    && apt-get install -y --no-install-recommends git git-lfs poppler-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1001 wiki && useradd -u 1001 -g 1001 -M wiki \
    && mkdir /data /workspaces && chown 1001:1001 /data /workspaces
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --compile-bytecode

ARG VCS_REF=unknown
ARG BUILD_DATE=unknown
ARG SOURCE_URL=unknown
LABEL org.opencontainers.image.title="wiki-hub" \
      org.opencontainers.image.description="Git-backed knowledge collections" \
      org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}"
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/data
USER 1001:1001
EXPOSE 8000
ENTRYPOINT ["wiki-hub"]
