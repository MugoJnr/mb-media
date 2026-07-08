FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl ca-certificates unzip && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"
ENV DENO_DIR=/tmp/deno

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Optional shared YouTube cookies for the whole service (Netscape cookies.txt).
# Mounted/created at runtime via env YTDLP_COOKIES_B64 or uploaded file path.
RUN mkdir -p /app/cookies /app/downloads

ENV PORT=5000
EXPOSE 5000

CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 1 --keep-alive 5"]
