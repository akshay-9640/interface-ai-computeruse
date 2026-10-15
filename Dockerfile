# Live demo image: Meridian Core mock app + replay engine + web console. No LLM, no API key.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements-demo.txt \
 && python -m playwright install --with-deps --only-shell chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8080
# One worker on purpose: run state (the one-at-a-time lock, live escalations) lives in-process.
CMD gunicorn -w 1 --threads 8 --timeout 180 -b 0.0.0.0:${PORT:-8080} demo.server:app
