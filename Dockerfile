# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN VITE_USE_MOCK=false VITE_API_URL=/api npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAREER_QUEST_ENV=production \
    CAREER_QUEST_STATE_PATH=/var/data/state.sqlite3 \
    PORT=8000
# Unprivileged runtime user; only /var/data is writable.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /var/data && chown app:app /var/data
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend/ backend/
COPY --from=frontend /build/frontend/dist/ frontend/dist/
RUN rm -rf backend/tests backend/runtime && python -m compileall -q backend
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/ready',timeout=3)"
CMD ["python", "-m", "backend.web"]
