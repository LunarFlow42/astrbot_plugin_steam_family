"""Steam "永不掉线" 保活协议单元测试（路线一全自动重登 + 路线二滚动续期）。"""

import base64
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.auth_manager import SteamAuthManager
from core.steam_auth import (
    PLATFORM_TYPE_MOBILE_APP,
    SteamAuthClient,
    SteamLoginError,
    encrypt_password_rsa,
    generate_steam_guard_code,
    generate_steam_guard_code_for_time,
    steamid_from_refresh_token,
)

# 由标准 Steam Guard 算法独立预计算的黄金向量:
# secret = b"SteamGuardTestSecret16" 的 Base64
GOLDEN_SECRET = "U3RlYW1HdWFyZFRlc3RTZWNyZXQxNg=="
GOLDEN_VECTORS = {
    1000000000: "8F87V",
    1760000000: "68793",
    1760000030: "XRK6Y",
}


# ---------------------------------------------------------------------------
# Steam Guard 本地算码 (路线一核心能力)
# ---------------------------------------------------------------------------
def test_generate_steam_guard_code_golden_vectors():
    for ts, expected in GOLDEN_VECTORS.items():
        assert generate_steam_guard_code_for_time(GOLDEN_SECRET, ts) == expected


def test_generate_steam_guard_code_30s_window_alignment():
    # 对齐到时间窗边界: 同一窗口（base 与 base+29）验证码一致，跨窗（base+30）变化
    t = 1760000000
    base = t - (t % 30)
    code_base = generate_steam_guard_code_for_time(GOLDEN_SECRET, base)
    assert generate_steam_guard_code_for_time(GOLDEN_SECRET, base + 29) == code_base
    assert generate_steam_guard_code_for_time(GOLDEN_SECRET, base + 30) != code_base


def test_generate_steam_guard_code_offset_matches_future_window():
    # 偏移 -30s 应等于上一时间窗的验证码
    t = 1760000000
    prev_window = generate_steam_guard_code_for_time(GOLDEN_SECRET, t - 30)
    with patch("core.steam_auth.time.time", return_value=t):
        assert generate_steam_guard_code(GOLDEN_SECRET, offset_seconds=-30) == prev_window


def test_generate_steam_guard_code_shape():
    code = generate_steam_guard_code(GOLDEN_SECRET)
    assert len(code) == 5
    assert all(c in "23456789BCDFGHJKMNPQRTVWXY" for c in code)


# ---------------------------------------------------------------------------
# RSA PKCS#1 v1.5 密码加密 (纯 Python 实现)
# ---------------------------------------------------------------------------
class _TinyRSAKey:
    """测试用 RSA 密钥（Miller-Rabin 现场生成 256bit 质数，还原私钥以验证加密内容与 PKCS#1 v1.5 填充结构）。"""

    def __init__(self, bits: int = 256, e_candidates=(65537, 257, 17, 5, 3)):
        import random

        rng = random.Random(1337)
        for _ in range(200):
            p = self._gen_prime(bits, rng)
            q = self._gen_prime(bits, rng)
            if p == q:
                continue
            self.p, self.q = p, q
            self.n = p * q
            self.key_len = (self.n.bit_length() + 7) // 8
            phi = (p - 1) * (q - 1)
            for e in e_candidates:
                if self._gcd(e, phi) == 1:
                    self.e = e
                    self.d = pow(e, -1, phi)
                    return
        raise ValueError("no valid RSA key")

    @staticmethod
    def _is_probable_prime(n: int, rng, rounds: int = 24) -> bool:
        if n < 2:
            return False
        for sp in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % sp == 0:
                return n == sp
        d = n - 1
        r = 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for _ in range(rounds):
            a = rng.randrange(2, n - 1)
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = pow(x, 2, n)
                if x == n - 1:
                    break
            else:
                return False
        return True

    @classmethod
    def _gen_prime(cls, bits: int, rng) -> int:
        while True:
            candidate = rng.getrandbits(bits) | (1 << (bits - 1)) | 1
            if cls._is_probable_prime(candidate, rng):
                return candidate

    @staticmethod
    def _gcd(a: int, b: int) -> int:
        while b:
            a, b = b, a % b
        return a


TEST_KEY = _TinyRSAKey(bits=256)


def test_encrypt_password_rsa_roundtrip():
    password = "s3cret-密码-Passw0rd!"
    ciphertext_b64 = encrypt_password_rsa(
        password, format(TEST_KEY.n, "x"), format(TEST_KEY.e, "x")
    )
    cipher = int.from_bytes(base64.b64decode(ciphertext_b64), "big")
    assert cipher < TEST_KEY.n, "RSA 密文必须小于模数"

    plaintext = pow(cipher, TEST_KEY.d, TEST_KEY.n).to_bytes(TEST_KEY.key_len, "big")
    # PKCS#1 v1.5 type 2 结构: 0x00 0x02 || 非0随机填充 || 0x00 || 消息
    assert plaintext[0] == 0x00 and plaintext[1] == 0x02
    sep = plaintext.index(b"\x00", 2)
    padding = plaintext[2:sep]
    assert len(padding) >= 8, "PKCS#1 v1.5 要求填充至少 8 字节"
    assert all(b != 0x00 for b in padding)
    assert plaintext[sep + 1 :].decode("utf-8") == password


def test_encrypt_password_rsa_non_deterministic():
    mod_hex, exp_hex = format(TEST_KEY.n, "x"), format(TEST_KEY.e, "x")
    c1 = encrypt_password_rsa("same_password", mod_hex, exp_hex)
    c2 = encrypt_password_rsa("same_password", mod_hex, exp_hex)
    assert c1 != c2, "PKCS#1 v1.5 随机填充应使相同明文产生不同密文"


def test_encrypt_password_rsa_too_long():
    with pytest.raises(SteamLoginError):
        encrypt_password_rsa("x" * 200, format(TEST_KEY.n, "x"), format(TEST_KEY.e, "x"))


# ---------------------------------------------------------------------------
# Refresh Token JWT 解析
# ---------------------------------------------------------------------------
def _make_fake_jwt(payload: dict) -> str:
    import json as _json

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.dummy_signature"


def test_steamid_from_refresh_token():
    rt = _make_fake_jwt({"aud": ["web", "mobile"], "sub": "76561198555000001", "exp": 9999999999})
    assert steamid_from_refresh_token(rt) == "76561198555000001"


def test_steamid_from_refresh_token_invalid():
    assert steamid_from_refresh_token("garbage") == ""
    assert steamid_from_refresh_token("") == ""


# ---------------------------------------------------------------------------
# SteamAuthClient 响应解析 / eresult 错误处理
# ---------------------------------------------------------------------------
def _fake_response(status_code=200, json_body=None, eresult="1"):
    resp = MagicMock()
    resp.status_code = status_code
    if json_body is None:
        json_body = {"response": {}}
    resp.json.return_value = json_body
    headers = {"x-eresult": eresult} if eresult is not None else {}
    resp.headers = headers
    return resp


@pytest.mark.asyncio
async def test_parse_response_raises_on_eresult_error():
    resp = _fake_response(
        json_body={"response": {}}, eresult="5", status_code=200
    )
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "凭据登录")
    assert exc.value.eresult == 5
    assert "InvalidPassword" in str(exc.value)


@pytest.mark.asyncio
async def test_parse_response_ok():
    resp = _fake_response(
        json_body={"response": {"access_token": "abc"}}, eresult="1"
    )
    assert SteamAuthClient._parse_response(resp, "续期") == {"access_token": "abc"}


# ---------------------------------------------------------------------------
# _parse_response fail-closed 语义（HTTP >= 400 / HTML 页 / 乱码头一律抛错）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parse_response_429_without_header_raises_rate_limit():
    """HTTP 429 且无 x-eresult 头：必须按 EResult 84 限流抛错，不允许 fail-open 空结果。"""
    resp = _fake_response(status_code=429, eresult=None)
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "轮询登录状态")
    assert exc.value.eresult == 84
    assert "RateLimitExceeded" in str(exc.value)


@pytest.mark.asyncio
async def test_parse_response_5xx_fails_closed():
    """HTTP 5xx（即使无 x-eresult 头）必须抛错而非静默成功。"""
    resp = _fake_response(status_code=503, eresult=None)
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "轮询登录状态")
    assert "503" in str(exc.value)


def test_parse_response_html_body_fails_closed():
    """HTML 错误页（响应体非 JSON）必须抛"响应体异常"，不允许静默降级为成功空 dict。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {}
    resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "凭据登录")
    assert "响应体异常" in str(exc.value)


def test_parse_response_non_dict_json_fails_closed():
    """响应体是合法 JSON 但结构不符合 {"response": ...} 预期时同样按异常处理。"""
    resp = _fake_response(status_code=200, json_body=["unexpected"], eresult="1")
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "凭据登录")
    assert "响应体异常" in str(exc.value)


def test_parse_response_garbled_eresult_header_fails_closed():
    """x-eresult 头存在但 int() 解析失败：必须抛错，不允许误判为成功。"""
    resp = _fake_response(
        status_code=200, json_body={"response": {"keep": 1}}, eresult="garbled"
    )
    with pytest.raises(SteamLoginError) as exc:
        SteamAuthClient._parse_response(resp, "凭据登录")
    assert "x-eresult" in str(exc.value)


def test_parse_response_valid_empty_response_ok():
    """合法场景保留：2xx + {"response": {}} + x-eresult=1（QR 轮询待确认态）照常返回空响应。"""
    resp = _fake_response(status_code=200, json_body={"response": {}}, eresult="1")
    assert SteamAuthClient._parse_response(resp, "轮询登录状态") == {}


# ---------------------------------------------------------------------------
# TOTP 对时 (ITwoFactorService/QueryTime)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_query_server_time_parses_server_time():
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)
    captured = {}

    class _Stub:
        async def post(self, url, data=None):
            captured["url"] = url
            resp = MagicMock()
            resp.json.return_value = {
                "response": {"server_time": "1760000000", "skew_time_seconds": "0"}
            }
            return resp

    client._client = _Stub()
    assert await client.query_server_time() == 1760000000
    assert "ITwoFactorService/QueryTime" in captured["url"]


@pytest.mark.asyncio
async def test_query_server_time_unreachable_returns_none():
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)

    class _Stub:
        async def post(self, url, data=None):
            raise httpx.ConnectError("network unreachable")

    client._client = _Stub()
    assert await client.query_server_time() is None


@pytest.mark.asyncio
async def test_query_server_time_invalid_body_returns_none():
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)

    class _Stub:
        async def post(self, url, data=None):
            resp = MagicMock()
            resp.json.return_value = {"response": {"server_time": "not-a-number"}}
            return resp

    client._client = _Stub()
    assert await client.query_server_time() is None


@pytest.mark.asyncio
async def test_login_with_password_totp_uses_server_time_and_retries_next_window():
    """TOTP 对时：验证码锚定服务器时间生成；88 被拒后自动换 +30 秒窗重试成功。"""
    client = SteamAuthClient()
    submitted: list[str] = []
    submit_calls = {"n": 0}

    async def fake_query_server_time():
        return 1760000000

    async def flaky_submit(client_id, steamid, code, code_type=3):
        submit_calls["n"] += 1
        submitted.append(code)  # 记录每一次实际提交的验证码（含被拒的）
        if submit_calls["n"] == 1:
            raise SteamLoginError("提交 Steam 令牌验证码 失败", eresult=88)

    async def fake_begin(account_name, password, persistence=1):
        return {
            "client_id": "123",
            "request_id": "req==",
            "interval": 0.1,
            "steamid": "76561198555000001",
            "allowed_confirmations": [{"confirmation_type": 3}],
        }

    async def fake_poll(client_id, request_id):
        return {"refresh_token": "rt_200d", "access_token": "at_24h", "account_name": "acc"}

    with (
        patch.object(client, "query_server_time", side_effect=fake_query_server_time),
        patch.object(client, "begin_session_with_credentials", side_effect=fake_begin),
        patch.object(client, "submit_steam_guard_code", side_effect=flaky_submit),
        patch.object(client, "poll_auth_session", side_effect=fake_poll),
        patch("core.steam_auth.time.time", return_value=1760000000.0),
    ):
        result = await client.login_with_password(
            "myaccount", "mypassword", shared_secret=GOLDEN_SECRET
        )

    # 黄金向量：1760000000 -> "68793"（被 88 拒），1760000030 -> "XRK6Y"（+30 秒窗重试成功）
    assert submitted == ["68793", "XRK6Y"], "首码被拒 (88) 后必须换 +30 秒时间窗重试"
    assert submit_calls["n"] == 2
    assert result["refresh_token"] == "rt_200d"


@pytest.mark.asyncio
async def test_login_with_password_totp_attempts_capped_at_3():
    """连续 88 被拒：总尝试不超过 3 次（含 +30 / −30 窗），随后抛出 88，绝不无限撞码触发 84/85。"""
    client = SteamAuthClient()
    submit_calls = {"n": 0}

    async def always_88(client_id, steamid, code, code_type=3):
        submit_calls["n"] += 1
        raise SteamLoginError("提交 Steam 令牌验证码 失败", eresult=88)

    async def fake_begin(account_name, password, persistence=1):
        return {
            "client_id": "123",
            "request_id": "req==",
            "interval": 0.1,
            "steamid": "76561198555000001",
            "allowed_confirmations": [{"confirmation_type": 3}],
        }

    with (
        patch.object(client, "query_server_time", new=AsyncMock(return_value=1760000000)),
        patch.object(client, "begin_session_with_credentials", side_effect=fake_begin),
        patch.object(client, "submit_steam_guard_code", side_effect=always_88),
        patch("core.steam_auth.time.time", return_value=1760000000.0),
    ):
        with pytest.raises(SteamLoginError) as exc:
            await client.login_with_password(
                "myaccount", "mypassword", shared_secret=GOLDEN_SECRET
            )

    assert submit_calls["n"] == 3, "Steam max_attempts=3：撞码超过 3 次会触发限流节流"
    assert exc.value.eresult == 88


@pytest.mark.asyncio
async def test_login_with_password_degrades_when_query_time_unreachable():
    """QueryTime 服务器不可达：按 offset=0 降级，验证码用本地时间生成，登录流程不受阻。"""
    client = SteamAuthClient()
    submitted: list[str] = []

    class _OfflineClient:
        async def post(self, url, data=None):
            raise httpx.ConnectError("network unreachable")

    client._client = _OfflineClient()

    async def fake_submit(client_id, steamid, code, code_type=3):
        submitted.append(code)

    async def fake_begin(account_name, password, persistence=1):
        return {
            "client_id": "123",
            "request_id": "req==",
            "interval": 0.1,
            "steamid": "76561198555000001",
            "allowed_confirmations": [{"confirmation_type": 3}],
        }

    async def fake_poll(client_id, request_id):
        return {"refresh_token": "rt_200d", "access_token": "at_24h", "account_name": "acc"}

    with (
        patch.object(client, "begin_session_with_credentials", side_effect=fake_begin),
        patch.object(client, "submit_steam_guard_code", side_effect=fake_submit),
        patch.object(client, "poll_auth_session", side_effect=fake_poll),
        patch("core.steam_auth.time.time", return_value=1760000000.0),
    ):
        result = await client.login_with_password(
            "myaccount", "mypassword", shared_secret=GOLDEN_SECRET
        )

    assert submitted == ["68793"], "对时失败应按 offset=0 用本地时间算码"
    assert result["refresh_token"] == "rt_200d"


@pytest.mark.asyncio
async def test_refresh_tokens_builds_correct_form():
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)
    rt = _make_fake_jwt({"aud": ["web", "mobile"], "sub": "76561198555000001", "exp": 9999999999})
    client._client = MagicMock()

    captured = {}

    class _AsyncClientStub:
        async def post(self, url, data=None):
            captured["url"] = url
            captured["data"] = data
            return _fake_response(
                json_body={
                    "response": {
                        "access_token": "new_access",
                        "refresh_token": "brand_new_refresh",
                    }
                },
                eresult="1",
            )

    client._client = _AsyncClientStub()
    result = await client.refresh_tokens(rt, steamid="", renewal=True)

    assert "GenerateAccessTokenForApp" in captured["url"]
    assert captured["data"]["renewal_type"] == "1", "必须携带 renewal_type=1 才能滚动续期"
    assert captured["data"]["refresh_token"] == rt
    assert captured["data"]["steamid"] == "76561198555000001", "steamid 应自动从 JWT 推导"
    assert result["access_token"] == "new_access"
    assert result["refresh_token"] == "brand_new_refresh"


# ---------------------------------------------------------------------------
# SteamAuthManager: 路线一 / 路线二编排逻辑
# ---------------------------------------------------------------------------
class _MemCreds:
    def __init__(self, **kwargs):
        self.store = {
            "access_token": kwargs.get("access_token", ""),
            "refresh_token": kwargs.get("refresh_token", ""),
            "steam_id": kwargs.get("steam_id", ""),
            "account_name": kwargs.get("account_name", ""),
            "password": kwargs.get("password", ""),
            "shared_secret": kwargs.get("shared_secret", ""),
            "cookie": kwargs.get("cookie", ""),
            "enable_auto_relogin": kwargs.get("enable_auto_relogin", True),
        }
        self.saved = {}

    def get(self, name):
        return self.store.get(name, "")

    def set(self, name, value):
        self.store[name] = value
        self.saved[name] = value


def _make_jwt(token_payload: dict) -> str:
    import json as _json

    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(_json.dumps(token_payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _fresh_access_token(hours=24):
    return _make_jwt(
        {
            "aud": ["web", "mobile"],
            "sub": "76561198555000001",
            "exp": int(time.time()) + hours * 3600,
            "iat": int(time.time()),
        }
    )


@pytest.mark.asyncio
async def test_ensure_valid_skips_network_when_token_fresh():
    creds = _MemCreds(access_token=_fresh_access_token(hours=24))
    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(),
    )
    # Token 剩 24h, 预检必须直接放行且不得发起任何网络请求
    manager.auth.refresh_tokens = AsyncMock(side_effect=AssertionError("should not refresh"))
    assert await manager.ensure_valid_access_token() is True
    manager.auth.refresh_tokens.assert_not_called()


@pytest.mark.asyncio
async def test_rolling_renewal_saves_new_refresh_token():
    """路线二核心：Steam 下发新 refresh_token 时必须立即持久化。"""
    rt = _make_jwt({"aud": ["web"], "sub": "76561198555000001", "exp": int(time.time()) + 3600})
    new_rt = _make_jwt(
        {"aud": ["web"], "sub": "76561198555000001", "exp": int(time.time()) + 200 * 86400}
    )
    creds = _MemCreds(refresh_token=rt, steam_id="76561198555000001")

    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(return_value=None),
    )
    manager.auth.refresh_tokens = AsyncMock(
        return_value={"access_token": _fresh_access_token(), "refresh_token": new_rt}
    )
    assert await manager.ensure_valid_access_token() is True
    assert creds.store["refresh_token"] == new_rt, "新 Refresh Token 必须被持久化"
    assert "brand_new" not in creds.store["refresh_token"]
    assert manager.auth.refresh_tokens.call_args.kwargs["renewal"] is True


@pytest.mark.asyncio
async def test_password_relogin_fallback_when_refresh_token_dead():
    """路线一：Refresh Token 失效后自动触发账号密码重登。"""
    dead_rt = _make_jwt({"aud": ["web"], "sub": "76561198555000001", "exp": 1})
    creds = _MemCreds(
        refresh_token=dead_rt,
        account_name="myaccount",
        password="mypassword",
        shared_secret="c2VjcmV0c2VjcmV0c2VjcmV0MTIz",
    )

    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(return_value=None),
    )
    # refresh_token 兑换失败（彻底失效）
    manager.auth.refresh_tokens = AsyncMock(
        side_effect=SteamLoginError("兑换 Access Token 失败", eresult=15)
    )
    # 密码重登成功，领到全新凭据
    fresh_at = _fresh_access_token()
    manager.auth.login_with_password = AsyncMock(
        return_value={
            "access_token": fresh_at,
            "refresh_token": "fresh_rt_200d",
            "steamid": "76561198555000001",
            "account_name": "myaccount",
        }
    )

    assert await manager.ensure_valid_access_token() is True
    manager.auth.login_with_password.assert_awaited_once_with(
        "myaccount", "mypassword", shared_secret="c2VjcmV0c2VjcmV0c2VjcmV0MTIz"
    )
    assert creds.store["refresh_token"] == "fresh_rt_200d"
    assert creds.store["access_token"] == fresh_at


@pytest.mark.asyncio
async def test_password_relogin_disabled_by_config():
    dead_rt = _make_jwt({"aud": ["web"], "sub": "76561198555000001", "exp": 1})
    creds = _MemCreds(
        refresh_token=dead_rt,
        account_name="myaccount",
        password="mypassword",
        shared_secret="x",
        enable_auto_relogin=False,
    )
    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(return_value=None),
    )
    manager.auth.refresh_tokens = AsyncMock(
        side_effect=SteamLoginError("兑换失败", eresult=15)
    )
    manager.auth.login_with_password = AsyncMock(
        side_effect=AssertionError("auto relogin disabled but attempted")
    )
    assert await manager.ensure_valid_access_token() is False
    manager.auth.login_with_password.assert_not_called()


@pytest.mark.asyncio
async def test_cookie_fallback_when_all_else_fails():
    dead_rt = _make_jwt({"aud": ["web"], "sub": "76561198555000001", "exp": 1})
    fresh_at = _fresh_access_token()
    creds = _MemCreds(refresh_token=dead_rt, cookie="steamLoginSecure=xxx")

    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(return_value=fresh_at),
    )
    manager.auth.refresh_tokens = AsyncMock(
        side_effect=SteamLoginError("兑换失败", eresult=15)
    )
    assert await manager.ensure_valid_access_token() is True
    assert creds.store["access_token"] == fresh_at


@pytest.mark.asyncio
async def test_relogin_busy_guard_when_ops_busy():
    """操作互斥：已有登录/续期会话（如扫码确认等待中）时，密码重登不得并行发起。"""
    creds = _MemCreds(account_name="myaccount", password="mypassword", shared_secret="s")
    manager = SteamAuthManager(
        auth=MagicMock(), get_cred=creds.get, set_cred=creds.set
    )
    manager._ops_busy = True
    result = await manager.relogin_with_password()
    assert result.startswith("⏳"), "忙碌期必须给等待提示而非发起第二条登录"
    manager.auth.login_with_password = AsyncMock(
        side_effect=AssertionError("busy 期间不得发起登录")
    )


@pytest.mark.asyncio
async def test_exchange_busy_guard_skips():
    """操作互斥：忙碌期间 Refresh Token 兑换直接跳过（False），并留 note 供看板展示。"""
    creds = _MemCreds(refresh_token="rt.some.sub")
    manager = SteamAuthManager(auth=MagicMock(), get_cred=creds.get, set_cred=creds.set)
    manager.auth.refresh_tokens = AsyncMock(
        side_effect=AssertionError("busy 期间不得发起兑换")
    )
    manager._ops_busy = True
    assert await manager.exchange_refresh_token() is False
    assert "跳过" in manager.last_keepalive_note


@pytest.mark.asyncio
async def test_relogin_requires_credentials():
    creds = _MemCreds()
    manager = SteamAuthManager(
        auth=MagicMock(), get_cred=creds.get, set_cred=creds.set
    )
    result = await manager.relogin_with_password()
    assert result.startswith("⚠️")
    assert "账号名" in result


@pytest.mark.asyncio
async def test_describe_mode_priorities():
    creds = _MemCreds(
        account_name="a", password="p", shared_secret="s", refresh_token="rt"
    )
    manager = SteamAuthManager(auth=MagicMock(), get_cred=creds.get, set_cred=creds.set)
    # 账号+密码+秘钥 齐备 -> 2FA 全自动重登
    assert "2FA" in manager.describe_mode()

    # 缺秘钥 -> 提示密码重登不完整
    creds.store["shared_secret"] = ""
    assert "shared_secret" in manager.describe_mode()

    # 清空密码凭据 -> 只剩 refresh_token -> 滚动续期
    creds.store["account_name"] = ""
    assert "滚动续期" in manager.describe_mode()

    # 清空 refresh_token, 有 Cookie -> Cookie 静默续签
    creds.store["refresh_token"] = ""
    creds.store["cookie"] = "c"
    assert "Cookie" in manager.describe_mode()


# ---------------------------------------------------------------------------
# 登录流程编排 (BeginAuthSessionViaCredentials -> 2FA -> Poll)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_with_password_submits_local_steam_guard_code():
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)

    submitted_codes = []

    async def fake_submit(client_id, steamid, code, code_type=3):
        submitted_codes.append(code)

    async def fake_begin(account_name, password, persistence=1):
        assert account_name == "myaccount"
        assert password == "mypassword"
        return {
            "client_id": "123456789",
            "request_id": "req==",
            "interval": 0.1,
            "steamid": "76561198555000001",
            "allowed_confirmations": [{"confirmation_type": 3}],
        }

    async def fake_poll(client_id, request_id):
        assert client_id == "123456789"
        return {
            "refresh_token": "rt_200d",
            "access_token": "at_24h",
            "account_name": "myaccount",
        }

    with (
        patch.object(client, "begin_session_with_credentials", side_effect=fake_begin),
        patch.object(client, "submit_steam_guard_code", side_effect=fake_submit),
        patch.object(client, "poll_auth_session", side_effect=fake_poll),
        # 服务器对时不可达 -> offset=0 降级（本用例只关注本地算码与提交链路）
        patch.object(client, "query_server_time", new=AsyncMock(return_value=None)),
    ):
        result = await client.login_with_password(
            "myaccount", "mypassword", shared_secret=GOLDEN_SECRET
        )

    assert len(submitted_codes) == 1
    assert result["refresh_token"] == "rt_200d"
    assert result["steamid"] == "76561198555000001"
    # 本地算出的码必须与黄金向量同属 Steam 字符表
    assert all(c in "23456789BCDFGHJKMNPQRTVWXY" for c in submitted_codes[0])


@pytest.mark.asyncio
async def test_login_with_password_requires_secret_for_device_guard():
    client = SteamAuthClient()

    async def fake_begin(account, password, persistence=1):
        return {
            "client_id": "1",
            "request_id": "r",
            "interval": 0.1,
            "steamid": "123",
            "allowed_confirmations": [{"confirmation_type": 3}],
        }

    with patch.object(client, "begin_session_with_credentials", side_effect=fake_begin):
        with pytest.raises(SteamLoginError) as exc:
            await client.login_with_password("myaccount", "mypassword", shared_secret="")
    assert "shared_secret" in str(exc.value)


@pytest.mark.asyncio
async def test_login_with_password_device_confirmation_only_fails_fast():
    """仅 confirmation_type=6（手机确认弹窗）保护：直接快败并说明原因，绝不进轮询干等 60 秒超时。"""
    client = SteamAuthClient()

    async def fake_begin(account, password, persistence=1):
        return {
            "client_id": "1",
            "request_id": "r",
            "interval": 0.1,
            "steamid": "123",
            "allowed_confirmations": [{"confirmation_type": 6}],
        }

    poll_mock = AsyncMock(return_value={})
    with (
        patch.object(client, "begin_session_with_credentials", side_effect=fake_begin),
        patch.object(client, "poll_auth_session", poll_mock),
    ):
        # 即使配置了 shared_secret 也无法自动过手机确认弹窗，必须同样快败
        with pytest.raises(SteamLoginError) as exc:
            await client.login_with_password("myaccount", "mypassword", shared_secret=GOLDEN_SECRET)
    assert "手机确认弹窗" in str(exc.value)
    poll_mock.assert_not_called()


# ---------------------------------------------------------------------------
# QR 扫码轮换：new_challenge_url 检测与回调通知
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_wait_for_session_tokens_notifies_challenge_rotation():
    """轮询响应出现全新 new_challenge_url 时触发轮换回调；与旧值相同时不重复触发。"""
    client = SteamAuthClient()
    poll_results = [
        {},  # 待确认，无轮换
        {"new_challenge_url": "https://s.team/q/NEW2"},  # 轮换 -> 回调一次
        {"new_challenge_url": "https://s.team/q/NEW2"},  # 与上次相同 -> 不回调
        {"refresh_token": "rt_final", "access_token": "at_final", "account_name": "acc"},
    ]
    rotations: list[str] = []

    async def on_rotated(url: str):
        rotations.append(url)

    async def fake_poll(client_id, request_id):
        return poll_results.pop(0)

    with patch.object(client, "poll_auth_session", side_effect=fake_poll):
        result = await client.wait_for_session_tokens(
            "client-1",
            "req==",
            poll_interval=0.01,
            timeout=5.0,
            challenge_url="https://s.team/q/ORIG",
            on_challenge_rotated=on_rotated,
        )

    assert rotations == ["https://s.team/q/NEW2"]
    assert result["refresh_token"] == "rt_final"


@pytest.mark.asyncio
async def test_wait_for_session_tokens_rotation_without_callback_is_safe():
    """on_challenge_rotated 缺省 (None) 时遇到轮换不报错，轮询照常继续。"""
    client = SteamAuthClient()
    poll_results = [
        {"new_challenge_url": "https://s.team/q/NEW2"},
        {"refresh_token": "rt_final", "access_token": "at_final"},
    ]

    async def fake_poll(client_id, request_id):
        return poll_results.pop(0)

    with patch.object(client, "poll_auth_session", side_effect=fake_poll):
        result = await client.wait_for_session_tokens(
            "client-1",
            "req==",
            poll_interval=0.01,
            timeout=5.0,
            challenge_url="https://s.team/q/ORIG",
        )

    assert result["refresh_token"] == "rt_final"


@pytest.mark.asyncio
async def test_wait_for_session_tokens_swallows_rotation_callback_error():
    """轮换回调自身抛错只记日志，不得中断轮询（以拿tokens为先）。"""
    client = SteamAuthClient()

    async def bad_on_rotated(url: str):
        raise RuntimeError("send failed")

    poll_results = [
        {"new_challenge_url": "https://s.team/q/NEW2"},
        {"refresh_token": "rt_final", "access_token": "at_final"},
    ]

    async def fake_poll(client_id, request_id):
        return poll_results.pop(0)

    with patch.object(client, "poll_auth_session", side_effect=fake_poll):
        result = await client.wait_for_session_tokens(
            "client-1",
            "req==",
            poll_interval=0.01,
            timeout=5.0,
            on_challenge_rotated=bad_on_rotated,
        )

    assert result["refresh_token"] == "rt_final"


@pytest.mark.asyncio
async def test_run_qr_login_resends_rotated_qr():
    """run_qr_login 实现轮换回调：二维码被 Steam 轮换时重新拉取并重发新二维码与链接。"""
    client = SteamAuthClient()
    creds = _MemCreds()
    manager = SteamAuthManager(auth=client, get_cred=creds.get, set_cred=creds.set)
    texts: list[str] = []
    images: list[bytes] = []

    async def send_text(t: str):
        texts.append(t)

    async def send_image(data: bytes):
        images.append(data)

    poll_results = [
        {"new_challenge_url": "https://s.team/q/NEW2"},
        {"refresh_token": "rt_200d", "access_token": "at_24h", "account_name": "acc"},
    ]

    async def fake_poll(client_id, request_id):
        return poll_results.pop(0)

    async def fake_fetch_qr(url: str):
        return f"PNG::{url}".encode()

    begin_mock = AsyncMock(
        return_value={
            "challenge_url": "https://s.team/q/ORIG",
            "client_id": "c1",
            "request_id": "r1",
            "interval": 0.01,
        }
    )
    with (
        patch.object(client, "begin_qr_session", begin_mock),
        patch.object(client, "poll_auth_session", side_effect=fake_poll),
        patch.object(client, "fetch_qr_image", side_effect=fake_fetch_qr),
    ):
        result = await manager.run_qr_login(
            send_text=send_text, send_image=send_image, timeout_seconds=5.0
        )

    assert result.startswith("✅")
    assert images == [
        b"PNG::https://s.team/q/ORIG",
        b"PNG::https://s.team/q/NEW2",
    ], "二维码轮换后必须用新链接重新渲染并重发"
    assert any("s.team/q/NEW2" in t for t in texts), "轮换后必须提示用户扫描新链接"
    assert creds.store["refresh_token"] == "rt_200d"


# ---------------------------------------------------------------------------
# refresh_tokens 边缘情况：AT 缺失时保住全新 200 天 Refresh Token
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_refresh_tokens_keeps_new_refresh_token_when_access_token_missing():
    """Steam 只顺延了新 RT 而 access_token 为空：不得抛错丢掉 200 天新令牌。"""
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)
    rt = _make_fake_jwt(
        {"aud": ["web", "mobile"], "sub": "76561198555000001", "exp": 9999999999}
    )

    class _AsyncClientStub:
        async def post(self, url, data=None):
            return _fake_response(
                json_body={"response": {"refresh_token": "brand_new_refresh"}}, eresult="1"
            )

    client._client = _AsyncClientStub()
    result = await client.refresh_tokens(rt, steamid="", renewal=True)
    assert result["refresh_token"] == "brand_new_refresh"
    assert result["access_token"] == ""


@pytest.mark.asyncio
async def test_refresh_tokens_raises_when_nothing_returned():
    """既没有 access_token 也没有新 refresh_token：仍按失败抛错。"""
    client = SteamAuthClient(platform_type=PLATFORM_TYPE_MOBILE_APP)
    rt = _make_fake_jwt(
        {"aud": ["web", "mobile"], "sub": "76561198555000001", "exp": 9999999999}
    )

    class _AsyncClientStub:
        async def post(self, url, data=None):
            return _fake_response(json_body={"response": {}}, eresult="1")

    client._client = _AsyncClientStub()
    with pytest.raises(SteamLoginError):
        await client.refresh_tokens(rt, steamid="", renewal=True)


@pytest.mark.asyncio
async def test_exchange_refresh_token_saves_new_rt_even_without_access_token():
    """manager 编排：先无条件持久化新 RT，再判断 AT——AT 缺失记 note 返回 False。"""
    rt = _make_jwt({"aud": ["web"], "sub": "76561198555000001", "exp": int(time.time()) + 3600})
    new_rt = _make_jwt(
        {"aud": ["web"], "sub": "76561198555000001", "exp": int(time.time()) + 200 * 86400}
    )
    creds = _MemCreds(refresh_token=rt, steam_id="76561198555000001")
    manager = SteamAuthManager(
        auth=MagicMock(),
        get_cred=creds.get,
        set_cred=creds.set,
        cookie_refresh_fn=AsyncMock(return_value=None),
    )
    manager.auth.refresh_tokens = AsyncMock(
        return_value={"access_token": "", "refresh_token": new_rt, "steamid": "76561198555000001"}
    )

    assert await manager.exchange_refresh_token() is False
    assert creds.store["refresh_token"] == new_rt, "即使 AT 缺失，新 Refresh Token 也必须先持久化"
    assert "access_token" not in creds.saved, "AT 缺失时不得把空 access_token 写入凭据"
    assert manager.last_keepalive_note, "AT 缺失时必须记录保活 note"
