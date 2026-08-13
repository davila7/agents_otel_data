"""Direct Anthropic API call, traced to Arize Phoenix.

Uses arize-phoenix-otel to register the OTLP exporter (Phoenix Cloud space
from PHOENIX_COLLECTOR_ENDPOINT / PHOENIX_API_KEY) plus OpenInference's
AnthropicInstrumentor, following
https://arize.com/docs/phoenix/integrations/llm-providers/anthropic
"""

import os

os.environ.setdefault('OTEL_SERVICE_NAME', 'messages-example')

from openinference.instrumentation.anthropic import AnthropicInstrumentor
from phoenix.otel import register

tracer_provider = register(
    project_name=os.environ.get('PHOENIX_PROJECT_NAME', 'agents-otel-data'),
    set_global_tracer_provider=True,
)
AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)

import anthropic  # imported after instrumentation so the client gets patched

client = anthropic.Anthropic()

tracer = tracer_provider.get_tracer(__name__)

with tracer.start_as_current_span(
    'chat-hello',
    openinference_span_kind='chain',
) as span:
    span.set_input('Hello, Phoenix!')
    message = client.messages.create(
        model='claude-sonnet-4-5',
        max_tokens=1024,
        messages=[{'role': 'user', 'content': 'Hello, Phoenix!'}],
    )
    span.set_output(message.content[0].text)

print(message.content)
tracer_provider.force_flush()
