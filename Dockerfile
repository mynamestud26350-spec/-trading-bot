FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_webhook.py .

ENV PYTHONUNBUFFERED=1

CMD exec gunicorn bot_webhook:app --bind 0.0.0.0:$PORT
