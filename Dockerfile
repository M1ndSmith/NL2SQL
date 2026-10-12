FROM python:3.12-slim

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin appuser

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nl2sql ./nl2sql
COPY config.yaml run.py ./
RUN chmod -R a+rX /app

USER appuser

EXPOSE 8000

ENTRYPOINT ["uvicorn", "nl2sql.api:app", "--host", "0.0.0.0", "--port", "8000"]
