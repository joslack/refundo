FROM python:3.12-slim

RUN pip install --no-cache-dir fastmcp==4.0.11 asyncpg==0.31.0

WORKDIR /app
COPY server.py .

CMD ["python", "server.py"]
