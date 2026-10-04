# FamilyPulse API + worker image (same image, different command).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir uv==0.8.17

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY start.sh ./start.sh

RUN useradd --system --uid 10001 familypulse && chown -R familypulse /app
USER familypulse
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if os.environ.get('FP_ROLE')=='worker' or urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=3).status == 200 else 1)"
CMD ["./start.sh"]
