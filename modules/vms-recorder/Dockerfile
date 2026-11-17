FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk
WORKDIR /app
COPY modules/vms-recorder/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
