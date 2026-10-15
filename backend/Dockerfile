FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && python -m spacy download en_core_web_sm

# Run as an unprivileged user, not root: the backend parses untrusted PDFs, and a bug in a parser should not
# hand an attacker root inside the container. /models is created here, owned by that user, so the named volume
# Docker mounts over it starts out writable (the embedding model is downloaded into it once).
RUN useradd --system --uid 10001 --create-home clearcase \
    && mkdir -p /models \
    && chown clearcase:clearcase /models

COPY app ./app
USER clearcase

EXPOSE 8000
# Healthy = the API answers AND MongoDB is reachable (the health endpoint reports status "ok" only then).
# Plain python is used because the slim image has no curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD ["python", "-c", "import json, sys, urllib.request; sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)).get('status') == 'ok' else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
