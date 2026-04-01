# ────────────────────────────────────────────────────────────────────
# Stage 1: Build the Vite/TypeScript frontend
# ────────────────────────────────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci --silent

COPY frontend/ ./
# Build produces frontend/dist/ which the Python backend will serve
RUN npm run build

# ────────────────────────────────────────────────────────────────────
# Stage 2: Python backend (FastAPI + uvicorn)
# ────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System deps (curl for healthchecks, ca-certs for HTTPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all Python source files
COPY *.py ./
COPY templates/ ./templates/
COPY data/ ./data/

# Copy built frontend into place so server.py serves it at /
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# Ensure data dir exists for SQLite / usage logs
RUN mkdir -p /app/data

# Expose backend port
EXPOSE 8340

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8340/api/health || exit 1

# Run the server
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8340"]
