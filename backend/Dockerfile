FROM python@sha256:86adf8dbadc3d6e82ee5dd2c74bec2e1c2467cdad47886280501df722372d2e1
ENV TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY backend/requirements-test.txt backend/requirements.txt backend/requirements-embeddings.txt ./
RUN pip install --no-cache-dir -r requirements-test.txt -r requirements-embeddings.txt && pip check
COPY backend/argon_recordings ./argon_recordings
COPY backend/tests ./tests
CMD ["uvicorn", "argon_recordings.api:create_app", "--factory", "--host", "127.0.0.1", "--port", "8000"]
