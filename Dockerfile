FROM ubuntu:22.04

RUN apt-get update && apt-get install -y curl wget && rm -rf /var/lib/apt/lists/*

# СКАЧИВАЕМ PLAYIT ДЛЯ LINUX X86_64
RUN curl -LO https://github.com/playit-cloud/playit-agent/releases/latest/download/playit-linux-amd64 && \
    chmod +x playit-linux-amd64

# ЗАПУСКАЕМ АГЕНТ
CMD ["/playit-linux-amd64"]
