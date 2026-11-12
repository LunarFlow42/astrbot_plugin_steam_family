"""ASF new-feature tests: offline IPC only, no native login or production credentials."""

import asyncio
import base64
import copy
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from config import PluginConfig
from core.asf_adapter import (
    ASF_STATE_KEY,
    AsfAdapterError,
    AsfCredential,
    AsfIpcClient,
    AsfSessionProvider,
    asf_state_key,
)
from core.steam_api import SharedApp, SteamAuthExpiredError, SteamRateLimitError
from core.steam_auth import SteamLoginError
from tests.test_main import _chat_event
from tests.test_notifications import _make_plugin, _offline_client

SID = "76561198000000001"
OTHER_SID = "76561198000000002"
KEY = "test-bridge-key-" + "k" * 40
PASSWORD = "ipc-test-password"


def _token(steam_id=SID, ttl=7200, aud=None, jti="first"):
    payload = {
        "sub": steam_id,
        "exp": int(time.time()) + ttl,
        "iat": int(time.time()),
        "aud": ["web", "client"] if aud is None else aud,
        "jti": jti,
    }
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{segment}.fake-signature"


def _payload(token=None, steam_id=SID):
    token = token or _token(steam_id)
    segment = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    return {
        "Success": True,
        "Result": {
            "ProtocolVersion": 1,
            "BotName": "main",
            "SteamID": steam_id,
            "AccountFlags": 0,
            "IsAccountLocked": False,
            "IsConnectedAndLoggedOn": True,
            "ExpiresAt": claims["exp"],
            "AccessToken": token,
        },
    }


def _settings(**kwargs):
    config = {
        "enable_asf": True,
        "asf_ipc_url": "http://127.0.0.1:1242",
        "asf_bot_name": "main",
        "asf_ipc_password": PASSWORD,
        "asf_bridge_key": KEY,
    }
    config.update(kwargs)
    return config


async def _install_transport(client, handler):
    await client._client.aclose()
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
        headers={"Authentication": PASSWORD, "X-Steam-Family-Key": KEY},
    )


async def _provider(handler, store=None, expected=SID, bridge_key=KEY):
    async def load_state():
        return store.get(ASF_STATE_KEY) if store is not None else None

    async def save_state(value):
        if store is not None:
            store[ASF_STATE_KEY] = value

    provider = AsfSessionProvider(
        "http://127.0.0.1:1242",
        "main",
        PASSWORD,
        bridge_key,
        expected_steam_id=expected,
        load_state=load_state,
        save_state=save_state,
    )
    await _install_transport(provider._client, handler)
    return provider


@pytest.fixture
async def plugins(monkeypatch):
    """Always drain test-owned tasks and close test-owned clients."""
    instances = []

    def make(tmp_path, **kwargs):
        plugin = _make_plugin(tmp_path, **kwargs)
        instances.append(plugin)
        return plugin

    yield make
    for plugin in instances:
        pending = [task for task in plugin._bg_tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await plugin.terminate()


def test_asf_feature_is_disabled_by_default_and_configuration_matches_schema():
    from pathlib import Path

    config = PluginConfig()
    assert config.enable_asf is False
    assert config.asf_ipc_url == "http://127.0.0.1:1242"
    schema = json.loads(Path("_conf_schema.json").read_text())
    for key in _settings():
        assert key in schema
        assert schema[key]["default"] == getattr(config, key)
    assert "asf_request_timeout" in schema
    configured = PluginConfig(asf_ipc_password=PASSWORD, asf_bridge_key=KEY)
    assert PASSWORD not in repr(configured)
    assert KEY not in repr(configured)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.1:1242",
        "http://asf.example:1242",
        "https://user:pass@asf.example",
        "https://asf.example?password=secret",
        "https://asf.example/#secret",
        "ftp://127.0.0.1",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:0",
        "https://asf.example/../Api",
        "https://asf.example/%2e%2e",
        "https://127.0.0.1\\@evil.example",
        "https://asf.example/path with spaces",
    ],
)
async def test_invalid_or_unencrypted_remote_url_is_rejected_before_request(url):
    with pytest.raises(AsfAdapterError) as exc:
        AsfIpcClient(url, "main", PASSWORD, KEY)
    assert exc.value.code == "configuration"
    assert "user:pass" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bot", ["", ".", "..", "ASF,main", "main..other", "*", "main/other", "name\n", "main other"]
)
async def test_selector_or_path_injection_is_not_an_asf_bot_name(bot):
    with pytest.raises(AsfAdapterError):
        AsfIpcClient("http://127.0.0.1:1242", bot, PASSWORD, KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "password,key",
    [
        ("", KEY),
        (PASSWORD, "short"),
        (PASSWORD, "k" * 257),
        ("pw\nheader", KEY),
        (PASSWORD, "k " * 30),
        ("密码", KEY),
    ],
)
async def test_both_credentials_are_required_and_safe_for_headers(password, key):
    with pytest.raises(AsfAdapterError):
        AsfIpcClient("http://127.0.0.1:1242", "main", password, key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:1242",
        "http://[::1]:1242",
        "http://localhost:1242",
        "https://asf.example/bridge",
    ],
)
async def test_valid_ipc_addresses_keep_environment_proxy_and_redirects_disabled(url):
    client = AsfIpcClient(url, "main", PASSWORD, KEY)
    try:
        assert client._client._trust_env is False
        assert client._client.follow_redirects is False
        assert client._client.headers["Authentication"] == PASSWORD
        assert client._client.headers["X-Steam-Family-Key"] == KEY
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_success_reads_only_companion_endpoint_and_keeps_token_out_of_repr():
    requests = []
    payload = _payload()

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    await _install_transport(client, handler)
    try:
        credential = await client.fetch_credential(SID)
        assert credential.steam_id == SID
        assert credential.access_token == payload["Result"]["AccessToken"]
        assert credential.access_token not in repr(credential)
        assert len(requests) == 1
        assert requests[0].method == "GET"
        assert requests[0].url.path == "/Api/SteamFamily/main/AccessToken"
        assert not requests[0].url.query
        assert requests[0].headers["Authentication"] == PASSWORD
        assert requests[0].headers["X-Steam-Family-Key"] == KEY
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"ProtocolVersion": True},
        {"ProtocolVersion": 2},
        {"BotName": "other"},
        {"SteamID": 76561198000000001},
        {"SteamID": OTHER_SID},
        {"AccountFlags": True},
        {"AccountFlags": None},
        {"IsAccountLocked": None},
        {"IsConnectedAndLoggedOn": "true"},
        {"ExpiresAt": 1},
        {"AccessToken": "bad"},
    ],
)
async def test_malformed_or_mismatched_bridge_result_never_returns_credentials(mutation):
    payload = _payload()
    payload["Result"].update(mutation)
    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    await _install_transport(client, lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(AsfAdapterError):
            await client.fetch_credential(SID)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aud", [["web", "renew"], ["web", "derive"], ["web", "machine"], "web", [], [None]]
)
async def test_long_lived_refresh_tokens_and_bad_audiences_are_rejected(aud):
    payload = _payload(_token(aud=aud))
    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    await _install_transport(client, lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(AsfAdapterError):
            await client.fetch_credential(SID)
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [{"IsAccountLocked": True}, {"AccountFlags": 1 << 23}])
async def test_account_lockdown_is_not_mistaken_for_usable_token(locked):
    payload = _payload()
    payload["Result"].update(locked)
    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    await _install_transport(client, lambda _: httpx.Response(200, json=payload))
    try:
        with pytest.raises(AsfAdapterError) as exc:
            await client.fetch_credential(SID)
        assert exc.value.code == "account_locked"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_error_responses_and_network_errors_are_sanitized():
    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    leaked = f"AccessToken=SECRET_TOKEN Authentication={PASSWORD} X-Steam-Family-Key={KEY}"
    await _install_transport(
        client, lambda _: httpx.Response(500, json={"Success": False, "Message": leaked})
    )
    try:
        with pytest.raises(AsfAdapterError) as exc:
            await client.fetch_credential(SID)
        assert "SECRET_TOKEN" not in str(exc.value)
        assert PASSWORD not in str(exc.value)
        assert KEY not in str(exc.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_redirect_does_not_forward_secrets_to_another_host():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example/"})

    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)
    await _install_transport(client, handler)
    try:
        with pytest.raises(AsfAdapterError, match="重定向"):
            await client.fetch_credential()
        assert len(requests) == 1
        assert requests[0].url.host == "127.0.0.1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_asf_ipc_failure_pauses_and_survives_restart_without_secret_persistence():
    store = {}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401)

    provider = await _provider(handler, store)
    restarted = None
    try:
        assert not await provider.ensure_valid_access_token()
        assert not await provider.ensure_valid_access_token(force=True)
        assert provider.public_status()["paused"]
        assert provider.cooldown_remaining > 3500
        restarted = await _provider(handler, store)
        assert not await restarted.ensure_valid_access_token(force=True)
        assert len(requests) == 1
        assert "冷却" in await restarted.resume_after_review()
        assert PASSWORD not in store[ASF_STATE_KEY]
        assert KEY not in store[ASF_STATE_KEY]
        assert set(store) == {ASF_STATE_KEY}
    finally:
        await provider.close()
        if restarted:
            await restarted.close()


@pytest.mark.asyncio
async def test_success_uses_short_cache_and_persists_no_access_token():
    store = {}
    payload = _payload()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload)

    provider = await _provider(handler, store)
    try:
        assert await provider.ensure_valid_access_token()
        assert await provider.ensure_valid_access_token()
        assert len(requests) == 1
        assert await provider.ensure_valid_access_token(force=True)
        assert len(requests) == 2
        assert provider.access_token not in store[ASF_STATE_KEY]
        assert provider.access_token not in json.dumps(provider.public_status())
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_expired_or_offline_asf_never_falls_back_to_stale_config_token(tmp_path, plugins):
    plugin = plugins(
        tmp_path,
        **_settings(
            access_token="native_old_token",
            refresh_token="native_rt",
            cookie="native_cookie",
            account_name="native_acc",
            password="native_pw",
            shared_secret="native_secret",
        ),
    )
    _offline_client(plugin)
    await plugin.storage.init_db()
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("no native fallback")
    )
    provider = plugin._get_asf_provider()
    await _install_transport(
        provider._client,
        lambda _: httpx.Response(503, json={"Success": False, "ErrorCode": "bot_offline"}),
    )
    result = await plugin._check_and_notify()
    assert result.startswith("⚠️")
    assert "ASF" in result
    assert "未回退" in result
    plugin.auth_manager.ensure_valid_access_token.assert_not_called()
    plugin.client.get_shared_library.assert_not_called()
    assert plugin.config.access_token == "native_old_token"
    assert plugin.config.refresh_token == "native_rt"
    assert plugin._active_access_token == ""


@pytest.mark.asyncio
async def test_asf_token_drives_existing_sync_and_owner_notification_pipeline(tmp_path, plugins):
    plugin = plugins(
        tmp_path, **_settings(access_token="native_original", family_group_id="native_group")
    )
    _offline_client(plugin)
    await plugin.storage.init_db()
    token = _token()
    payload = _payload(token)
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=payload))
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("no native auth")
    )
    apps = [SharedApp(appid=100, name="Test Game", owner_steamids=[SID])]
    plugin.client.get_shared_library = AsyncMock(return_value=apps)
    before_config = copy.deepcopy(plugin.context.get_config.return_value)

    result = await plugin._check_and_notify()
    assert "首次同步成功" in result
    plugin.client.get_family_group_id.assert_awaited_once_with(token, SID)
    plugin.client.get_shared_library.assert_awaited_once_with(
        "group_id_1", token, filter_non_games=True
    )
    plugin.client.get_player_summaries.assert_awaited_once_with([SID], token)
    assert await plugin.storage.get_latest_snapshot()
    assert plugin.context.get_config.return_value == before_config
    assert plugin.config.access_token == "native_original"
    assert plugin.config.family_group_id == "native_group"
    assert await plugin.storage.get_kv("access_token") is None
    assert await plugin.storage.get_kv("refresh_token") is None
    assert token not in json.dumps(await plugin.storage.get_kv_all())
    plugin.auth_manager.ensure_valid_access_token.assert_not_called()


@pytest.mark.asyncio
async def test_asf_disabled_never_constructs_or_queries_provider_and_native_sync_unchanged(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings(enable_asf=False))
    _offline_client(plugin)
    await plugin.storage.init_db()
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(return_value=True)
    plugin.client.get_shared_library = AsyncMock(
        return_value=[SharedApp(appid=100, name="Native Game", owner_steamids=[SID])]
    )
    with patch(
        "main.AsfSessionProvider.__init__", side_effect=AssertionError("disabled means unused")
    ):
        result = await plugin._check_and_notify()
    assert "首次同步成功" in result
    assert plugin.asf_provider is None
    plugin.auth_manager.ensure_valid_access_token.assert_awaited_once()
    plugin.client.get_family_group_id.assert_awaited_once_with("token", SID)


@pytest.mark.asyncio
async def test_asf_does_not_restore_native_long_lived_credentials(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings(steam_id="", access_token=""))
    await plugin.storage.init_db()
    await plugin.storage.set_kv("access_token", "native_old_at")
    await plugin.storage.set_kv("refresh_token", "native_old_rt")
    plugin.auth_manager.ensure_state_loaded = AsyncMock(
        side_effect=AssertionError("do not touch native state")
    )
    await plugin._restore_credentials()
    assert plugin.config.access_token == ""
    assert plugin.config.refresh_token == ""
    assert plugin.config.steam_id == ""
    plugin.auth_manager.ensure_state_loaded.assert_not_called()


@pytest.mark.asyncio
async def test_asf_does_not_invoke_existing_login_commands_while_enabled(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    plugin.auth_manager.run_qr_login = AsyncMock(side_effect=AssertionError("no native QR"))
    plugin.auth_manager.relogin_with_password = AsyncMock(
        side_effect=AssertionError("no native password")
    )
    plugin.auth_manager.exchange_refresh_token = AsyncMock(
        side_effect=AssertionError("no native renewal")
    )
    event = _chat_event()
    for command in (plugin.cmd_qr_login, plugin.cmd_relogin, plugin.cmd_refresh):
        result = [r async for r in command(event)]
        assert "ASF 托管" in result[0]
    plugin.auth_manager.run_qr_login.assert_not_called()
    plugin.auth_manager.relogin_with_password.assert_not_called()
    plugin.auth_manager.exchange_refresh_token.assert_not_called()


@pytest.mark.asyncio
async def test_turning_asf_off_restores_native_source_without_mutating_credentials(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings(access_token="native_at"))
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload()))
    assert await provider.ensure_valid_access_token()
    assert plugin._active_access_token != "native_at"
    plugin.context.get_config.return_value["enable_asf"] = False
    plugin._refresh_config()
    assert plugin._active_access_token == "native_at"
    assert plugin._active_steam_id == SID
    await asyncio.sleep(0)
    assert provider.access_token == ""
    assert plugin.asf_provider is None


@pytest.mark.asyncio
async def test_steam_rejecting_asf_token_requires_a_different_token_before_retry(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    payload = _payload()
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=payload))
    assert await provider.ensure_valid_access_token()
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("never native fallback")
    )
    assert not await plugin._recover_after_auth_expired(SteamAuthExpiredError("rejected"))
    assert plugin._active_access_token == ""
    assert provider.cooldown_remaining > 0
    plugin.auth_manager.ensure_valid_access_token.assert_not_called()
    payload["Result"]["AccessToken"] = _token(jti="second")
    provider._cooldown_until = 0
    assert await provider.ensure_valid_access_token()


@pytest.mark.asyncio
async def test_steam_rate_limit_does_not_blacklist_still_valid_asf_token():
    payload = _payload()
    provider = await _provider(lambda _: httpx.Response(200, json=payload))
    try:
        assert await provider.ensure_valid_access_token()
        await provider.record_api_auth_failure(84)
        assert provider.cooldown_remaining > 1400
        assert not await provider.ensure_valid_access_token(force=True)
        provider._cooldown_until = 0
        assert await provider.ensure_valid_access_token()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_known_lockdown_persists_and_configuration_secret_change_cannot_clear_it():
    store = {}
    provider = await _provider(
        lambda _: httpx.Response(409, json={"Success": False, "ErrorCode": "account_locked"}), store
    )
    changed = None
    try:
        assert not await provider.ensure_valid_access_token()
        assert provider.security_blocked
        changed = await _provider(
            lambda _: httpx.Response(200, json=_payload()), store, bridge_key=KEY + "other"
        )
        assert not await changed.ensure_valid_access_token(force=True)
        assert changed.security_blocked
    finally:
        await provider.close()
        if changed:
            await changed.close()


@pytest.mark.asyncio
async def test_provider_reads_pin_account_on_first_use_and_rejects_switched_bot_after_restart():
    store = {}
    payload = _payload()
    provider = await _provider(lambda _: httpx.Response(200, json=payload), store, expected="")
    restarted = None
    try:
        assert await provider.ensure_valid_access_token()
        assert provider.steam_id == SID
        payload = _payload(steam_id=OTHER_SID)
        restarted = await _provider(lambda _: httpx.Response(200, json=payload), store, expected="")
        assert not await restarted.ensure_valid_access_token()
        assert restarted.public_status()["paused"]
        assert "不匹配" in restarted.last_note
    finally:
        await provider.close()
        if restarted:
            await restarted.close()


@pytest.mark.asyncio
async def test_asf_readonly_status_command_never_makes_ipc_request(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    provider.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("status must not request tokens")
    )
    status = [r async for r in plugin.cmd_asf_status(_chat_event())]
    assert "不发起 IPC" in status[0]
    assert PASSWORD not in status[0]
    assert KEY not in status[0]
    provider.ensure_valid_access_token.assert_not_called()


@pytest.mark.asyncio
async def test_status_loads_persistent_pause_without_contacting_asf():
    store = {}
    provider = await _provider(
        lambda _: httpx.Response(409, json={"Success": False, "ErrorCode": "account_locked"}),
        store,
    )
    restarted = None
    try:
        assert not await provider.ensure_valid_access_token()
        restarted = await _provider(
            lambda _: (_ for _ in ()).throw(AssertionError("status must not contact ASF")), store
        )
        status = await restarted.get_status()
        assert status["security_blocked"]
        assert restarted.access_token == ""
    finally:
        await provider.close()
        if restarted:
            await restarted.close()


@pytest.mark.asyncio
async def test_resume_never_ignores_configured_steam_id_mismatch_with_pinned_account():
    store = {}
    provider = await _provider(lambda _: httpx.Response(200, json=_payload()), store, expected="")
    changed = None
    try:
        assert await provider.ensure_valid_access_token()
        changed = await _provider(
            lambda _: httpx.Response(200, json=_payload()), store, expected=OTHER_SID
        )
        assert not await changed.ensure_valid_access_token()
        assert "不匹配" in await changed.resume_after_review()
        assert not await changed.ensure_valid_access_token(force=True)
        assert changed.access_token == ""
    finally:
        await provider.close()
        if changed:
            await changed.close()


@pytest.mark.asyncio
async def test_asf_web_status_omits_secrets_and_uses_independent_auth_state(tmp_path, plugins):
    plugin = plugins(
        tmp_path, **_settings(access_token="native_old_at", refresh_token="native_secret_rt")
    )
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    provider._credential = AsfCredential(SID, _token(), int(time.time()) + 7200)
    response = await plugin.web_api_get_data()
    text = json.dumps(response)
    assert provider.access_token not in text
    assert PASSWORD not in text and KEY not in text
    assert "native_secret_rt" not in text
    assert response["token_info"]["auth_source"] == "asf"
    assert response["token_info"]["has_refresh_token"] is False
    assert response["asf_info"]["bot_name"] == "main"


@pytest.mark.asyncio
async def test_invalid_asf_settings_fail_closed_without_altering_native_config(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings(asf_bridge_key="short"))
    await plugin.storage.init_db()
    _offline_client(plugin)
    original = plugin.config.model_dump()
    result = await plugin._check_and_notify()
    assert result.startswith("⚠️")
    assert plugin.config.model_dump() == original
    plugin.client.get_shared_library.assert_not_called()


@pytest.mark.asyncio
async def test_storage_failure_never_leaves_usable_asf_token():
    provider = await _provider(lambda _: httpx.Response(200, json=_payload()))
    provider._save_state = AsyncMock(side_effect=RuntimeError("database unavailable"))
    try:
        with pytest.raises(AsfAdapterError, match="保存 ASF 安全状态"):
            await provider.ensure_valid_access_token()
        assert provider.access_token == ""
        assert provider.public_status()["paused"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_state_read_failure_before_any_ipc_request():
    requests = []
    provider = await _provider(lambda r: requests.append(r) or httpx.Response(200, json=_payload()))
    provider._state_loaded = False
    provider._load_state = AsyncMock(side_effect=RuntimeError("db unavailable"))
    try:
        with pytest.raises(RuntimeError):
            await provider.ensure_valid_access_token()
        assert requests == []
        assert provider.access_token == ""
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_concurrent_force_requests_are_merged():
    requests = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        requests.append(request)
        started.set()
        await release.wait()
        return httpx.Response(200, json=_payload())

    provider = await _provider(handler)
    try:
        task = asyncio.create_task(provider.ensure_valid_access_token(force=True))
        await started.wait()
        duplicate = asyncio.create_task(provider.ensure_valid_access_token(force=True))
        await asyncio.sleep(0)
        release.set()
        assert await task
        assert await duplicate
        assert len(requests) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_asf_group_auth_resume_refused_and_resume_never_fetches_tokens(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    provider._security_blocked = True
    provider._state_loaded = True
    provider._client.fetch_credential = AsyncMock(
        side_effect=AssertionError("resume doesn't authenticate")
    )
    group = [r async for r in plugin.cmd_asf_resume(_chat_event("group"))]
    assert "私聊" in group[0]
    assert provider.security_blocked
    private = [r async for r in plugin.cmd_asf_resume(_chat_event())]
    assert private[0].startswith("✅")
    assert not provider.security_blocked
    provider._client.fetch_credential.assert_not_called()


@pytest.mark.asyncio
async def test_asf_steam_rate_limit_and_lockdown_never_change_native_safety_state(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings())
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload()))
    plugin.client.get_shared_library = AsyncMock(side_effect=SteamRateLimitError("limited"))
    result = await plugin._check_and_notify()
    assert result.startswith("⚠️")
    assert provider.cooldown_remaining > 0
    assert plugin.auth_manager.auth_cooldown_remaining == 0
    assert await plugin.storage.get_kv("steam_auth_state") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bot_offline", "token_unavailable"])
async def test_temporary_asf_failure_clears_cached_credential_and_never_uses_stale_token(kind):
    payload = _payload()
    provider = await _provider(lambda _: httpx.Response(200, json=payload))
    try:
        assert await provider.ensure_valid_access_token()
        payload = {"Success": False, "ErrorCode": kind}
        assert not await provider.ensure_valid_access_token(force=True)
        assert provider.access_token == ""
        assert provider.cooldown_remaining > 0
        assert not await provider.ensure_valid_access_token(force=True)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_ipc_network_error_is_sanitized_and_clears_credential():
    client = AsfIpcClient("http://127.0.0.1:1242", "main", PASSWORD, KEY)

    def handler(request):
        raise httpx.ConnectError(f"secret response {PASSWORD} {KEY}", request=request)

    await _install_transport(client, handler)
    try:
        with pytest.raises(AsfAdapterError) as exc:
            await client.fetch_credential(SID)
        assert PASSWORD not in str(exc.value)
        assert KEY not in str(exc.value)
        assert "未回退" in str(exc.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ipc_retry_after_cannot_be_bypassed_after_restart():
    store = {}
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    provider = await _provider(handler, store)
    restarted = None
    try:
        assert not await provider.ensure_valid_access_token()
        assert provider.cooldown_remaining > 7100
        restarted = await _provider(handler, store)
        assert not await restarted.ensure_valid_access_token(force=True)
        assert len(requests) == 1
    finally:
        await provider.close()
        if restarted:
            await restarted.close()


@pytest.mark.asyncio
async def test_cancelled_ipc_request_releases_lock_and_exposes_no_old_token():
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=_payload())

    provider = await _provider(handler)
    provider._credential = AsfCredential(SID, "old-in-memory-token", int(time.time()) + 7200)
    try:
        task = asyncio.create_task(provider.ensure_valid_access_token(force=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.access_token == ""
        release.set()
        assert await provider.ensure_valid_access_token(force=True)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_closed_provider_never_fetches_new_credential():
    requests = []
    provider = await _provider(
        lambda request: requests.append(request) or httpx.Response(200, json=_payload())
    )
    await provider.close()
    assert not await provider.ensure_valid_access_token(force=True)
    assert requests == []


@pytest.mark.asyncio
async def test_asf_known_steam_lockdown_stops_sync_without_modifying_native_security(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings())
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload()))
    plugin.client.get_shared_library = AsyncMock(
        side_effect=SteamAuthExpiredError("locked", eresult=73)
    )
    result = await plugin._check_and_notify()
    assert "ASF" in result and result.startswith("⚠️")
    assert provider.security_blocked
    assert not plugin.auth_manager.security_blocked
    assert await plugin.storage.get_kv("steam_auth_state") is None
    assert await plugin.storage.get_latest_snapshot() is None


@pytest.mark.asyncio
async def test_old_native_auth_state_does_not_get_reset_by_asf_resume(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    await plugin.auth_manager._record_failure(SteamLoginError("native locked", eresult=73))
    old_state = await plugin.storage.get_kv("steam_auth_state")
    provider = plugin._get_asf_provider()
    provider._security_blocked = True
    provider._state_loaded = True
    assert (await provider.resume_after_review()).startswith("✅")
    assert plugin.auth_manager.security_blocked
    assert await plugin.storage.get_kv("steam_auth_state") == old_state


@pytest.mark.asyncio
async def test_native_qr_password_and_refresh_commands_keep_original_calls_when_asf_disabled(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings(enable_asf=False, refresh_token="native-rt"))
    await plugin.storage.init_db()
    plugin.auth_manager.run_qr_login = AsyncMock(return_value="❌ native QR test result")
    plugin.auth_manager.relogin_with_password = AsyncMock(
        return_value="❌ native password test result"
    )
    plugin.auth_manager.exchange_refresh_token = AsyncMock(return_value=False)
    event = _chat_event()
    awaitable_results = [r async for r in plugin.cmd_qr_login(event)]
    assert "native QR test result" in awaitable_results[-1]
    plugin.auth_manager.run_qr_login.assert_awaited_once()
    assert "native password test result" in [r async for r in plugin.cmd_relogin(event)][-1]
    plugin.auth_manager.relogin_with_password.assert_awaited_once()
    assert plugin.auth_manager.relogin_with_password.call_args.kwargs["interactive"] is True
    [r async for r in plugin.cmd_refresh(event)]
    plugin.auth_manager.exchange_refresh_token.assert_awaited_once_with(force_new=True)
    assert plugin.asf_provider is None


@pytest.mark.asyncio
async def test_asf_startup_does_not_bootstrap_native_auth_and_schedules_existing_sync(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings(access_token="", steam_id=""))
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("do not bootstrap native auth")
    )
    plugin._check_and_notify = AsyncMock(return_value="✅ scheduled ASF sync")
    plugin._polling_loop = AsyncMock()
    plugin._drain_pending_notifications = AsyncMock()
    await plugin._init_and_start_polling()
    await asyncio.sleep(0)
    plugin.auth_manager.ensure_valid_access_token.assert_not_called()
    plugin._check_and_notify.assert_awaited_once()
    plugin._polling_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_switching_asf_off_during_sync_does_not_mix_in_native_credentials(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings(access_token="native_should_not_be_used"))
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    token = _token()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload(token)))

    async def get_group(access_token, steam_id):
        assert access_token == token and steam_id == SID
        plugin.context.get_config.return_value["enable_asf"] = False
        plugin._refresh_config()
        return "asf_group"

    plugin.client.get_family_group_id = AsyncMock(side_effect=get_group)
    plugin.client.get_shared_library = AsyncMock(
        return_value=[SharedApp(appid=100, name="ASF Game", owner_steamids=[SID])]
    )
    result = await plugin._check_and_notify()
    assert "首次同步成功" in result
    plugin.client.get_shared_library.assert_awaited_once_with(
        "asf_group", token, filter_non_games=True
    )
    plugin.client.get_playtime_summary.assert_awaited_once_with("asf_group", token)
    assert plugin.config.access_token == "native_should_not_be_used"


@pytest.mark.asyncio
async def test_asf_rejection_after_mode_switch_never_calls_native_recovery(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings(access_token="native_should_not_be_used"))
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    payload = _payload()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=payload))
    plugin.auth_manager.ensure_valid_access_token = AsyncMock(
        side_effect=AssertionError("an ASF sync cannot recover via native login")
    )

    async def reject(group_id, access_token, **kwargs):
        plugin.context.get_config.return_value["enable_asf"] = False
        plugin._refresh_config()
        raise SteamAuthExpiredError("rejected")

    plugin.client.get_shared_library = AsyncMock(side_effect=reject)
    result = await plugin._check_and_notify()
    assert "ASF" in result and "未回退" in result
    plugin.auth_manager.ensure_valid_access_token.assert_not_called()
    assert provider.access_token == ""


@pytest.mark.asyncio
async def test_expired_asf_access_token_is_not_used_for_family_api(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    await _install_transport(
        provider._client, lambda _: httpx.Response(200, json=_payload(_token(ttl=-1)))
    )
    result = await plugin._check_and_notify()
    assert "到期" in result
    assert provider.access_token == ""
    plugin.client.get_family_group_id.assert_not_called()
    plugin.client.get_shared_library.assert_not_called()


@pytest.mark.asyncio
async def test_existing_ownership_notifications_still_work_with_asf_source(tmp_path, plugins):
    from tests.test_notifications import _pushed_texts, fake_message_chain

    plugin = plugins(tmp_path, **_settings())
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.storage.subscribe("test_group")
    await plugin.storage.save_snapshot(
        [SharedApp(appid=100, name="Test Game", owner_steamids=[SID])]
    )
    await plugin.storage.save_member_profiles({OTHER_SID: {"persona_name": "New Owner"}})
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload()))
    plugin.client.get_shared_library = AsyncMock(
        return_value=[SharedApp(appid=100, name="Test Game", owner_steamids=[SID, OTHER_SID])]
    )
    with fake_message_chain():
        result = await plugin._check_and_notify()
    assert "已入队 1 条变动事件" in result
    assert any(
        "归属变动：新增义父 New Owner" in text
        for text in _pushed_texts(plugin.context.send_message)
    )
    assert await plugin.storage.pending_count() == 0


@pytest.mark.asyncio
async def test_asf_sources_keep_separate_local_pause_state_and_do_not_overwrite_each_other(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    main_provider = plugin._get_asf_provider()
    await _install_transport(
        main_provider._client,
        lambda _: httpx.Response(409, json={"Success": False, "ErrorCode": "account_locked"}),
    )
    assert not await main_provider.ensure_valid_access_token()
    main_key = asf_state_key(plugin.config.asf_ipc_url, "main")
    assert json.loads(await plugin.storage.get_kv(main_key))["security_blocked"]

    plugin.config.asf_bot_name = "another"
    another = plugin._get_asf_provider()
    payload = _payload()
    payload["Result"]["BotName"] = "another"
    await _install_transport(another._client, lambda _: httpx.Response(200, json=payload))
    assert await another.ensure_valid_access_token()
    another_key = asf_state_key(plugin.config.asf_ipc_url, "another")
    assert main_key != another_key
    assert json.loads(await plugin.storage.get_kv(main_key))["security_blocked"]
    assert not json.loads(await plugin.storage.get_kv(another_key))["security_blocked"]

    plugin.config.asf_bot_name = "main"
    restored = plugin._get_asf_provider()
    assert not await restored.ensure_valid_access_token()
    assert restored.security_blocked


@pytest.mark.asyncio
async def test_asf_config_changes_do_not_replace_provider_during_active_ipc_request(
    tmp_path, plugins
):
    plugin = plugins(tmp_path, **_settings())
    await plugin.storage.init_db()
    old = plugin._get_asf_provider()
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        started.set()
        await release.wait()
        return httpx.Response(200, json=_payload())

    await _install_transport(old._client, handler)
    task = asyncio.create_task(old.ensure_valid_access_token())
    await started.wait()
    plugin.config.asf_bridge_key = KEY + "changed"
    assert plugin._get_asf_provider() is old
    release.set()
    assert await task
    assert plugin._get_asf_provider() is not old


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"bound_steam_id": None},
        {"bound_steam_id": 76561198000000001},
        {"security_blocked": "false"},
        {"cooldown_until": "NaN"},
        {"rejected_token_hash": None},
        {"rejected_token_hash": "not-a-hash"},
    ],
)
async def test_corrupted_asf_security_state_blocks_reading_credentials(mutation):
    store = {}
    original = await _provider(lambda _: httpx.Response(200, json=_payload()), store)
    requests = []
    restored = None
    try:
        assert await original.ensure_valid_access_token()
        state = json.loads(store[ASF_STATE_KEY])
        state.update(mutation)
        store[ASF_STATE_KEY] = json.dumps(state)
        restored = await _provider(
            lambda request: requests.append(request) or httpx.Response(200, json=_payload()), store
        )
        assert not await restored.ensure_valid_access_token()
        assert restored.public_status()["paused"]
        assert restored.access_token == ""
        assert requests == []
    finally:
        await original.close()
        if restored:
            await restored.close()


@pytest.mark.asyncio
async def test_proxy_reload_is_deferred_until_asf_sync_finishes(tmp_path, plugins):
    plugin = plugins(tmp_path, **_settings())
    _offline_client(plugin)
    await plugin.storage.init_db()
    provider = plugin._get_asf_provider()
    await _install_transport(provider._client, lambda _: httpx.Response(200, json=_payload()))
    original_client = plugin.client
    original_auth = plugin.auth

    async def get_group(access_token, steam_id):
        plugin.context.get_config.return_value["proxy"] = "http://127.0.0.1:9999"
        plugin._refresh_config()
        assert plugin.client is original_client
        assert plugin.auth is original_auth
        assert plugin.config.proxy == ""
        return "asf_group"

    plugin.client.get_family_group_id = AsyncMock(side_effect=get_group)
    plugin.client.get_shared_library = AsyncMock(
        return_value=[SharedApp(appid=100, name="ASF Game", owner_steamids=[SID])]
    )
    assert "首次同步成功" in await plugin._check_and_notify()
    assert plugin._asf_sync_source is None
