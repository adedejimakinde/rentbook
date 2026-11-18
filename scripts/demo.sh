#!/usr/bin/env bash
# Start the whole demo: settings, containers, database, CSS, catalogue and demo data.
# Safe to run more than once. Development only: it uses config.settings.dev with DEBUG on.
set -euo pipefail

cd "$(dirname "$0")/.."

DEMO_PASSWORD="demo1234"

random_key() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
    else
        python3 -c "import secrets; print(secrets.token_hex(32))"
    fi
}

# Set KEY=value in .env, replacing an existing line or appending a new one.
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" .env; then
        sed -i "s|^${key}=.*|${key}=${value}|" .env
    else
        printf '%s=%s\n' "$key" "$value" >> .env
    fi
}

if [ ! -f .env ]; then
    echo "Creating .env from .env.example for development..."
    cp .env.example .env
    set_env DJANGO_SETTINGS_MODULE config.settings.dev
    set_env DEBUG True
    set_env SECRET_KEY "$(random_key)"
    set_env ADMIN_URL staff-portal/
else
    echo "Using the existing .env."
fi

# Containers do not inherit this variable, and the dev settings read it from .env.
if [ -n "${CODESPACES:-}" ]; then
    set_env CODESPACES true
fi

echo "Starting containers..."
docker compose up -d

echo "Waiting for the database..."
for _ in $(seq 1 60); do
    if docker compose exec -T db pg_isready -q; then
        break
    fi
    sleep 2
done
docker compose exec -T db pg_isready -q || { echo "The database did not become ready." >&2; exit 1; }

echo "Waiting for the web container..."
for _ in $(seq 1 60); do
    if docker compose exec -T web python -c "import django" >/dev/null 2>&1; then
        break
    fi
    sleep 2
done

manage() {
    docker compose exec -T web python manage.py "$@"
}

echo "Migrating..."
manage migrate --noinput

echo "Building CSS..."
manage tailwind build

echo "Seeding catalogue and demo data..."
manage seed_catalogue
manage seed_demo

admin_url="$(grep '^ADMIN_URL=' .env | cut -d= -f2-)"
admin_url="${admin_url#/}"
if [ -n "${CODESPACE_NAME:-}" ] && [ -n "${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-}" ]; then
    base="https://${CODESPACE_NAME}-8000.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN}"
else
    base="http://localhost:8000"
fi

cat <<INFO

Demo is ready.

  Site:   ${base}/
  Staff:  ${base}/${admin_url}

  Owner login:  owner / ${DEMO_PASSWORD}   (everything)
  Crew login:   crew  / ${DEMO_PASSWORD}   (crew schedule, mark delivered and returned)

In Codespaces, open the Ports tab and click the globe next to port 8000
("Atglace demo") if the browser did not open by itself.
INFO
