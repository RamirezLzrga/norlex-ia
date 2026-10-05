# Este Dockerfile es el que usa Render (runtime: docker). Es obligatorio:
# pyodbc necesita el driver ODBC de SQL Server instalado en el sistema.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Driver ODBC 18 de SQL Server, por la via que documenta Microsoft.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
       | tee /etc/apt/trusted.gpg.d/microsoft.asc > /dev/null \
    && curl -fsSL https://packages.microsoft.com/config/debian/12/prod.list \
       | tee /etc/apt/sources.list.d/mssql-release.list > /dev/null \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 norlex \
    && chown -R norlex:norlex /srv
USER norlex

# Render define $PORT; 8099 solo sirve para docker run local.
ENV PORT=8099
EXPOSE 8099

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
