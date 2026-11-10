FROM python:3.12-slim
WORKDIR /app
# DejaVu fonts give the PDF report Uzbek/Russian (Cyrillic) glyphs and bold headings.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
ENV PORT=8000 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
# Bounded open connections; Railway's PORT is honoured. The engine service overrides this command.
CMD ["sh","-c","uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --limit-concurrency 64 --timeout-graceful-shutdown 20 --no-server-header"]
