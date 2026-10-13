from __future__ import annotations

import pytest

from gluonify.top import Secret, Top, TopKeyMissingError, env_name


def test_env_name_follows_microprofile() -> None:
    assert env_name("webhook.key") == "WEBHOOK_KEY"
    assert env_name("gluonify.gdown-user") == "GLUONIFY_GDOWN_USER"
    assert env_name("WEBHOOK_KEY") == "WEBHOOK_KEY"


def test_prefixed_key_wins() -> None:
    top = Top({"APP_WEBHOOK_KEY": "prefixed", "WEBHOOK_KEY": "bare"})
    assert top.get("webhook.key") == "prefixed"
    assert top.names("webhook.key") == ("APP_WEBHOOK_KEY", "WEBHOOK_KEY")


def test_literal_namespace_keys_are_unprefixed() -> None:
    assert Top({"WEBHOOK_KEY": "bare"}).get("webhook.key") == "bare"


def test_blank_is_absent() -> None:
    top = Top({"APP_A": "  ", "A": ""})
    assert top.get("a") is None
    assert top.get("a", "d") == "d"


def test_require() -> None:
    assert Top({"APP_A": "1"}).require("a") == "1"
    with pytest.raises(TopKeyMissingError) as e:
        Top({}).require("webhook.key")
    assert "APP_WEBHOOK_KEY" in str(e.value)
    assert "WEBHOOK_KEY" in str(e.value)
    assert str(TopKeyMissingError()) == ""


def test_secret_never_prints() -> None:
    top = Top({"APP_PASSWORD": "hunter2"})
    s = top.secret("password")
    assert s is not None
    assert "hunter2" not in repr(s)
    assert "hunter2" not in str(s)
    assert s.reveal() == "hunter2"
    assert s == Secret("hunter2")
    assert s != "hunter2"
    assert hash(s) == hash(Secret("hunter2"))
    assert top.secret("absent") is None
    assert top.require_secret("password").reveal() == "hunter2"


def test_default_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_FROM_OS", "yes")
    assert Top().get("from.os") == "yes"
