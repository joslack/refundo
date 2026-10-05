# The verifier's container, built once as refundo-verifier from the repo root. It holds the only copy
# of world/ that a trial uses, and the agent's container never sees it.
FROM python:3.12-slim

RUN pip install --no-cache-dir pydantic

COPY world /opt/refundo/world
COPY evals/environment/verifier/grade.py /opt/refundo/grade.py
COPY evals/environment/verifier/test.sh /tests/test.sh
RUN chmod +x /tests/test.sh

WORKDIR /opt/refundo
