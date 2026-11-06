"""同源产物端点: 本地图片 / 预告片直出, 以及远程预告片直链的流式代理.

背景: 详情页把 ``trailer_urls[0]`` 的外站直链直接交给 ``<video>``, 浏览器直连会被
CORS / 防盗链 / Referer 校验挡掉, 表现为「预告片无法播放」. 这里让浏览器只请求本服务,
由后端转发上游字节并透传 Range, 拖动与断点续播照常工作.

- 本地图片: ``FileResponse`` (no-cache, 换封面后立刻生效).
- 本地预告片: ``IndexedFileResponse`` (206 / 416 / HEAD / 断连停止读取).
- 远程直链: 只放行公网 (``EgressPolicy``), 重定向逐跳复查, 逐块转发, 过滤 hop-by-hop 头.

``AMANE_LOCAL_ARTIFACTS=0`` 关闭响应注入后本端点仍可用, 只是前端不会再引用它.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit

import httpx2 as httpx
import structlog
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from ..net.egress import EgressPolicy
from ..net.errors import EgressRejected, RequestError
from ..playback.file_response import IndexedFileResponse

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastapi import Request

logger = structlog.get_logger()

_EGRESS = EgressPolicy()
_NO_CACHE = {"Cache-Control": "private, no-cache"}
_PASSTHROUGH = frozenset(
    {
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
        "cache-control",
    }
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

_client: httpx.AsyncClient | None = None
"""模块级长连接客户端; 与 scrape 用的 WebClient 分开, 由进程退出回收."""


def _stream_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            # 不用自动跟随: 每一跳都要过 EgressPolicy, 否则 302 能把请求带进内网.
            follow_redirects=False,
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            headers={"Accept-Encoding": "identity"},
        )
    return _client


def image_response(path: Path) -> FileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path=path, media_type=media_type, headers=dict(_NO_CACHE))


def local_video_response(request: "Request", path: Path) -> IndexedFileResponse:
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return IndexedFileResponse(request, path, media_type=media_type, headers=dict(_NO_CACHE))


async def _open_upstream(
    client: httpx.AsyncClient, method: str, url: str, headers: Mapping[str, str]
) -> httpx.Response:
    """逐跳复查公网地址后打开上游流; 返回未关闭的 response, 由调用方负责释放."""
    current = url
    for _ in range(_EGRESS.max_redirects + 1):
        try:
            await _EGRESS.ensure_allowed(current)
        except EgressRejected as exc:
            raise HTTPException(status_code=400, detail=exc.detail) from exc
        except RequestError as exc:
            # 解析失败属于瞬态网络故障, 不是地址不合规.
            raise HTTPException(status_code=502, detail="预告片地址不可达") from exc

        hop_headers = dict(headers)
        parsed = urlsplit(current)
        hop_headers.setdefault("Referer", f"{parsed.scheme}://{parsed.netloc}/")
        upstream_request = client.build_request(method, current, headers=hop_headers)
        try:
            response = await client.send(upstream_request, stream=True)
        except (httpx.InvalidURL, httpx.RequestError) as exc:
            logger.warning("trailer upstream request failed", url=current, error=str(exc))
            raise HTTPException(status_code=502, detail="预告片来源不可达") from exc

        location = response.headers.get("location") if response.status_code in _REDIRECT_CODES else None
        if not location:
            return response
        status = response.status_code
        await response.aclose()
        current = urljoin(current, location)
        if status == 303 and method != "GET":
            method = "GET"
    raise HTTPException(status_code=502, detail="预告片重定向次数过多")


async def proxy_remote_trailer(request: "Request", url: str) -> StreamingResponse:
    """把远程直链转发给浏览器, 只请求本服务即可播放."""
    outbound: dict[str, str] = {}
    for name in ("range", "if-range", "if-none-match"):
        value = request.headers.get(name)
        if value:
            outbound[name] = value
    if "," in outbound.get("range", "").split("=", 1)[-1]:
        raise HTTPException(status_code=400, detail="不支持多段请求")

    method = "HEAD" if request.method == "HEAD" else "GET"
    response = await _open_upstream(_stream_client(), method, url, outbound)
    try:
        if response.status_code >= 400:
            raise HTTPException(status_code=502, detail="预告片来源失败")
        headers = {k: v for k, v in response.headers.items() if k.casefold() in _PASSTHROUGH}
        headers.setdefault("content-type", "video/mp4")
        headers.setdefault("Cache-Control", "private, no-cache")

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(body(), status_code=response.status_code, headers=headers)
    except Exception:
        # 构造 StreamingResponse 前失败 (如上游 4xx): 立即释放连接, 不依赖生成器 finally.
        await response.aclose()
        raise
