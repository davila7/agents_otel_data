"""Direct Anthropic API call, traced to Langfuse.

Uses the Langfuse Python SDK plus OpenTelemetry's AnthropicInstrumentor,
following https://langfuse.com/integrations/model-providers/anthropic
"""

import os

os.environ.setdefault('OTEL_SERVICE_NAME', 'messages-example')

from langfuse import get_client, propagate_attributes
from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

langfuse = get_client()
assert langfuse.auth_check(), 'Langfuse authentication failed — check .env'

AnthropicInstrumentor().instrument()

import anthropic  # imported after instrumentation so the client gets patched

client = anthropic.Anthropic()

with langfuse.start_as_current_observation(
    as_type='span', name='chat-hello', input='Hello, Langfuse!'
) as span:
    with propagate_attributes(tags=['example', 'messages']):
        message = client.messages.create(
            model='claude-sonnet-4-5',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': 'Hello, Langfuse!'}],
        )
    span.update(output=message.content[0].text)

print(message.content)
langfuse.flush()
