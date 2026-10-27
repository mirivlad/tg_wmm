FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/wmm.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin wmm \
    && mkdir /data && chown -R 10001:10001 /data

COPY wmm/ ./wmm/
USER 10001:10001
VOLUME ["/data"]
CMD ["python", "-m", "wmm"]
