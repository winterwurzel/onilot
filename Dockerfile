FROM python:3.13-alpine

LABEL org.opencontainers.image.title="onilot" \
      org.opencontainers.image.description="Self-hosted backend for Tolino e-readers (patched firmware 5.x)" \
      org.opencontainers.image.source="https://github.com/winterwurzel/onilot" \
      org.opencontainers.image.licenses="MIT"

RUN addgroup -S -g 1000 onilot && adduser -S -u 1000 -G onilot -H onilot \
    && mkdir -p /library /data && chown onilot:onilot /library /data

WORKDIR /app
COPY onilot/ /app/onilot/

ENV ONILOT_LIBRARY=/library \
    ONILOT_DATA=/data \
    ONILOT_BIND=0.0.0.0 \
    ONILOT_PORT=8095 \
    PYTHONUNBUFFERED=1

USER onilot
VOLUME ["/library", "/data"]
EXPOSE 8095

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('ONILOT_PORT', '8095'), timeout=4)"

ENTRYPOINT ["python", "-m", "onilot"]
