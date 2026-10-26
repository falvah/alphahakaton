#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker compose build cabinet
docker compose run --rm --no-deps -T \
  -e DATA_DIR=/tmp/alpha-check -e SANDBOX_MODE=true -e DEBUG=false \
  -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/scripts:/app/scripts:ro" \
  -v "$PWD/docs/reference/mlcard:/app/docs/reference/mlcard:ro" \
  --entrypoint sh cabinet -ec '
    python -m pip check
    python manage.py check
    python manage.py makemigrations --check --dry-run
    python manage.py test tests --noinput -v 1
    python scripts/check_http.py
  '
if command -v node >/dev/null 2>&1; then
  node --check static/js/app.js
  node --test tests/frontend.test.cjs
else
  echo "JavaScript tests skipped: install Node.js to run tests/frontend.test.cjs."
fi
