FROM python:3.12-slim
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk
WORKDIR /app
COPY modules/my-module/app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
