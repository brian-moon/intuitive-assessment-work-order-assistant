# One image: the React front end is built in the first stage and served by the API in the second.
FROM node:20-alpine AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app/ app/
COPY data/ data/
COPY evals/ evals/
COPY reports/ reports/
RUN mkdir -p branding
COPY --from=web /web/dist frontend/dist
RUN useradd --uid 10001 --no-create-home appuser && chown -R appuser /app/reports
USER appuser
EXPOSE 8790
HEALTHCHECK --interval=5s --timeout=3s --retries=20 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8790"]
