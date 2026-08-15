# Phoenix demos

Three traced scenarios written to an Arize Phoenix Cloud space
(`app.phoenix.arize.com/s/dan-avila7`, project `agents-otel-data`):

- `01_messages.py` — direct Anthropic call, OpenInference `AnthropicInstrumentor`.
- `02_tools.py` — Pydantic AI agent with two tools, native OTEL instrumentation.
- `03_mcp.py` — Pydantic AI agent calling `mcp-server-time` over stdio.

## Run

```bash
uv sync
set -a && source .env && set +a
uv run 01_messages.py
```

`.env` (gitignored) needs `ANTHROPIC_API_KEY`, `PHOENIX_API_KEY`,
`PHOENIX_COLLECTOR_ENDPOINT=https://app.phoenix.arize.com/s/dan-avila7`,
`PHOENIX_PROJECT_NAME=agents-otel-data`.
