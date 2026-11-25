FROM node:22-bookworm-slim AS javascript
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-open-sans fonts-liberation && rm -rf /var/lib/apt/lists/*
COPY --from=javascript /usr/local/bin/node /usr/local/bin/node
WORKDIR /studio
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app app
ENV STUDIO_ENV=production STUDIO_DATA_DIR=/data STUDIO_MAX_UPLOAD_BYTES=67108864
ENV STUDIO_KEY_SHA256=83fcdb6b235f2775b4dc7b7b28bd0a56eac2e70797df98707e0f4fbe4aef73ad
EXPOSE 8080
CMD ["sh","-c","exec uvicorn app.server:app --host 0.0.0.0 --port \"${PORT:-8080}\" --workers 1"]
