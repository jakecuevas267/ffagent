"""Recent performance from Sleeper's weekly stats (undocumented, public): actual points under league
scoring, snap share and targets. Keyed by Sleeper id; other platforms map through the crosswalk."""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ffagent.sources.projections import score

STATS = "https://api.sleeper.app/v1/stats/nfl/regular/{season}/{week}"


@dataclass
class Recent:
    weeks: int                 # games with data in the window
    points: list[float]        # per week, under league scoring, most recent last
    avg_points: float
    snap_share: float | None   # average of off_snp / tm_off_snp
    targets_per_game: float | None

    def line(self) -> str:
        pts = ", ".join(f"{p:.1f}" for p in self.points)
        s = f"last {self.weeks}: {pts} ({self.avg_points:.1f} avg)"
        if self.snap_share is not None:
            s += f", {self.snap_share:.0%} snaps"
        if self.targets_per_game is not None:
            s += f", {self.targets_per_game:.1f} tgt/g"
        return s


class SleeperStats:
    def __init__(self, http: httpx.Client | None = None):
        self._http = http or httpx.Client(timeout=60)
        self._cache: dict[tuple[int, int], dict] = {}

    def week(self, season: int, week: int) -> dict[str, dict]:
        key = (season, week)
        if key not in self._cache:
            r = self._http.get(STATS.format(season=season, week=week))
            r.raise_for_status()
            self._cache[key] = r.json()
        return self._cache[key]

    def recent(self, season: int, through_week: int, scoring: dict[str, float], window: int = 3) -> dict[str, Recent]:
        weeks = [w for w in range(through_week - window + 1, through_week + 1) if w >= 1]
        per: dict[str, list[tuple[float, dict]]] = {}
        for w in weeks:
            for pid, st in self.week(season, w).items():
                if st and st.get("gp"):
                    per.setdefault(pid, []).append((score(st, scoring), st))
        out = {}
        for pid, rows in per.items():
            pts = [p for p, _ in rows]
            snaps = [st["off_snp"] / st["tm_off_snp"] for _, st in rows if st.get("off_snp") and st.get("tm_off_snp")]
            tgts = [st.get("rec_tgt") for _, st in rows if st.get("rec_tgt") is not None]
            out[pid] = Recent(weeks=len(rows), points=pts, avg_points=sum(pts) / len(pts),
                              snap_share=(sum(snaps) / len(snaps)) if snaps else None,
                              targets_per_game=(sum(tgts) / len(tgts)) if tgts else None)
        return out
