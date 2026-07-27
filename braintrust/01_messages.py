"""Direct Anthropic API call, traced to Braintrust.

Uses the native Braintrust SDK integration (init_logger + wrap_anthropic),
following https://www.braintrust.dev/docs/integrations/ai-providers/anthropic

Requires BRAINTRUST_API_KEY in the environment.
"""

import anthropic
from braintrust import init_logger, wrap_anthropic

logger = init_logger(project='agents-otel-data')

client = wrap_anthropic(anthropic.Anthropic())

message = client.messages.create(
    model='claude-sonnet-4-5',
    max_tokens=1024,
    messages=[{'role': 'user', 'content': 'Hello, Braintrust!'}],
)

print(message.content[0].text)
logger.flush()
