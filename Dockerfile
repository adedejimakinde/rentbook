FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Pango, HarfBuzz and fonts are the system libraries WeasyPrint needs to render PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libharfbuzz-subset0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# requirements-dev.txt for local development; docker-compose.prod.yml passes
# requirements.txt so production images carry no test or lint tools.
ARG REQUIREMENTS=requirements-dev.txt
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r "$REQUIREMENTS"

COPY . .

# Build the Tailwind CSS into the image, so containers start without a build
# step or network access. The standalone CLI is downloaded for this step only
# and removed afterwards. The dev settings are used just to run the command.
RUN DJANGO_SETTINGS_MODULE=config.settings.dev python manage.py tailwind build \
    && rm -rf .django_tailwind_cli

RUN useradd --create-home app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]
