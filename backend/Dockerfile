FROM python:3.12-slim

# Install system dependencies including Tor for IP rotation
RUN apt-get update && \
    apt-get install -y --no-install-recommends megatools curl tor && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Download Psiphon binary for free IP rotation
RUN curl -L -o /usr/local/bin/psiphon-tunnel-core \
    https://raw.githubusercontent.com/Psiphon-Labs/psiphon-tunnel-core-binaries/master/linux/psiphon-tunnel-core-x86_64 && \
    chmod +x /usr/local/bin/psiphon-tunnel-core && \
    mkdir -p /tmp/psiphon_datastore

RUN megadl --version || true

WORKDIR /app

# Install poetry
RUN pip install --no-cache-dir poetry

# Copy dependency files
COPY pyproject.toml poetry.lock* ./

# Install dependencies (no virtualenv in Docker)
RUN poetry config virtualenvs.create false && \
    poetry install --no-interaction --no-ansi --no-root

# Copy application code
COPY . .

# Create data directory for SQLite
RUN mkdir -p /data

# Expose port
EXPOSE 10000

# Start the app (Render uses port 10000 by default)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
