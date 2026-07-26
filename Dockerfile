FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY start.sh ./

RUN pip install --no-cache-dir -e . && chmod +x start.sh

EXPOSE 8000
CMD ["./start.sh"]
