"""Telemetry wiring (ADR-012): JSON log formatting with trace correlation,
and the OTEL gate that keeps exporters off unless an endpoint is configured.

Exporter/processor/instrumentor classes are stubbed out so no export threads
or network connections ever start; the global tracer/meter providers are
still set, which the OTEL API permits once per process."""

import json
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

import bookie_emulator.telemetry as telemetry
from bookie_emulator.config import Settings
from bookie_emulator.telemetry import JsonLogFormatter, configure_logging, configure_telemetry


def make_record(level: int = logging.INFO, exc_info: Any = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="bookie.test", level=level, pathname=__file__, lineno=1, msg="hello %s", args=("world",), exc_info=exc_info
    )


@pytest.fixture
def restore_root_logging() -> Any:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


class TestJsonLogFormatter:
    def test_formats_single_line_json(self) -> None:
        entry = json.loads(JsonLogFormatter().format(make_record()))
        assert entry["level"] == "info"
        assert entry["logger"] == "bookie.test"
        assert entry["message"] == "hello world"
        assert "timestamp" in entry
        assert "exception" not in entry
        assert "trace_id" not in entry  # no active span

    def test_includes_exception_details(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = make_record(level=logging.ERROR, exc_info=sys.exc_info())
        entry = json.loads(JsonLogFormatter().format(record))
        assert entry["level"] == "error"
        assert "ValueError: boom" in entry["exception"]

    def test_correlates_with_the_active_span(self) -> None:
        tracer = TracerProvider().get_tracer("test")
        span = tracer.start_span("fmt")
        with trace.use_span(span, end_on_exit=True):
            entry = json.loads(JsonLogFormatter().format(make_record()))
        ctx = span.get_span_context()
        assert entry["trace_id"] == format(ctx.trace_id, "032x")
        assert entry["span_id"] == format(ctx.span_id, "016x")


class TestConfigureLogging:
    def test_installs_json_handler_at_level(self, restore_root_logging: Any) -> None:
        configure_logging("warning")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
        assert root.level == logging.WARNING


class RecordingInstrumentor:
    instrumented: list[Any] = []

    def instrument(self, **kwargs: Any) -> None:
        RecordingInstrumentor.instrumented.append(kwargs)


class RecordingFastAPIInstrumentor:
    apps: list[FastAPI] = []

    @classmethod
    def instrument_app(cls, app: FastAPI) -> None:
        cls.apps.append(app)


class StubExporter:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


class StubSpanProcessor:
    def __init__(self, exporter: Any) -> None:
        self.exporter = exporter

    def on_start(self, *args: Any, **kwargs: Any) -> None: ...

    def on_end(self, *args: Any, **kwargs: Any) -> None: ...

    def shutdown(self) -> None: ...

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class TestConfigureTelemetry:
    def test_without_endpoint_only_logging_is_configured(
        self, monkeypatch: pytest.MonkeyPatch, restore_root_logging: Any
    ) -> None:
        exporters: list[Any] = []
        monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **kw: exporters.append(kw))
        configure_telemetry(FastAPI(), Settings(_env_file=None, log_level="debug"))
        assert exporters == []  # gate: no exporter construction without an endpoint
        assert isinstance(logging.getLogger().handlers[0].formatter, JsonLogFormatter)

    def test_with_endpoint_wires_providers_and_instrumentors(
        self, monkeypatch: pytest.MonkeyPatch, restore_root_logging: Any
    ) -> None:
        import opentelemetry.instrumentation.fastapi as fastapi_instr
        import opentelemetry.instrumentation.httpx as httpx_instr
        import opentelemetry.instrumentation.sqlalchemy as sqlalchemy_instr
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        span_exporters: list[StubExporter] = []
        metric_exporters: list[StubExporter] = []
        monkeypatch.setattr(
            telemetry, "OTLPSpanExporter", lambda **kw: span_exporters.append(StubExporter(**kw)) or span_exporters[-1]
        )
        monkeypatch.setattr(
            telemetry,
            "OTLPMetricExporter",
            lambda **kw: metric_exporters.append(StubExporter(**kw)) or metric_exporters[-1],
        )
        monkeypatch.setattr(telemetry, "BatchSpanProcessor", StubSpanProcessor)
        monkeypatch.setattr(telemetry, "PeriodicExportingMetricReader", lambda exporter: InMemoryMetricReader())
        monkeypatch.setattr(fastapi_instr, "FastAPIInstrumentor", RecordingFastAPIInstrumentor)
        monkeypatch.setattr(httpx_instr, "HTTPXClientInstrumentor", RecordingInstrumentor)
        monkeypatch.setattr(sqlalchemy_instr, "SQLAlchemyInstrumentor", RecordingInstrumentor)
        RecordingInstrumentor.instrumented = []
        RecordingFastAPIInstrumentor.apps = []

        app = FastAPI()
        settings = Settings(_env_file=None, otel_exporter_otlp_endpoint="collector:4317")
        configure_telemetry(app, settings)

        assert span_exporters[0].kwargs == {"endpoint": "collector:4317", "insecure": True}
        assert metric_exporters[0].kwargs == {"endpoint": "collector:4317", "insecure": True}
        assert RecordingFastAPIInstrumentor.apps == [app]
        # httpx instrumented; sqlalchemy skipped without an engine
        assert RecordingInstrumentor.instrumented == [{}]

    def test_engine_enables_sqlalchemy_instrumentation(
        self, monkeypatch: pytest.MonkeyPatch, restore_root_logging: Any
    ) -> None:
        import opentelemetry.instrumentation.fastapi as fastapi_instr
        import opentelemetry.instrumentation.httpx as httpx_instr
        import opentelemetry.instrumentation.sqlalchemy as sqlalchemy_instr
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        monkeypatch.setattr(telemetry, "OTLPSpanExporter", StubExporter)
        monkeypatch.setattr(telemetry, "OTLPMetricExporter", StubExporter)
        monkeypatch.setattr(telemetry, "BatchSpanProcessor", StubSpanProcessor)
        monkeypatch.setattr(telemetry, "PeriodicExportingMetricReader", lambda exporter: InMemoryMetricReader())
        monkeypatch.setattr(fastapi_instr, "FastAPIInstrumentor", RecordingFastAPIInstrumentor)
        monkeypatch.setattr(httpx_instr, "HTTPXClientInstrumentor", RecordingInstrumentor)
        monkeypatch.setattr(sqlalchemy_instr, "SQLAlchemyInstrumentor", RecordingInstrumentor)
        RecordingInstrumentor.instrumented = []

        class FakeEngine:
            sync_engine = object()

        settings = Settings(_env_file=None, otel_exporter_otlp_endpoint="collector:4317")
        configure_telemetry(FastAPI(), settings, engine=FakeEngine())  # type: ignore[arg-type]

        assert {} in RecordingInstrumentor.instrumented  # httpx
        assert {"engine": FakeEngine.sync_engine} in RecordingInstrumentor.instrumented  # sqlalchemy
