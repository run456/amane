"""演员头像写盘: 把上传图同步到每个关联影片目录的 .actors/, 并重写 NFO.

Emby / Kodi 读取本地演员头像的标准方式是影片目录下的 ``.actors/<名字>.jpg``
(空格转下划线); NFO 的 ``<actor><thumb>`` 是补充 (Kodi 读, Emby 常忽略).
两者都写, 且 ``write_nfo()`` 会自动发现该文件补 thumb, 因此整理 / 保存 /
同步产物等后续写盘不会把 thumb 冲掉. 全程只写盘, 不调用 Emby API.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from .nfo import actor_avatar_filename, write_nfo

if TYPE_CHECKING:
    from ..db.models import Actor

logger = structlog.get_logger()


@dataclass
class ActorAvatarSyncResult:
    images_written: int = 0
    nfos_written: int = 0
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _normalize(name: str) -> str:
    """名字比对忽略空白与大小写, 兼容日文全角空格 / 别名写法."""
    return re.sub(r"\s+", "", name).casefold()


def _backup_nfo(path: Path) -> None:
    """覆盖前留一份 .nfo.bak, 与 handlers.file 的写盘行为一致."""
    if not path.exists():
        return
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError as exc:
        logger.warning("actor avatar nfo backup failed", path=str(path), error=str(exc))


async def sync_actor_avatar(repo: Any, actor: Actor, data: bytes) -> ActorAvatarSyncResult:
    """按该演员在每部关联影片里实际出现的名字写 .actors 图, 再重写 NFO.

    只认规范名 / 别名, 不会把同一部影片里的其它演员误写成本头像.
    库关闭 write_nfo 时仍写 .actors 图 (Emby 主要靠它), 只是不动 NFO.
    """
    result = ActorAvatarSyncResult()
    assert actor.id is not None
    metadata_ids = await repo.list_metadata_ids(actor_ids=[actor.id])
    if not metadata_ids:
        result.skipped.append("无关联影片")
        return result

    aliases = await repo.get_actor_aliases(actor.id)
    accepted = {_normalize(n) for n in (actor.name, *aliases) if isinstance(n, str)}

    for metadata_id in metadata_ids:
        metadata = await repo.get_metadata(metadata_id)
        if metadata is None:
            result.skipped.append(f"#{metadata_id}: 条目不存在")
            continue
        names = {
            n.strip()
            for n in (metadata.actors or [])
            if isinstance(n, str) and _normalize(n) in accepted
        }
        if not names:
            result.skipped.append(f"{metadata.number}: 影片演员表中无该演员名")
            continue
        files = await repo.get_media_by_metadata_id(metadata_id)
        for media_file in files:
            src = Path(media_file.path)
            if not src.is_file():
                result.skipped.append(f"{src.name}: 视频不在磁盘")
                continue
            library = await repo.get_library(media_file.library_id)
            if library is None:
                result.skipped.append(f"{src.name}: 媒体库不存在")
                continue
            try:
                actors_dir = src.parent / ".actors"
                actors_dir.mkdir(parents=True, exist_ok=True)
                for name in names:
                    (actors_dir / actor_avatar_filename(name)).write_bytes(data)
                    result.images_written += 1
                if library.write_nfo:
                    nfo = src.with_suffix(".nfo")
                    _backup_nfo(nfo)
                    if await write_nfo(metadata, nfo):
                        result.nfos_written += 1
                    else:
                        result.errors.append(f"{src.name}: NFO 写入失败")
            except OSError as exc:
                logger.exception("actor avatar sync failed", path=str(src))
                result.errors.append(f"{src.name}: {exc}")

    logger.info(
        "actor avatar synced",
        actor_id=actor.id,
        images=result.images_written,
        nfos=result.nfos_written,
        skipped=len(result.skipped),
        errors=len(result.errors),
    )
    return result
