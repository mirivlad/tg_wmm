FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/wmm.db

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng libzbar0 libgomp1 libgl1 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin wmm \
    && mkdir /data && chown -R 10001:10001 /data

COPY wmm/ ./wmm/
# Bundle checksum-validated Russian PP-OCRv5 models into the image.
# This prevents first receipt processing from depending on runtime internet access.
RUN python -c "from wmm.ocr_engine import get_rapidocr; get_rapidocr()"
USER 10001:10001
VOLUME ["/data"]
CMD ["python", "-m", "wmm"]
