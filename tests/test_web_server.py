import pytest
import asyncio
import httpx
from core.storage import FamilyStorage
from core.renderer import CardRenderer
from core.web_server import DashboardWebServer

@pytest.mark.asyncio
async def test_dashboard_web_server(tmp_path):
    db_path = str(tmp_path / "web_test.db")
    storage = FamilyStorage(db_path)
    await storage.init_db()
    renderer = CardRenderer()

    # Start server on free port
    server = DashboardWebServer(host="127.0.0.1", port=16188, storage=storage, renderer=renderer)
    await server.start()

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("http://127.0.0.1:16188/")
            assert resp.status_code == 200
            assert "Steam 家庭资产全景看板" in resp.text

            api_resp = await client.get("http://127.0.0.1:16188/api/data")
            assert api_resp.status_code == 200
            assert "total_games" in api_resp.json()
    finally:
        await server.stop()
