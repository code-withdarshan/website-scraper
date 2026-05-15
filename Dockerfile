FROM python:3.12-slim

WORKDIR /app

# System deps (libxml etc. not needed since we use html.parser)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app.py scraper.py ./
COPY templates ./templates
COPY static ./static

ENV PORT=5000
EXPOSE 5000

# Production server (waitress)
CMD ["python", "app.py"]
