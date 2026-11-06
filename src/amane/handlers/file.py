import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from PIL import Image

from ..config import HotSettings, WatermarkConfig
from ..db.models import Library, MediaFile
from ..db.repo_types import MediaFileUpdates
from ..enums import ActorGender, DownloadableResource, LinkMode
from ..library import MEDIA_EXTENSIONS, LibraryFileKind, LibraryScan, ancestor_dirs, prune_empty_dirs
from ..library.rules import is_in_trash
from ..media import ResourceStore, apply_cover_watermarks_from_info, crop_poster
from ..media import write_nfo as write_nfo_file
from ..media.local_artifacts import LocalArtifactKind, LocalArtifacts, discover as discover_local_artifacts
from ..media.resource_store import internal_url_hash
from ..net.http import WebClient
from ..observability.models import OrganizeConflict, OrganizeConflictReason
from ..organize import (
    MoveMode,
    PlaceOutcome,
    ResolvedPaths,
    SubtitleConflict,
    TargetState,
    discover_subtitles,
    execute_organize,
    place_subtitles,
    render_strm_content,
    resolve_paths,
    target_state,
    video_dest,
)
from ..organize.link import create_video_link
from ..parsing import ContentType, FileInfo, parse_file_info
from ..utils.path import existing_disk_path as existing_disk_path_sync
from ..utils.path import is_descendant, nfc_path, path_is_under
from ..utils.threads import existing_disk_path, in_thread, path_is_dir
from ._common import LibraryTaskLocks, refresh_external_subtitle
from .models import (
    ORGANIZE_CONFLICT_LIMIT,
    CleanupPayload,
    CleanupResult,
    OrganizePayload,
    OrganizeResult,
)
from .protocol import TaskHandler, TaskResult

if TYPE_CHECKING:
    from ..config import HotSettings
    from ..db.models import Metadata
    from ..db.repository import Repository
    from ..media import ResourceStore
    from ..net.http import WebClient

logger = structlog.get_logger()

# 整理时保留视频源目录里"原来就有"的图片 / 预告片, 让它们跟随视频进整理目录.
# 命中本地产物的角色完全跳过刮削下载 (而非先写刮削图再覆盖), 避免留下错图.
# 设为 0/false/off 可完全退回原版「无条件用刮削图覆盖」行为.
_KEEP_LOCAL_ARTIFACTS = os.getenv("AMANE_ORGANIZE_KEEP_LOCAL_ARTIFACTS", "1").strip().lower() not in {
    "0", "false", "no", "off"
}


@dataclass
class FileOperationsResult:
    outcome: PlaceOutcome
    dest: Path | None = None
    """PLACED 是落盘路径; FAILED 时可能仍带路径, 以便回写 MediaFile.path 补链接."""
    conflict_target: Path | None = None
    """CONFLICT 时被占用的目标路径."""
    subtitle_conflicts: list[SubtitleConflict] = field(default_factory=list)
    error: str | None = None


async def execute_file_operations(
    media_file: MediaFile,
    metadata: Metadata,
    paths: ResolvedPaths,
    move_mode: MoveMode,
    resource_store: ResourceStore,
    file_info: FileInfo,
    download_images: bool = True,
    write_nfo: bool = True,
    copy_resources: Sequence[DownloadableResource] | None = None,
    web_client: WebClient | None = None,
    config: HotSettings | None = None,
    library: Library | None = None,
    safe_dirs: Sequence[Path] | None = (),
    watermark_dir: Path | None = None,
    actor_genders: dict[str, ActorGender] | None = None,
) -> FileOperationsResult:
    """``file_info`` 由渲染路径的那一次 ``parse_file_info`` 产出, 必须带上 ``has_external_subtitle``.

    未带库列时函数不报错, 直接少打中字角标 — 调用方负责构造.
    """
    source_path = await existing_disk_path(Path(media_file.path))
    if source_path is None:
        logger.warning("source file missing", path=media_file.path)
        return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=f"源文件不存在: {media_file.path}")

    # 目标被别的文件占用时立即返回: 不下载图片, 不写附属文件, 不改库内任何路径.
    # 读不出目标状态时同样立即返回, 但记为失败而不是冲突.
    # 目标路径必须与 execute_organize 同一算法: 模板写死扩展名时, 模板渲染结果与真实目标不同.
    dest = video_dest(paths.video.parent, paths.video.stem, source_path)
    check = await target_state(source_path, dest)
    if check.state is TargetState.OCCUPIED:
        logger.warning("organize target occupied", source=str(source_path), dest=str(dest))
        return FileOperationsResult(outcome=PlaceOutcome.CONFLICT, conflict_target=dest)
    if check.state is TargetState.UNKNOWN:
        logger.warning("organize target state unknown", source=str(source_path), dest=str(dest), error=check.error)
        return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=check.error)

    # 整理开始前先用与片库 / 详情页完全相同的规则发现源目录的本地产物.
    # 命中哪些角色就把哪些角色的刮削资源从下载清单剔除: 刮削图根本不落盘, 也不触发
    # crop_poster 从横幅裁封面, 从源头杜绝「识别到 A、复制了 B」.
    local_artifacts: LocalArtifacts = LocalArtifacts(files={})
    if _KEEP_LOCAL_ARTIFACTS:
        local_artifacts = await _discover_for_organize(source_path, metadata.number)

    # 下载图片; 水印打在库路径副本上, 不能修改 Resource 原图.
    if web_client and download_images:
        logger.debug("downloading images via store", number=metadata.number)
        kinds = set(copy_resources) if copy_resources is not None else set(DownloadableResource)
        kinds -= _resources_covered_by_local(local_artifacts)
        await _download_images_via_store(
            metadata,
            resource_store,
            web_client,
            paths,
            config,
            kinds,
            file_info=file_info,
            content_type=metadata.content_type,
            watermark_dir=watermark_dir,
            preserve_existing=_KEEP_LOCAL_ARTIFACTS,
        )

    # 发现字幕: 必须在视频移动前检查同目录.
    subtitles: list[Path] = []
    if library is not None:
        subtitles = await discover_subtitles(source_path, library.subtitle_extensions, file_info)

    org_result = await execute_organize(
        source=source_path,
        target_dir=paths.video.parent,
        target_stem=paths.video.stem,
        mode=move_mode,
    )

    if org_result.outcome is PlaceOutcome.CONFLICT:
        return FileOperationsResult(outcome=PlaceOutcome.CONFLICT, conflict_target=org_result.dest)

    # 写链接; 失败仍带 dest, 以便回写 MediaFile.path.
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and paths.link is not None:
        mode = LinkMode(library.link_mode) if library is not None else LinkMode.STRM
        strm_content: str | None = None
        if mode == LinkMode.STRM and library is not None:
            try:
                strm_content = render_strm_content(
                    library.strm_content_template,
                    org_result.dest,
                    Path(library.path),
                    metadata,
                    source_path=source_path,
                    file_info=file_info,
                    link=paths.link,
                    actor_genders=actor_genders,
                )
            except ValueError as e:
                return FileOperationsResult(outcome=PlaceOutcome.FAILED, dest=org_result.dest, error=str(e))
        link_result = await create_video_link(org_result.dest, paths.link, mode, content=strm_content)
        if link_result.outcome is not PlaceOutcome.PLACED:
            return FileOperationsResult(outcome=PlaceOutcome.FAILED, dest=org_result.dest, error=link_result.error)

    # 写入 NFO.
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and write_nfo:
        await write_nfo_file(metadata, paths.nfo)

    # 整理落盘后, 把视频源目录里"原来就有"的图片 / 预告片按库模板目标名写进整理目录.
    # 命中本地产物的角色已在下载阶段剔除, 这里只负责搬运 (逐文件, 不再整目录跳过);
    # 整理失败不写附属文件, 不留孤儿.
    if org_result.outcome is PlaceOutcome.PLACED and _KEEP_LOCAL_ARTIFACTS:
        kept = await copy_existing_artifacts(source_path, paths, local_artifacts)
        if kept:
            logger.info("local artifacts kept", number=metadata.number, files=[p.name for p in kept])

    # 字幕按模板落到 video_dest 侧.
    subtitle_conflicts: list[SubtitleConflict] = []
    if org_result.outcome is PlaceOutcome.PLACED and org_result.dest and library is not None and subtitles:
        subtitle_conflicts = await place_subtitles(
            subtitles,
            video_source=source_path,
            video_dest=org_result.dest,
            library=library,
            metadata=metadata,
            file_info=file_info,
            mode=move_mode,
            safe_dirs=safe_dirs,
            link_dir=paths.link.parent if paths.link is not None else None,
            link_name=paths.link.stem if paths.link is not None else None,
            actor_genders=actor_genders,
        )

    if org_result.outcome is PlaceOutcome.PLACED:
        logger.debug("file operations done", number=metadata.number, dest=str(org_result.dest), mode=str(move_mode))
        return FileOperationsResult(
            outcome=PlaceOutcome.PLACED, dest=org_result.dest, subtitle_conflicts=subtitle_conflicts
        )
    logger.debug("file operations failed", number=metadata.number, error=org_result.error)
    return FileOperationsResult(outcome=PlaceOutcome.FAILED, error=org_result.error)


async def apply_file_operations(
    repo: Repository,
    media_file_id: int | None,
    metadata: Metadata,
    config: HotSettings,
    resource_store: ResourceStore,
    *,
    write_nfo: bool = True,
    copy_resources: Sequence[DownloadableResource] | None = None,
    web_client: WebClient | None = None,
    safe_dirs: Sequence[Path] | None = (),
    watermark_dir: Path | None = None,
) -> FileOperationsResult | None:
    """缺少 media_file_id 或对应记录时返回 None (跳过, 不视为失败).
    Library 由 MediaFile.library_id 派生.
    """
    if media_file_id is None:
        return None
    media_file = await repo.get_media_file(media_file_id)
    if media_file is None:
        return None
    library = await repo.get_library(media_file.library_id)
    if library is None:
        return None

    # 渲染路径后执行落盘.
    ext = Path(media_file.path).suffix.lstrip(".")
    file_info = parse_file_info(media_file.path, has_external_subtitle=media_file.has_external_subtitle)
    actor_genders = {a.name: a.gender for a in await repo.get_actors_by_names(metadata.actors)}
    paths = resolve_paths(
        library,
        metadata,
        ext=ext,
        file_info=file_info,
        source_path=Path(media_file.path),
        safe_dirs=safe_dirs,
        actor_genders=actor_genders,
    )
    return await execute_file_operations(
        media_file,
        metadata,
        paths,
        library.move_mode,
        resource_store,
        file_info,
        download_images=True,
        write_nfo=write_nfo,
        copy_resources=copy_resources,
        web_client=web_client,
        config=config,
        library=library,
        safe_dirs=safe_dirs,
        watermark_dir=watermark_dir,
        actor_genders=actor_genders,
    )


async def commit_organized_media_file(
    repo: Repository,
    media: MediaFile,
    placed: Path,
    library_root: Path,
) -> None:
    """整理后的路径仍在本库内才改 path; 已离开本库且源路径不在磁盘上则删行.

    目标路径已被另一行占用时删本行; 占用行缺少刮削字段则从本行补上.
    """
    if media.id is None:
        return
    library = await repo.get_library(media.library_id)
    if not is_descendant(placed, library_root):
        if await existing_disk_path(Path(media.path), follow_symlinks=False) is None:
            await repo.delete_media_file(media.id)
        return

    occupant = await repo.get_media_file_by_path(str(placed))
    if occupant is None or occupant.id == media.id:
        updated = await repo.update_media_file(media.id, path=str(placed))
        if updated is not None and library is not None:
            await refresh_external_subtitle(repo, updated, library)
        return
    if occupant.id is None:
        return
    occupant_updates: MediaFileUpdates = {}
    if occupant.metadata_id is None and media.metadata_id is not None:
        occupant_updates["metadata_id"] = media.metadata_id
        occupant_updates["status"] = media.status
    if occupant.oshash is None and media.oshash is not None:
        occupant_updates["oshash"] = media.oshash
    if occupant.number is None and media.number is not None:
        occupant_updates["number"] = media.number
    if occupant_updates:
        await repo.update_media_file(occupant.id, **occupant_updates)
    await repo.delete_media_file(media.id)
    if library is not None:
        await refresh_external_subtitle(repo, occupant, library)


async def _resolve_local(url: str, store: ResourceStore, client: WebClient) -> Path | None:
    """内部 `/api/resources/{hash}` 与外部 URL 均由 store 解析 (命中缓存直出)."""
    return await store.acquire(url, client)


async def _acquire_first_local(urls: list[str], store: ResourceStore, client: WebClient) -> Path | None:
    for url in urls:
        path = await _resolve_local(url, store, client)
        if path:
            return path
    return None


def _copy_into_library(src: Path, dst: Path) -> None:
    """复制到媒体库, 元数据复制失败不算失败.

    成功路径保持 copy2 的全部行为 (Windows 的 CopyFile2 与其余平台的 copyfile + copystat).
    网盘 / FUSE 挂载 (CloudDrive2 等) 常对 utime、chmod 返回 EPERM: copy2 在内容写入之后
    抛错, 而元数据缺失不影响产物可用, 不应让整个整理任务失败.
    """
    try:
        shutil.copy2(src, dst)
    except OSError:
        # 内容可能已经写好, 重拷一次以区分内容失败与元数据失败: 内容失败会在此再次抛出.
        shutil.copyfile(src, dst)
        try:
            shutil.copystat(src, dst)
        except OSError as exc:
            logger.debug("copy metadata skipped", src=str(src), dst=str(dst), error=str(exc))


# extrafanart / extrathumbs 子目录里的剧照仍按原有逻辑搬运; 封面 / 背景 / 缩略图 / 预告片
# 的发现已统一交给 media.local_artifacts.discover(), 不再维护第二套命名表, 避免
# 「片库显示 A、整理复制 B」.
_LOCAL_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".jfif")
_LOCAL_EXTRAFANART_DIRS = ("extrafanart", "extrathumbs")


def _same_path(a: Path, b: Path) -> bool:
    """路径身份比较: 绝对化 + NFC, 不解析符号链接 (网盘 / 挂载点解析可能失败)."""
    left = nfc_path(os.path.normpath(os.path.abspath(a)))
    right = nfc_path(os.path.normpath(os.path.abspath(b)))
    return left == right


def _copy_local_image(src: Path, dst: Path) -> bool:
    """把本地图片按目标文件名落盘; 非 JPEG 统一转 JPEG, 保持媒体服务器可读."""
    try:
        if _same_path(src, dst):
            return False  # 源就是目标, 不自我覆盖
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in {".jpg", ".jpeg"}:
            _copy_into_library(src, dst)
        else:
            with Image.open(src) as im:
                im.convert("RGB").save(dst, "JPEG", quality=95)
        return True
    except Exception as exc:  # noqa: BLE001 - 单个附属文件失败不影响整理
        logger.warning("local artifact copy failed", src=str(src), dst=str(dst), error=str(exc))
        return False


@in_thread
def _discover_for_organize(source: Path, number: str | None) -> LocalArtifacts:
    """整理开始前用与片库 / 详情页完全相同的规则发现源目录本地产物."""
    return discover_local_artifacts(source, number=number)


def _resources_covered_by_local(art: LocalArtifacts) -> set[DownloadableResource]:
    """本地已有产物的角色不再下载刮削图 (用户确认「完全跳过刮削图」).

    fanart 没有独立刮削源: 原版由 thumb_local 同时写 thumb 与 fanart, 所以本地只要有
    thumb 或 fanart 任一, 就整体剔除 thumb; 搬运时两者互为后备, 目标不会出现混搭.
    """
    covered: set[DownloadableResource] = set()
    if art.has(LocalArtifactKind.poster):
        covered.add(DownloadableResource.poster)
    if art.has(LocalArtifactKind.thumb) or art.has(LocalArtifactKind.fanart):
        covered.add(DownloadableResource.thumb)
    if art.has(LocalArtifactKind.trailer):
        covered.add(DownloadableResource.trailer)
    return covered


@in_thread
def copy_existing_artifacts(
    source: Path, paths: ResolvedPaths, artifacts: LocalArtifacts | None = None
) -> list[Path]:
    """把源目录"原来就有"的封面 / 背景 / 缩略图 / 预告片写进整理目标目录.

    - 发现规则直接复用 ``media.local_artifacts.discover()`` (与片库 / 详情页显示同源),
      保证「识别到的封面」与「复制到目标文件夹的封面」是同一张图.
    - 只按库模板渲染的一套目标名 (``paths.poster`` / ``thumb`` / ``fanart`` / ``trailer``)
      落盘, 不额外写 ``<片名>-poster.jpg`` 之类第二套命名.
    - 逐文件判断而非整目录跳过: 原地整理 (只写 strm) 时源 / 目标同目录, 仍需把
      ``<片名>-poster.jpg`` 复制成模板要求的 ``poster.jpg``.
    - thumb 与 fanart 互为后备: 原版刮削本就是同一张图写两处, 本地只有其一时也补齐
      另一处, 避免目标目录出现「本地图 + 刮削图」混搭.
    - 返回本次成功写入的路径; 单文件失败只记日志, 不阻断整理.
    """
    art = artifacts if artifacts is not None else discover_local_artifacts(source)
    src_dir = source.parent
    files = dict(art.files)
    thumb_src = files.get(LocalArtifactKind.thumb) or files.get(LocalArtifactKind.fanart)
    fanart_src = files.get(LocalArtifactKind.fanart) or files.get(LocalArtifactKind.thumb)
    targets = {
        LocalArtifactKind.poster: paths.poster,
        LocalArtifactKind.thumb: paths.thumb,
        LocalArtifactKind.fanart: paths.fanart,
        LocalArtifactKind.trailer: paths.trailer,
    }
    sources = {
        LocalArtifactKind.poster: files.get(LocalArtifactKind.poster),
        LocalArtifactKind.thumb: thumb_src,
        LocalArtifactKind.fanart: fanart_src,
        LocalArtifactKind.trailer: files.get(LocalArtifactKind.trailer),
    }

    written: list[Path] = []
    for kind, src_file in sources.items():
        if src_file is None:
            continue
        dst = targets[kind]
        if _same_path(src_file, dst):
            continue  # 已在目标名上: 原地即生效, 也不自我覆盖
        if kind is LocalArtifactKind.trailer:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _copy_into_library(src_file, dst)
                written.append(dst)
            except OSError as exc:
                logger.warning("local trailer copy failed", src=str(src_file), error=str(exc))
        elif _copy_local_image(src_file, dst):
            written.append(dst)

    for dirname in _LOCAL_EXTRAFANART_DIRS:
        extra_dir = src_dir / dirname
        try:
            if not extra_dir.is_dir() or _same_path(extra_dir, paths.extrafanart_dir):
                continue
            images = sorted(
                (p for p in extra_dir.iterdir() if p.is_file() and p.suffix.lower() in _LOCAL_IMAGE_SUFFIXES),
                key=lambda p: p.name,
            )
        except OSError as exc:
            logger.warning("local extrafanart scan failed", dir=str(extra_dir), error=str(exc))
            continue
        for img in images:
            target = paths.extrafanart_dir / (img.stem + ".jpg")
            if _copy_local_image(img, target):
                written.append(target)
    return written


@in_thread
def _place_library_images(
    paths: ResolvedPaths,
    *,
    thumb_local: Path | None,
    poster_local: Path | None,
    trailer_local: Path | None,
    extrafanart: Sequence[Path],
    kinds: set[DownloadableResource],
    config: HotSettings | None,
    file_info: FileInfo | None,
    content_type: ContentType,
    watermark_dir: Path | None,
    watermarks: bool = True,
    preserve_existing: bool = False,
) -> None:
    # 复制封面 / 海报 / 预告片 / extrafanart 到库路径.
    # preserve_existing=True 时目标上已有产物 (含视频源目录带过来的原图) 一律保留,
    # 刮削下载的图只补缺; 同步写盘链路不传此参数, 保持覆盖写回.
    def _free(dst: Path) -> bool:
        return not (preserve_existing and dst.exists())

    written: set[Path] = set()
    if thumb_local is not None and DownloadableResource.thumb in kinds:
        # fanart 没有刮削来源, 内容取 thumb, 随 thumb 一同重写.
        for target in (paths.thumb, paths.fanart):
            if not _free(target):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(thumb_local, target)
            written.add(target)

    if DownloadableResource.poster in kinds and _free(paths.poster):
        if poster_local is not None:
            paths.poster.parent.mkdir(parents=True, exist_ok=True)
            _copy_into_library(poster_local, paths.poster)
            written.add(paths.poster)
        elif (
            thumb_local is not None
            and config is not None
            and config.scraping.crop_poster
            and paths.thumb.exists()
        ):
            paths.poster.parent.mkdir(parents=True, exist_ok=True)
            crop_poster(
                paths.thumb,
                paths.poster,
                poster_ratio=config.scraping.poster_ratio,
                jpeg_quality=config.scraping.jpeg_quality,
            )
            written.add(paths.poster)

    if trailer_local is not None and DownloadableResource.trailer in kinds and _free(paths.trailer):
        paths.trailer.parent.mkdir(parents=True, exist_ok=True)
        _copy_into_library(trailer_local, paths.trailer)

    if extrafanart and DownloadableResource.extrafanart in kinds:
        paths.extrafanart_dir.mkdir(parents=True, exist_ok=True)
        for i, p in enumerate(extrafanart):
            target = paths.extrafanart_dir / f"{i + 1}.jpg"
            if not _free(target):
                continue
            _copy_into_library(p, target)

    # 叠加水印: 只作用于本次实际写入的图, 保留下来的原图不重复叠加.
    wm = config.watermark if config is not None else WatermarkConfig()
    if watermarks and wm.enabled and file_info is not None:
        jpeg_quality = config.scraping.jpeg_quality if config is not None else 95
        for target in (paths.thumb, paths.poster):
            if target in written and target.exists():
                apply_cover_watermarks_from_info(
                    target,
                    file_info,
                    content_type,
                    jpeg_quality=jpeg_quality,
                    watermark_dir=watermark_dir,
                    scale=wm.scale,
                    corners=wm.corners,
                )


async def _download_images_via_store(
    metadata: Metadata,
    store: ResourceStore,
    client: WebClient,
    paths: ResolvedPaths,
    config: HotSettings | None,
    kinds: set[DownloadableResource],
    file_info: FileInfo | None,
    content_type: ContentType,
    watermark_dir: Path | None = None,
    watermarks: bool = True,
    preserve_existing: bool = False,
) -> None:
    """优先用 Resource 已有文件; 缺失则现场 acquire.

    ``preserve_existing`` 供整理链路使用: 目标上已有的本地图不被刮削图覆盖.
    """
    thumb_local = None
    if DownloadableResource.thumb in kinds:
        thumb_urls = metadata.thumb_urls
        thumb_local = await _acquire_first_local(thumb_urls, store, client) if thumb_urls else None

    poster_local = None
    if DownloadableResource.poster in kinds:
        poster_urls = metadata.poster_urls
        poster_local = await _acquire_first_local(poster_urls, store, client) if poster_urls else None

    trailer_local = None
    if DownloadableResource.trailer in kinds:
        trailer_urls = metadata.trailer_urls
        trailer_local = await _acquire_first_local(trailer_urls, store, client) if trailer_urls else None

    extrafanart: list[Path] = []
    if DownloadableResource.extrafanart in kinds:
        extrafanart_by_site = metadata.extrafanart_urls
        if extrafanart_by_site:
            priority = list(extrafanart_by_site.keys())
            extrafanart = await store.acquire_extrafanart(extrafanart_by_site, priority, client)

    await _place_library_images(
        paths,
        thumb_local=thumb_local,
        poster_local=poster_local,
        trailer_local=trailer_local,
        extrafanart=extrafanart,
        kinds=kinds,
        config=config,
        file_info=file_info,
        content_type=content_type,
        watermark_dir=watermark_dir,
        watermarks=watermarks,
        preserve_existing=preserve_existing,
    )


@in_thread
def _classify_indexed(path: Path, scan: LibraryScan) -> LibraryFileKind | None:
    return scan.classify(path)


class OrganizeHandler(TaskHandler[OrganizePayload, OrganizeResult]):
    """依据已有 Metadata 整理范围内的 MediaFile; 不刮削, 不修改 Metadata, 不扫描磁盘.

    同库执行期与 DELETE 共用一把锁.
    """

    def __init__(
        self,
        repo: Repository,
        config: HotSettings,
        resource_store: ResourceStore,
        web_client: WebClient | None = None,
        safe_dirs: Sequence[Path] | None = (),
        watermark_dir: Path | None = None,
        *,
        library_locks: LibraryTaskLocks | None = None,
    ):
        super().__init__(payload_t=OrganizePayload, result_t=OrganizeResult)
        self._repo = repo
        self._config = config
        self._web_client = web_client
        self._resource_store = resource_store
        self._safe_dirs = safe_dirs
        self._watermark_dir = watermark_dir
        self._library_locks = library_locks if library_locks is not None else LibraryTaskLocks()

    async def _load_scope(self, payload: OrganizePayload, library: Library) -> list[MediaFile]:
        assert library.id is not None
        if payload.media_file_ids is not None:
            return await self._repo.list_media_files(library_id=library.id, ids=payload.media_file_ids, limit=None)
        indexed = await self._repo.list_media_files(library_id=library.id, limit=None)
        if nfc_path(payload.path) != nfc_path(library.path):
            return [mf for mf in indexed if path_is_under(mf.path, payload.path)]
        return indexed

    async def handle(self, payload: OrganizePayload) -> TaskResult[OrganizeResult]:
        library = await self._repo.get_library(payload.library_id)
        if library is None:
            return TaskResult(success=False, error=f"媒体库 {payload.library_id} 不存在")
        assert library.id is not None
        library_root = Path(library.path)
        if not await path_is_dir(library_root):
            return TaskResult(success=False, error=f"不是目录: {library.path}")
        if payload.media_file_ids is None:
            scope = Path(payload.path) if payload.path else library_root
            if nfc_path(str(scope)) != nfc_path(library.path) and not await path_is_dir(scope):
                return TaskResult(success=False, error=f"不是目录: {scope}")

        lock = await self._library_locks.get(library.id)
        async with lock:
            return await self._handle_unlocked(payload, library)

    async def _handle_unlocked(self, payload: OrganizePayload, library: Library) -> TaskResult[OrganizeResult]:
        indexed = await self._load_scope(payload, library)
        skipped = 0
        if payload.media_file_ids and not indexed:
            # 勾选快照里的行在本次运行前已消失 (自动整理的单文件范围是主要来源): 不是「空跑」, 记一次跳过.
            logger.warning("organize scope missing", library_id=library.id, media_file_ids=payload.media_file_ids)
            skipped += 1
        library_root = Path(library.path)
        media_extensions = frozenset(self._config.watcher.media_extensions) or MEDIA_EXTENSIONS
        scan = LibraryScan(
            trailer_pattern=library.trailer_pattern,
            blacklist_patterns=library.blacklist_patterns,
            min_file_size=library.min_file_size,
            media_extensions=media_extensions,
        )
        live: list[MediaFile] = []
        prune_total = len(indexed)
        if prune_total:
            await self.report_progress(0, prune_total, "prune")
            for i, mf in enumerate(indexed, start=1):
                if mf.id is not None:
                    mf_path = Path(mf.path)
                    missing = await existing_disk_path(mf_path, follow_symlinks=False) is None
                    if missing or not is_descendant(mf_path, library_root) or is_in_trash(mf_path):
                        await self._repo.delete_media_file(mf.id)
                    else:
                        kind = await _classify_indexed(mf_path, scan)
                        if kind is LibraryFileKind.UNWANTED or kind is LibraryFileKind.SKIP:
                            skipped += 1
                        else:
                            # MEDIA, 以及 classify 返回 None 的已索引行 (不在当前扩展名白名单,
                            # 例如库曾用 patterns 收过 .txt). 仍按索引落盘, 不按扫描 glob 再裁一次.
                            live.append(mf)
                await self.report_progress(i, prune_total, "prune")
        else:
            live = indexed

        organized = 0
        conflicted = 0
        conflicts: list[OrganizeConflict] = []
        failed = 0
        total = len(live)
        # 只有移动方式会移走源文件, 复制 / 硬链接 / 符号链接不腾空目录.
        prune_candidates: set[Path] = set()
        collect_prune = payload.prune_empty_dirs and library.move_mode is MoveMode.MOVE
        if total == 0:
            await self.report_progress(1, 1, "done")
        else:
            await self.report_progress(0, total, "organize")
            for i, media_file in enumerate(live, start=1):
                path_str = media_file.path
                file_path = Path(path_str)
                if media_file.metadata_id is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                metadata = await self._repo.get_metadata(media_file.metadata_id)
                if metadata is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                write_nfo = library.write_nfo if payload.write_nfo is None else payload.write_nfo
                copy_resources = library.copy_resources if payload.copy_resources is None else payload.copy_resources
                fop_result = await apply_file_operations(
                    self._repo,
                    media_file.id,
                    metadata,
                    self._config,
                    self._resource_store,
                    write_nfo=write_nfo,
                    copy_resources=copy_resources,
                    web_client=self._web_client,
                    safe_dirs=self._safe_dirs,
                    watermark_dir=self._watermark_dir,
                )
                if fop_result is None:
                    skipped += 1
                    await self.report_progress(i, total, file_path.name)
                    continue

                if fop_result.dest and media_file.id is not None:
                    await commit_organized_media_file(self._repo, media_file, fop_result.dest, library_root)
                match fop_result.outcome:
                    case PlaceOutcome.PLACED:
                        organized += 1
                        if collect_prune:
                            prune_candidates.update(ancestor_dirs(file_path, library_root=library_root))
                        # 字幕冲突的 path 是留在原处的那条字幕, 不是视频源路径.
                        conflicted += _record_conflicts(
                            conflicts,
                            [
                                OrganizeConflict(
                                    path=str(item.source),
                                    target=str(item.target),
                                    reason=OrganizeConflictReason.subtitle_target_exists,
                                )
                                for item in fop_result.subtitle_conflicts
                            ],
                        )
                    case PlaceOutcome.CONFLICT:
                        # 行不提交新路径, 留在源路径: 下次整理会再次报告, 直到用户自行处理.
                        target = fop_result.conflict_target
                        conflicted += _record_conflicts(
                            conflicts,
                            []
                            if target is None
                            else [
                                OrganizeConflict(
                                    path=path_str, target=str(target), reason=OrganizeConflictReason.target_exists
                                )
                            ],
                        )
                    case PlaceOutcome.FAILED:
                        logger.warning("organize failed", path=path_str, error=fop_result.error)
                        failed += 1
                await self.report_progress(i, total, file_path.name)
            await self.report_progress(total, total, "done")

        pruned_dirs = 0
        if prune_candidates:
            pruned_dirs = (await prune_empty_dirs(prune_candidates, library_root=library_root)).removed

        logger.info(
            "organize completed",
            path=payload.path,
            organized=organized,
            skipped=skipped,
            conflicted=conflicted,
            failed=failed,
            pruned_dirs=pruned_dirs,
        )

        return TaskResult(
            True,
            result=OrganizeResult(
                organized=organized,
                skipped=skipped,
                conflicted=conflicted,
                failed=failed,
                pruned_dirs=pruned_dirs,
                conflicts=conflicts,
            ),
        )


def _record_conflicts(conflicts: list[OrganizeConflict], entries: Sequence[OrganizeConflict]) -> int:
    """登记未处理的目标, 返回条数; 列表达到上限后只计数, 差额只留在任务日志里."""
    conflicts.extend(entries[: max(0, ORGANIZE_CONFLICT_LIMIT - len(conflicts))])
    return len(entries)


def _add_resource_ref(url: str, live_urls: set[str], live_hashes: set[str]) -> None:
    if not url:
        return
    url_hash = internal_url_hash(url)
    if url_hash is not None:
        live_hashes.add(url_hash)
    else:
        live_urls.add(url)


async def _collect_live_resource_refs(repo: Repository) -> tuple[set[str], set[str]]:
    live_urls: set[str] = set()
    live_hashes: set[str] = set()
    offset = 0
    page = 500
    # 收集 Metadata 仍引用的 locator / url_hash.
    while True:
        batch, _total = await repo.list_metadata(offset=offset, limit=page)
        if not batch:
            break
        for meta in batch:
            for u in (*meta.poster_urls, *meta.thumb_urls, *meta.trailer_urls):
                _add_resource_ref(u, live_urls, live_hashes)
            for site_urls in meta.extrafanart_urls.values():
                for u in site_urls:
                    _add_resource_ref(u, live_urls, live_hashes)
        offset += len(batch)
        if len(batch) < page:
            break

    offset = 0
    # 收集 Actor 头像 URL.
    while True:
        actors = await repo.list_actors(offset=offset, limit=page)
        if not actors:
            break
        for actor in actors:
            for u in actor.image_urls or []:
                _add_resource_ref(u, live_urls, live_hashes)
        offset += len(actors)
        if len(actors) < page:
            break
    return live_urls, live_hashes


@in_thread
def _missing_media_ids(rows: Sequence[tuple[int, str]]) -> list[int]:
    """不跟随符号链接判断存在."""
    return [media_id for media_id, path in rows if existing_disk_path_sync(path, follow_symlinks=False) is None]


class CleanupHandler(TaskHandler[CleanupPayload, CleanupResult]):
    """不因缺少 MediaFile 而删除 Metadata."""

    def __init__(self, repo: Repository, resource_store: ResourceStore):
        super().__init__(payload_t=CleanupPayload, result_t=CleanupResult)
        self._repo = repo
        self._resource_store = resource_store

    async def handle(self, payload: CleanupPayload) -> TaskResult[CleanupResult]:
        files_removed = 0
        resources_removed = 0

        # 删除磁盘上不存在的 MediaFile 记录.
        if payload.remove_missing_files:
            missing_ids: list[int] = []
            offset = 0
            page = 500
            while True:
                batch = await self._repo.list_media_files(limit=page, offset=offset)
                if not batch:
                    break
                missing_ids.extend(await _missing_media_ids([(mf.id, mf.path) for mf in batch if mf.id is not None]))
                offset += len(batch)
                if len(batch) < page:
                    break
            for media_id in missing_ids:
                await self._repo.delete_media_file(media_id)
                files_removed += 1

        # 删除不被 Metadata / Actor URL 引用的 Resource.
        if payload.remove_unreferenced_resources:
            live_urls, live_hashes = await _collect_live_resource_refs(self._repo)
            resources_removed = await self._resource_store.purge_unreferenced(live_urls, live_hashes)

        logger.info("cleanup completed", files_removed=files_removed, resources_removed=resources_removed)

        return TaskResult(True, result=CleanupResult(files_removed=files_removed, resources_removed=resources_removed))


# ---------------------------------------------------------------------------
# 元数据写回: 只重写 NFO 与附属图片, 不移动视频、不改索引、不写链接与字幕.
# 与 ORGANIZE 共用 _download_images_via_store / write_nfo_file, 保证产物格式一致.
# 由编辑保存、封面裁切与 POST /metadata/{id}/sync-artifacts 调用.
# ---------------------------------------------------------------------------


@dataclass
class ArtifactSyncResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {"written": self.written, "skipped": self.skipped, "errors": self.errors}


def _backup_nfo(path: Path) -> None:
    """覆盖前留一份 .nfo.bak, 保留媒体服务器写入的节点以便人工恢复."""
    if not path.exists():
        return
    try:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    except OSError as exc:
        logger.warning("nfo backup failed", path=str(path), error=str(exc))


async def write_metadata_artifacts(
    repo: "Repository",
    metadata: "Metadata",
    *,
    resource_store: ResourceStore,
    web_client: WebClient,
    config: HotSettings,
) -> "ArtifactSyncResult":
    """把当前 Metadata 的 NFO 与封面 / 海报 / 背景图重写到视频实际所在目录.

    - 只写盘: 不改 DB, 不移动视频, 不触发整理, 不叠加水印 (每次从 Resource 原图重拷).
    - 产物按视频同目录的 Emby/Kodi 命名: {video_name}.nfo / poster.jpg / thumb.jpg / fanart.jpg.
      不再按库整理模板推算目录: 「先入库后刮削」的文件往往不在模板算出的位置, 旧逻辑会因
      目标目录不存在而整条跳过, 或把 NFO 写到视频不在的目录, 媒体服务器自然读不到.
    - 遵守 Library.write_nfo 与 Library.copy_resources; 不复制预告片, 保留本地 trailer.mp4.
    - 仅处理视频仍在磁盘的 MediaFile; 单个文件失败记入 errors 并继续.
    """
    result = ArtifactSyncResult()
    assert metadata.id is not None
    files = await repo.get_media_by_metadata_id(metadata.id)
    if not files:
        result.skipped.append("无关联媒体文件")
        return result

    for media_file in files:
        src = Path(media_file.path)
        library = await repo.get_library(media_file.library_id)
        if library is None:
            result.skipped.append(f"{src.name}: 媒体库不存在")
            continue
        if await existing_disk_path(src) is None:
            result.skipped.append(f"{src.name}: 源视频不在磁盘")
            continue

        # 视频所在目录就是媒体服务器读取的目录, 产物一律写这里.
        paths = ResolvedPaths(
            video=src,
            thumb=src.parent / "thumb.jpg",
            poster=src.parent / "poster.jpg",
            fanart=src.parent / "fanart.jpg",
            extrafanart_dir=src.parent / "extrafanart",
            nfo=src.with_suffix(".nfo"),
            trailer=src.parent / "trailer.mp4",
            link=None,
        )
        file_info = parse_file_info(media_file.path, has_external_subtitle=media_file.has_external_subtitle)

        # 预告片不参与写回: 本地 trailer.mp4 可能是用户自行放置的.
        kinds = set(library.copy_resources) - {DownloadableResource.trailer}
        try:
            await _download_images_via_store(
                metadata,
                resource_store,
                web_client,
                paths,
                config,
                kinds,
                file_info=file_info,
                content_type=metadata.content_type,
                watermarks=False,
            )
            if library.write_nfo:
                _backup_nfo(paths.nfo)
                if not await write_nfo_file(metadata, paths.nfo):
                    result.errors.append(f"{src.name}: NFO 写入失败")
                    continue
            result.written.append(str(src.parent))
        except Exception as exc:  # noqa: BLE001 - 单文件失败不影响其它文件
            logger.exception("artifact sync failed", number=metadata.number, path=str(src))
            result.errors.append(f"{src.name}: {exc}")
    return result
