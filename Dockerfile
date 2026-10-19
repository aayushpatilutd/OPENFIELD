FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libffi-dev \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The database and filed documents must outlive the container.
VOLUME ["/data"]
ENV INVOICEHUB_DB=/data/invoicehub.db \
    INVOICEHUB_STORAGE=/data/storage

EXPOSE 5100

# Shell form, not exec form: Render (and most PaaS) inject the port as $PORT at
# runtime, and the JSON exec form does not expand variables — the container
# would bind 5100, Render would health-check the port it assigned, find
# nothing, and kill the deploy.
#
# One worker on purpose: APScheduler runs in-process, so a second worker runs
# every scheduled agent twice.
CMD gunicorn --bind "0.0.0.0:${PORT:-5100}" --workers 1 --threads 8 \
             --timeout 300 --access-logfile - wsgi:app
