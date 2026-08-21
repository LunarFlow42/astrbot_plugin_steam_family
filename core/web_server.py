import asyncio
import json
import logging
from typing import Any, Callable, Coroutine
from urllib.parse import parse_qs, urlparse

try:
    from .renderer import CardRenderer
    from .storage import FamilyStorage
except (ImportError, ValueError):
    from core.renderer import CardRenderer
    from core.storage import FamilyStorage

logger = logging.getLogger(__name__)


class DashboardWebServer:
    """
    轻量级异步 Web 服务器，用于提供 Steam 家庭资产大盘与调试管理中心。
    支持 0.0.0.0 远程访问与 127.0.0.1 本地访问。
    """

    def __init__(
        self,
        host: str,
        port: int,
        storage: FamilyStorage,
        renderer: CardRenderer,
        sync_callback: Callable[[], Coroutine[Any, Any, str]] | None = None,
    ):
        self.host = host
        self.port = port
        self.storage = storage
        self.renderer = renderer
        self.sync_callback = sync_callback
        self._server: asyncio.Server | None = None

    async def start(self):
        """启动 Web 服务器。"""
        try:
            self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
            logger.info(f"Steam Family Web Dashboard running at http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(
                f"Failed to start Steam Family Web Dashboard on {self.host}:{self.port}: {e}"
            )

    async def stop(self):
        """停止 Web 服务器。"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("Steam Family Web Dashboard server stopped.")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            line = await reader.readline()
            if not line:
                writer.close()
                return
            request_line = line.decode("utf-8", errors="ignore").strip()
            parts = request_line.split()
            if len(parts) >= 2:
                method = parts[0]
                raw_path = parts[1]
                parsed = urlparse(raw_path)
                path = parsed.path
                query_params = parse_qs(parsed.query)

                if method in ("GET", "HEAD") and path in (
                    "/",
                    "/dashboard",
                    "/index.html",
                    "/dashboard.html",
                ):
                    data = await self.storage.get_dashboard_data()
                    html = self.renderer.render_dashboard_html(data)
                    body = html.encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/html; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    writer.write(headers + body)
                    await writer.drain()
                    writer.close()
                    return

                elif path == "/api/sync":
                    msg = "未配置同步回调函数"
                    success = False
                    if self.sync_callback:
                        try:
                            msg = await self.sync_callback()
                            success = not msg.startswith("❌") and not msg.startswith("⚠️")
                        except Exception as ex:
                            msg = f"同步执行异常: {ex}"
                            success = False
                    resp_data = {"success": success, "message": msg}
                    body = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    writer.write(headers + body)
                    await writer.drain()
                    writer.close()
                    return

                elif path == "/api/db/remove_app":
                    appid_str = query_params.get("appid", ["0"])[0]
                    appid = int(appid_str) if appid_str.isdigit() else 0
                    if appid > 0:
                        ok = await self.storage.remove_app_from_latest_snapshot(appid)
                        resp_data = {
                            "success": ok,
                            "message": f"已从当前快照中移出 AppID {appid}"
                            if ok
                            else "未能找到该游戏快照",
                        }
                    else:
                        resp_data = {"success": False, "message": "无效的 AppID"}
                    body = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    writer.write(headers + body)
                    await writer.drain()
                    writer.close()
                    return

                elif path == "/api/db/reset_snapshots":
                    await self.storage.reset_snapshots()
                    resp_data = {"success": True, "message": "本地快照数据库已成功重置"}
                    body = json.dumps(resp_data, ensure_ascii=False).encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    writer.write(headers + body)
                    await writer.drain()
                    writer.close()
                    return

                elif path == "/api/data":
                    data = await self.storage.get_dashboard_data()
                    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: application/json; charset=utf-8\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Access-Control-Allow-Origin: *\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                    writer.write(headers + body)
                    await writer.drain()
                    writer.close()
                    return

            # 404 handler
            writer.write(
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\nConnection: close\r\n\r\nNot Found"
            )
            await writer.drain()
            writer.close()
        except Exception as e:
            logger.debug(f"Web server client handler exception: {e}")
            try:
                writer.close()
            except Exception:
                pass
