"""响应层注入视频同目录的本地封面 / 背景 / 预告片 URL.

Amane 原本只把 ``Metadata.poster_urls`` / ``thumb_urls`` / ``trailer_urls`` 里的外链交给前端,
磁盘上已经存在的图片从不参与显示. 这里在响应装配时一次批量扫描 (放线程池), 按用户确认的
「本地优先」把同源 URL 插到各 URL 列表首位, 同时补齐单值字段:

- 不写数据库、不触发重扫: 每次请求按当前 ``MediaFile.path`` 现算, 整理后自动跟随.
- 本地图片 URL 带文件 ``mtime_ns`` 版本号 (``&v=``): 用户替换 / 修改盘上的图后
  URL 变化, 前端 ``<img>`` 才会重新请求 (固定 URL 会被浏览器与 React key 缓存).
- 本地缺失的 kind 保持原样, 外链仍在列表里作后备.
- 影片目录下 ``extrafanart/`` (``extrathumbs/``) 里的整组剧照优先于单张 fanart 与远程
  ``extrafanart_urls``: 有本地图集就只返回本地图, 没有才回退原列表 (方案 A).
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
    extrafanart_url,
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


def _local_url(metadata_id: int, art: LocalArtifacts, kind: LocalArtifactKind) -> str:
    """带文件版本号 (mtime_ns) 的同源产物 URL; 图片类专用."""
    return artifact_url(metadata_id, kind, art.version(kind))


def _is_local_artifact(url: str, metadata_id: int, kind: LocalArtifactKind) -> bool:
    """是否为本条目的同源产物 URL (可能带着上一次扫描的旧 ``&v=`` 参数).

    注入不写库, 但列表可能已含历史同源 URL; 按前缀判断才能把旧版本号一并排除,
    避免每次刷新都在列表里累积一条同源地址.
    """
    base = artifact_url(metadata_id, kind)
    return url == base or url.startswith(base + "&")


def _is_local_extrafanart(url: str, metadata_id: int) -> bool:
    """历史注入的同源剧照 URL (可能带旧 ``&v=`` / ``&r=``); 图集清空时一并剔除."""
    return url.startswith(f"/api/metadata/{metadata_id}/extrafanart/")


def _inject_one(response: MetadataResponse, art: LocalArtifacts) -> MetadataResponse:
    # 只有 extrafanart/ 子目录、没有任何单张图片时也要注入图集, 不能提前返回.
    if not art.files and not art.extrafanart:
        return response
    update: dict[str, object] = {}
    metadata_id = response.id

    if art.has(LocalArtifactKind.poster):
        url = _local_url(metadata_id, art, LocalArtifactKind.poster)
        rest = [u for u in response.poster_urls if not _is_local_artifact(u, metadata_id, LocalArtifactKind.poster)]
        update["poster_urls"] = [url, *rest]
        update["poster_url"] = url
    if art.has(LocalArtifactKind.thumb):
        url = _local_url(metadata_id, art, LocalArtifactKind.thumb)
        rest = [u for u in response.thumb_urls if not _is_local_artifact(u, metadata_id, LocalArtifactKind.thumb)]
        update["thumb_urls"] = [url, *rest]
        update["thumb_url"] = url
    if art.extrafanart:
        # 本地图集齐全: 只返回本地图, 不再拼远程外链. 本地剧照往往就是那批外链下载下来的,
        # 拼一起会重复, 且外链多已失效 -> 破图; 用户要的是「有本地就别再碰外链」.
        # URL 带目录指纹 &v=, 增删 / 替换任意一张都会让 <img> 重新请求.
        update["extrafanart"] = [
            extrafanart_url(metadata_id, i, art.extrafanart_version)
            for i in range(len(art.extrafanart))
        ]
    else:
        # 无本地图集: 剔除历史注入的同源 URL (含旧图集与单张 fanart), 保留远程外链;
        # 单张 fanart.jpg 仍置首位. 用户清空 extrafanart/ 目录后不会残留坏掉的本地地址.
        rest = [
            u for u in response.extrafanart
            if not _is_local_extrafanart(u, metadata_id)
            and not _is_local_artifact(u, metadata_id, LocalArtifactKind.fanart)
        ]
        if art.has(LocalArtifactKind.fanart):
            rest = [_local_url(metadata_id, art, LocalArtifactKind.fanart), *rest]
        update["extrafanart"] = rest
    if art.has(LocalArtifactKind.trailer) or pick_proxyable_trailer(response.trailer_urls) is not None:
        # 预告片 URL 不挂版本号: 播放器按 /api/metadata/ 前缀判定 kind, 保持原样最稳.
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
