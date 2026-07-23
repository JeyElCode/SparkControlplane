"""Throttle-flag parsing, XID journal parsing, and the derived alert facts."""

from __future__ import annotations

import time

from app.services.alerts import gather_facts, merged_config
from app.services.telemetry import CounterState, parse_sample, parse_xid_lines

THROTTLE_ACTIVE = """
@@gpu@@
0, NVIDIA GB10, 51200, 122880, 37, 62, 41.2
@@throttle@@
Active, Not Active
@@cpu@@
cpu  1000 0 500 8000 100 0 50 0 0 0
"""

THROTTLE_IDLE = THROTTLE_ACTIVE.replace("Active, Not Active", "Not Active, Not Active")


def _parse(raw: str):
    s, _ = parse_sample(raw, node_id=1, ts=1.0, qsfp_iface="x", models_dir="/m",
                        prev=CounterState())
    return s


def test_throttle_flag_parsing():
    assert _parse(THROTTLE_ACTIVE).gpu_throttle is True
    # "Not Active" must not substring-match as "Active"
    assert _parse(THROTTLE_IDLE).gpu_throttle is False
    # section absent -> unknown
    assert _parse("@@gpu@@\n0, GB10, 1, 2, 3, 4, 5.0\n").gpu_throttle is None


XID_JOURNAL = """\
1721721721.123456 dgx-md-01 kernel: NVRM: Xid (PCI:0000:0009:01:00): 79, pid=4173, GPU has fallen off the bus.
1721721800.500000 dgx-md-01 kernel: NVRM: Xid (PCI:0000:0009:01:00): 63, pid=888, Row remapper pending
1721721900.000000 dgx-md-01 kernel: something mentioning xid without a number
"""


def test_xid_line_parsing():
    events = parse_xid_lines(XID_JOURNAL)
    assert len(events) == 3
    assert events[0].xid == 79 and events[0].ts == 1721721721.123456
    assert "fallen off the bus" in events[0].message
    assert events[1].xid == 63
    assert events[2].xid is None  # unparseable number, event still surfaced
    assert parse_xid_lines("") == []


def test_throttle_and_xid_facts(monkeypatch):
    """gather_facts derives the two new rules from the engine caches."""
    from app.schemas import XidEvent
    from app.services.telemetry import NodeSample, engine

    now = time.time()
    engine._node_names[9] = "dgx-md-01"
    engine._samples[9] = NodeSample(node_id=9, ts=now, reachable=True, gpu_throttle=True)
    engine._xids[9] = [XidEvent(ts=now - 10, xid=79, message="GPU has fallen off the bus")]
    try:
        facts = {f.rule: f for f in gather_facts(merged_config(None))
                 if f.subject == "dgx-md-01"}
        assert facts["gpu_throttle"].active is True
        assert facts["gpu_xid"].active is True
        assert facts["gpu_xid"].sustain == 0          # fires immediately
        assert "Xid 79" in facts["gpu_xid"].message
        # an old XID outside the window no longer alerts
        engine._xids[9] = [XidEvent(ts=now - 10_000, xid=79, message="old")]
        facts = {f.rule: f for f in gather_facts(merged_config(None))
                 if f.subject == "dgx-md-01"}
        assert facts["gpu_xid"].active is False
    finally:
        engine._samples.pop(9, None)
        engine._node_names.pop(9, None)
        engine._xids.pop(9, None)
