FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /studio
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
ENV STUDIO_ENV=production STUDIO_DATA_DIR=/data STUDIO_MAX_UPLOAD_BYTES=67108864
VOLUME /data
CMD ["uvicorn","app.server:app","--host","0.0.0.0","--port","8080","--workers","1"]
