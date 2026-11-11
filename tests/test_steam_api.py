import time
from unittest.mock import MagicMock, patch

import httpx
import pytest

from core.steam_api import AppDetails, SteamFamilyClient
from tests.mock_data import MOCK_FAMILY_APPS_RESPONSE


@pytest.mark.asyncio
async def test_parse_shared_apps():
    client = SteamFamilyClient()
    apps = client.parse_shared_apps(MOCK_FAMILY_APPS_RESPONSE)
    assert len(apps) == 2
    assert apps[0].appid == 1086940
    assert apps[0].name == "Baldur's Gate 3"
    assert "76561198000000001" in apps[0].owner_steamids


@pytest.mark.asyncio
async def test_filter_non_games():
    client = SteamFamilyClient()
    raw_data = {
        "response": {
            "apps": [
                {
                    "appid": 100,
                    "name": "Valid Game",
                    "ownerSteamids": ["123"],
                    "appType": 1,
                },
                {
                    "appid": 101,
                    "name": "Palworld Dedicated Server",
                    "ownerSteamids": ["123"],
                    "is_free": True,
                },
                {
                    "appid": 102,
                    "name": "Cyberpunk 2077 Soundtrack",
                    "ownerSteamids": ["123"],
                    "is_free": True,
                },
                {
                    "appid": 103,
                    "name": "Excluded Game",
                    "ownerSteamids": ["123"],
                    "excludeReason": 1,
                },
            ]
        }
    }
    apps = client.parse_shared_apps(raw_data, filter_non_games=True)
    assert len(apps) == 1
    assert apps[0].appid == 100


@pytest.mark.asyncio
async def test_refresh_token_via_cookie():
    client = SteamFamilyClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "success": 1,
        "data": {"webapi_token": "refreshed_jwt_token_123"},
    }
    with patch.object(client._client, "get", return_value=mock_resp):
        token = await client.refresh_token_via_cookie("mock_steamLoginSecure_value")
        assert token == "refreshed_jwt_token_123"


def test_decode_steam_jwt():
    import base64
    import json
    import time

    from core.steam_api import decode_steam_jwt

    # Create dummy JWT
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    exp_future = int(time.time()) + 7200
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"sub": "76561198000000001", "exp": exp_future, "iat": int(time.time())}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    dummy_token = f"{header}.{payload}.dummy_signature"

    info = decode_steam_jwt(dummy_token)
    assert info is not None
    assert info["valid"] is True
    assert info["steamid"] == "76561198000000001"
    assert info["is_expired"] is False
    assert info["remaining_hours"] >= 1.9


@pytest.mark.asyncio
async def test_get_shared_library_and_playtime():
    client = SteamFamilyClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "entries": [{"steamid": "76561198000000001", "appid": 1086940, "seconds_played": 3600}]
        }
    }
    with patch.object(client._client, "get", return_value=mock_resp) as mock_get:
        entries = await client.get_playtime_summary("12345", "token")
        assert len(entries) == 1
        assert entries[0]["appid"] == 1086940
        mock_get.assert_called_once()


@pytest.mark.asyncio
async def test_get_player_summaries():
    client = SteamFamilyClient()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "response": {
            "accounts": [
                {
                    "public_data": {
                        "steamid": "76561198000000001",
                        "persona_name": "义父测试员",
                        "sha_digest_avatar": "MDEyMzQ1Njc4OWFiY2RlZjAxMjM=",
                    }
                }
            ]
        }
    }
    with patch.object(client._client, "get", return_value=mock_resp):
        summaries = await client.get_player_summaries(["76561198000000001"], "test_access_token")
        assert "76561198000000001" in summaries
        assert summaries["76561198000000001"]["persona_name"] == "义父测试员"
        assert "avatars.steamstatic.com" in summaries["76561198000000001"]["avatar_url"]


# ---------------------------------------------------------------------------
# 商店详情批量抓取 / 真实好评率（get_app_details_batch / get_review_summary）
# ---------------------------------------------------------------------------


def _make_resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


def _detail_data(appid: int, name: str = "测试游戏", initial: int = 29800) -> dict:
    return {
        "type": "game",
        "name": name,
        "steam_appid": appid,
        "is_free": False,
        "header_image": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
        "price_overview": {
            "currency": "CNY",
            "initial": initial,
            "final": initial,
            "discount_percent": 0,
        },
        "genres": [{"id": "3", "description": "角色扮演"}],
        "categories": [{"id": 1, "description": "多玩家"}],
    }


@pytest.mark.asyncio
async def test_get_app_details_batch_parses_multi_id_response():
    """批量请求返回多键结构时直接解析，去重入参且不触发单发兜底。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0  # 测试中放行批量限流间隔
    client._batch_probe_done = True  # 预置"批量可用"探测结论
    client._batch_supported = True
    batch_payload = {
        "1086940": {"success": True, "data": _detail_data(1086940, "Baldur's Gate 3", 29800)},
        "1245620": {"success": True, "data": _detail_data(1245620, "ELDEN RING", 29800)},
    }
    with patch.object(client._client, "get", return_value=_make_resp(200, batch_payload)) as mock_get:
        results = await client.get_app_details_batch([1086940, 1086940, 1245620])

        assert set(results) == {1086940, 1245620}
        assert results[1086940].name == "Baldur's Gate 3"
        assert results[1245620].name == "ELDEN RING"
        assert results[1086940].final_price == 298.0
        # 仅 1 次批量请求完成，未走单发兜底
        assert mock_get.call_count == 1
        params = mock_get.call_args.kwargs["params"]
        assert params["appids"] == "1086940,1245620"  # 入参已去重
        # 结果同时写入内存缓存
        assert client._app_details_cache[1086940].name == "Baldur's Gate 3"


@pytest.mark.asyncio
async def test_get_app_details_batch_missing_id_falls_back_to_single():
    """批量响应缺某个 id 时，缺失者逐个走单发兜底。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    client._batch_probe_done = True
    client._batch_supported = True

    def route(url, params=None, timeout=None):
        appids = str(params.get("appids", "")) if params else ""
        if "," in appids:
            # 批量响应只含 1086940，缺 1245620
            return _make_resp(200, {"1086940": {"success": True, "data": _detail_data(1086940, "Baldur's Gate 3")}})
        if appids == "1245620":
            return _make_resp(200, {"1245620": {"success": True, "data": _detail_data(1245620, "ELDEN RING")}})
        return _make_resp(500, {})

    with patch.object(client._client, "get", side_effect=route) as mock_get:
        results = await client.get_app_details_batch([1086940, 1245620])

        assert set(results) == {1086940, 1245620}
        assert results[1245620].name == "ELDEN RING"
        # 1 次批量 + 1 次单发兜底
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_get_app_details_batch_unsupported_degrades_to_single():
    """实测批量 400（本环境批量拿不到完整数据）→ 降级单发并记住探测结论。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    assert client._batch_probe_done is False  # 初始未探测

    single_payloads = {
        "1086940": {"success": True, "data": _detail_data(1086940, "Baldur's Gate 3")},
        "1245620": {"success": True, "data": _detail_data(1245620, "ELDEN RING")},
        "582010": {"success": True, "data": _detail_data(582010, "Monster Hunter")},
    }

    def route(url, params=None, timeout=None):
        appids = str(params.get("appids", "")) if params else ""
        if "," in appids:
            return _make_resp(400, None)  # 实测形态：400 + body null
        return _make_resp(200, {appids: single_payloads[appids]})

    with patch.object(client._client, "get", side_effect=route) as mock_get:
        results = await client.get_app_details_batch([1086940, 1245620])
        assert set(results) == {1086940, 1245620}
        assert client._batch_probe_done is True
        assert client._batch_supported is False
        first_calls = mock_get.call_count

        # 第二次批量调用：不再浪费探测请求，直接逐个单发（新 id 才发请求）
        results2 = await client.get_app_details_batch([1086940, 582010])
        assert set(results2) == {1086940, 582010}
        assert results2[582010].name == "Monster Hunter"
        # 第二次只应有 1 个新请求（582010 单发；1086940 命中缓存）
        assert mock_get.call_count == first_calls + 1
        assert "," not in str(mock_get.call_args.kwargs["params"]["appids"])


@pytest.mark.asyncio
async def test_get_app_details_batch_rate_limited_returns_cache_only():
    """熔断期间批量只返回缓存命中部分，完全不发请求、不抛错。"""
    client = SteamFamilyClient()
    client._rate_limited_until = time.time() + 120
    cached = AppDetails(appid=1086940, name="Baldur's Gate 3")
    client._app_details_cache[1086940] = cached

    with patch.object(client._client, "get", return_value=_make_resp(200, {})) as mock_get:
        results = await client.get_app_details_batch([1086940, 1245620])

    assert results == {1086940: cached}
    assert 1245620 not in results
    mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_get_review_summary_parses_query_summary():
    """正常解析好评率与评价简述，且命中进程内 TTL 缓存后不再发请求。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    payload = {
        "success": 1,
        "query_summary": {
            "review_score_desc": "好评如潮",
            "total_positive": 96,
            "total_negative": 4,
            "total_reviews": 100,
        },
        "reviews": [],
        "cursor": "*",
    }
    with patch.object(client._client, "get", return_value=_make_resp(200, payload)) as mock_get:
        summary = await client.get_review_summary(1086940)
        assert summary == (96, "好评如潮")
        params = mock_get.call_args.kwargs["params"]
        assert params["purchase_type"] == "all"
        assert params["num_per_page"] == "0"
        assert params["language"] == "schinese"
        assert params["l"] == "schinese", "实测必须携带 l=schinese 才会返回中文评价简述"

        # 第二次调用命中缓存，不再发请求
        assert await client.get_review_summary(1086940) == (96, "好评如潮")
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_get_review_summary_zero_reviews_returns_none():
    """total_reviews=0 时返回 None，且 None 结果进入短期缓存。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    payload = {"success": 1, "query_summary": {"review_score_desc": "无评价", "total_reviews": 0}}
    with patch.object(client._client, "get", return_value=_make_resp(200, payload)) as mock_get:
        assert await client.get_review_summary(440) is None
        # 短期缓存命中，不发第二次请求
        assert await client.get_review_summary(440) is None
        assert mock_get.call_count == 1


@pytest.mark.asyncio
async def test_get_review_summary_429_triggers_breaker():
    """评价接口 429 时返回 None 并设置 60 秒熔断。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    with patch.object(client._client, "get", return_value=_make_resp(429, {})):
        assert await client.get_review_summary(730) is None
    assert client.is_rate_limited is True
    assert client.rate_limit_cooldown_remaining > 0
    assert client.rate_limit_cooldown_remaining <= 61
    # 熔断期间再查也直接 None，不打请求
    with patch.object(client._client, "get", side_effect=AssertionError("不应发起请求")) as mock_get:
        assert await client.get_review_summary(730) is None
        mock_get.assert_not_called()


@pytest.mark.asyncio
async def test_get_review_summary_network_error_returns_none():
    """网络异常时异常安全返回 None。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    with patch.object(client._client, "get", side_effect=httpx.ConnectError("connection refused")):
        assert await client.get_review_summary(570) is None


@pytest.mark.asyncio
async def test_get_app_details_include_review_enriches_details():
    """include_review=True 时用真实评价数据补全 AppDetails 并写入缓存。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    review_payload = {
        "success": 1,
        "query_summary": {"review_score_desc": "好评如潮", "total_positive": 96, "total_reviews": 100},
    }

    def route(url, params=None, timeout=None):
        if "appreviews" in str(url):
            return _make_resp(200, review_payload)
        return _make_resp(200, {"1086940": {"success": True, "data": _detail_data(1086940, "Baldur's Gate 3")}})

    with patch.object(client._client, "get", side_effect=route) as mock_get:
        details = await client.get_app_details(1086940, include_review=True)
        assert details is not None
        assert details.positive_rate == 96
        assert details.review_summary == "好评如潮"
        assert mock_get.call_count == 2  # 1 次 appdetails + 1 次 appreviews
        # 补全结果同时惠及内存缓存
        assert client._app_details_cache[1086940].positive_rate == 96

        # 再次调用：详情与评价全部命中缓存，零新请求
        details2 = await client.get_app_details(1086940, include_review=True)
        assert details2.positive_rate == 96
        assert mock_get.call_count == 2


@pytest.mark.asyncio
async def test_get_app_details_neutral_review_defaults():
    """不含评价数据时 positive_rate 默认 0、review_summary 为空串（移除硬编码假数据）。"""
    client = SteamFamilyClient()
    client._store_limiter.min_interval = 0.0
    with patch.object(
        client._client,
        "get",
        return_value=_make_resp(200, {"1086940": {"success": True, "data": _detail_data(1086940, "Baldur's Gate 3")}}),
    ) as mock_get:
        details = await client.get_app_details(1086940)
        assert details is not None
        assert details.positive_rate == 0
        assert details.review_summary == ""
        assert mock_get.call_count == 1  # 默认 include_review=False 不查评价接口
