FROM python:3.12-slim

# Unbuffered, prompt logs; no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Fonts for the Pillow-rendered log-feed profile cards: DejaVu for Latin, and
# Noto Sans CJK so (often Japanese) material titles render instead of tofu boxes.
# Pillow's built-in fallback is used if these are ever absent.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

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
