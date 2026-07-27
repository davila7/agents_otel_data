# Langfuse

Same three agent observability scenarios as [`../logfire`](../logfire), sending traces to [Langfuse](https://langfuse.com) using the official integrations:

- **Langfuse Python SDK** (`get_client()`) as the OpenTelemetry backend, with `flush()` before exit.
- **`AnthropicInstrumentor`** (OpenTelemetry) for raw Anthropic SDK calls.
- **Pydantic AI's native OTEL instrumentation** (`Agent.instrument_all()`) for agents, tools, and MCP.
- Each run is wrapped in a named root span (`chat-hello`, `travel-assistant`, `time-assistant-mcp`) with explicit trace input/output and tags, per [Langfuse best practices](https://langfuse.com/docs/observability/best-practices).

## Examples

| File | Pattern |
|------|---------|
| `01_messages.py` | Direct Anthropic API call |
| `02_tools.py` | Pydantic AI agent with tool calling (`get_weather`, `get_currency`) |
| `03_mcp.py` | Pydantic AI agent with an MCP server (`mcp-server-time` over stdio) |

## Setup

```bash
cd langfuse
uv sync
```

Create a project at [cloud.langfuse.com](https://cloud.langfuse.com) and get API keys from **Project Settings → API Keys**. Put them in `.env` (gitignored):

```
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://us.cloud.langfuse.com   # US; EU is https://cloud.langfuse.com
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
set -a && source .env && set +a
uv run 01_messages.py
uv run 02_tools.py
uv run 03_mcp.py
```

Then check **Tracing → Traces** in the Langfuse dashboard.

## Notes

- `cryptography` is pinned `<45` because newer versions require a rust toolchain to build from source on Intel macOS.
