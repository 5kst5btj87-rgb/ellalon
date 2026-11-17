FROM python:3.12-slim
COPY sdk/python /sdk
RUN pip install --no-cache-dir /sdk "pyrisco>=0.6,<1"
WORKDIR /app
COPY modules/integrations/app.py modules/integrations/adapters.py ./
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
