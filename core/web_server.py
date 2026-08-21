import asyncio
import logging

try:
    from .renderer import CardRenderer
    from .storage import FamilyStorage
except (ImportError, ValueError):
    from core.renderer import CardRenderer
    from core.storage import FamilyStorage

logger = logging.getLogger(__name__)


class DashboardWebServer:
    def __init__(self, host: str, port: int, storage: FamilyStorage, renderer: CardRenderer):
        self.host = host
        self.port = port
        self.storage = storage
        self.renderer = renderer
        self._server: asyncio.Server | None = None

    async def start(self):
        try:
            self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
            logger.info(f"Steam Family Web Dashboard running at http://{self.host}:{self.port}")
        except Exception as e:
            logger.error(
                f"Failed to start Steam Family Web Dashboard on {self.host}:{self.port}: {e}"
            )

    async def stop(self):
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
            if len(parts) >= 2 and parts[0] == "GET":
                path = parts[1]
                if path in ("/", "/dashboard", "/index.html", "/dashboard.html"):
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
                elif path == "/api/data":
                    import json

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
        except Exception:
            try:
                writer.close()
            except Exception:
                pass
