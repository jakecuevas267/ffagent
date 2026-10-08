from __future__ import annotations

from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver


def checkpointer(data_dir: Path) -> SqliteSaver:
    data_dir.mkdir(parents=True, exist_ok=True)
    import sqlite3
    conn = sqlite3.connect(str(data_dir / "ffagent.sqlite"), check_same_thread=False)
    return SqliteSaver(conn)
