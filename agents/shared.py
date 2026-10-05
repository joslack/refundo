"""What every agent graph here does the same way: choose the model for a run, take its tools from the MCP server,
and read the policy.

Harbor calls a graph's make_graph with the run's config. The model name, any model settings and the MCP servers
the task declares arrive in config["configurable"]; Harbor does not connect to the servers itself.
"""

from pathlib import Path

from langchain.chat_models import init_chat_model
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain_mcp_adapters.client import MultiServerMCPClient

DEFAULT_MODEL = "openai:gpt-6-luna"  # used when the run names no model
REQUEST_TIMEOUT = 180  # seconds for one model call; a call that hangs is then retried instead of stalling the run
RETRIES = 8  # per model call; providers limit tokens per minute, and a refused call succeeds a moment later


def chat_model(configurable: dict):
    """The model for this run. A run may pass settings such as reasoning_effort in model_kwargs."""
    name = configurable.get("model", DEFAULT_MODEL)
    settings = {"timeout": REQUEST_TIMEOUT, "max_retries": RETRIES} | dict(configurable.get("model_kwargs") or {})
    if pace := configurable.get("calls_per_minute"):
        # Set by the runner only for models that would pass the provider's tokens-per-minute limit: this trial's
        # share of that limit. The bucket starts full and holds half a minute of calls, so a call waits only once
        # the trial has been calling faster than its share. A wait happens inside the model call and shows as
        # model time in the trace.
        limiter = InMemoryRateLimiter(requests_per_second=pace / 60, check_every_n_seconds=0.1, max_bucket_size=pace / 2)
        limiter.available_tokens = pace / 2
        settings["rate_limiter"] = limiter
    if name.startswith("openai:"):
        # Some OpenAI models accept tools only on the Responses API, so every OpenAI model goes through it.
        settings.setdefault("use_responses_api", True)
    return init_chat_model(name, **settings)


async def tools_for(configurable: dict, wanted: set[str]) -> list:
    """The named tools from the task's MCP server. The server offers every design's tools; a graph gets only its own."""
    servers = {s["name"]: {"transport": s["transport"].replace("-", "_"), "url": s["url"]}
               for s in configurable["mcp_servers"]}
    offered = {t.name: t for t in await MultiServerMCPClient(servers).get_tools()}
    if missing := wanted - offered.keys():
        raise RuntimeError(f"The MCP server does not offer {sorted(missing)}; this agent and the server are out of step.")
    return [offered[name] for name in sorted(wanted)]


def policy() -> str:
    return (Path(__file__).parent / "policy.md").read_text()
