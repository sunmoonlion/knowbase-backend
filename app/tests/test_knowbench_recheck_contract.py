"""跨仓回归：真实 knowbench 客户端 → HTTP → knowbase 路由/数据库。

常规 provider HTTP 测试始终运行；两仓并列检出时额外执行本测试。
也可用 KNOWBENCH_CONTRACT_APP 指向 consumer 的 app 目录。身份依赖沿用
provider HTTP fixture，测试范围是接口契约，不代替生产 OIDC/TLS 验收。
"""

import asyncio
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from test_durable_delivery_db import db as db  # noqa: F401
from test_user_library_http import BASE  # noqa: F401
from test_user_library_http import api as api
from test_user_resolution_refresh_http import create_list, upload

# 两仓顶层包都叫 app，分进程加载以避免拿错实现；响应来自真实 provider。
CONSUMER = r"""
import asyncio, json, sys
import httpx
from app.infrastructure.external.knowbase_data_v2 import KnowbaseDataV2

class LoopbackTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        assert request.headers['authorization'] == 'Bearer test-user-token'
        assert request.method == 'POST'
        assert request.url.query == b''
        forwarded = httpx.Request(
            request.method,
            'http://127.0.0.1:' + sys.argv[1] + request.url.raw_path.decode(),
            headers=request.headers, content=await request.aread(),
        )
        return await self.inner.handle_async_request(forwarded)

    async def aclose(self):
        await self.inner.aclose()

async def main():
    client = KnowbaseDataV2(
        {'KNOWBASE_INTERNAL_URL': 'https://knowbase.contract.test'},
        transport=LoopbackTransport(),
    )
    result = await client.refresh_mine('test-user-token', sys.argv[2])
    assert result['resolution_id'] == sys.argv[2]
    assert result['revision'] == 2
    assert [i['status'] for i in result['items']] == ['resolved', 'to_confirm']
    print(json.dumps({'revision': result['revision']}))

asyncio.run(main())
"""


@pytest.fixture
def consumer_app():
    default = (
        Path(__file__).resolve().parents[4] / "knowbench-app/knowbench-backend/app"
    )
    consumer = Path(os.environ.get("KNOWBENCH_CONTRACT_APP", str(default)))
    python = consumer / ".venv/bin/python"
    if (
        not (consumer / "app/infrastructure/external/knowbase_data_v2.py").is_file()
        or not python.exists()
    ):
        pytest.skip(
            "requires sibling knowbench backend (or KNOWBENCH_CONTRACT_APP) and venv"
        )
    return consumer, python


async def test_real_consumer_refresh_reaches_provider(api, consumer_app):
    consumer, python = consumer_app
    client, state = api
    await client.put(f"{BASE}/account", json={"default_display": True})
    original = await create_list(client)
    await upload(client, 2025)
    app = state["app"]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off", ws="none")
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        running = asyncio.create_task(server.serve(sockets=[sock]))
        process = None
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if running.done():
                        await running
                        pytest.fail("provider server stopped before becoming ready")
                    await asyncio.sleep(0.01)
            process = await asyncio.create_subprocess_exec(
                str(python),
                "-c",
                CONSUMER,
                str(sock.getsockname()[1]),
                original["resolution_id"],
                cwd=consumer,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            async with asyncio.timeout(30):
                stdout, stderr = await process.communicate()
            assert process.returncode == 0, stderr.decode()
            assert b'"revision": 2' in stdout
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            server.should_exit = True
            await running
