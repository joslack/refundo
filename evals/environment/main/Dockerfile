# The agent's container, built once as refundo-main. Harbor installs the LangGraph project into it at the
# start of every trial; the same packages are installed once here, so that step finds them already
# downloaded. fastmcp is for the reference solution, which runs in this container on oracle runs.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastmcp==4.0.11

# evals/build.py passes the dependency list from agents/langgraph.json.
ARG AGENT_DEPS
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && . "$HOME/.local/bin/env" \
    && uv python install 3.12 \
    && uv venv /tmp/warm --python 3.12 \
    && . /tmp/warm/bin/activate \
    && uv pip install --prerelease=if-necessary langgraph python-dotenv $AGENT_DEPS \
    && rm -rf /tmp/warm

WORKDIR /app
