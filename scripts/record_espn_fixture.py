"""Record a scrubbed ESPN league fixture: python scripts/record_espn_fixture.py <league_id> <out_name>

Manager names and member ids are replaced; nothing from .env is written.
"""
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from ffagent.providers.espn_client import ESPNClient

load_dotenv(".env")
league_id, out = sys.argv[1], sys.argv[2]
c = ESPNClient.from_env(2026)
lg = c.league(league_id, ["mSettings", "mTeam", "mRoster", "mMatchup", "mStatus"])
members = {m["id"]: f"manager{i + 1}" for i, m in enumerate(lg.get("members", []))}
for m in lg.get("members", []):
    m["displayName"] = members[m["id"]]; m["firstName"] = m["lastName"] = ""
    m.pop("notificationSettings", None)
for t in lg.get("teams", []):
    t["name"] = f"Team {t['id']}"
    for key in ("logo", "logoType"):
        t.pop(key, None)
# Member ids (SWIDs) also appear in primaryOwner and elsewhere: replace every occurrence anywhere.
text = json.dumps(lg)
for real, alias in members.items():
    text = text.replace(real, alias).replace(real.strip("{}"), alias)
lg = json.loads(text)
dest = Path("tests/fixtures/espn") / f"{out}.json"
dest.write_text(json.dumps(lg, indent=0))
print("wrote", dest, dest.stat().st_size // 1024, "KB; top-level keys:", sorted(lg))
