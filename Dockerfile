# Portable image -- builds and runs identically on Mac, Windows, or a
# cloud host, as long as Docker is installed.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Most hosts (Render, Fly.io, Railway, etc.) inject PORT themselves;
# 8000 is just the local default.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py"]
