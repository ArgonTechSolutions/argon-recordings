FROM node@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1
ENV TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 RECORDINGS_DATA=/data RECORDINGS_STATIC=/app/static
WORKDIR /app
COPY backend/requirements.txt backend/requirements-embeddings.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-embeddings.txt && pip check
COPY backend/argon_recordings ./argon_recordings
COPY --from=web /web/dist ./static
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
CMD ["uvicorn", "argon_recordings.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
