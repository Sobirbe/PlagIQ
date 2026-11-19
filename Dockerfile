FROM python:3.12-slim
WORKDIR /app
# DejaVu fonts give the PDF report Uzbek/Russian (Cyrillic) glyphs and bold headings.
# Tesseract with the Uzbek (Latin, Cyrillic), Russian and English models reads scanned PDF pages (OCR).
# tini runs as process 1: it passes Railway's stop signal to the server's whole process group (-g), so checks end
# cleanly on a redeploy, and collects finished helper processes (a stopped file reader's children would otherwise
# stay behind as "zombie" processes). Without it, "sh" was process 1, ignored the stop signal and was force-killed.
RUN apt-get update && apt-get install -y --no-install-recommends tini fonts-dejavu-core \
        tesseract-ocr tesseract-ocr-uzb tesseract-ocr-uzb-cyrl tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
ENV PORT=8000 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
# Bounded open connections; Railway's PORT is honoured. The engine service overrides this command.
ENTRYPOINT ["/usr/bin/tini","-g","--"]
CMD ["sh","-c","exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --limit-concurrency 64 --timeout-graceful-shutdown 20 --no-server-header"]
