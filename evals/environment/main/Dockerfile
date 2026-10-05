# The agent's container, built once as refundo-main. Harbor installs the LangGraph project into it.
# fastmcp is here for the reference solution, which runs in this container on oracle runs.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastmcp==4.0.11

WORKDIR /app
