"""Pydantic AI agent using an MCP server, traced by Logfire.

Connects to the `mcp-server-time` MCP server over stdio (launched with uvx).
Logfire traces both the agent run and the MCP protocol calls
(initialize, list_tools, call_tool).
"""

import asyncio

import logfire
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

logfire.configure(service_name='mcp-example')
logfire.instrument_pydantic_ai()
logfire.instrument_mcp()

# cryptography is pinned <45 because newer versions need a rust toolchain
# to build from source on Intel macOS
time_server = MCPToolset(
    StdioTransport('uvx', args=['--with', 'cryptography<45', 'mcp-server-time'])
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
