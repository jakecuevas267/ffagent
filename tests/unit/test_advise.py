from ffagent.llm.advise import Advice, AdviceOut, Adviser, build_messages, load_playbook, validate


class FakeLLM:
    def __init__(self, out):
        self.out, self.messages = out, None

    def invoke(self, messages):
        self.messages = messages
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


CANDS = [{"id": "fa1", "name": "A"}, {"id": "fa2", "name": "B"}]
DROPS = [{"id": "d1", "name": "D"}]


def test_validate_rejects_unknown_players_and_held_drops():
    out = AdviceOut(proposals=[
        Advice(add_id="ghost", drop_id="d1", action="claim", principle="p", rationale="r"),
        Advice(add_id="fa1", drop_id="held", action="claim", principle="p", rationale="r"),
        Advice(add_id="fa2", drop_id="d1", action="claim", bid=500, principle="p", rationale="r"),
    ])
    ok, rejected = validate(out, {"fa1", "fa2"}, {"d1"}, budget=40, max_actions=3)
    assert [a.add_id for a in ok] == ["fa2"] and ok[0].bid == 40
    assert len(rejected) == 2 and "not a candidate" in rejected[0] and "not droppable" in rejected[1]


def test_validate_caps_count_and_strips_bids_in_priority_leagues():
    out = AdviceOut(proposals=[Advice(add_id=f"fa{i}", drop_id="d1", action="claim", bid=5, principle="p", rationale="r") for i in (1, 2)])
    ok, _ = validate(out, {"fa1", "fa2"}, {"d1"}, budget=None, max_actions=1)
    assert len(ok) == 1 and ok[0].bid is None


def test_skip_and_empty_are_valid():
    out = AdviceOut(proposals=[Advice(add_id="fa1", drop_id=None, action="skip", principle="p", rationale="nothing worth it")])
    assert validate(out, {"fa1"}, set(), 10, 3) == ([], [])


def test_adviser_passes_playbook_and_facts_to_the_model():
    llm = FakeLLM(AdviceOut(proposals=[Advice(add_id="fa1", drop_id="d1", action="pickup", principle="Team RB1s are always pickups", rationale="x")],
                            watch=["B: see Sunday"]))
    adv = Adviser(llm, "fake-model", playbook="- Team RB1s are always pickups")
    r = adv.advise({"format": "redraft"}, [{"id": "d1"}], CANDS, DROPS, [{"verdict": "rejected", "note": "no kickers"}], None, 3)
    assert r.error is None and [a.add_id for a in r.proposals] == ["fa1"] and r.watch == ["B: see Sunday"]
    system, human = llm.messages
    assert "Team RB1s are always pickups" in system[1] and "no kickers" in human[1] and '"fa2"' in human[1]


def test_adviser_failure_leaves_deterministic_proposals_in_place():
    r = Adviser(FakeLLM(RuntimeError("rate limited")), "m", "").advise({}, [], CANDS, DROPS, [], 10, 3)
    assert r.proposals == [] and "rate limited" in r.error


def test_disabled_adviser():
    r = Adviser(None).advise({}, [], CANDS, DROPS, [], 10, 3)
    assert not Adviser(None).enabled and r.error == "advise node off"


def test_load_playbook_prefers_personal_over_example(tmp_path):
    (tmp_path / "waivers.example.md").write_text("example")
    assert load_playbook(tmp_path, "waivers") == "example"
    (tmp_path / "waivers.md").write_text("mine")
    assert load_playbook(tmp_path, "waivers") == "mine"
    assert load_playbook(tmp_path, "lineup") == ""


def test_build_messages_shape():
    msgs = build_messages("pb", {"format": "dynasty"}, [], CANDS, DROPS, [], 50, 2)
    assert msgs[0][0] == "system" and "pb" in msgs[0][1] and msgs[1][0] == "human" and '"faab_remaining": 50' in msgs[1][1]
