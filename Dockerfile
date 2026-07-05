FROM python:3.12-slim

# Unbuffered, prompt logs; no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first so this layer caches until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then the app source.
COPY . .

# Run unprivileged; make sure the state dir exists and is writable by that user
# (a named volume mounted here inherits this ownership on first creation).
RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser /app
USER appuser

# Per-guild settings (contest pin, shame, alerts, log feed) live here.
VOLUME ["/app/data"]

# Slash-command-only gateway bot: nothing to EXPOSE.
CMD ["python", "main.py"]
