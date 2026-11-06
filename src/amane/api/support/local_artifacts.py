"""响应层注入视频同目录的本地封面 / 背景 / 预告片 URL.

Amane 原本只把 ``Metadata.poster_urls`` / ``thumb_urls`` / ``trailer_urls`` 里的外链交给前端,
磁盘上已经存在的图片从不参与显示. 这里在响应装配时一次批量扫描 (放线程池), 按用户确认的
「本地优先」把同源 URL 插到各 URL 列表首位, 同时补齐单值字段:

- 不写数据库、不触发重扫: 每次请求按当前 ``MediaFile.path`` 现算, 整理后自动跟随.
- 本地缺失的 kind 保持原样, 外链仍在列表里作后备.
- ``AMANE_LOCAL_ARTIFACTS=0`` 可整体关闭, 退回原版行为.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ...media.local_artifacts import (
    LocalArtifactKind,
    LocalArtifacts,
    artifact_url,
    discover,
    pick_proxyable_trailer,
)
from ..models import MetadataResponse

if TYPE_CHECKING:
    from ...db.models import Metadata
    from ...db.repository import Repository

_ENABLED = os.getenv("AMANE_LOCAL_ARTIFACTS", "1").strip().lower() not in {"0", "false", "no", "off"}


def _scan(paths: dict[int, str], numbers: dict[int, str]) -> dict[int, LocalArtifacts]:
    """同步磁盘扫描, 由 ``asyncio.to_thread`` 承接."""
    return {
        metadata_id: discover(Path(path), number=numbers.get(metadata_id))
        for metadata_id, path in paths.items()
    }


def _inject_one(response: MetadataResponse, art: LocalArtifacts) -> MetadataResponse:
    if not art.files:
        return response
    update: dict[str, object] = {}
    metadata_id = response.id

    if art.has(LocalArtifactKind.poster):
        url = artifact_url(metadata_id, LocalArtifactKind.poster)
        update["poster_urls"] = [url, *[u for u in response.poster_urls if u != url]]
        update["poster_url"] = url
    if art.has(LocalArtifactKind.thumb):
        url = artifact_url(metadata_id, LocalArtifactKind.thumb)
        update["thumb_urls"] = [url, *[u for u in response.thumb_urls if u != url]]
        update["thumb_url"] = url
    if art.has(LocalArtifactKind.fanart):
        # 背景图: 放进剧照列表首位, 让详情页灯箱能看到本地 fanart.
        url = artifact_url(metadata_id, LocalArtifactKind.fanart)
        update["extrafanart"] = [url, *[u for u in response.extrafanart if u != url]]
    if art.has(LocalArtifactKind.trailer) or pick_proxyable_trailer(response.trailer_urls) is not None:
        url = artifact_url(metadata_id, LocalArtifactKind.trailer)
        update["trailer_urls"] = [url, *[u for u in response.trailer_urls if u != url]]
        update["trailer_url"] = url
    return response.model_copy(update=update) if update else response


async def inject_local_artifacts(
    repo: "Repository",
    responses: list[MetadataResponse],
    metadata: Sequence["Metadata"],
) -> list[MetadataResponse]:
    """批量注入; 失败只跳过本项, 不影响列表其余内容."""
    if not _ENABLED or not responses:
        return responses
    numbers: dict[int, str] = {}
    for item in metadata:
        if item.id is not None:
            numbers[item.id] = item.number
    try:
        paths = await repo.first_media_paths([r.id for r in responses])
        artifacts = await asyncio.to_thread(_scan, paths, numbers)
    except Exception:  # noqa: BLE001 - 磁盘扫描失败不应让片库打不开
        return responses
    by_id = {r.id: r for r in responses}
    injected: dict[int, MetadataResponse] = {}
    for metadata_id, art in artifacts.items():
        response = by_id.get(metadata_id)
        if response is not None:
            injected[metadata_id] = _inject_one(response, art)
    return [injected.get(r.id, r) for r in responses]
