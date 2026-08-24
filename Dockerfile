FROM python:3.12-slim

# tzdata so TZ actually resolves - the calendar works in local dates, and a
# container stuck on UTC puts BST evenings on the wrong day.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py contentlist.py ./
COPY static ./static

ENV TVCAL_DB=/data/tvcal.db \
    TVCAL_CONTENT_LIST=/hostdata/.content_list.json \
    PYTHONUNBUFFERED=1

EXPOSE 8087

HEALTHCHECK --interval=60s --timeout=5s --start-period=15s \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8087/api/shows',timeout=4)"

CMD ["gunicorn", "--workers", "1", "--threads", "4", \
     "--bind", "0.0.0.0:8087", "--access-logfile", "-", "app:app"]
