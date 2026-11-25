# Same image as backend/Dockerfile, at the repo root for Nobus Cube (build from GitHub).
FROM python:3.12-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
COPY tools/ ./tools/
ENV PORT=8000
EXPOSE 8000
# One worker: live reader links are kept in memory.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips '*'"]
