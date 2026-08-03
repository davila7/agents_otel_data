"""Pydantic AI agent using an MCP server, traced to LangSmith via OTLP.

Same scenario as logfire/03_mcp.py and langfuse/03_mcp.py: connects to the
`mcp-server-time` MCP server over stdio; the trace shows the agent run,
model requests, and the MCP tool calls.
"""

import asyncio

import otel_setup

otel_setup.configure(service_name='mcp-example')

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


async def main():
    async with agent:
        result = await agent.run(
            'What time is it right now in Santiago de Chile and in Tokyo? '
            'What is the time difference between them?'
        )
    print(result.output)


asyncio.run(main())
otel_setup.shutdown()
