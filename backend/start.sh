#!/bin/sh
# Container entrypoint (Railway / any PaaS). FP_ROLE selects the process:
#   api (default): apply migrations (advisory-locked), then serve on $PORT
#   worker:        rule evaluation + notification outbox loop
set -e
case "${FP_ROLE:-api}" in
  worker)
    exec python -m app.worker
    ;;
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" \
      --proxy-headers --forwarded-allow-ips='*'
    ;;
  *)
    echo "Unknown FP_ROLE=${FP_ROLE}" >&2
    exit 1
    ;;
esac
