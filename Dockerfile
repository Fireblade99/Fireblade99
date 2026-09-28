FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
COPY qlik_gateway ./qlik_gateway
RUN pip install --no-cache-dir ".[postgres]" && useradd -r -u 10001 gateway
USER gateway
EXPOSE 8080
# API: qlik-gateway api   |   coordinator: qlik-gateway worker
CMD ["qlik-gateway", "api", "--port", "8080", "--workers", "2"]
