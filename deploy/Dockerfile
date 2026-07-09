FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 mailsync \
    && useradd --uid 10001 --gid mailsync --create-home mailsync \
    && mkdir -p /app/data /app/logs /data \
    && chown -R mailsync:mailsync /app /data

COPY --chown=mailsync:mailsync \
    backend/mail_sync.py \
    /app/

COPY --chown=mailsync:mailsync \
    backend/src \
    /app/src

COPY --chown=mailsync:mailsync \
    backend/html \
    /app/html

ENV PYTHONPATH=/app/src

USER mailsync

EXPOSE 8080

ENTRYPOINT ["python3", "-u", "/app/mail_sync.py"]
CMD ["watch"]
