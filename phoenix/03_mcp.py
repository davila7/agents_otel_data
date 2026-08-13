"""Pydantic AI agent using an MCP server, traced to Arize Phoenix.

Same scenario as logfire/03_mcp.py: connects to the `mcp-server-time` MCP
server over stdio. Pydantic AI's native OTEL instrumentation traces the agent
run, model requests, and the MCP tool calls; arize-phoenix-otel provides the
global tracer provider pointed at the Phoenix Cloud space.
"""

import asyncio
import os

os.environ.setdefault('OTEL_SERVICE_NAME', 'mcp-example')

from phoenix.otel import register

tracer_provider = register(
    project_name=os.environ.get('PHOENIX_PROJECT_NAME', 'agents-otel-data'),
    set_global_tracer_provider=True,
)

from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

Agent.instrument_all()

# cryptography is pinned <45 because newer versions need a rust toolchain
# to build from source on Intel macOS; mcp is pinned <2 because
# mcp-server-time still imports McpError, renamed to MCPError in mcp 2.x
time_server = MCPToolset(
    StdioTransport('uvx', args=['--with', 'cryptography<45', '--with', 'mcp<2', 'mcp-server-time'])
)

agent = Agent(
    'anthropic:claude-sonnet-4-5',
    toolsets=[time_server],
    system_prompt='You are a helpful assistant with access to time tools.',
)

QUESTION = (
    'What time is it right now in Santiago de Chile and in Tokyo? '
    'What is the time difference between them?'
)

tracer = tracer_provider.get_tracer(__name__)


async def main():
    with tracer.start_as_current_span(
        'time-assistant-mcp',
        openinference_span_kind='agent',
    ) as span:
        span.set_input(QUESTION)
        async with agent:
            result = await agent.run(QUESTION)
        span.set_output(result.output)
    print(result.output)


asyncio.run(main())
tracer_provider.force_flush()
