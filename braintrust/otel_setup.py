"""Shared OTEL configuration: export traces to Braintrust via OTLP.

Braintrust ingests standard OpenTelemetry traces and converts LLM calls
into Braintrust LLM spans, per
https://www.braintrust.dev/docs/integrations/opentelemetry

Required env vars (put them in .env):
    BRAINTRUST_API_KEY=sk-...
    BRAINTRUST_PARENT=project_name:agents-otel-data   # optional
    BRAINTRUST_API_URL=https://api.braintrust.dev     # optional; EU is api-eu
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
    api_url = os.environ.get('BRAINTRUST_API_URL', 'https://api.braintrust.dev')
    parent = os.environ.get('BRAINTRUST_PARENT', 'project_name:agents-otel-data')

    exporter = OTLPSpanExporter(
        endpoint=f'{api_url}/otel/v1/traces',
        headers={
            'Authorization': f'Bearer {os.environ["BRAINTRUST_API_KEY"]}',
            'x-bt-parent': parent,
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
