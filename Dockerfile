# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m venv /opt/venv && /opt/venv/bin/pip install --no-compile .

FROM python:3.13-slim AS runtime
WORKDIR /app
RUN groupadd --system --gid 1001 poi && useradd --system --uid 1001 --gid 1001 poi
COPY --from=build --link /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
USER 1001:1001
EXPOSE 8000
CMD ["uvicorn", "poi_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
