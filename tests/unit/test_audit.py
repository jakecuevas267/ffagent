import json
from datetime import datetime
from pathlib import Path

from ffagent.analysis.audit import audit_team, notice_message
from ffagent.domain.identity import PlayerIndex
from ffagent.domain.models import InjuryStatus, Player, Position, RosterEntry, Slot, Team
from ffagent.schedule.nfl import parse_scoreboard

FIX = Path(__file__).parent.parent / "fixtures"
SCHED = parse_scoreboard(json.loads((FIX / "espn" / "scoreboard_2026_w5.json").read_text()))
SAT = datetime.fromisoformat("2026-10-10T12:00:00-06:00")   # nothing but Thursday has kicked off
SUN_PM = datetime.fromisoformat("2026-10-11T15:30:00-06:00")  # early and late Sunday windows started


def P(id, pos, team, injury=InjuryStatus.HEALTHY, note=None):
    return Player(id=id, name=id, positions=[pos], team=team, injury_status=injury, injury_note=note)


def setup(starters, bench):
    players = [p for p, _ in starters if p] + bench
    team = Team(id="7", name="Rivals", owner="rival", roster=[RosterEntry(player_id=p.id if p else None, slot=s) for p, s in starters]
                + [RosterEntry(player_id=b.id, slot=Slot.BN) for b in bench])
    return team, PlayerIndex(players), [RosterEntry(player_id=p.id if p else None, slot=s) for p, s in starters]


def test_bye_out_doubtful_and_empty_are_flagged_with_fixes():
    team, idx, st = setup([(P("kc_wr", Position.WR, "KC"), Slot.WR),               # KC on bye in week 5
                           (P("hurt_rb", Position.RB, "NYG", InjuryStatus.OUT, "knee"), Slot.RB),
                           (P("iffy_te", Position.TE, "BUF", InjuryStatus.DOUBTFUL), Slot.TE),
                           (None, Slot.FLEX), (P("ok_qb", Position.QB, "BUF"), Slot.QB)],
                          [P("b_wr", Position.WR, "MIA"), P("b_rb", Position.RB, "NYJ"), P("b_te", Position.TE, "SEA")])
    issues = audit_team(team, st, idx, SCHED, SAT)
    by = {i.key: i for i in issues}
    assert by["WR:kc_wr:bye"].fix == "b_wr" and by["WR:kc_wr:bye"].fixable
    assert by["RB:hurt_rb:out"].detail == "Out (knee)" and by["RB:hurt_rb:out"].fix == "b_rb"
    assert by["TE:iffy_te:doubtful"].fix == "b_te"
    assert by["FLEX:empty:empty"].fixable is False  # every bench player already used as a fix
    assert "ok_qb" not in str(issues)


def test_locked_starters_are_reported_but_unfixable():
    team, idx, st = setup([(P("dal_rb", Position.RB, "DAL", InjuryStatus.OUT), Slot.RB)], [P("b_rb", Position.RB, "NYJ")])
    i = audit_team(team, st, idx, SCHED, SAT)[0]   # DAL played Thursday night
    assert i.locked and not i.fixable and i.fix is None


def test_bench_fix_must_be_healthy_unlocked_and_playing():
    team, idx, st = setup([(P("kc_wr", Position.WR, "KC"), Slot.WR)],
                          [P("b1", Position.WR, "CAR"),                              # bye
                           P("b2", Position.WR, "DAL"),                              # played Thursday: locked
                           P("b3", Position.WR, "MIA", InjuryStatus.OUT),            # hurt
                           P("b4", Position.WR, "BAL")])                             # fine (Sunday night)
    assert audit_team(team, st, idx, SCHED, SUN_PM)[0].fix == "b4"


def test_questionable_is_not_an_issue():
    team, idx, st = setup([(P("q", Position.WR, "BUF", InjuryStatus.QUESTIONABLE), Slot.WR)], [])
    assert audit_team(team, st, idx, SCHED, SAT) == []


def test_fix_prefers_higher_value_when_known():
    from ffagent.sources.values import Value
    team, idx, st = setup([(P("kc_wr", Position.WR, "KC"), Slot.WR)], [P("low", Position.WR, "MIA"), P("high", Position.WR, "BAL")])
    vals = {"low": Value("low", 4.0, "t"), "high": Value("high", 11.0, "t")}
    assert audit_team(team, st, idx, SCHED, SAT, vals)[0].fix == "high"


def test_notice_message_only_covers_fixable_issues():
    team, idx, st = setup([(P("kc_wr", Position.WR, "KC"), Slot.WR), (P("dal_rb", Position.RB, "DAL", InjuryStatus.OUT), Slot.RB)],
                          [P("b_wr", Position.WR, "MIA")])
    msg = notice_message("rival", "Rivals", audit_team(team, st, idx, SCHED, SAT))
    assert msg.startswith("Hey rival, heads up") and "kc_wr (WR) is on bye — b_wr is on your bench" in msg and "dal_rb" not in msg
