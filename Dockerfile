FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates && rm -rf /var/lib/apt/lists/*
ARG GO2RTC_VERSION=1.9.14
RUN curl -fsSL -o /usr/local/bin/go2rtc https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64 && chmod +x /usr/local/bin/go2rtc
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DATA_DIR=/data DB_PATH=/data/cameras.db PORT=8000 PIPE_CMD="python3 -m app.pipe"
EXPOSE 8000
CMD ["sh", "bin/start.sh"]
