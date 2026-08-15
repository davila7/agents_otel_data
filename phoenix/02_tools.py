"""Pydantic AI agent with tool calling, traced to Arize Phoenix.

Uses Pydantic AI's native OpenTelemetry instrumentation (Agent.instrument_all)
with arize-phoenix-otel providing the global tracer provider, following
https://arize.com/docs/phoenix/integrations/frameworks/pydantic-ai

Same scenario as logfire/02_tools.py: the agent decides which tools to call;
the trace shows the agent run, model requests, and tool calls with arguments
and results.
"""

import os

os.environ.setdefault('OTEL_SERVICE_NAME', 'tools-example')

from phoenix.otel import register

tracer_provider = register(
    project_name=os.environ.get('PHOENIX_PROJECT_NAME', 'agents-otel-data'),
    set_global_tracer_provider=True,
)

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


QUESTION = (
    'I am visiting Santiago and Lima next week. '
    'What is the weather like in each, and what currency should I bring?'
)

tracer = tracer_provider.get_tracer(__name__)

with tracer.start_as_current_span(
    'travel-assistant',
    openinference_span_kind='agent',
) as span:
    span.set_input(QUESTION)
    result = agent.run_sync(QUESTION)
    span.set_output(result.output)

print(result.output)
tracer_provider.force_flush()
