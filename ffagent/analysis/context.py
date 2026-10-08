"""Turn league data + projections + schedule into optimizer candidates, with reasons."""
from __future__ import annotations

from datetime import datetime

from ffagent.analysis.lineup import Candidate
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import LeagueSettings, Slot, Team
from ffagent.schedule.nfl import WeekSchedule
from ffagent.sources.projections import Projection, score

_NOT_STARTABLE = {Slot.IR, Slot.TAXI}


def lineup_candidates(team: Team, settings: LeagueSettings, index: PlayerIndex,
                      projections: dict[str, Projection], schedule: WeekSchedule, now: datetime) -> list[Candidate]:
    out: list[Candidate] = []
    for e in team.roster:
        if e.player_id is None or e.slot in _NOT_STARTABLE:
            continue
        p = index.by_id(e.player_id)
        if p is None:
            continue
        proj = projections.get(p.id)
        pts = score(proj.stats, settings.scoring) if proj else 0.0
        available, reason = True, None
        game = schedule.game_for(p.team) if p.team else None
        if p.injury_status.unplayable:
            available, reason = False, str(p.injury_status)
        elif p.team is None or p.team in schedule.teams_on_bye or game is None:
            available, reason = False, "bye" if p.team in schedule.teams_on_bye else "no game this week"
        elif p.injury_status.value in ("questionable", "doubtful"):
            reason = p.injury_note or "practice report"
        out.append(Candidate(player=p, points=round(pts, 2), current_slot=e.slot, available=available,
                             locked=bool(game and game.started(now)), reason=reason))
    return out
