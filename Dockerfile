# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db AS builder

COPY --from=uv /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_BUILD_CONSTRAINT=/build/build-constraints.txt \
    UV_PROJECT_ENVIRONMENT=/opt/atvr4samsung/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

COPY pyproject.toml uv.lock build-constraints.txt README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-editable --no-cache

FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

ARG VERSION
ARG VCS_REF

LABEL org.opencontainers.image.title="atvr4samsung" \
      org.opencontainers.image.description="Use the native iOS Apple TV Remote with a Samsung Frame TV" \
      org.opencontainers.image.source="https://github.com/vb3/atvr4samsung" \
      org.opencontainers.image.url="https://github.com/vb3/atvr4samsung" \
      org.opencontainers.image.documentation="https://github.com/vb3/atvr4samsung/blob/main/docs/operations.md" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}"

ENV HOME=/data \
    PATH=/opt/atvr4samsung/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/atvr4samsung/.venv /opt/atvr4samsung/.venv
COPY LICENSE THIRD_PARTY_NOTICES.md /licenses/
COPY src/atvr4samsung/companion/protocol/LICENSE-companion-base.md /licenses/

WORKDIR /data
USER 65532:65532

ENTRYPOINT ["atvr4samsung"]
CMD ["--config", "/config/config.yaml"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["atvr4samsung", "--config", "/config/config.yaml", "healthcheck"]
