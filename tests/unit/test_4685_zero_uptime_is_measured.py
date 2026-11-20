"""A measured 0% uptime and 0 ms latency are readings, not "not measured" (#4685).

`GET /api/monitoring/agents/{name}` and `perform_health_check` built the 24h
figures with a truthiness test, so an agent that was unreachable for the
whole window reported `uptime_percent_24h: null`, the value every consumer
reads as "never measured". The same test turned a measured 0 ms average
latency into `null`.

Pinned here against a real DB, on both call sites:
  * an agent whose only checks in the window are unhealthy reports 0.0;
  * a measured 0 ms average latency reports 0.0;
  * an agent with no checks in the window still reports None for both.

The handlers are called directly, with the same stubs as the #3950 tests.
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


def _seed(db, name, *, aggregate, latencies, age=timedelta(0)):
    """Write aggregate rows with the given statuses and network rows with the
    given latencies, all taken `age` ago."""
    for status in aggregate:
        db.create_health_check(name, "aggregate", status)
    for latency in latencies:
        db.create_health_check(
            name, "network", "healthy",
            network_metrics={"reachable": True, "latency_ms": latency},
        )
    _hrun(
        "UPDATE agent_health_checks SET checked_at = :t WHERE agent_name = :n",
        t=_iso(datetime.now(timezone.utc) - age),
        n=name,
    )


def _service():
    return SimpleNamespace(
        is_running=True, config=SimpleNamespace(docker_check_interval=30)
    )


def _agent_route(monkeypatch, db, name):
    mon = importlib.import_module("routers.monitoring")
    view = importlib.import_module("services.circuit_breaker_view")
    monkeypatch.setattr(mon, "db", db)
    monkeypatch.setattr(mon, "get_monitoring_service", _service)
    monkeypatch.setattr(mon, "_declared_metrics_block", lambda n: None)
    monkeypatch.setattr(view, "build_circuit_breaker_block", lambda n: None)
    return asyncio.run(mon.get_agent_health(name))


def _health_check(monkeypatch, db, name):
    svc = importlib.import_module("services.monitoring_service")
    from db_models import BusinessHealthCheck, DockerHealthCheck, NetworkHealthCheck

    now = _iso(datetime.now(timezone.utc))

    async def network(agent_name, *_a, **_kw):
        return NetworkHealthCheck(agent_name=agent_name, reachable=False, checked_at=now)

    async def business(agent_name, *_a, **_kw):
        return BusinessHealthCheck(agent_name=agent_name, status="unhealthy", checked_at=now)

    monkeypatch.setattr(svc, "db", db)
    monkeypatch.setattr(svc, "_is_circuit_dormant", lambda n: False)
    monkeypatch.setattr(
        svc, "check_docker_health",
        lambda agent_name: DockerHealthCheck(
            agent_name=agent_name, container_status="running", checked_at=now),
    )
    monkeypatch.setattr(svc, "check_network_health", network)
    monkeypatch.setattr(svc, "check_business_health", business)
    return asyncio.run(svc.perform_health_check(name, store_results=False))


def test_4685_agent_route_reports_zero_uptime_and_latency(db, monkeypatch):
    _seed(db, "down-all-day", aggregate=["unhealthy"] * 3, latencies=[0.0, 0.0])

    detail = _agent_route(monkeypatch, db, "down-all-day")

    assert detail.uptime_percent_24h == 0.0
    assert detail.avg_latency_24h_ms == 0.0


def test_4685_agent_route_without_checks_in_window_reports_none(db, monkeypatch):
    _seed(db, "quiet", aggregate=["unhealthy"], latencies=[12.0],
          age=timedelta(hours=25))

    detail = _agent_route(monkeypatch, db, "quiet")

    assert detail.uptime_percent_24h is None
    assert detail.avg_latency_24h_ms is None


def test_4685_health_check_reports_zero_uptime_and_latency(db, monkeypatch):
    _seed(db, "down-all-day", aggregate=["unhealthy"] * 3, latencies=[0.0, 0.0])

    detail = _health_check(monkeypatch, db, "down-all-day")

    assert detail.uptime_percent_24h == 0.0
    assert detail.avg_latency_24h_ms == 0.0


def test_4685_health_check_without_checks_in_window_reports_none(db, monkeypatch):
    detail = _health_check(monkeypatch, db, "never-checked")

    assert detail.uptime_percent_24h is None
    assert detail.avg_latency_24h_ms is None
