import asyncio

from sqlalchemy import select

from spike_monitor.db import SessionLocal
from spike_monitor.models import Device, PingResult, Status, StatusEvent
from spike_monitor.monitoring.engine import MonitorEngine
from spike_monitor.monitoring.pinger import PingOutcome, parse_ping_output


def make_devices():
    with SessionLocal() as db:
        core = Device(name="core", ip_address="10.0.0.1")
        tower = Device(name="tower", ip_address="10.0.0.2", parent=core)
        cpe = Device(name="cpe", ip_address="10.0.0.3", parent=tower)
        other = Device(name="other", ip_address="10.0.0.4", latency_warn_ms=50)
        db.add_all([core, tower, cpe, other])
        db.commit()


def run(engine):
    return asyncio.run(engine.run_cycle())


def statuses():
    with SessionLocal() as db:
        return {d.name: d.status for d in db.scalars(select(Device))}


def test_parent_aware_status(app):
    make_devices()
    alive = {"10.0.0.1": True, "10.0.0.2": True, "10.0.0.3": True, "10.0.0.4": True}

    async def fake_ping(host, count, timeout):
        if alive[host]:
            return PingOutcome(True, 80.0 if host == "10.0.0.4" else 5.0, 0.0)
        return PingOutcome(False, None, 100.0)

    engine = MonitorEngine(SessionLocal, ping_func=fake_ping, down_after=2)
    run(engine)
    assert statuses() == {"core": Status.UP, "tower": Status.UP, "cpe": Status.UP, "other": Status.WARNING}

    # Tower and everything behind it stops answering.
    alive["10.0.0.2"] = alive["10.0.0.3"] = False
    run(engine)  # first failure is debounced
    assert statuses()["tower"] == Status.UP
    events = run(engine)
    s = statuses()
    assert s["tower"] == Status.DOWN and s["cpe"] == Status.UNREACHABLE
    assert {e.new_status for e in events} == {Status.DOWN, Status.UNREACHABLE}

    alive["10.0.0.2"] = alive["10.0.0.3"] = True
    run(engine)
    assert statuses()["cpe"] == Status.UP
    with SessionLocal() as db:
        assert db.scalars(select(PingResult)).first()
        assert len(db.scalars(select(StatusEvent)).all()) >= 5


def test_parse_windows_and_linux_ping():
    win = "Reply from 10.0.0.1: bytes=32 time=12ms TTL=64\nReply from 10.0.0.1: bytes=32 time<1ms TTL=64\nRequest timed out.\n"
    r = parse_ping_output(win, 3)
    assert r.is_alive and round(r.loss_pct) == 33 and r.avg_ms == 6.5
    linux = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=10.5 ms\n64 bytes from 1.1.1.1: icmp_seq=2 ttl=57 time=11.5 ms\n"
    r = parse_ping_output(linux, 2)
    assert r.loss_pct == 0 and r.avg_ms == 11.0
    assert not parse_ping_output("Destination host unreachable.", 3).is_alive
