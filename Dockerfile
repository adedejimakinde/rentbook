FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# requirements-dev.txt for local development; docker-compose.prod.yml passes
# requirements.txt so production images carry no test or lint tools.
ARG REQUIREMENTS=requirements-dev.txt
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r "$REQUIREMENTS"

COPY . .

RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
