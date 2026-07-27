"""Claude Agent SDK with an in-process MCP server, traced to LangSmith.

Uses LangSmith's native Claude Agent SDK integration
(langsmith.integrations.claude_agent_sdk.configure_claude_agent_sdk),
following the LangSmith quickstart for the Claude Agent SDK.

Unlike 03_mcp.py (external MCP server over stdio), here the MCP server runs
in-process via create_sdk_mcp_server, and the agent loop is Claude Code's.
"""

import asyncio
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    create_sdk_mcp_server,
    tool,
)
from langsmith.integrations.claude_agent_sdk import configure_claude_agent_sdk

configure_claude_agent_sdk()


@tool(
    'get_weather',
    'Gets the current weather for a given city',
    {'city': str},
)
async def get_weather(args: dict[str, Any]) -> dict[str, Any]:
    city = args['city']
    weather_data = {
        'Santiago': 'Sunny, 24°C',
        'Buenos Aires': 'Cloudy, 18°C',
        'Lima': 'Foggy, 16°C',
        'Tokyo': 'Clear, 20°C',
    }
    weather = weather_data.get(city, 'Weather data not available')
    return {'content': [{'type': 'text', 'text': f'Weather in {city}: {weather}'}]}


async def main() -> None:
    weather_server = create_sdk_mcp_server(
        name='weather',
        version='1.0.0',
        tools=[get_weather],
    )

    options = ClaudeAgentOptions(
        model='claude-sonnet-4-5-20250929',
        system_prompt='You are a friendly travel assistant who helps with weather information.',
        mcp_servers={'weather': weather_server},
        allowed_tools=['mcp__weather__get_weather'],
    )

    async with ClaudeSDKClient(options=options) as client:
        await client.query("What's the weather like in Santiago and Tokyo?")

        async for message in client.receive_response():
            print(message)


if __name__ == '__main__':
    asyncio.run(main())
