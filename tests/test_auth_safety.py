"""Offline protocol/security regressions. Never use production Steam credentials."""

import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.auth_manager import AUTH_STATE_KEY, SteamAuthManager
from core.steam_auth import (
    GUARD_TYPE_DEVICE_CONFIRMATION,
    GUARD_TYPE_EMAIL_CONFIRMATION,
    GUARD_TYPE_MACHINE_TOKEN,
    PERSISTENCE_EPHEMERAL,
    PLATFORM_TYPE_MOBILE_APP,
    PLATFORM_TYPE_STEAM_CLIENT,
    PLATFORM_TYPE_WEB_BROWSER,
    SteamAuthClient,
    SteamLoginError,
)
from core.steam_auth_proto import _MESSAGES, decode_response, encode_request
from tests.test_steam_auth import GOLDEN_SECRET, _make_fake_jwt

SID = "76561198555000001"
OTHER_SID = "76561198555000002"


def _jwt(*, steamid=SID, aud=None, exp=9999999999):
    return _make_fake_jwt({"sub": steamid, "aud": aud or ["web", "renew"], "exp": exp})


def _credentials(**overrides):
    store = {
        "steam_id": SID,
        "account_name": "test_account",
        "password": "test_password",
        "shared_secret": GOLDEN_SECRET,
        "enable_auto_relogin": True,
    }
    store.update(overrides)
    auth = MagicMock()
    auth.device_name = "test-host (AstrBot Steam Family)"
    auth.refresh_tokens = AsyncMock(side_effect=SteamLoginError("network", eresult=3))
    auth.login_with_password = AsyncMock()
    auth.begin_qr_session = AsyncMock()
    cookie = AsyncMock(return_value=None)
    manager = SteamAuthManager(auth, lambda key: store.get(key, ""), store.__setitem__, cookie)
    return manager, auth, store, cookie


def _request_from_form(request, name):
    message = _MESSAGES[f"{name}_Request"]()
    parts = request.content.split(b'name="input_protobuf_encoded"')[1]
    encoded = parts.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0]
    message.ParseFromString(base64.b64decode(encoded))
    return message


def test_protocol_enums_are_not_device_confirmation_machine_token_swap():
    assert GUARD_TYPE_DEVICE_CONFIRMATION == 4
    assert GUARD_TYPE_EMAIL_CONFIRMATION == 5
    assert GUARD_TYPE_MACHINE_TOKEN == 6
    assert PERSISTENCE_EPHEMERAL == 0
    assert "AccessDenied" in str(SteamLoginError("failed", 15))
    assert "NeedTwoFactor" in str(SteamLoginError("failed", 85))
    assert "Throttle" in str(SteamLoginError("failed", 87))
    assert "AccountLockedDown" in str(SteamLoginError("failed", 73))


def test_protobuf_wire_vectors_preserve_uint64_fixed64_and_bytes():
    assert (
        base64.b64decode(encode_request("GetPasswordRSAPublicKey", {"account_name": "bob"}))
        == b"\x0a\x03bob"
    )
    poll = base64.b64decode(
        encode_request("PollAuthSessionStatus", {"client_id": "123", "request_id": "AQID"})
    )
    assert poll == b"\x08\x7b\x12\x03\x01\x02\x03"
    refresh = base64.b64decode(
        encode_request(
            "GenerateAccessTokenForApp", {"refresh_token": "r", "steamid": "42", "renewal_type": 1}
        )
    )
    assert refresh == b"\x0a\x01r\x11" + (42).to_bytes(8, "little") + b"\x18\x01"
    response = _MESSAGES["BeginAuthSessionViaQR_Response"](
        client_id=18446744073709551614,
        request_id=b"\x00\xff\x08",
        interval=5.0,
    )
    decoded = decode_response("BeginAuthSessionViaQR", response.SerializeToString())
    assert decoded["client_id"] == "18446744073709551614"
    assert decoded["request_id"] == "AP8I"


@pytest.mark.asyncio
async def test_web_login_sends_nested_device_data_and_guard_token_in_protobuf():
    captured = []

    def respond(request):
        method = request.url.path.strip("/").split("/")[1]
        captured.append((method, request))
        if method == "GetPasswordRSAPublicKey":
            payload = {
                "publickey_mod": "ff" * 128,
                "publickey_exp": "010001",
                "timestamp": "123456",
            }
        elif method == "BeginAuthSessionViaCredentials":
            payload = {"client_id": "123", "request_id": "AQID", "steamid": SID}
        else:
            payload = {
                "client_id": "124",
                "request_id": "AQID",
                "challenge_url": "https://s.team/q/test",
            }
        return httpx.Response(200, headers={"x-eresult": "1"}, json={"response": payload})

    client = SteamAuthClient(device_name="stable-test-host")
    await client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client._client = transport
        with patch("core.steam_auth.platform.system", return_value="Linux"):
            await client.begin_session_with_credentials(
                "test_account", "test_password", guard_data="guard"
            )
            await client.begin_qr_session()
    rsa = _MESSAGES["GetPasswordRSAPublicKey_Request"]()
    rsa.ParseFromString(base64.b64decode(captured[0][1].url.params["input_protobuf_encoded"]))
    assert rsa.account_name == "test_account"
    credentials = _request_from_form(captured[1][1], "BeginAuthSessionViaCredentials")
    assert credentials.website_id == "Community"
    assert credentials.persistence == 1 and credentials.remember_login is True
    assert credentials.device_details.device_friendly_name == "stable-test-host"
    assert credentials.device_details.platform_type == PLATFORM_TYPE_WEB_BROWSER
    assert credentials.device_details.os_type == -203
    assert credentials.guard_data == "guard"
    assert not credentials.HasField("platform_type")
    qr = _request_from_form(captured[2][1], "BeginAuthSessionViaQR")
    assert qr.device_details == credentials.device_details
    assert qr.website_id == "Community"
    assert "test_password" not in captured[1][1].content.decode()


def test_auth_response_fails_closed_and_keeps_security_eresult():
    for payload in ({}, {"response": None}, {"response": []}):
        response = httpx.Response(200, headers={"x-eresult": "1"}, json=payload)
        with pytest.raises(SteamLoginError):
            SteamAuthClient._parse_response(response, "test")
    with pytest.raises(SteamLoginError):
        SteamAuthClient._parse_response(httpx.Response(200, json={"response": {}}), "test")
    with pytest.raises(SteamLoginError) as locked:
        SteamAuthClient._parse_response(httpx.Response(403, headers={"x-eresult": "73"}), "test")
    assert locked.value.eresult == 73
    response = httpx.Response(429, headers={"retry-after": "3600"})
    with pytest.raises(SteamLoginError) as limited:
        SteamAuthClient._parse_response(response, "test")
    assert limited.value.retry_after == 3600


def test_auth_response_decodes_binary_and_rejects_malformed_protobuf():
    binary = _MESSAGES["PollAuthSessionStatus_Response"](
        refresh_token="rt", new_guard_data="guard"
    ).SerializeToString()
    resp = httpx.Response(
        200, headers={"x-eresult": "1", "content-type": "application/octet-stream"}, content=binary
    )
    assert SteamAuthClient._parse_response(resp, "poll", "PollAuthSessionStatus") == {
        "refresh_token": "rt",
        "new_guard_data": "guard",
    }
    invalid = httpx.Response(200, headers=resp.headers, content=b"\x80")
    with pytest.raises(SteamLoginError):
        SteamAuthClient._parse_response(invalid, "poll", "PollAuthSessionStatus")


@pytest.mark.asyncio
async def test_do_not_fake_steamclient_cm_or_start_new_mobile_login():
    with pytest.raises(ValueError, match="CM"):
        SteamAuthClient(platform_type=PLATFORM_TYPE_STEAM_CLIENT)
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)
    try:
        with pytest.raises(SteamLoginError, match="网页端"):
            await client.begin_qr_session()
        with pytest.raises(SteamLoginError, match="网页端"):
            await client.begin_session_with_credentials("a", "p")
        with pytest.raises(SteamLoginError, match="CM"):
            await client.refresh_tokens(_jwt(aud=["web", "client", "renew"]))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_qr_generation_is_local_and_rejects_untrusted_urls():
    client = SteamAuthClient()
    client._client.get = AsyncMock(side_effect=AssertionError("no network"))
    try:
        png = await client.fetch_qr_image("https://s.team/q/valid")
        assert png.startswith(b"\x89PNG\r\n\x1a\n")
        assert await client.fetch_qr_image("https://evil.example/q/phish") is None
        client._client.get.assert_not_called()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_poll_tracks_new_client_and_preserves_guard_and_refresh_only_result(caplog):
    client = SteamAuthClient()
    rt = _jwt()
    client.poll_auth_session = AsyncMock(
        side_effect=[
            {
                "new_client_id": "124",
                "new_challenge_url": "https://s.team/q/rotated",
                "new_guard_data": "guard",
            },
            {"refresh_token": rt, "account_name": "test_account"},
        ]
    )
    try:
        with patch("core.steam_auth.asyncio.sleep", new=AsyncMock()):
            result = await client.wait_for_session_tokens(
                "123", "AQID", challenge_url="https://s.team/q/original"
            )
        assert client.poll_auth_session.call_args_list[1].args == ("124", "AQID")
        assert result["access_token"] == ""
        assert result["steamid"] == SID
        assert result["new_guard_data"] == "guard"
        assert "s.team/q/rotated" not in caplog.text
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_interactive_confirmation_is_opt_in_and_uses_correct_enum():
    client = SteamAuthClient()
    client.begin_session_with_credentials = AsyncMock(
        return_value={
            "client_id": "123",
            "request_id": "AQID",
            "steamid": SID,
            "allowed_confirmations": [{"confirmation_type": 4}],
        }
    )
    client.wait_for_session_tokens = AsyncMock(return_value={"refresh_token": _jwt()})
    note = AsyncMock()
    try:
        with pytest.raises(SteamLoginError) as required:
            await client.login_with_password("test_account", "test_password")
        assert required.value.requires_confirmation
        client.wait_for_session_tokens.assert_not_called()
        await client.login_with_password(
            "test_account", "test_password", allow_interactive=True, on_confirmation_required=note
        )
        note.assert_awaited_once()
        client.wait_for_session_tokens.assert_awaited_once()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_guard_none_does_not_send_unnecessary_totp():
    client = SteamAuthClient()
    client.begin_session_with_credentials = AsyncMock(
        return_value={
            "client_id": "123",
            "request_id": "AQID",
            "steamid": SID,
            "allowed_confirmations": [{"confirmation_type": 1}, {"confirmation_type": 3}],
        }
    )
    client.submit_steam_guard_code = AsyncMock(side_effect=AssertionError("no code needed"))
    client.wait_for_session_tokens = AsyncMock(return_value={"refresh_token": _jwt()})
    try:
        await client.login_with_password(
            "test_account", "test_password", shared_secret=GOLDEN_SECRET
        )
        client.submit_steam_guard_code.assert_not_called()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_secret_fails_before_starting_device_login():
    client = SteamAuthClient()
    client.begin_session_with_credentials = AsyncMock(side_effect=AssertionError("no login"))
    try:
        with pytest.raises(SteamLoginError, match="Base64"):
            await client.login_with_password("a", "p", shared_secret="not-base64!")
        client.begin_session_with_credentials.assert_not_called()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_browser_refresh_reuses_session_and_only_transfers_to_steam():
    captured = []
    at = _jwt(aud=["web"])

    def respond(request):
        captured.append(request)
        if request.url.host == "login.steampowered.com":
            return httpx.Response(
                200,
                json={
                    "steamID": SID,
                    "transfer_info": [
                        {
                            "url": "https://store.steampowered.com/login/settoken",
                            "params": {"nonce": "nonce", "auth": "auth"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"result": 1},
            headers={"set-cookie": f"steamLoginSecure={SID}%7C%7C{at}; Secure; Path=/"},
        )

    client = SteamAuthClient()
    await client.close()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=False
    ) as transport:
        client._client = transport
        result = await client.refresh_tokens(_jwt())
    assert result["access_token"] == at
    assert result["refresh_token"] == ""
    assert len(captured) == 2
    assert captured[0].url.path == "/jwt/finalizelogin"
    assert captured[0].headers["origin"] == "https://steamcommunity.com"
    assert all("BeginAuthSession" not in request.url.path for request in captured)


@pytest.mark.asyncio
async def test_browser_refresh_validates_all_transfer_targets_before_sending_tokens():
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "transfer_info": [
                    {"url": "https://store.steampowered.com/login/settoken", "params": {}},
                    {"url": "https://evil.example/login/settoken", "params": {}},
                ]
            },
        )

    client = SteamAuthClient()
    await client.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
        client._client = transport
        with pytest.raises(SteamLoginError, match="非官方"):
            await client.refresh_tokens(_jwt())
    assert len(captured) == 1  # only Steam's finalizelogin was called


@pytest.mark.asyncio
async def test_rate_limit_persisted_and_force_refresh_cannot_bypass_it():
    manager, auth, store, cookie = _credentials(refresh_token=_jwt(), cookie="cookie")
    auth.refresh_tokens.side_effect = SteamLoginError("limited", eresult=84, retry_after=3600)
    with patch("core.auth_manager.time.time", return_value=1000):
        assert not await manager.ensure_valid_access_token(force=True)
        assert manager.auth_cooldown_remaining == 3600
        assert not await manager.exchange_refresh_token(force_new=True)
        assert not (await manager.run_qr_login()).startswith("✅")
        assert not (await manager.relogin_with_password(interactive=True)).startswith("✅")
        cookie.assert_not_called()
        auth.login_with_password.assert_not_called()
        restarted = SteamAuthManager(
            auth, lambda key: store.get(key, ""), store.__setitem__, cookie
        )
        restarted.restore_state(store[AUTH_STATE_KEY])
        assert not await restarted.exchange_refresh_token(force_new=True)
        assert "冷却" in await restarted.resume_after_security_review()
    assert auth.refresh_tokens.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [17, 43, 72, 73, 82, 105])
async def test_security_errors_stop_all_authentication_including_after_restart(result):
    manager, auth, store, cookie = _credentials(refresh_token=_jwt(), cookie="cookie")
    auth.refresh_tokens.side_effect = SteamLoginError("security", eresult=result)
    assert not await manager.ensure_valid_access_token(force=True)
    assert manager.security_blocked
    assert not await manager.refresh_via_cookie()
    assert "暂停" in await manager.relogin_with_password(interactive=True)
    assert "暂停" in await manager.run_qr_login()
    cookie.assert_not_called()
    restarted = SteamAuthManager(auth, lambda key: store.get(key, ""), store.__setitem__)
    restarted.restore_state(store[AUTH_STATE_KEY])
    assert restarted.security_blocked
    assert not await restarted.ensure_valid_access_token(force=True)
    assert auth.refresh_tokens.await_count == 1


@pytest.mark.asyncio
async def test_security_resume_does_not_attempt_login():
    manager, auth, store, _ = _credentials()
    await manager._record_failure(SteamLoginError("locked", eresult=73))
    assert (await manager.resume_after_security_review()).startswith("✅")
    assert not manager.security_blocked
    auth.login_with_password.assert_not_called()
    assert "test_password" not in store[AUTH_STATE_KEY]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [3, 15, 16, 20])
async def test_temporary_session_failure_never_escalates_to_new_password_login(result):
    manager, auth, _, _ = _credentials(refresh_token=_jwt())
    auth.refresh_tokens.side_effect = SteamLoginError("temporary", eresult=result)
    assert not await manager.ensure_valid_access_token(force=True)
    auth.login_with_password.assert_not_called()
    assert "不因网络错误" in manager.last_keepalive_note


@pytest.mark.asyncio
async def test_cookie_reuse_precedes_expensive_password_login():
    manager, auth, _, cookie = _credentials(refresh_token=_jwt(exp=1), cookie="cookie")
    auth.refresh_tokens.side_effect = SteamLoginError("expired", eresult=27)
    cookie.return_value = _jwt(aud=["web"])
    assert await manager.ensure_valid_access_token(force=True)
    cookie.assert_awaited_once()
    auth.login_with_password.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_password_pauses_auto_login_instead_of_retrying_each_poll():
    manager, auth, store, _ = _credentials()
    auth.login_with_password.side_effect = SteamLoginError("bad password", eresult=5)
    with patch("core.auth_manager.time.time", return_value=1000):
        assert (await manager.relogin_with_password()).startswith("❌")
    with patch("core.auth_manager.time.time", return_value=10000):
        assert not await manager.ensure_valid_access_token(force=True)
    assert manager.automatic_login_paused
    assert auth.login_with_password.await_count == 1
    restarted = SteamAuthManager(auth, lambda key: store.get(key, ""), store.__setitem__)
    restarted.restore_state(store[AUTH_STATE_KEY])
    assert restarted.automatic_login_paused


@pytest.mark.asyncio
async def test_remembered_guard_data_is_account_scoped_and_persisted():
    manager, auth, store, _ = _credentials(
        steam_guard_data="oldguard", steam_guard_account="TEST_ACCOUNT"
    )
    auth.login_with_password.return_value = {
        "steamid": SID,
        "account_name": "test_account",
        "refresh_token": _jwt(),
        "access_token": _jwt(),
        "new_guard_data": "newguard",
    }
    assert (await manager.relogin_with_password()).startswith("✅")
    assert auth.login_with_password.call_args.kwargs["guard_data"] == "oldguard"
    assert store["steam_guard_data"] == "newguard"
    manager2, auth2, _, _ = _credentials(
        steam_guard_data="otherguard", steam_guard_account="someone_else"
    )
    auth2.login_with_password.return_value = auth.login_with_password.return_value
    await manager2.relogin_with_password()
    assert "guard_data" not in auth2.login_with_password.call_args.kwargs


@pytest.mark.asyncio
async def test_qr_wrong_account_does_not_replace_existing_tokens():
    manager, auth, store, _ = _credentials(access_token="original_at", refresh_token="original_rt")
    auth.begin_qr_session.return_value = {
        "client_id": "123",
        "request_id": "AQID",
        "challenge_url": "https://s.team/q/test",
    }
    auth.wait_for_session_tokens = AsyncMock(
        return_value={
            "steamid": OTHER_SID,
            "account_name": "test_account",
            "refresh_token": _jwt(steamid=OTHER_SID),
            "access_token": _jwt(steamid=OTHER_SID),
        }
    )
    result = await manager.run_qr_login()
    assert "不匹配" in result
    assert store["refresh_token"] == "original_rt"
    assert store["access_token"] == "original_at"


@pytest.mark.asyncio
async def test_auth_writes_complete_before_attempting_network():
    store = {"account_name": "a", "password": "p", "steam_id": SID}
    saved = []
    auth = MagicMock()

    async def persist(key, value):
        await asyncio.sleep(0)
        store[key] = value
        saved.append(key)

    async def login(*args, **kwargs):
        assert AUTH_STATE_KEY in saved
        return {"steamid": SID, "refresh_token": _jwt(), "access_token": _jwt()}

    auth.login_with_password = AsyncMock(side_effect=login)
    manager = SteamAuthManager(auth, lambda key: store.get(key, ""), persist)
    assert (await manager.relogin_with_password()).startswith("✅")
    assert saved.index("refresh_token") < saved.index("access_token")


@pytest.mark.asyncio
async def test_qr_images_only_callback_and_invalid_rotation_fail_closed():
    manager, auth, _, _ = _credentials()
    auth.begin_qr_session.return_value = {
        "client_id": "123",
        "request_id": "AQID",
        "challenge_url": "https://s.team/q/test",
    }
    auth.fetch_qr_image = AsyncMock(return_value=None)
    auth.wait_for_session_tokens = AsyncMock(
        return_value={"steamid": SID, "refresh_token": _jwt(), "access_token": _jwt()}
    )
    assert (await manager.run_qr_login(send_image=AsyncMock(), send_text=None)).startswith("✅")


@pytest.mark.asyncio
async def test_unknown_confirmation_type_and_missing_session_stop_before_poll():
    client = SteamAuthClient()
    client.begin_session_with_credentials = AsyncMock(
        return_value={
            "client_id": "123",
            "request_id": "AQID",
            "steamid": SID,
            "allowed_confirmations": [{"confirmation_type": 999}],
        }
    )
    client.poll_auth_session = AsyncMock(side_effect=AssertionError("no polling"))
    try:
        with pytest.raises(SteamLoginError, match="未知"):
            await client.login_with_password("a", "p")
        client.begin_session_with_credentials.return_value = {"steamid": SID}
        with pytest.raises(SteamLoginError, match="完整"):
            await client.login_with_password("a", "p")
        client.poll_auth_session.assert_not_called()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_browser_guard_token_runs_checkdevice_not_unverified_polling():
    client = SteamAuthClient()
    client.begin_session_with_credentials = AsyncMock(
        return_value={
            "client_id": "123",
            "request_id": "AQID",
            "steamid": SID,
            "allowed_confirmations": [{"confirmation_type": 2}, {"confirmation_type": 6}],
        }
    )
    client.wait_for_session_tokens = AsyncMock(return_value={"refresh_token": _jwt()})
    check = AsyncMock()
    try:
        with patch.object(client, "_check_browser_machine_auth", check):
            await client.login_with_password("a", "p", guard_data="guard")
        check.assert_awaited_once_with("123", SID, "guard")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_qr_success_uses_local_callbacks_saves_guard_and_rotates_rt_without_repeat_login():
    manager, auth, store, _ = _credentials()
    order = []
    rt = _jwt()
    new_rt = _jwt(aud=["web", "mobile", "renew"])
    auth.begin_qr_session.return_value = {
        "client_id": "123",
        "request_id": "AQID",
        "challenge_url": "https://s.team/q/test",
    }
    auth.wait_for_session_tokens = AsyncMock(
        return_value={
            "steamid": SID,
            "account_name": "test_account",
            "refresh_token": rt,
            "new_guard_data": "guard",
        }
    )

    async def exchange(*args, **kwargs):
        assert store["refresh_token"] == rt
        assert store["steam_guard_data"] == "guard"
        order.append("exchange")
        return {"steamid": SID, "access_token": _jwt(aud=["web"]), "refresh_token": new_rt}

    auth.refresh_tokens = AsyncMock(side_effect=exchange)
    assert (await manager.run_qr_login()).startswith("✅")
    assert order == ["exchange"]
    assert store["refresh_token"] == new_rt
    auth.begin_qr_session.assert_awaited_once()
    auth.login_with_password.assert_not_called()


@pytest.mark.asyncio
async def test_new_login_success_cannot_start_second_login_within_minimum_interval():
    manager, auth, _, _ = _credentials()
    auth.login_with_password.return_value = {
        "steamid": SID,
        "refresh_token": _jwt(),
        "access_token": _jwt(),
    }
    with patch("core.auth_manager.time.time", return_value=1000):
        assert (await manager.relogin_with_password()).startswith("✅")
        assert "间隔" in await manager.relogin_with_password(interactive=True)
        assert "间隔" in await manager.run_qr_login()
    assert auth.login_with_password.await_count == 1


@pytest.mark.asyncio
async def test_concurrent_exchange_is_single_and_cookie_does_not_run_during_qr():
    manager, auth, _, cookie = _credentials(refresh_token=_jwt())
    started = asyncio.Event()
    release = asyncio.Event()

    async def exchange(*args, **kwargs):
        started.set()
        await release.wait()
        return {"steamid": SID, "access_token": _jwt()}

    auth.refresh_tokens = AsyncMock(side_effect=exchange)
    task = asyncio.create_task(manager.exchange_refresh_token())
    await started.wait()
    assert not await manager.exchange_refresh_token()
    assert not await manager.refresh_via_cookie()
    assert not await manager.ensure_valid_access_token(force=True)
    release.set()
    assert await task
    auth.refresh_tokens.assert_awaited_once()
    cookie.assert_not_called()


@pytest.mark.asyncio
async def test_ordinary_refresh_failure_cooldown_also_blocks_force_and_survives_restart():
    manager, auth, store, _ = _credentials(refresh_token=_jwt())
    with patch("core.auth_manager.time.time", return_value=1000):
        assert not await manager.exchange_refresh_token(force_new=True)
        assert not await manager.exchange_refresh_token(force_new=True)
        restarted = SteamAuthManager(auth, lambda key: store.get(key, ""), store.__setitem__)
        restarted.restore_state(store[AUTH_STATE_KEY])
        assert not await restarted.exchange_refresh_token(force_new=True)
    assert auth.refresh_tokens.await_count == 1


@pytest.mark.asyncio
async def test_retry_after_http_date_is_respected():
    response = httpx.Response(429, headers={"retry-after": "Thu, 01 Jan 1970 01:00:00 GMT"})
    with patch("core.steam_auth.time.time", return_value=1000):
        with pytest.raises(SteamLoginError) as exc:
            SteamAuthClient._parse_response(response, "limited")
    assert exc.value.retry_after == 2600


@pytest.mark.asyncio
async def test_no_auth_request_escapes_persistent_security_block_on_main_sync(tmp_path):
    from tests.test_notifications import _make_plugin, _offline_client

    plugin = _make_plugin(tmp_path)
    _offline_client(plugin)
    await plugin.storage.init_db()
    await plugin.auth_manager._record_failure(SteamLoginError("locked", eresult=73))
    msg = await plugin._check_and_notify_core()
    assert "暂停" in msg
    plugin.client.get_shared_library.assert_not_called()
    plugin.client.get_family_group_id.assert_not_called()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_credential_restore_uses_newer_kv_and_preserves_guard_security_state(tmp_path):
    import json

    from tests.test_notifications import _make_plugin

    old = _make_fake_jwt({"sub": SID, "iat": 100, "exp": 9999999999})
    new = _make_fake_jwt({"sub": SID, "iat": 200, "exp": 9999999999})
    plugin = _make_plugin(tmp_path, access_token=old, refresh_token=old, steam_id=SID)
    await plugin.storage.init_db()
    await plugin.storage.set_kv("access_token", new)
    await plugin.storage.set_kv("refresh_token", new)
    await plugin.storage.set_kv("steam_guard_data", "guard")
    await plugin.storage.set_kv("steam_guard_account", "test_account")
    await plugin.storage.set_kv("auth_device_name", "persisted-host")
    await plugin.storage.set_kv(AUTH_STATE_KEY, json.dumps({"security_blocked": True}))
    await plugin._restore_credentials()
    assert plugin.config.access_token == new
    assert plugin.config.refresh_token == new
    assert plugin.auth_manager.security_blocked
    assert plugin.auth.device_name == "persisted-host"
    assert plugin.auth_manager._get_cred("steam_guard_data") == "guard"
    await plugin.terminate()


@pytest.mark.asyncio
async def test_save_credentials_command_only_saves_does_not_login(tmp_path):
    from tests.test_main import _chat_event
    from tests.test_notifications import _make_plugin

    plugin = _make_plugin(tmp_path)
    await plugin.storage.init_db()
    plugin.auth_manager.relogin_with_password = AsyncMock(
        side_effect=AssertionError("do not login implicitly")
    )
    result = [r async for r in plugin.cmd_cred(_chat_event(), "account", "password", GOLDEN_SECRET)]
    assert "不会立即创建新登录设备" in result[0]
    plugin.auth_manager.relogin_with_password.assert_not_called()
    if plugin._bg_tasks:
        await asyncio.gather(*list(plugin._bg_tasks))
    await plugin.terminate()


@pytest.mark.asyncio
async def test_qr_and_password_commands_refuse_group_chat(tmp_path):
    from tests.test_main import _chat_event
    from tests.test_notifications import _make_plugin

    plugin = _make_plugin(tmp_path)
    plugin.auth_manager.run_qr_login = AsyncMock()
    plugin.auth_manager.relogin_with_password = AsyncMock()
    assert "私聊" in [r async for r in plugin.cmd_qr_login(_chat_event("group"))][0]
    assert "私聊" in [r async for r in plugin.cmd_relogin(_chat_event("group"))][0]
    plugin.auth_manager.run_qr_login.assert_not_called()
    plugin.auth_manager.relogin_with_password.assert_not_called()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_lazy_state_loader_runs_before_manual_login_even_during_startup():
    import json

    auth = MagicMock()
    auth.begin_qr_session = AsyncMock(side_effect=AssertionError("state must block login"))
    loader = AsyncMock(return_value=json.dumps({"security_blocked": True}))
    manager = SteamAuthManager(auth, lambda _: "", lambda *_: None, load_state_fn=loader)
    assert "暂停" in await manager.run_qr_login()
    auth.begin_qr_session.assert_not_called()
    loader.assert_awaited_once()
    assert "暂停" in await manager.relogin_with_password(interactive=True)
    loader.assert_awaited_once()


@pytest.mark.asyncio
async def test_state_loader_failure_does_not_send_auth_request():
    auth = MagicMock()
    auth.begin_qr_session = AsyncMock(side_effect=AssertionError("no login"))
    manager = SteamAuthManager(
        auth,
        lambda _: "",
        lambda *_: None,
        load_state_fn=AsyncMock(side_effect=RuntimeError("db failure")),
    )
    with pytest.raises(RuntimeError, match="db failure"):
        await manager.run_qr_login()
    auth.begin_qr_session.assert_not_called()


def test_corrupt_auth_state_fails_closed():
    manager, _, _, _ = _credentials()
    manager.restore_state("broken json")
    assert manager.security_blocked
    assert "损坏" in manager.last_keepalive_note


@pytest.mark.asyncio
async def test_access_token_not_expired_never_triggers_early_password_login():
    import time

    manager, auth, _, _ = _credentials(access_token=_jwt(exp=int(time.time()) + 600))
    assert await manager.ensure_valid_access_token()
    auth.login_with_password.assert_not_called()
    assert "尚未到期" in manager.last_keepalive_note


@pytest.mark.asyncio
async def test_explicit_proxy_is_not_overridden_by_environment():
    client = SteamAuthClient()
    from core.steam_api import SteamFamilyClient

    family = SteamFamilyClient()
    try:
        assert client._client._trust_env is False
        assert family._client._trust_env is False
    finally:
        await client.close()
        await family.close()


@pytest.mark.asyncio
async def test_family_api_locked_result_does_not_trigger_force_relogin(tmp_path):
    from core.steam_api import SteamAuthExpiredError, SteamFamilyClient
    from tests.test_notifications import _make_plugin, _offline_client

    resp = httpx.Response(200, headers={"x-eresult": "73"}, json={"response": {}})
    with pytest.raises(SteamAuthExpiredError) as error:
        SteamFamilyClient._check_auth_result(resp)
    assert error.value.eresult == 73
    plugin = _make_plugin(tmp_path)
    _offline_client(plugin)
    await plugin.storage.init_db()
    plugin.auth.refresh_tokens = AsyncMock(
        side_effect=AssertionError("must not refresh locked account")
    )
    plugin.auth.login_with_password = AsyncMock(
        side_effect=AssertionError("must not login locked account")
    )
    assert not await plugin._recover_after_auth_expired(error.value)
    assert plugin.auth_manager.security_blocked
    plugin.auth.refresh_tokens.assert_not_called()
    plugin.auth.login_with_password.assert_not_called()
    await plugin.terminate()


@pytest.mark.asyncio
async def test_cookie_explicit_lock_blocks_password_fallback():
    from core.steam_api import SteamAuthExpiredError

    manager, auth, _, cookie = _credentials(cookie="cookie")
    cookie.side_effect = SteamAuthExpiredError("locked", eresult=73)
    assert not await manager.ensure_valid_access_token(force=True)
    assert manager.security_blocked
    auth.login_with_password.assert_not_called()


@pytest.mark.asyncio
async def test_browser_finalizelogin_preserves_lock_result_on_403():
    client = SteamAuthClient()
    await client.close()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403, headers={"x-eresult": "73"}))
    ) as transport:
        client._client = transport
        with pytest.raises(SteamLoginError) as exc:
            await client.refresh_tokens(_jwt())
    assert exc.value.eresult == 73


@pytest.mark.asyncio
async def test_refresh_account_mismatch_rejected_before_any_network_call():
    client = SteamAuthClient()
    client._post = AsyncMock(side_effect=AssertionError("must not send mismatched identity"))
    try:
        with pytest.raises(SteamLoginError, match="不匹配"):
            await client.refresh_tokens(_jwt(), steamid=OTHER_SID)
        client._post.assert_not_called()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_transient_login_failures_stop_at_three_attempts():
    manager, auth, _, _ = _credentials()
    auth.login_with_password.side_effect = SteamLoginError("network", eresult=3)
    for now in (1000, 3000, 5000, 7000):
        with patch("core.auth_manager.time.time", return_value=now):
            await manager.relogin_with_password()
    assert manager.automatic_login_paused
    assert auth.login_with_password.await_count == 3


@pytest.mark.asyncio
async def test_cancelled_login_retains_throttle_and_releases_mutex():
    manager, auth, _, _ = _credentials()
    started = asyncio.Event()

    async def blocked_login(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    auth.login_with_password = AsyncMock(side_effect=blocked_login)
    with patch("core.auth_manager.time.time", return_value=1000):
        task = asyncio.create_task(manager.relogin_with_password())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not manager._ops_busy
        assert "间隔" in await manager.relogin_with_password()


@pytest.mark.asyncio
async def test_config_reload_does_not_change_proxy_during_auth_session(tmp_path):
    from tests.test_notifications import _make_plugin

    plugin = _make_plugin(tmp_path)
    old_auth = plugin.auth
    old_client = plugin.client
    plugin.context.get_config.return_value["proxy"] = "http://127.0.0.1:9999"
    plugin.auth_manager._ops_busy = True
    plugin._refresh_config()
    assert plugin.auth is old_auth
    assert plugin.client is old_client
    assert plugin.config.proxy == ""
    plugin.auth_manager._ops_busy = False
    await plugin.terminate()


@pytest.mark.asyncio
async def test_restore_prefers_last_rotated_rt_even_when_issued_in_same_second(tmp_path):
    from tests.test_notifications import _make_plugin

    old = _make_fake_jwt({"sub": SID, "iat": 100, "exp": 9999999999, "jti": "old"})
    new = _make_fake_jwt({"sub": SID, "iat": 100, "exp": 9999999999, "jti": "new"})
    plugin = _make_plugin(tmp_path, refresh_token=old, steam_id=SID)
    await plugin.storage.init_db()
    await plugin.storage.set_kv("refresh_token", new)
    await plugin.storage.set_kv("steam_refresh_account_id", SID)
    await plugin._restore_credentials()
    assert plugin.config.refresh_token == new
    await plugin.terminate()


@pytest.mark.asyncio
async def test_expired_refresh_token_is_not_sent_to_steam_again():
    manager, auth, _, _ = _credentials(refresh_token=_jwt(exp=1), enable_auto_relogin=False)
    assert not await manager.ensure_valid_access_token(force=True)
    auth.refresh_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_poll_request_obeys_overall_timeout_not_http_timeout():
    client = SteamAuthClient()

    async def stalled_poll(*args):
        await asyncio.Event().wait()

    client.poll_auth_session = AsyncMock(side_effect=stalled_poll)
    try:
        with pytest.raises(SteamLoginError, match="超时"):
            await client.wait_for_session_tokens("123", "AQID", timeout=0.01)
    finally:
        await client.close()
