"""Shared OTEL configuration: export traces to LangSmith via OTLP.

LangSmith ingests standard OpenTelemetry traces, per
https://docs.langchain.com/langsmith/trace-with-opentelemetry

Required env vars (put them in .env):
    LANGSMITH_API_KEY=lsv2_...
    LANGSMITH_PROJECT=agents-otel-data      # optional, defaults to "default"
    LANGSMITH_ENDPOINT=https://api.smith.langchain.com   # optional
"""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_provider: TracerProvider | None = None


def configure(service_name: str) -> None:
    global _provider
    endpoint = os.environ.get('LANGSMITH_ENDPOINT', 'https://api.smith.langchain.com')
    project = os.environ.get('LANGSMITH_PROJECT', 'default')

    exporter = OTLPSpanExporter(
        endpoint=f'{endpoint}/otel/v1/traces',
        headers={
            'x-api-key': os.environ['LANGSMITH_API_KEY'],
            'Langsmith-Project': project,
        },
        timeout=10,
    )
    _provider = TracerProvider(resource=Resource.create({'service.name': service_name}))
    _provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(_provider)


def shutdown() -> None:
    """Flush pending spans before the script exits."""
    if _provider is not None:
        _provider.shutdown()
