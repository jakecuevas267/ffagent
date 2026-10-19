import pytest

from ffagent.secrets import EnvBackend, Found, KeyringBackend, Secrets, SSMBackend


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, key):
        return self.store.get((service, key))

    def set_password(self, service, key, value):
        self.store[(service, key)] = value


class FakeSSM:
    def __init__(self, params=None, fail=None):
        self.params, self.fail, self.calls = params or {}, fail, 0

    def get_parameter(self, Name, WithDecryption):
        self.calls += 1
        if self.fail:
            raise self.fail
        if Name not in self.params:
            class ParameterNotFound(Exception):
                pass
            raise ParameterNotFound(Name)
        return {"Parameter": {"Value": self.params[Name]}}

    def put_parameter(self, Name, Value, Type, Overwrite):
        assert Type == "SecureString" and Overwrite
        self.params[Name] = Value


def test_order_first_backend_with_a_value_wins(monkeypatch):
    monkeypatch.setenv("ESPN_S2", "from-env")
    kr = FakeKeyring(); kr.set_password("ffagent", "ESPN_S2", "from-keyring")
    s = Secrets([KeyringBackend(kr), EnvBackend()])
    assert s.lookup("ESPN_S2") == ("from-keyring", "keyring")
    s2 = Secrets([EnvBackend(), KeyringBackend(kr)])
    assert s2.lookup("ESPN_S2") == ("from-env", "env")


def test_alias_names_are_consulted(monkeypatch):
    monkeypatch.delenv("FANTASY_INFORMATION_SOURCE_API_KEY", raising=False)
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_KEY", "k")
    assert Secrets([EnvBackend()]).get("FANTASY_INFORMATION_SOURCE_API_KEY") == "k"


def test_ssm_reads_with_prefix_and_caches():
    ssm = FakeSSM({"/ffagent/ESPN_SWID": "{swid}"})
    b = SSMBackend(ssm)
    assert b.get("ESPN_SWID") == "{swid}" and b.get("ESPN_SWID") == "{swid}" and ssm.calls == 1
    assert b.get("NOPE") is None


def test_ssm_errors_read_as_missing_and_are_noted():
    b = SSMBackend(FakeSSM(fail=PermissionError("denied")))
    assert b.get("ESPN_S2") is None and b.last_error == "PermissionError"


def test_set_goes_to_the_first_writable_backend():
    kr = FakeKeyring(); ssm = FakeSSM()
    s = Secrets([EnvBackend(), SSMBackend(ssm), KeyringBackend(kr)])
    assert s.set("ESPN_S2", "v") == "ssm" and ssm.params["/ffagent/ESPN_S2"] == "v"
    with pytest.raises(RuntimeError):
        Secrets([EnvBackend()]).set("ESPN_S2", "v")


def test_export_fills_environ_without_overwriting(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ESPN_S2", "already")
    kr = FakeKeyring(); kr.set_password("ffagent", "ANTHROPIC_API_KEY", "llm"); kr.set_password("ffagent", "ESPN_S2", "other")
    s = Secrets([KeyringBackend(kr), EnvBackend()])
    import os
    assert s.export(["ANTHROPIC_API_KEY", "ESPN_S2"]) == ["ANTHROPIC_API_KEY"]
    assert os.environ["ANTHROPIC_API_KEY"] == "llm" and os.environ["ESPN_S2"] == "already"


def test_check_reports_where_each_key_lives(monkeypatch):
    monkeypatch.setenv("ESPN_S2", "x"); monkeypatch.delenv("ESPN_SWID", raising=False)
    found = Secrets([EnvBackend()]).check(["ESPN_S2", "ESPN_SWID"])
    assert found == [Found("ESPN_S2", "env"), Found("ESPN_SWID", None)]


def test_from_env_spec(monkeypatch):
    monkeypatch.setenv("FFAGENT_SECRETS", "env")
    assert [b.name for b in Secrets.from_env().backends] == ["env"]
    with pytest.raises(ValueError):
        Secrets.from_env("vault")


def test_earlier_backend_wins_even_under_an_alias(monkeypatch):
    monkeypatch.setenv("FANTASY_INFORMATION_SOURCE_API_KEY", "from-env")
    ssm = FakeSSM({"/ffagent/FANTASY_INFORMATION_SOURCE_KEY": "from-ssm"})   # stored under the alias name
    s = Secrets([SSMBackend(ssm), EnvBackend()])
    assert s.lookup("FANTASY_INFORMATION_SOURCE_API_KEY") == ("from-ssm", "ssm")
