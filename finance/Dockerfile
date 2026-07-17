FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=America/Sao_Paulo

RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN groupadd -g 1000 app && \
    useradd -m -u 1000 -g 1000 -s /bin/bash app

RUN git config --system --add safe.directory /repo

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
USER app

CMD ["python", "main.py"]
