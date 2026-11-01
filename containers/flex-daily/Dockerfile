FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
WORKDIR /app
COPY containers/flex-daily/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && useradd --uid 10001 --create-home worker
COPY application/ibkr_flex_source.py /app/application/ibkr_flex_source.py
COPY application/flex_daily /app/application/flex_daily
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "application.flex_daily"]
