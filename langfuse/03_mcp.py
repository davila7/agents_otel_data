"""Pydantic AI agent using an MCP server, traced to Langfuse.

Same scenario as logfire/03_mcp.py: connects to the `mcp-server-time` MCP
server over stdio. Pydantic AI's native OTEL instrumentation traces the agent
run, model requests, and the MCP tool calls.
"""

import asyncio
import os

os.environ.setdefault('OTEL_SERVICE_NAME', 'mcp-example')

from langfuse import get_client, propagate_attributes
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StdioTransport

langfuse = get_client()
assert langfuse.auth_check(), 'Langfuse authentication failed — check .env'

Agent.instrument_all()

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

QUESTION = (
    'What time is it right now in Santiago de Chile and in Tokyo? '
    'What is the time difference between them?'
)


async def main():
    with langfuse.start_as_current_observation(
        as_type='agent', name='time-assistant-mcp', input=QUESTION
    ) as span:
        with propagate_attributes(tags=['example', 'mcp']):
            async with agent:
                result = await agent.run(QUESTION)
        span.update(output=result.output)
    print(result.output)


asyncio.run(main())
langfuse.flush()
