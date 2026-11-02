"""
OpenTelemetry Instrumentation Helper
====================================
Configures OTLP gRPC trace exporter and W3C trace context propagation.
"""

import os
import logging
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger("telemetry")

def init_tracer(service_name: str) -> trace.Tracer:
    """Initialize OpenTelemetry tracer provider with OTLP gRPC exporter."""
    collector_addr = os.getenv("COLLECTOR_ADDR") or os.getenv("COLLECTOR_SERVICE_ADDR") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    
    resource = Resource.create({SERVICE_NAME: service_name})
    provider = TracerProvider(resource=resource)

    if collector_addr:
        # Strip protocol if present for gRPC exporter
        endpoint = collector_addr.replace("http://", "").replace("https://", "")
        if ":" not in endpoint:
            endpoint = f"{endpoint}:4317"
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            logger.info(f"OpenTelemetry OTLP exporter connected to {endpoint} for {service_name}")
        except Exception as e:
            logger.warning(f"Failed to initialize OTLP exporter: {e}")

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)

def inject_trace_headers(headers: dict = None) -> dict:
    """Inject W3C tracecontext headers into outgoing HTTP request headers."""
    if headers is None:
        headers = {}
    TraceContextTextMapPropagator().inject(headers)
    return headers
