# The web app as Cloud Run runs it. Settings and keys come from the service's
# environment and Secret Manager at run time; none are built into the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY . .

# Cloud Run sends traffic to $PORT and ends HTTPS in front of the app.
CMD exec uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips="*"
