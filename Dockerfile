FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /mnt/sharepoint \
    && chown -R appuser:appuser /mnt/sharepoint \
    && printf '* * * * * root cd /app && /usr/local/bin/python -m app.cron_tick >> /proc/1/fd/1 2>> /proc/1/fd/2\n' > /etc/cron.d/sharepoint-sync \
    && chmod 0644 /etc/cron.d/sharepoint-sync

EXPOSE 8000
CMD ["python", "-m", "app.container"]
