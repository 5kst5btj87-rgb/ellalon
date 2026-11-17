FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk opencv-python-headless==4.10.* numpy
WORKDIR /app
COPY modules/analytics/ .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
