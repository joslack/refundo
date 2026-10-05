# The agent's container. Harbor installs the LangGraph project into it; asyncpg is for the verifier.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir asyncpg==0.31.0

WORKDIR /app
