from datetime import UTC, datetime

from ffagent.analysis.injuries import Snapshot
from ffagent.store.db import Store, fingerprint


def test_snapshot_roundtrip_and_latest(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    a = Snapshot(datetime(2026, 10, 11, 9, 0, tzinfo=UTC), {"p1": ("healthy", None)})
    b = Snapshot(datetime(2026, 10, 11, 15, 0, tzinfo=UTC), {"p1": ("questionable", "knee")})
    s.save_snapshot("sleeper:1", 5, a)
    s.save_snapshot("sleeper:1", 5, b)
    latest = s.latest_snapshot("sleeper:1", 5)
    assert latest.taken_at == b.taken_at and latest.statuses == {"p1": ("questionable", "knee")}
    assert s.latest_snapshot("sleeper:1", 6) is None


def test_decisions_rejected_and_approved():
    s = Store()
    props = [{"id": "a", "kind": "lineup_swap", "payload": {"slot": "RB", "player_in": "x", "player_out": "y"}},
             {"id": "b", "kind": "lineup_swap", "payload": {"slot": "WR", "player_in": "m", "player_out": "n"}}]
    decs = [{"action_id": "a", "verdict": "rejected", "note": "nope"}, {"action_id": "b", "verdict": "approved"}]
    s.record_decisions("espn:9", 5, "lineup", props, decs)
    assert s.rejected_fingerprints("espn:9", 5) == {fingerprint("lineup_swap", props[0]["payload"])}
    assert s.approved_payloads("espn:9", 5, "lineup") == [props[1]["payload"]]
    assert s.approved_payloads("espn:9", 5, "injury") == []


def test_edited_payload_is_what_gets_recorded():
    s = Store()
    props = [{"id": "a", "kind": "lineup_swap", "payload": {"slot": "RB", "player_in": "x", "player_out": "y"}}]
    s.record_decisions("k", 5, "lineup", props, [{"action_id": "a", "verdict": "edited", "edited_payload": {"player_in": "z"}}])
    assert s.approved_payloads("k", 5)[0]["player_in"] == "z"
