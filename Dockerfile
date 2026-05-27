# For Hugging Face Spaces (Docker SDK) or any container host.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Hugging Face Spaces serve on port 7860; PORT overrides locally.
ENV PORT=7860
EXPOSE 7860

# Single worker keeps the in-memory results store consistent; threads handle
# the handful of concurrent raters.
CMD gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:${PORT} app:app
