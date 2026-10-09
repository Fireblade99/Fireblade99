FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ANALYZER_CONFIG=/app/config.yaml

# tzdata: report times in the Excel file follow the TZ variable
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Optional corporate root CA (certs/*.crt) for networks that re-sign HTTPS
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
COPY requirements.txt requirements-web.txt ./
# Use an internal mirror if PyPI is not reachable: --build-arg PIP_INDEX_URL=https://nexus/.../simple
ARG PIP_INDEX_URL
RUN pip install -r requirements-web.txt

COPY main.py web.py ./
COPY src ./src

RUN useradd --uid 10001 --create-home analyzer && mkdir -p /data && chown analyzer /data
USER analyzer
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"

CMD ["python", "web.py", "--data-dir", "/data"]
