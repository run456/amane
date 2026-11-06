"""扫描时直接从影片旁已有的 NFO / 文件名建立 Metadata 条目, 不依赖刮削.

REFRESH 默认会对 ``PENDING`` 文件扇出 SCRAPE; 本模块在文件登记后把条目建好, 并把
``MediaFile`` 置为 ``SCRAPED`` 且回填 ``metadata_id``, 扫描末尾的状态筛选因而自然跳过刮削.

分流:
- 同目录有 ``.nfo`` → 解析 Kodi / Amane 风格字段导入 (B);
- 无 NFO → 只建占位条目, 标题取番号, 字段不锁, 后续刮削仍可补全 (A).

开关: 环境变量 ``AMANE_IMPORT_NFO_ON_SCAN=0`` 可退回原版「必须刮削」行为.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import aiofiles
import structlog

from ..db.models import MediaFileStatus
from ..db.repo_types import MetadataFields, WriteMode
from ..parsing import parse_file_info
from ..utils.text import normalize_long_text, strip_illegal_xml_chars
from ..utils.threads import in_thread, path_exists

if TYPE_CHECKING:
    from ..db.models import MediaFile, Metadata
    from ..db.repository import Repository

logger = structlog.get_logger()

_ENABLED = os.getenv("AMANE_IMPORT_NFO_ON_SCAN", "1").strip().lower() not in {"0", "false", "no", "off"}

# 这些标签已被字段映射消费, 不作为 <*id> 外部编号收集.
_KNOWN_TAGS = frozenset(
    {
        "num",
        "title",
        "originaltitle",
        "sorttitle",
        "plot",
        "outline",
        "premiered",
        "releasedate",
        "year",
        "mpaa",
        "rating",
        "criticrating",
        "runtime",
        "studio",
        "maker",
        "publisher",
        "label",
        "series",
        "set",
        "poster",
        "cover",
        "thumb",
        "trailer",
        "director",
        "tag",
        "genre",
        "actor",
        "name",
        "type",
        "uniqueid",
        "id",
        "movie",
        "episodedetails",
    }
)


def _texts(root: ET.Element, *tags: str) -> list[str]:
    """按 tags 顺序收集所有同名后代节点的文本, 保留出现顺序."""
    out: list[str] = []
    for tag in tags:
        for element in root.iter(tag):
            text = (element.text or "").strip()
            if text:
                out.append(text)
    return out


def _first(root: ET.Element, *tags: str) -> str | None:
    for tag in tags:
        for element in root.iter(tag):
            text = (element.text or "").strip()
            if text:
                return text
    return None


def _strip_number_prefix(title: str, number: str | None) -> str:
    """Amane 写 NFO 时把 title 写成「番号 + 空格 + 标题」, 导入时剥掉番号前缀."""
    if not number:
        return title
    stripped = re.sub(r"^\s*" + re.escape(number) + r"\s+", "", title, count=1, flags=re.IGNORECASE).strip()
    return stripped or title


def _external_ids(root: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag
        if not isinstance(tag, str) or not tag.endswith("id") or tag in _KNOWN_TAGS:
            continue
        value = (element.text or "").strip()
        if value:
            out[tag[:-2]] = value
    return out


def parse_nfo(text: str, *, number_hint: str | None = None) -> tuple[MetadataFields, str | None]:
    """解析 Kodi / Amane 的 ``<movie>`` NFO. XML 不合法时抛出, 由调用方兜底."""
    # ET 不接受带 encoding 声明的 str, 统一回编码成 bytes 再解析.
    root = ET.fromstring(strip_illegal_xml_chars(text).encode("utf-8"))
    fields: MetadataFields = {}

    number = _first(root, "num") or number_hint

    raw_title = _first(root, "title", "originaltitle", "sorttitle")
    if raw_title:
        fields["title"] = _strip_number_prefix(raw_title, number)

    plot = normalize_long_text(_first(root, "plot", "outline"))
    if plot:
        fields["plot"] = plot

    release = _first(root, "premiered", "releasedate", "year")
    if release:
        fields["release"] = release

    actors: list[str] = []
    for actor in root.iter("actor"):
        name = _first(actor, "name") or (actor.text or "").strip()
        if name and name not in actors:
            actors.append(name)
    if actors:
        fields["actors"] = actors

    directors = list(dict.fromkeys(_texts(root, "director")))
    if directors:
        fields["directors"] = directors

    # Amane 把每个 tag 同时写成 <tag> 与 <genre>, 只取一组并保序去重.
    tags = list(dict.fromkeys(_texts(root, "tag") or _texts(root, "genre")))
    if tags:
        fields["tags"] = tags

    studio = _first(root, "studio", "maker")
    if studio:
        fields["studio"] = studio

    publisher = _first(root, "publisher", "label")
    if publisher:
        fields["publisher"] = publisher

    series = _first(root, "series")
    if not series:
        for set_element in root.iter("set"):
            series = _first(set_element, "name")
            if series:
                break
    if series:
        fields["series"] = series

    runtime = _first(root, "runtime")
    if runtime:
        try:
            fields["runtime"] = int(runtime)
        except ValueError:
            pass

    rating = _first(root, "rating")
    if rating:
        try:
            fields["scores"] = {"nfo": float(rating)}
        except ValueError:
            pass

    posters = _texts(root, "poster")
    if posters:
        fields["poster_urls"] = posters
    covers = _texts(root, "cover") or _texts(root, "thumb")
    if covers:
        fields["thumb_urls"] = covers
    trailers = _texts(root, "trailer")
    if trailers:
        fields["trailer_urls"] = trailers

    external_ids = _external_ids(root)
    if external_ids:
        fields["external_ids"] = external_ids

    return fields, number


@in_thread
def _single_nfo(parent: Path) -> Path | None:
    """目录里恰好只有一个 .nfo 时接受它 (整理器可能用了与视频不同的命名)."""
    try:
        found = [p for p in parent.iterdir() if p.is_file() and p.suffix.lower() == ".nfo"]
    except OSError:
        return None
    return found[0] if len(found) == 1 else None


async def _find_nfo(video: Path) -> Path | None:
    parent = video.parent
    for candidate in (video.with_suffix(".nfo"), parent / "movie.nfo"):
        if await path_exists(candidate):
            return candidate
    single = await _single_nfo(parent)
    if single is not None:
        return single
    # 分集视频常放在 CD1/ 子目录, NFO 在上一级.
    grand = parent.parent
    if grand != parent:
        for candidate in (grand / f"{parent.name}.nfo", grand / "movie.nfo"):
            if await path_exists(candidate):
                return candidate
    return None


def _is_placeholder(meta: Metadata, number: str) -> bool:
    title = (meta.title or "").strip()
    return not title or title.casefold() == number.casefold()


async def ensure_metadata_from_disk(repo: Repository, media: MediaFile) -> Metadata | None:
    """为尚未关联条目的文件建/挂 Metadata, 并把它标记为已刮削以跳过自动 SCRAPE.

    番号无法识别时返回 None 且不改动文件状态, 仍走原来的刮削链路.
    """
    if not _ENABLED or media.id is None or media.metadata_id is not None:
        return None

    video = Path(media.path)
    parsed = parse_file_info(video)
    number = parsed.number
    if not number:
        logger.debug("materialize skipped: no number", path=str(video))
        return None

    fields: MetadataFields = {}
    nfo_path = await _find_nfo(video)
    if nfo_path is not None:
        try:
            async with aiofiles.open(nfo_path, "rb") as f:
                raw: bytes = await f.read()
            fields, nfo_number = parse_nfo(raw.decode("utf-8-sig", errors="replace"), number_hint=number)
            if nfo_number:
                number = nfo_number
        except Exception:
            logger.warning("nfo parse failed, fallback to placeholder", path=str(nfo_path), exc_info=True)
            fields = {}

    existing = await repo.get_metadata_by_number(number)
    created = existing is None
    if created:
        meta = await repo.upsert_metadata(number, content_type=parsed.content_type)
    else:
        meta = existing
    if meta is None or meta.id is None:
        return None

    if fields and (created or _is_placeholder(meta, number)):
        fields.setdefault("title", number)
        # 不上锁: 用户之后主动点刮削时, 新结果要能覆盖这里导入的资料.
        # 保护改由 ScrapePayload.overwrite_locked 控制 (自动刮削不覆盖, 手动刮削覆盖).
        updated = await repo.update_metadata(meta.id, mode=WriteMode.AUTO, **fields)
        if updated is not None:
            meta = updated
        action = "imported"
    elif created:
        # 占位条目不加锁, 后续刮削可以正常补全.
        updated = await repo.update_metadata(meta.id, mode=WriteMode.AUTO, title=number)
        if updated is not None:
            meta = updated
        action = "placeholder"
    else:
        action = "linked"

    await repo.update_media_file(media.id, status=MediaFileStatus.SCRAPED, metadata_id=meta.id)
    logger.info("metadata materialized", path=str(video), number=number, action=action, nfo=str(nfo_path))
    return meta
