FROM python:3.13-alpine

LABEL org.opencontainers.image.title="dlna-radio-server" \
      org.opencontainers.image.description="Internet radio streams as a DLNA media server, transcoded to MP3 when needed (Yamaha MusicCast favourites)" \
      org.opencontainers.image.source="https://github.com/adorobis/dlna-radio-server" \
      org.opencontainers.image.licenses="MIT"

RUN apk add --no-cache ffmpeg \
    && pip install --no-cache-dir aiohttp==3.12.15 pyyaml==6.0.2
COPY dlna_radio.py /app/dlna_radio.py
COPY stations.example.yaml /app/stations.example.yaml
ENV PYTHONUNBUFFERED=1
VOLUME /config
EXPOSE 8357/tcp 1900/udp
CMD ["python", "/app/dlna_radio.py"]
