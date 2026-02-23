FROM python:3.13-slim

RUN apt-get update && apt-get install -y cron && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY crontab /etc/cron.d/my-cron-job
RUN chmod 0644 /etc/cron.d/my-cron-job && \
    crontab /etc/cron.d/my-cron-job && \
    touch /var/log/cron.log

COPY main.py .

CMD ["cron", "-f"]
