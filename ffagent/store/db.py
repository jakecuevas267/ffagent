"""SQLite persistence: LangGraph checkpoints plus our own snapshots and decisions, one file."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from ffagent.analysis.injuries import Snapshot

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  league_key TEXT, week INTEGER, taken_at TEXT, statuses TEXT,
  PRIMARY KEY (league_key, week, taken_at));
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, league_key TEXT, week INTEGER, workflow TEXT,
  fingerprint TEXT, verdict TEXT, note TEXT, payload TEXT, decided_at TEXT);
CREATE INDEX IF NOT EXISTS decisions_lw ON decisions (league_key, week);
"""


def fingerprint(kind: str, payload: dict) -> str:
    return f"{kind}:{payload.get('slot')}:{payload.get('player_in')}:{payload.get('player_out')}"


class Store:
    def __init__(self, path: Path | str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def checkpointer(self) -> SqliteSaver:
        return SqliteSaver(self._conn)

    # -- snapshots -----------------------------------------------------------------------------
    def save_snapshot(self, league_key: str, week: int, snap: Snapshot) -> None:
        self._conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?)",
                           (league_key, week, snap.taken_at.isoformat(), json.dumps(snap.statuses)))
        self._conn.commit()

    def latest_snapshot(self, league_key: str, week: int) -> Snapshot | None:
        row = self._conn.execute("SELECT taken_at, statuses FROM snapshots WHERE league_key=? AND week=? "
                                 "ORDER BY taken_at DESC LIMIT 1", (league_key, week)).fetchone()
        if not row:
            return None
        return Snapshot(datetime.fromisoformat(row[0]), {k: tuple(v) for k, v in json.loads(row[1]).items()})

    # -- decisions -----------------------------------------------------------------------------
    def record_decisions(self, league_key: str, week: int, workflow: str, proposals: list[dict],
                         decisions: list[dict], now: datetime | None = None) -> None:
        by_id = {d["action_id"]: d for d in decisions}
        ts = (now or datetime.now(UTC)).isoformat()
        rows = []
        for p in proposals:
            d = by_id.get(p["id"])
            if d is None:
                continue
            payload = p["payload"] | (d.get("edited_payload") or {})
            rows.append((league_key, week, workflow, fingerprint(p["kind"], payload), d["verdict"],
                         d.get("note", ""), json.dumps(payload), ts))
        self._conn.executemany("INSERT INTO decisions (league_key, week, workflow, fingerprint, verdict, note, "
                               "payload, decided_at) VALUES (?,?,?,?,?,?,?,?)", rows)
        self._conn.commit()

    def rejected_fingerprints(self, league_key: str, week: int) -> set[str]:
        rows = self._conn.execute("SELECT fingerprint FROM decisions WHERE league_key=? AND week=? AND verdict='rejected'",
                                  (league_key, week)).fetchall()
        return {r[0] for r in rows}

    def approved_payloads(self, league_key: str, week: int, workflow: str | None = None) -> list[dict]:
        q = "SELECT payload FROM decisions WHERE league_key=? AND week=? AND verdict IN ('approved','edited')"
        args: list = [league_key, week]
        if workflow:
            q += " AND workflow=?"
            args.append(workflow)
        return [json.loads(r[0]) for r in self._conn.execute(q, args).fetchall()]


def checkpointer(data_dir: Path) -> SqliteSaver:  # kept for callers that only need checkpoints
    return Store(Path(data_dir) / "ffagent.sqlite").checkpointer()
