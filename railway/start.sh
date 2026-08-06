#!/usr/bin/env bash
set -euo pipefail

python manage.py migrate --run-syncdb --noinput
python manage.py ensure_v2_schema
python manage.py backfill_management_numbers
python manage.py repair_member_dates
python manage.py bootstrap_admin
python manage.py seed_defaults
python manage.py collectstatic --noinput
python manage.py validate_templates

exec gunicorn config.wsgi:application \
  --bind "0.0.0.0:${PORT:-8000}" \
  --workers "${WEB_CONCURRENCY:-2}" \
  --threads "${GUNICORN_THREADS:-4}" \
  --timeout "${GUNICORN_TIMEOUT:-120}" \
  --access-logfile - \
  --error-logfile -
