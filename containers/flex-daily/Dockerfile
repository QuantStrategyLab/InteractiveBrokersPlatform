FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
WORKDIR /app
COPY containers/flex-daily/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && useradd --uid 10001 --create-home worker
COPY src/interactive_brokers_platform/__init__.py /app/src/interactive_brokers_platform/__init__.py
COPY src/interactive_brokers_platform/application/__init__.py /app/src/interactive_brokers_platform/application/__init__.py
COPY src/interactive_brokers_platform/application/ibkr_flex_source.py /app/src/interactive_brokers_platform/application/ibkr_flex_source.py
COPY src/interactive_brokers_platform/application/flex_daily/*.py /app/src/interactive_brokers_platform/application/flex_daily/
USER 10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src
ENTRYPOINT ["python", "-m", "interactive_brokers_platform.application.flex_daily"]
