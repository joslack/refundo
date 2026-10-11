"""Start the served agent on this machine, with the records of each demo account and the review page.

    uv run --env-file .env python service/run.py            start everything; Ctrl-C stops the two servers
    uv run --env-file .env python service/run.py --trace    the same, and each run is traced in LangSmith
    uv run python service/run.py down                       remove the accounts' containers and their records

It starts four things:
    the records   Postgres with one database per account in service/accounts.py, and one MCP server per account.
                  These are the image and the seed an evaluation task uses, so evals/build.py has to have run once.
    the agent     a LangGraph development server on port 2024 serving service/agent.py, with one assistant per account
    the page      service/review.py on port 8501: send a request, and review what waits for a person
    the chat      LangChain's Agent Chat UI on port 3000, fetched into service/chat-ui the first time. It needs Node.

Without --trace nothing is sent to LangSmith, whatever .env says.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from langgraph_sdk import get_sync_client

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "service"))
from accounts import ACCOUNTS, FIRST_PORT, GRAPH, SCENARIOS, SERVER, assistant_id, configurable, database  # noqa: E402

from world.sql import seed_sql  # noqa: E402

PROJECT = "refundo-service"
SERVE = ["uv", "run", "--with", "langgraph-cli[inmem]", "--with", "langchain-fireworks", "--with", "langchain-mcp-adapters"]
CHAT_UI = ROOT / "service/chat-ui"
CHAT_UI_COMMIT = "28cfb43a62b7179ed4065dd761e16ece8c29d983"  # github.com/langchain-ai/agent-chat-ui on 2026-10-07
PNPM = ["npx", "-y", "pnpm@10"]


def compose_file() -> str:
    """Postgres, and one MCP server per account over that account's database."""
    servers = "".join(f"""
  mcp-{database(a).replace("_", "-")}:
    image: refundo-mcp
    environment: {{DATABASE_URL: "postgresql://postgres:refundo@postgres:5432/{database(a)}"}}
    depends_on: {{postgres: {{condition: service_healthy}}}}
    ports: ["127.0.0.1:{FIRST_PORT + n}:8000"]""" for n, a in enumerate(ACCOUNTS))
    return f"""services:
  postgres:
    image: postgres:16-alpine
    environment: {{POSTGRES_PASSWORD: refundo}}
    healthcheck: {{test: ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"], interval: 2s, timeout: 5s, retries: 30}}{servers}
"""


def compose(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run docker compose with the file above, which it reads from its input."""
    return subprocess.run(["docker", "compose", "-p", PROJECT, "-f", "-", *args], input=compose_file(), text=True,
                          check=True, **kwargs)


def psql(sql: str, into: str = "postgres") -> str:
    """Run SQL in the Postgres container and give back what it printed."""
    done = subprocess.run(["docker", "exec", "-i", f"{PROJECT}-postgres-1", "psql", "-U", "postgres", "-d", into, "-qtA",
                           "-v", "ON_ERROR_STOP=1"], input=sql, text=True, check=True, capture_output=True)
    return done.stdout


def start_records() -> None:
    """Each account's database is made once. An account that has one keeps it, with the proposals recorded in it."""
    compose("up", "-d", "--wait", "postgres", capture_output=True)
    have = set(psql("SELECT datname FROM pg_database").split())
    for account in ACCOUNTS:
        if database(account) not in have:
            psql(f"CREATE DATABASE {database(account)}")
            psql(seed_sql(SCENARIOS[account].world, SCENARIOS[account].request), into=database(account))
    compose("up", "-d", capture_output=True)


def fetch_chat() -> None:
    """LangChain's Agent Chat UI at one commit, in service/chat-ui, which is not part of this repository, with
    service/chat-ui.patch on top. The patch shows each tool call as one row with its result, shows submit_proposal
    as a proposal, and reads a dollar sign as a dollar sign and not as the start of a formula."""
    if not CHAT_UI.exists():
        print("Fetching Agent Chat UI, the first time only...", flush=True)
        subprocess.run(["git", "clone", "-q", "https://github.com/langchain-ai/agent-chat-ui", str(CHAT_UI)], check=True)
    subprocess.run(["git", "checkout", "-q", "--force", CHAT_UI_COMMIT], cwd=CHAT_UI, check=True)
    subprocess.run(["git", "apply", str(ROOT / "service/chat-ui.patch")], cwd=CHAT_UI, check=True)
    if not (CHAT_UI / "node_modules").exists():
        subprocess.run([*PNPM, "install"], cwd=CHAT_UI, check=True, capture_output=True)


def register_assistants() -> None:
    """One assistant per account. An assistant that exists keeps its review setting."""
    client = get_sync_client(url=SERVER)
    for _ in range(120):
        try:
            client.assistants.search(limit=1)
            break
        except Exception:  # the server is still starting
            time.sleep(0.5)
    else:
        sys.exit(f"The agent's server did not answer on {SERVER}.")
    have = {a["assistant_id"]: a for a in client.assistants.search(graph_id=GRAPH, limit=100)}
    for account in ACCOUNTS:
        if assistant_id(account) not in have:
            client.assistants.create(GRAPH, assistant_id=assistant_id(account), name=account,
                                     config={"configurable": configurable(account, "all")})


if __name__ == "__main__":
    if sys.argv[1:] == ["down"]:
        compose("down", "-v")
        shutil.rmtree(ROOT / "service/.langgraph_api", ignore_errors=True)  # the threads, which point at those records
        sys.exit()
    if not (ROOT / "agents/policy.md").exists():  # evals/build.py copies it too; the agents read it from there
        shutil.copy(ROOT / "docs/policy.md", ROOT / "agents/policy.md")
    fetch_chat()
    print("Starting the accounts' records...", flush=True)
    start_records()
    env = os.environ | {"LANGSMITH_TRACING": "true" if "--trace" in sys.argv else "false", "LANGSMITH_PROJECT": "Refundo service"}
    # The server reads the paths in langgraph.json from the folder it starts in, and keeps its threads there.
    agent = subprocess.Popen([*SERVE, "langgraph", "dev", "--no-browser", "--port", "2024"], cwd=ROOT / "service", env=env)
    register_assistants()
    page = subprocess.Popen(["uv", "run", "streamlit", "run", "service/review.py", "--server.port", "8501",
                             "--server.headless", "true"], cwd=ROOT, env=env)
    chat = subprocess.Popen([*PNPM, "dev", "--port", "3000"], cwd=CHAT_UI, stdout=subprocess.DEVNULL, start_new_session=True)
    print(f"\nThe review page: http://localhost:8501\nThe chat: opened from the review page, one link per account\n"
          f"The agent's server: {SERVER}\n", flush=True)
    try:
        while agent.poll() is None and page.poll() is None and chat.poll() is None:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        agent.terminate(), page.terminate()
        os.killpg(chat.pid, signal.SIGTERM)  # npx starts the chat's server as a child of its own
