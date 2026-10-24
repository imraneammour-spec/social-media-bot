FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000 POSTENZA_DATA_DIR=/app/data POSTENZA_EMBEDDED_SCHEDULER=false
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip && python -m pip install --no-cache-dir -r requirements.txt
RUN groupadd --gid 10001 postenza && useradd --uid 10001 --gid 10001 --no-create-home postenza && mkdir -p /app/data && chown postenza:postenza /app/data
COPY --chown=postenza:postenza . .
USER postenza
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)"
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
