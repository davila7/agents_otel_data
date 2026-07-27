# Braintrust

Same three agent observability scenarios as [`../logfire`](../logfire), [`../langfuse`](../langfuse), and [`../langsmith`](../langsmith), sending traces to [Braintrust](https://www.braintrust.dev):

- `01_messages.py` uses the **native Braintrust SDK** (`init_logger` + `wrap_anthropic`).
- `02_tools.py` / `03_mcp.py` use **Pydantic AI's native OTEL instrumentation** (`Agent.instrument_all()`) exported via OTLP to Braintrust's OpenTelemetry endpoint (see `otel_setup.py`). Braintrust converts the LLM spans into its native `LLM` span type automatically.

## Examples

| File | Pattern |
|------|---------|
| `01_messages.py` | Direct Anthropic API call |
| `02_tools.py` | Pydantic AI agent with tool calling (`get_weather`, `get_currency`) |
| `03_mcp.py` | Pydantic AI agent with an MCP server (`mcp-server-time` over stdio) |

## Setup

```bash
cd braintrust
uv sync
```

Get an API key from [braintrust.dev](https://www.braintrust.dev) → **Settings → API Keys**, and create a project (e.g. `agents-otel-data`). Put them in `.env` (gitignored):

```
BRAINTRUST_API_KEY=sk-...
BRAINTRUST_PARENT=project_name:agents-otel-data
ANTHROPIC_API_KEY=sk-ant-...
```

## Run

```bash
set -a && source .env && set +a
uv run 01_messages.py
uv run 02_tools.py
uv run 03_mcp.py
```

Then check **Logs** in the project on the Braintrust dashboard.

## Notes

- `cryptography` is pinned `<45` because newer versions require a rust toolchain to build from source on Intel macOS.
- OTLP endpoint reference: https://www.braintrust.dev/docs/integrations/opentelemetry — endpoint `https://api.braintrust.dev/otel/v1/traces` with `Authorization: Bearer <key>` and `x-bt-parent: project_name:<name>` headers (EU: `https://api-eu.braintrust.dev`).
