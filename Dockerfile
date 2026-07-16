FROM python:3.11-slim

# ffmpeg + Deno (yt-dlp JS challenge) + Node (bgutil POT HTTP provider)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg curl ca-certificates unzip git \
      python3 make g++ \
      libcairo2-dev libpango1.0-dev libjpeg-dev libgif-dev librsvg2-dev \
      gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"
ENV DENO_DIR=/tmp/deno
ENV POT_DIR=/opt/bgutil-ytdlp-pot-provider/server
ENV POT_PORT=4416
ENV POT_ENABLED=1
ENV POT_PROVIDER_URL=http://127.0.0.1:4416

# Pin to match bgutil-ytdlp-pot-provider PyPI plugin major/minor.
RUN git clone --depth 1 --branch 1.3.1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      /opt/bgutil-ytdlp-pot-provider && \
    cd /opt/bgutil-ytdlp-pot-provider/server && \
    npm ci && \
    npx tsc && \
    npm prune --omit=dev && \
    npm cache clean --force

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/cookies /app/downloads && \
    chmod +x /app/scripts/start.sh

ENV PORT=5000
EXPOSE 5000

CMD ["sh", "/app/scripts/start.sh"]
