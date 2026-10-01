# syntax=docker/dockerfile:1

# Stage: builder — ставит зависимости в venv, в финальный образ pip и кэши не попадают
FROM python:3.12-slim-bookworm AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

# Stage: runtime — только venv и код, процесс от непривилегированного пользователя
FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --gid 1001 app && useradd --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY main.py ./
COPY bot ./bot
USER app
CMD ["python", "main.py"]
