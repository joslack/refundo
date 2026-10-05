# The agent's container, built once as refundo-main. Harbor installs the LangGraph project into it at the
# start of every trial. The same installs are done once here, in the same order, so that step finds every
# package already downloaded and never goes to the network: with 16 trials at once it was taking over
# half a minute per trial. fastmcp is for the reference solution, which runs in this container on oracle runs.
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
    && uv pip install langgraph python-dotenv \
    && for dep in $AGENT_DEPS; do uv pip install --prerelease=if-necessary "$dep"; done \
    && rm -rf /tmp/warm
ENV UV_OFFLINE=1

WORKDIR /app
