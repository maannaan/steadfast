FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/data && chown app:app /app/data

COPY service/ service/
USER app

EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

# Uvicorn waits for in-flight requests on SIGTERM; the app's lifespan then drains
# in-flight calls (SHUTDOWN_DRAIN_S) so a deploy or restart does not drop calls.
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "75"]
