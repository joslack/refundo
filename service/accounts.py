"""The accounts the served agent can be tried on, and where each one's records are.

Each account is one scenario of the test world. An account has its own database and its own MCP server over it,
as a task has in an evaluation, and its own assistant on the agent's server. An assistant is the served graph
with one account's MCP server and the review setting in its config, so choosing an account is choosing an assistant.
"""

from uuid import NAMESPACE_URL, uuid5

from world.scenarios import ALL

# Chosen to show each kind of outcome: a refund, a denial, a credit, a part refund, a requester with no right to
# ask, and three escalations.
ACCOUNTS = ["MO-01", "MO-02", "AN-08", "BE-10", "AUTH-03", "BE-09", "ESC-01", "ESC-06", "ESC-09a"]
SCENARIOS = {s.id: s for s in ALL if s.id in ACCOUNTS}

GRAPH = "refunds"
SERVER = "http://localhost:2024"
CHAT = "http://localhost:3000"  # LangChain's Agent Chat UI, which service/run.py fetches and runs
FIRST_PORT = 8101
REVIEW = {"all": "Every decision", "escalations": "Escalations only"}


def database(account: str) -> str:
    return account.lower().replace("-", "_")


def mcp_url(account: str) -> str:
    return f"http://localhost:{FIRST_PORT + ACCOUNTS.index(account)}/mcp"


def assistant_id(account: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"refundo-service:{account}"))


def configurable(account: str, review: str) -> dict:
    return {"review": review, "mcp_servers": [{"name": "quillstack", "transport": "streamable-http", "url": mcp_url(account)}]}


def chat_url(account: str) -> str:
    return f"{CHAT}/?apiUrl={SERVER}&assistantId={assistant_id(account)}"
