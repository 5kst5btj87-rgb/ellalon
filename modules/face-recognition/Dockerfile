FROM python:3.12-slim
# ENGINE=none            -> API + watchlist only (no inference), tiny image
# ENGINE=insightface     -> CPU inference (onnxruntime)
# ENGINE=insightface-gpu -> CUDA inference (requires nvidia toolkit; set gpu: true in module.yaml)
ARG ENGINE=insightface
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 g++ \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk numpy opencv-python-headless==4.10.* python-multipart
RUN if [ "$ENGINE" = "insightface" ]; then pip install --no-cache-dir insightface onnxruntime; \
    elif [ "$ENGINE" = "insightface-gpu" ]; then pip install --no-cache-dir insightface onnxruntime-gpu; fi
ENV FACE_ENGINE=$ENGINE
WORKDIR /app
COPY modules/face-recognition/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
