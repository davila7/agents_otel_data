# LangSmith

Same three agent observability scenarios as [`../logfire`](../logfire) and [`../langfuse`](../langfuse), sending traces to [LangSmith](https://smith.langchain.com):

- `01_messages.py` uses the **native LangSmith SDK** (`wrap_anthropic` + `@traceable`).
- `02_tools.py` / `03_mcp.py` use **Pydantic AI's native OTEL instrumentation** (`Agent.instrument_all()`) exported via OTLP to LangSmith's OpenTelemetry endpoint (see `otel_setup.py`).

## Examples

| File | Pattern |
|------|---------|
| `01_messages.py` | Direct Anthropic API call |
| `02_tools.py` | Pydantic AI agent with tool calling (`get_weather`, `get_currency`) |
| `03_mcp.py` | Pydantic AI agent with an MCP server (`mcp-server-time` over stdio) |
| `04_claude_agent_sdk.py` | Claude Agent SDK with an in-process MCP server, via LangSmith's native `configure_claude_agent_sdk()` integration |

## Setup

```bash
cd langsmith
uv sync
```

Get an API key from [smith.langchain.com](https://smith.langchain.com) → **Settings → API Keys**. Put it in `.env` (gitignored):

```
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...
LANGSMITH_PROJECT=agents-otel-data
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
set -a && source .env && set +a
uv run 01_messages.py
uv run 02_tools.py
uv run 03_mcp.py
```

Then check the project's **Runs** in the LangSmith dashboard.

## Notes

- `cryptography` is pinned `<45` because newer versions require a rust toolchain to build from source on Intel macOS.
- OTLP endpoint reference: https://docs.langchain.com/langsmith/trace-with-opentelemetry
