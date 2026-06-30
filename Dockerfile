FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
COPY constraints.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt -c constraints.txt \
    && apt-get purge -y git \
    && apt-get autoremove -y --purge \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["gunicorn", "--bind", ":8080", "--workers", "1", "--threads", "1", "--timeout", "300", "main:app"]
