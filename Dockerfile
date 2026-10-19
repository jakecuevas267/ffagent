# ffagent: human-in-the-loop fantasy football agent. `docker compose run ffagent demo` needs nothing else.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --create-home --uid 1000 app
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY ffagent ./ffagent
COPY playbook ./playbook
COPY docs ./docs
COPY tests ./tests
RUN pip install --no-cache-dir ".[keyring,ssm]" && chown -R app:app /app

USER app
ENV FFAGENT_FIXTURES=/app/tests/fixtures
VOLUME ["/app/data"]
ENTRYPOINT ["ffagent"]
CMD ["demo"]
