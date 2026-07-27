# Logfire (Pydantic)

Examples of AI agent observability with [Pydantic Logfire](https://logfire.pydantic.dev), sending traces via OpenTelemetry. Each example is a common agent pattern, so the same scenarios can be replicated with other providers (Langfuse, LangSmith).

## Examples

| File | Pattern | What Logfire shows |
|------|---------|--------------------|
| `01_messages.py` | Direct Anthropic API call | LLM span: prompt, response, model, tokens, cost, latency + system metrics |
| `02_tools.py` | Pydantic AI agent with tool calling | Agent run span → model requests → tool calls (`get_weather`, `get_currency`) with args and results |
| `03_mcp.py` | Pydantic AI agent with an MCP server | Agent run + MCP protocol spans (initialize, list_tools, call_tool) against `mcp-server-time` |

Each example uses its own `service_name` (`messages-example`, `tools-example`, `mcp-example`) so you can filter them in the dashboard.

## Setup

```bash
cd logfire
uv sync

# Authenticate with Logfire (opens OAuth in the browser, no API key required)
uv run logfire auth

# Link this folder to your Logfire project
uv run logfire projects use --org 'davila7' 'starter-project'
```

Put your Anthropic key in `.env` (gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
set -a && source .env && set +a
uv run 01_messages.py
uv run 02_tools.py
uv run 03_mcp.py
```

Then open the [Logfire dashboard](https://logfire-us.pydantic.dev/davila7/starter-project) → Live view.

## Instrumentation used

- `logfire.instrument_anthropic()` — traces raw Anthropic API calls.
- `logfire.instrument_pydantic_ai()` — traces Pydantic AI agent runs, model requests, and tool calls.
- `logfire.instrument_mcp()` — traces MCP protocol messages (requests/responses to the server).
- `logfire.instrument_system_metrics()` — host CPU, memory, disk, network.

## Notes

- `cryptography` is pinned `<45` (in `pyproject.toml` and in the `uvx` invocation inside `03_mcp.py`) because newer versions require a rust toolchain to build from source on Intel macOS.
