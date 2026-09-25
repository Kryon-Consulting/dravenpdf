"""Prometheus metrics, on a registry per app (so tests can build many apps)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from dravenpdf.render.pool import BrowserPool


class _PoolCollector(Collector):
    def __init__(self, get_pool: Callable[[], BrowserPool | None]) -> None:
        self._get_pool = get_pool

    def collect(self) -> Iterator[Metric]:
        pool = self._get_pool()
        if pool is None:
            return
        for name, doc, value in (
            ("dravenpdf_renders_active", "Renders running now.", pool.active),
            ("dravenpdf_renders_waiting", "Renders waiting for a browser slot.", pool.waiting),
            ("dravenpdf_renders_total", "Renders finished since start.", pool.renders),
            ("dravenpdf_browser_launches_total", "Chromium launches.", pool.launches),
            (
                "dravenpdf_browser_restarts_total",
                "Chromium relaunches after a crash.",
                pool.restarts,
            ),
        ):
            yield GaugeMetricFamily(name, doc, value=value)


class Metrics:
    def __init__(self, get_pool: Callable[[], BrowserPool | None]) -> None:
        self.registry = CollectorRegistry()
        self.render_seconds = Histogram(
            "dravenpdf_render_seconds",
            "Time to render a PDF, by source.",
            ["source"],
            registry=self.registry,
            buckets=(0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120),
        )
        self.errors = Counter(
            "dravenpdf_errors", "Error responses, by error code.", ["code"], registry=self.registry
        )
        self.registry.register(_PoolCollector(get_pool))
