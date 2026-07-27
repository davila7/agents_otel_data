"""Pydantic AI agent with tool calling, traced to LangSmith via OTLP.

Same scenario as logfire/02_tools.py and langfuse/02_tools.py: the agent
decides which tools to call; the trace shows the agent run, model requests,
and tool calls with arguments and results.
"""

import otel_setup

otel_setup.configure(service_name='tools-example')

from pydantic_ai import Agent, RunContext

Agent.instrument_all()

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
otel_setup.shutdown()
