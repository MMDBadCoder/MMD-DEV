"""OpenRouter customer keys are unrestricted by model, but capped by credit."""
import pytest

from mmd.openrouter import KeyInfo, OpenRouter, OpenRouterError


def test_a_client_without_a_management_key_refuses_to_exist():
    with pytest.raises(OpenRouterError):
        OpenRouter("")


def test_key_info_tolerates_missing_numbers():
    info = OpenRouter._info({"hash": "h", "name": "user-1", "limit": None})
    assert isinstance(info, KeyInfo)
    assert info.usage_usd == 0.0
    assert info.limit_usd is None
    assert info.limit_remaining is None


def test_key_info_reads_usage_and_limit():
    info = OpenRouter._info({"hash": "h1", "name": "user-3", "usage": "1.25",
                             "limit": 10, "limit_remaining": 8.75,
                             "disabled": False})
    assert (info.usage_usd, info.limit_usd, info.limit_remaining) == (1.25, 10.0, 8.75)
    assert info.disabled is False


def test_new_customer_key_is_not_attached_to_a_guarded_workspace(monkeypatch):
    client = OpenRouter("management-key")
    sent = []
    monkeypatch.setattr(client, "_call", lambda method, path, **kw:
                        sent.append(kw["json"]) or
                        {"key": "secret", "data": {"hash": "hash", "limit": 2}})
    try:
        client.create_key("mmd-user1-ali", 2)
    finally:
        client.close()
    assert sent == [{"name": "mmd-user1-ali", "limit": 2}]
    assert "workspace_id" not in sent[0]
