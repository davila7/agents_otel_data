import anthropic

import logfire

logfire.configure(service_name='messages-example')
logfire.instrument_system_metrics()
logfire.instrument_anthropic()

client = anthropic.Anthropic()
message = client.messages.create(
    model='claude-sonnet-4-5',
    max_tokens=1024,
    messages=[{'role': 'user', 'content': 'Hello, Logfire!'}],
)
print(message.content)
