FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app

COPY mock/ mock/
USER app

EXPOSE 8081
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=2)"

CMD ["uvicorn", "mock.app:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log", "--timeout-keep-alive", "75"]
