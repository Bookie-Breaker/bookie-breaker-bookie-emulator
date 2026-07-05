"""Structured JSON logging and optional OpenTelemetry wiring (ADR-012).

OTEL exporters are only started when OTEL_EXPORTER_OTLP_ENDPOINT is set, so
local development and tests never open network connections or spawn export
threads.
"""

import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from bookie_emulator import __version__
from bookie_emulator.config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncEngine


class JsonLogFormatter(logging.Formatter):
    """Single-line JSON log records with trace correlation when available."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            entry["trace_id"] = format(ctx.trace_id, "032x")
            entry["span_id"] = format(ctx.span_id, "016x")
        return json.dumps(entry)


def configure_logging(log_level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level.upper())


def configure_telemetry(app: "FastAPI", settings: Settings, engine: "AsyncEngine | None" = None) -> None:
    configure_logging(settings.log_level)

    if settings.otel_exporter_otlp_endpoint is None:
        return

    resource = Resource.create({"service.name": settings.otel_service_name, "service.version": __version__})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True))
    )
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True)
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
