FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk tzdata python-multipart
WORKDIR /app
COPY modules/assistant/ .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
