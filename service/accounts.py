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

# The people the chat offers at its sign-in step: three accounts, each a different person in a different role. The
# line under a name is what a support person would say about the account, and it does not give the answer. Each has
# the message its scenario's customer wrote, and Priya has a second one that makes the same request an escalation.
SIGN_IN = {
    "MO-01": {"situation": "Monthly plan that renewed two days ago. Nobody has used the workspace since.", "also": []},
    "BE-10": {"situation": "The September invoice is for 6 seats. The workspace is set to 4.",
              "also": ["Our September invoice is for 6 seats but we only have 4. Please fix it and refund the difference. "
                       "If this isn't sorted out this week you'll be hearing from our lawyer."]},
    "AUTH-03": {"situation": "Wants this month's renewal refunded.", "also": []},
}
ROLES = {"owner": "Owner", "billing_admin": "Billing Admin", "member": "Member"}

GRAPH = "refunds"
SERVER = "http://localhost:2024"
CHAT = "http://localhost:3000"  # LangChain's Agent Chat UI, which service/run.py fetches and runs
FIRST_PORT = 8101
POSTGRES_PORT = 8100  # the served agent and the review page read and write what was issued; the MCP servers do not
REVIEW = {"all": "Every decision", "escalations": "Escalations only"}


def database(account: str) -> str:
    return account.lower().replace("-", "_")


def database_url(account: str) -> str:
    return f"postgresql://postgres:refundo@localhost:{POSTGRES_PORT}/{database(account)}"


def mcp_url(account: str) -> str:
    return f"http://localhost:{FIRST_PORT + ACCOUNTS.index(account)}/mcp"


def assistant_id(account: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"refundo-service:{account}"))


def configurable(account: str, review: str) -> dict:
    return {"account": account, "review": review, "mcp_servers": [{"name": "quillstack", "transport": "streamable-http", "url": mcp_url(account)}]}


def chat_url(account: str) -> str:
    """The chat as support staff see it, with the tool calls and the review card, on any account."""
    return f"{CHAT}/?view=staff&apiUrl={SERVER}&assistantId={assistant_id(account)}"


def customers() -> list[dict]:
    """What the chat's sign-in step shows, read from each account's own records."""
    out = []
    for account, shown in SIGN_IN.items():
        world, request = SCENARIOS[account].world, SCENARIOS[account].request
        person = next(m for m in world.members if m.id == request.requester_user_id)
        out.append({"account": account, "assistantId": assistant_id(account), "apiUrl": SERVER, "name": person.name,
                    "role": ROLES[person.role.value], "workspace": world.workspaces[0].name,
                    "situation": shown["situation"], "messages": [request.message, *shown["also"]]})
    return out
