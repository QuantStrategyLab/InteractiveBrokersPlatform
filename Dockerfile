FROM python:3.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    PATH="/app/.venv/bin:${PATH}" \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN python -m pip install --upgrade pip uv \
    && uv sync --frozen --no-dev \
    && /app/.venv/bin/python -c 'from importlib import metadata as m; import json; from pathlib import Path; payload=json.loads(m.distribution("us-equity-strategies").read_text("direct_url.json") or "{}"); rev=(payload.get("vcs_info") or {}).get("commit_id"); assert isinstance(rev, str) and rev.strip(); Path("/app/UES_REVISION").write_text(rev.strip() + "\n", encoding="utf-8")' \
    && /app/.venv/bin/python scripts/validate_cloud_run_startup.py \
    && apt-get purge -y git \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app

USER appuser

RUN /app/.venv/bin/python -m scripts.publish_digest_before_read --help

CMD ["/app/.venv/bin/python", "-m", "gunicorn", "--bind", ":8080", "--workers", "1", "--threads", "1", "--timeout", "300", "main:app"]
