import asyncio
import hashlib
import io
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from PIL import Image, ImageOps
from pydantic import TypeAdapter

from ...aggregate import compute_merge_updates
from ...db.embeddings import EmbeddingKind
from ...db.models import Metadata, MetadataSortField, SavedQueryEntity, SortOrder, TaskType
from ...db.repo_types import WriteMode
from ...db.repos.media import file_phase_of
from ...handlers import ScrapePayload
from ...handlers.file import ArtifactSyncResult, write_metadata_artifacts
from ...media import manual_crop_image
from ...media.artifact_proxy import image_response, local_video_response, proxy_remote_trailer
from ...media.lara import LaraClient, LaraError
from ...media.local_artifacts import (
    LocalArtifactKind,
    LocalArtifacts,
    discover,
    pick_proxyable_trailer,
)
from ...media.resource_store import RESOURCE_URL_PREFIX
from ...parsing import FilePhaseSummary as ParsedFilePhase
from ...parsing import summarize_file_phases
from ...semantic import SemanticUnavailable
from ...utils.dates import normalize_calendar_date
from ...utils.model import to_resp
from ..deps import RepoDep, RuntimeDep
from ..models import (
    CommentResponse,
    CropPosterRequest,
    FilePhaseSummary,
    MergeRequest,
    MetadataBatchDeleteResponse,
    MetadataBatchIdsRequest,
    MetadataBatchScrapeRequest,
    MetadataBatchScrapeResponse,
    MetadataDetailResponse,
    MetadataListResponse,
    MetadataLocksRequest,
    MetadataMergeEntriesRequest,
    MetadataResponse,
    MetadataTranslateItem,
    MetadataTranslateRequest,
    MetadataTranslateResponse,
    MetadataUserTagsRequest,
    PartialMetadata,
    SemanticHitInfo,
    UserTagLinksResponse,
    UserTagResponse,
    to_media_file_response,
)
from ..support.local_artifacts import inject_local_artifacts
from ..support.metadata_filters import MetadataFilters, parse_metadata_filters, require_known_definition
from .saved_queries import resolve_saved_query_id_subquery

if TYPE_CHECKING:
    from ...app.runtime import AppRuntime
    from ...db.repo_types import MetadataFields
    from ...db.repository import Repository

logger = structlog.get_logger()

router = APIRouter(prefix="/metadata", tags=["metadata"])

# 封面上传: 限制原始体积与最终边长, 避免把超大原图塞进资源库.
_MAX_POSTER_BYTES = 20 * 1024 * 1024
_MAX_POSTER_EDGE = 2400


async def _auto_sync_artifacts(
    metadata: Metadata, repo: "Repository", runtime: "AppRuntime"
) -> ArtifactSyncResult | None:
    """保存成功后把 NFO / 封面写回磁盘. 失败只记日志, 不影响已提交的 DB 写入."""
    try:
        return await write_metadata_artifacts(
            repo,
            metadata,
            resource_store=runtime.resource_store,
            web_client=runtime.web_client,
            config=runtime.config.hot,
            )
    except Exception:
        logger.exception("auto artifact sync failed", metadata_id=metadata.id)
        return None


async def build_metadata_responses(repo: RepoDep, items: Sequence[Metadata]) -> list[MetadataResponse]:
    """列表与语义检索共用: 补 file_count / file_phase 聚合."""
    summaries = await repo.summarize_media_by_metadata(items)
    responses: list[MetadataResponse] = []
    for m in items:
        summary = summaries.get(m.id) if m.id is not None else None
        response = to_resp(MetadataResponse, m)
        if summary is not None:
            response = response.model_copy(
                update={
                    "file_count": summary.file_count,
                    "file_phase": _file_phase_resp(summary.phase),
                }
            )
        responses.append(response)
    return await inject_local_artifacts(repo, responses, items)


def _file_phase_resp(summary: ParsedFilePhase) -> FilePhaseSummary:
    return FilePhaseSummary(
        has_subtitle=summary.has_subtitle,
        censored=summary.censored,
        uncensored=summary.uncensored,
        mosaics=list(summary.mosaics),
        definition=summary.definition,
    )


@router.get("/schema")
async def get_metadata_schema() -> dict:
    return TypeAdapter(PartialMetadata).json_schema()


@router.get("")
async def list_metadata(
    repo: RepoDep,
    runtime: RuntimeDep,
    filters: Annotated[MetadataFilters, Depends(parse_metadata_filters)],
    search: Annotated[str | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    sort_by: Annotated[MetadataSortField, Query(description="Sort field")] = MetadataSortField.UPDATED_AT,
    order: Annotated[SortOrder, Query(description="Sort order")] = SortOrder.DESC,
    semantic_query: Annotated[
        str | None, Query(min_length=1, max_length=200, description="按画面描述排序; 提供时忽略 sort_by / order")
    ] = None,
    semantic_kind: Annotated[Literal["all", "cover", "still"], Query(description="参与语义排序的图片来源")] = "all",
) -> MetadataListResponse:
    definition = require_known_definition(filters.definition)
    id_subquery_sql = None
    if filters.saved_query_id is not None:
        id_subquery_sql = await resolve_saved_query_id_subquery(repo, filters.saved_query_id, SavedQueryEntity.METADATA)
    if semantic_query is not None:
        return await _semantic_list(
            runtime=runtime,
            repo=repo,
            filters=filters,
            search=search,
            semantic_query=semantic_query,
            semantic_kind=semantic_kind,
            definition=definition,
            id_subquery_sql=id_subquery_sql,
            offset=offset,
            limit=limit,
        )
    items, total = await repo.list_metadata(
        keyword=search,
        offset=offset,
        limit=limit,
        sort_by=sort_by,
        order=order,
        actor_ids=filters.actor_id,
        director_ids=filters.director_id,
        tag_ids=filters.tag_id,
        studio_ids=filters.studio_id,
        publisher_ids=filters.publisher_id,
        series_ids=filters.series_id,
        user_tag_ids=filters.user_tag_id,
        has_files=filters.has_files,
        has_subtitle=filters.has_subtitle,
        vr=filters.vr,
        mosaic=filters.mosaic,
        uncensored=filters.uncensored,
        definition=definition,
        content_type=filters.content_type,
        id_subquery_sql=id_subquery_sql,
        library_id=filters.library_id,
    )
    return MetadataListResponse(items=await build_metadata_responses(repo, items), total=total)


async def _semantic_list(
    *,
    runtime: RuntimeDep,
    repo: RepoDep,
    filters: MetadataFilters,
    search: str | None,
    semantic_query: str,
    semantic_kind: Literal["all", "cover", "still"],
    definition: str | None,
    id_subquery_sql: str | None,
    offset: int,
    limit: int,
) -> MetadataListResponse:
    """语义参数出现时接管排序: 候选集来自同一套筛选, 返回结构与普通列表一致."""
    service = runtime.semantic
    if service is None or not service.enabled:
        raise HTTPException(status_code=503, detail="语义检索未启用")
    allowed_ids = None
    if filters.active or search:
        allowed_ids = await repo.list_metadata_ids(
            keyword=search,
            actor_ids=filters.actor_id,
            director_ids=filters.director_id,
            tag_ids=filters.tag_id,
            studio_ids=filters.studio_id,
            publisher_ids=filters.publisher_id,
            series_ids=filters.series_id,
            user_tag_ids=filters.user_tag_id,
            has_files=filters.has_files,
            has_subtitle=filters.has_subtitle,
            vr=filters.vr,
            mosaic=filters.mosaic,
            uncensored=filters.uncensored,
            definition=definition,
            content_type=filters.content_type,
            id_subquery_sql=id_subquery_sql,
            library_id=filters.library_id,
        )
    kind = None if semantic_kind == "all" else EmbeddingKind(semantic_kind)
    try:
        hits = await service.search(semantic_query, limit, kind, allowed_ids, offset)
        total = await service.count(kind, allowed_ids)
    except SemanticUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not hits:
        return MetadataListResponse(items=[], total=total)
    ids = [hit.metadata_id for hit in hits]
    items, _ = await repo.list_metadata(ids=ids, limit=len(ids))
    by_id = {item.id: item for item in items}
    ordered = [by_id[hit.metadata_id] for hit in hits if hit.metadata_id in by_id]
    responses = {response.id: response for response in await build_metadata_responses(repo, ordered)}
    return MetadataListResponse(
        items=[responses[hit.metadata_id] for hit in hits if hit.metadata_id in responses],
        total=total,
        semantic_hits=[
            SemanticHitInfo(
                id=hit.metadata_id,
                kind=hit.kind,
                resource_url=hit.resource_url,
                similarity=hit.score,
            )
            for hit in hits
            if hit.metadata_id in responses
        ],
    )


@router.post("/batch/delete")
async def batch_delete_metadata(req: MetadataBatchIdsRequest, repo: RepoDep) -> MetadataBatchDeleteResponse:
    """级联行为与单条删除一致."""
    deleted, missing = await repo.batch_delete_metadata(req.ids)
    logger.info("metadata batch deleted", deleted=deleted, missing=missing)
    return MetadataBatchDeleteResponse(deleted=deleted, missing=missing)


@router.post("/batch/scrape", status_code=202)
async def batch_scrape_metadata(req: MetadataBatchScrapeRequest, repo: RepoDep) -> MetadataBatchScrapeResponse:
    """以各自 number 重新刮削."""
    missing = 0
    payloads: list[ScrapePayload] = []
    for metadata_id in req.ids:
        metadata = await repo.get_metadata(metadata_id)
        if metadata is None:
            missing += 1
            continue
        payloads.append(
            ScrapePayload(
                number=metadata.number,
                content_type=req.content_type if req.content_type is not None else metadata.content_type,
                use_cache=req.use_cache,
                # 从片库主动点「重新刮削」即表示要用新结果覆盖: 先清掉 NFO 导入 / 手动编辑留下的字段锁,
                # 否则 upsert_metadata 会跳过锁定列, 界面与磁盘都看不到新数据.
                overwrite_locked=True,
            )
        )
    # 批量提交动辄数百条, 逐条提交会重复付出事务开销
    tasks = await repo.create_tasks(TaskType.SCRAPE, payloads)
    task_ids = [task.id for task in tasks if task.id is not None]
    logger.info("metadata batch scrape submitted", submitted=len(task_ids), missing=missing)
    return MetadataBatchScrapeResponse(submitted=len(task_ids), missing=missing, task_ids=task_ids)


@router.post("/batch/user-tags")
async def batch_metadata_user_tags(req: MetadataUserTagsRequest, repo: RepoDep) -> UserTagLinksResponse:
    """把一组用户标签应用到一组影片; 未知标签 id 返回 404, 不存在的影片 id 计入 missing."""
    try:
        result = await repo.apply_metadata_user_tags(req.ids, req.user_tag_ids, action=req.action)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    logger.info(
        "metadata user tags applied",
        action=req.action,
        changed=result.changed,
        unchanged=result.unchanged,
        missing=result.missing,
    )
    return UserTagLinksResponse(changed=result.changed, unchanged=result.unchanged, missing=result.missing)


@router.post("/merge-entries")
async def merge_metadata_entries(req: MetadataMergeEntriesRequest, repo: RepoDep) -> MetadataResponse:
    """把来源条目的 raw 快照补入目标并删除来源: 目标字段不变, 关联记录迁入目标.

    ``rescrape`` 要求合并提交后再按目标番号入队 SCRAPE: 只有 raw 已落库, 复用快照的聚合
    才能按当前优先级重建字段与站点级字典.
    """
    try:
        metadata = await repo.merge_metadata_entries(req.target_id, req.source_ids)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    rescrape_task_id: int | None = None
    if req.rescrape:
        task = await repo.create_task(
            TaskType.SCRAPE,
            ScrapePayload(number=metadata.number, content_type=metadata.content_type),
        )
        rescrape_task_id = task.id
    logger.info(
        "metadata entries merged",
        target_id=req.target_id,
        source_ids=req.source_ids,
        rescrape_task_id=rescrape_task_id,
    )
    return to_resp(MetadataResponse, metadata)


@router.get("/{metadata_id}/artifact")
@router.head("/{metadata_id}/artifact")
async def get_metadata_artifact(
    metadata_id: int,
    request: Request,
    repo: RepoDep,
    kind: Annotated[LocalArtifactKind, Query(description="产物类型")] = LocalArtifactKind.poster,
) -> Response:
    """视频同目录的本地图片 / 预告片直出; 没有本地预告片时把远程直链走服务端代理.

    按当前 ``MediaFile.path`` 实时定位, 不写数据库、不需要重扫: 整理后文件挪了位置也自动跟随.
    响应层把同源 URL 插到各 URL 列表首位, 浏览器只请求本服务, 绕开外站 CORS / 防盗链.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    paths = await repo.first_media_paths([metadata_id])
    art = LocalArtifacts(files={})
    video_path = paths.get(metadata_id)
    if video_path is not None:
        art = await asyncio.to_thread(discover, Path(video_path), number=metadata.number)

    if kind is LocalArtifactKind.trailer:
        local = art.files.get(kind)
        if local is not None:
            return local_video_response(request, local)
        remote = pick_proxyable_trailer(metadata.trailer_urls)
        if remote is not None:
            return await proxy_remote_trailer(request, remote)
        raise HTTPException(status_code=404, detail="无可播放的预告片")

    local = art.files.get(kind)
    if local is None:
        raise HTTPException(status_code=404, detail="本地图片不存在")
    return image_response(local)


@router.get("/{metadata_id}")
async def get_metadata(metadata_id: int, repo: RepoDep) -> MetadataDetailResponse:
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    # 关联文件 / 用户 tag / 评论 / 分类 id
    files = await repo.get_media_by_metadata_id(metadata_id)
    user_tags = await repo.list_metadata_user_tags(metadata_id)
    comments = await repo.list_comments(metadata_id)
    (
        actor_ids,
        actor_genders,
        director_ids,
        tag_ids,
        studio_id,
        publisher_id,
        series_id,
    ) = await repo.resolve_metadata_facet_ids(metadata)
    metadata_response = to_resp(MetadataResponse, metadata).model_copy(
        update={
            "file_count": len(files),
            "file_phase": _file_phase_resp(
                summarize_file_phases((file_phase_of(f) for f in files), metadata.content_type)
            ),
        }
    )
    # 详情页与列表页同一套本地产物注入: 本地封面优先, 本地 trailer.mp4 优先可播.
    (metadata_response,) = await inject_local_artifacts(repo, [metadata_response], [metadata])
    return MetadataDetailResponse(
        metadata=metadata_response,
        files=[to_media_file_response(f, metadata.content_type) for f in files],
        user_tags=[to_resp(UserTagResponse, t) for t in user_tags],
        comments=[to_resp(CommentResponse, c) for c in comments],
        actor_ids=actor_ids,
        actor_genders=actor_genders,
        director_ids=director_ids,
        tag_ids=tag_ids,
        studio_id=studio_id,
        publisher_id=publisher_id,
        series_id=series_id,
    )


@router.patch("/{metadata_id}")
async def update_metadata(
    metadata_id: int, req: PartialMetadata, repo: RepoDep, runtime: RuntimeDep
) -> MetadataResponse:
    updates = cast("MetadataFields", {k: v for k, v in req.model_dump().items() if v is not None})
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")
    if "release" in updates:
        raw_release = updates["release"]
        if isinstance(raw_release, str):
            normalized = normalize_calendar_date(raw_release)
            if normalized is None:
                raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
            updates["release"] = normalized
        else:
            raise HTTPException(status_code=422, detail="发行日期必须为 YYYY-MM-DD")
    metadata = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **updates)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata updated", metadata_id=metadata_id, fields=list(updates.keys()))
    await _auto_sync_artifacts(metadata, repo, runtime)
    return to_resp(MetadataResponse, metadata)


@router.put("/{metadata_id}/locks")
async def set_metadata_locks(metadata_id: int, req: MetadataLocksRequest, repo: RepoDep) -> MetadataResponse:
    """整体替换锁定字段集合."""
    metadata = await repo.set_metadata_locks(metadata_id, req.fields)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata locks updated", metadata_id=metadata_id, fields=[str(field) for field in req.fields])
    return to_resp(MetadataResponse, metadata)


@router.delete("/{metadata_id}", status_code=204)
async def delete_metadata(metadata_id: int, repo: RepoDep) -> None:
    deleted = await repo.delete_metadata(metadata_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="影片不存在")
    logger.info("metadata deleted", metadata_id=metadata_id)


@router.post("/{metadata_id}/crop-poster")
async def crop_poster_from_thumb(
    metadata_id: int,
    req: CropPosterRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> MetadataResponse:
    """从封面 (thumb) 按像素框裁切海报, 写入派生 Resource 并更新 poster_urls.

    先按 EXIF 转正、再顺时针旋转 ``req.rotation``, 框相对旋转后的图像.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    if not metadata.thumb_urls:
        raise HTTPException(status_code=400, detail="无封面图可裁切")

    # 按像素框裁切封面, 写入派生 Resource
    thumb_url = metadata.thumb_urls[0]
    box = (req.left, req.top, req.right, req.bottom)
    try:
        poster_url = await manual_crop_image(
            thumb_url,
            box,
            runtime.resource_store,
            runtime.web_client,
            runtime.config.hot,
            runtime.config.cold.data_dir,
            subject="封面图",
            rotation=req.rotation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # 写回 poster_urls
    updated = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, poster_urls=[poster_url])
    assert updated is not None
    logger.info(
        "poster cropped",
        metadata_id=metadata_id,
        box=box,
        rotation=req.rotation,
        poster_url=poster_url,
    )
    await _auto_sync_artifacts(updated, repo, runtime)
    return to_resp(MetadataResponse, updated)


@router.post("/{metadata_id}/translate")
async def translate_metadata(
    metadata_id: int,
    req: MetadataTranslateRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> MetadataTranslateResponse:
    """按需翻译标题/简介: 只返回译文, 不写库.

    译文交前端二次确认, 用户勾选后才走 PATCH 写回: 一次点击不会意外覆盖原文,
    也避免有限额度被误消耗. 写回时 ``WriteMode.MANUAL`` 会自动锁定字段.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    config = runtime.config.hot.lara
    client = LaraClient(config)
    if not client.configured:
        raise HTTPException(
            status_code=400, detail="尚未配置 Lara Access Key, 请到「设置 → lara」填写"
        )

    target = (req.target or config.target_language or "zh-CN").strip()
    source = (req.source or config.source_language or "").strip() or None
    items: list[MetadataTranslateItem] = []
    characters = 0
    detected: str | None = None
    for field in req.fields:
        text = getattr(metadata, field, None)
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            translation, source_language = await client.translate(
                text, target=target, source=source
            )
        except LaraError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        detected = detected or source_language
        characters += len(text)
        items.append(MetadataTranslateItem(field=field, source=text, translation=translation))

    if not items:
        raise HTTPException(status_code=400, detail="标题与简介都为空, 没有可翻译的内容")
    logger.info(
        "metadata translated",
        metadata_id=metadata_id,
        fields=[item.field for item in items],
        characters=characters,
    )
    return MetadataTranslateResponse(
        target_language=target,
        source_language=detected,
        characters=characters,
        items=items,
    )


@router.post("/{metadata_id}/poster")
async def upload_poster(
    metadata_id: int,
    file: Annotated[UploadFile, File(description="本地图片文件")],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> MetadataResponse:
    """用上传的本地图片替换封面, 并自动写盘.

    poster 与 thumb 同时指向这张图: 详情页优先显示 thumb, Emby 取 poster,
    只改其中一个会出现「网页换了、Emby 没换」.
    """
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > _MAX_POSTER_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 20MB")

    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法识别该图片") from exc
    image.thumbnail((_MAX_POSTER_EDGE, _MAX_POSTER_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92, optimize=True)
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]

    async def producer(dest: Path) -> bool:
        dest.write_bytes(data)
        return True

    resource = await runtime.resource_store.acquire_derived(
        f"upload:{digest}", "poster", digest, producer, ext=".jpg"
    )
    if resource is None:
        raise HTTPException(status_code=500, detail="封面写入失败")
    url = f"{RESOURCE_URL_PREFIX}/{runtime.resource_store.url_hash(resource.url)}"

    updated = await repo.update_metadata(
        metadata_id,
        mode=WriteMode.MANUAL,
        poster_urls=[url],
        thumb_urls=[url],
    )
    assert updated is not None
    await _auto_sync_artifacts(updated, repo, runtime)
    logger.info("poster uploaded", metadata_id=metadata_id, bytes=len(data))
    return to_resp(MetadataResponse, updated)


@router.post("/{metadata_id}/sync-artifacts")
async def sync_artifacts(metadata_id: int, repo: RepoDep, runtime: RuntimeDep) -> dict[str, list[str]]:
    """手动把当前 NFO / 封面重写到已整理目录 (自动写盘失败或批量补写时使用)."""
    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")
    result = await write_metadata_artifacts(
        repo,
        metadata,
        resource_store=runtime.resource_store,
        web_client=runtime.web_client,
        config=runtime.config.hot,
    )
    logger.info(
        "artifacts synced",
        metadata_id=metadata_id,
        written=len(result.written),
        skipped=len(result.skipped),
        errors=len(result.errors),
    )
    return result.as_dict()


@router.post("/{metadata_id}/merge")
async def merge_metadata(metadata_id: int, req: MergeRequest, repo: RepoDep) -> MetadataResponse:
    if not req.selections:
        raise HTTPException(status_code=400, detail="未提供选择项")

    metadata = await repo.get_metadata(metadata_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="影片不存在")

    # 按 selections 从 raw 合并字段
    try:
        updates = compute_merge_updates(metadata.raw, metadata.field_sources, req.selections)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    if not updates:
        raise HTTPException(status_code=400, detail="没有有效的选择项")

    # 写回元数据
    updated = await repo.update_metadata(metadata_id, mode=WriteMode.MANUAL, **cast("MetadataFields", updates))
    assert updated is not None
    logger.info("metadata merged", metadata_id=metadata_id, selections=req.selections)
    return to_resp(MetadataResponse, updated)
