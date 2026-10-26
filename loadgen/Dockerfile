FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --retries 10 --timeout 60 -r requirements.txt \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /app/results && chown app:app /app/results

COPY loadgen/ loadgen/
USER app

CMD ["python", "-m", "loadgen.generate", "--help"]
