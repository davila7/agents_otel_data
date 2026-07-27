"""Pydantic AI agent with tool calling, traced by Logfire.

The agent decides which tools to call; Logfire shows the full agent run:
model requests, tool calls with arguments and results, and the final answer.
"""

import logfire
from pydantic_ai import Agent, RunContext

logfire.configure(service_name='tools-example')
logfire.instrument_pydantic_ai()

agent = Agent(
    'anthropic:claude-sonnet-4-5',
    system_prompt='You are a helpful travel assistant. Use the tools to answer.',
)

CITY_WEATHER = {
    'santiago': ('sunny', 24),
    'buenos aires': ('cloudy', 18),
    'lima': ('foggy', 16),
}

CITY_CURRENCY = {
    'santiago': 'CLP',
    'buenos aires': 'ARS',
    'lima': 'PEN',
}


@agent.tool
def get_weather(ctx: RunContext[None], city: str) -> str:
    """Get the current weather for a city."""
    condition, temp = CITY_WEATHER.get(city.lower(), ('unknown', 0))
    return f'{condition}, {temp}°C'


@agent.tool
def get_currency(ctx: RunContext[None], city: str) -> str:
    """Get the local currency code for a city."""
    return CITY_CURRENCY.get(city.lower(), 'unknown')


result = agent.run_sync(
    'I am visiting Santiago and Lima next week. '
    'What is the weather like in each, and what currency should I bring?'
)
print(result.output)
