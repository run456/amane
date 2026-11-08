"""本地剧照图集直出: 只服务影片目录下 ``extrafanart/`` (``extrathumbs/``) 内的图片.

``index`` 只在服务端扫描结果的内存列表里取值, 不接受用户提供的路径, 杜绝目录穿越.
与 ``/metadata/{id}/artifact`` 共用 ``media.local_artifacts`` 的同一套扫描函数, 保证
「响应里注入了哪几张」与「端点肯返回哪几张」始终一致; 不写库、不缓存索引, 盘上图
换了立刻生效.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ...media.local_artifacts import list_local_extrafanart
from ..deps import RepoDep
from ..support.http_cache import etag_matches, format_etag
from ..support.media_type import media_type_and_headers

router = APIRouter(prefix="/metadata", tags=["extrafanart"])

_CACHE_CONTROL = "private, no-cache"
"""每次使用前都要带 If-None-Match 再验证; 用户库属私有内容, 不放进共享缓存."""


@router.get("/{metadata_id}/extrafanart/{index}")
async def serve_local_extrafanart(
    metadata_id: int, index: int, request: Request, repo: RepoDep
) -> Response:
    """按当前 ``MediaFile.path`` 实时定位剧照; 越界 / 无关联文件一律 404."""
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    # 与响应层注入共用 first_media_paths, 避免「注入了 URL 但端点找不到图」。
    paths = await repo.first_media_paths([metadata_id])
    video_path = paths.get(metadata_id)
    items = await asyncio.to_thread(list_local_extrafanart, video_path) if video_path else []
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="剧照不存在")

    path = items[index]
    try:
        stat = path.stat()
    except OSError:
        raise HTTPException(status_code=404, detail="剧照不存在") from None

    etag = f"{path.name}-{stat.st_size}-{stat.st_mtime_ns}"
    if etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"Cache-Control": _CACHE_CONTROL, "ETag": format_etag(etag)})

    media_type, headers = media_type_and_headers(path)
    headers["Cache-Control"] = _CACHE_CONTROL
    headers["ETag"] = format_etag(etag)
    return FileResponse(path=path, media_type=media_type, headers=headers)
