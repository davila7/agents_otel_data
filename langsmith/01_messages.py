"""Direct Anthropic API call, traced to LangSmith.

Uses the native LangSmith SDK integration (wrap_anthropic + @traceable),
following https://docs.langchain.com/langsmith/trace-anthropic

Requires LANGSMITH_TRACING=true and LANGSMITH_API_KEY in the environment.
"""

import anthropic
from langsmith import traceable
from langsmith.wrappers import wrap_anthropic

client = wrap_anthropic(anthropic.Anthropic())


@traceable(name='chat-hello')
def chat(prompt: str) -> str:
    message = client.messages.create(
        model='claude-sonnet-4-5',
        max_tokens=1024,
        messages=[{'role': 'user', 'content': prompt}],
    )
    return message.content[0].text


print(chat('Hello, LangSmith!'))
