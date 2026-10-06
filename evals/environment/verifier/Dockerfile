# The verifier's container, built once as refundo-verifier from the repo root. It holds the only copy
# of world/ that a trial uses, and the agent's container never sees it.
FROM python:3.12-slim

# Everything world/ imports, pinned: see requirements.txt.
COPY evals/environment/verifier/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY world /opt/refundo/world
COPY evals/environment/verifier/grade.py /opt/refundo/grade.py
COPY evals/environment/verifier/test.sh /tests/test.sh
RUN chmod +x /tests/test.sh

WORKDIR /opt/refundo
# A package missing from requirements.txt stops the build here, and not a trial at grading time.
RUN python -c "import world.oracle, world.labeling, world.scenarios"
