# Herald — GitHub activity digest generator.
# Calls the Anthropic Messages API directly (no Node/Claude Code needed).
FROM python:3.12-slim

# git is required at runtime: deep_analysis shallow-clones repos for context.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code. Config and secrets are mounted at runtime (ConfigMap/Secret),
# not baked into the image.
COPY herald.py .
COPY schema/ ./schema/

# Non-root user; owns the writable cache/reports dirs.
RUN useradd --create-home --uid 1000 herald \
    && mkdir -p /app/.cache /app/reports \
    && chown -R herald:herald /app
USER herald

# HERALD_CONFIG points at the mounted config; cache/reports default under /app.
ENV HERALD_CONFIG=/app/config/herald.json \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "herald.py"]
CMD ["digest", "--help"]
