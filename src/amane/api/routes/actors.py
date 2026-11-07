"""身份治理 (rename/merge/delete/rules) 仍由 /facets/actor 处理."""

import hashlib
import io
from pathlib import Path
from typing import Annotated, cast

import structlog
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from PIL import Image, ImageOps

from ...db.models import Actor, FacetKind, SavedQueryEntity, TaskType
from ...db.repo_types import ActorBrowseItem, ActorBrowseParams, ActorPersonFields, WriteMode
from ...handlers import ActorScrapePayload
from ...media import manual_crop_image
from ...media.actor_avatar import sync_actor_avatar
from ...media.lara import LaraClient, LaraError
from ...media.resource_store import RESOURCE_URL_PREFIX
from ...utils.dates import normalize_calendar_date
from ...utils.model import to_resp
from ..deps import RepoDep, RuntimeDep
from ..models import (
    ActorAvatarUploadResponse,
    ActorClearPersonBatchRequest,
    ActorClearPersonBatchResponse,
    ActorGenderBatchRequest,
    ActorGenderBatchResponse,
    ActorListResponse,
    ActorLocksRequest,
    ActorResponse,
    ActorScrapeRequest,
    ActorTranslateItem,
    ActorTranslateRequest,
    ActorTranslateResponse,
    ActorUpdateRequest,
    ActorUserTagsRequest,
    CropAvatarRequest,
    TaskResponse,
    UserTagLinksResponse,
    UserTagResponse,
)
from ..models.actors import normalize_actor_locks
from .saved_queries import resolve_saved_query_id_subquery

logger = structlog.get_logger()

router = APIRouter(prefix="/actors", tags=["actors"])

_MAX_AVATAR_BYTES = 20 * 1024 * 1024
_MAX_AVATAR_EDGE = 2400


def _from_browse(item: ActorBrowseItem) -> ActorResponse:
    """列表只填卡片/表格字段; 简介/别名/标签/源字典见详情."""
    return ActorResponse(
        id=item.id,
        name=item.name,
        count=item.count,
        gender=item.gender,
        birthday=item.birthday,
        birthplace=item.birthplace,
        height=item.height,
        bust=item.bust,
        waist=item.waist,
        hip=item.hip,
        cup=item.cup,
        image_urls=list(item.image_urls),
        updated_at=item.updated_at,
    )


def _from_actor(
    actor: Actor,
    *,
    count: int,
    aliases: list[str] | None = None,
    user_tags: list[UserTagResponse] | None = None,
    include_raw: bool = False,
) -> ActorResponse:
    assert actor.id is not None
    return ActorResponse(
        id=actor.id,
        name=actor.name,
        count=count,
        aliases=list(aliases or []),
        user_tags=list(user_tags or []),
        gender=actor.gender,
        birthday=actor.birthday,
        birthplace=actor.birthplace,
        height=actor.height,
        bust=actor.bust,
        waist=actor.waist,
        hip=actor.hip,
        cup=actor.cup,
        overview=actor.overview,
        tagline=actor.tagline,
        image_urls=list(actor.image_urls or []),
        provider_ids=dict(actor.provider_ids or {}),
        source_urls=dict(actor.source_urls or {}),
        field_sources=dict(actor.field_sources or {}),
        raw=dict(actor.raw or {}) if include_raw else {},
        locked_fields=normalize_actor_locks(actor.locked_fields),
        updated_at=actor.updated_at,
    )


async def _actor_user_tags(repo: RepoDep, actor_id: int) -> list[UserTagResponse]:
    return [to_resp(UserTagResponse, tag) for tag in await repo.list_actor_user_tags(actor_id)]


async def _detail_response(repo: RepoDep, actor: Actor) -> ActorResponse:
    """详情全量响应: count / 别名 / 用户标签 / raw 均补齐."""
    assert actor.id is not None
    item = await repo.get_facet(FacetKind.ACTOR, actor.id)
    count = item.count if item is not None else 0
    aliases = await repo.get_actor_aliases(actor.id)
    user_tags = await _actor_user_tags(repo, actor.id)
    return _from_actor(actor, count=count, aliases=aliases, user_tags=user_tags, include_raw=True)


@router.get("")
async def list_actors(repo: RepoDep, params: Annotated[ActorBrowseParams, Query()]) -> ActorListResponse:
    id_subquery_sql = None
    if params.saved_query_id is not None:
        id_subquery_sql = await resolve_saved_query_id_subquery(repo, params.saved_query_id, SavedQueryEntity.ACTOR)
    items, total = await repo.browse_actors(params, id_subquery_sql=id_subquery_sql)
    return ActorListResponse(items=[_from_browse(i) for i in items], total=total)


@router.post("/batch/user-tags")
async def batch_actor_user_tags(req: ActorUserTagsRequest, repo: RepoDep) -> UserTagLinksResponse:
    """把一组用户标签应用到一组演员; 未知标签 id 返回 404, 不存在的演员 id 计入 missing."""
    try:
        result = await repo.apply_actor_user_tags(req.ids, req.user_tag_ids, action=req.action)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    logger.info(
        "actor user tags applied",
        action=req.action,
        changed=result.changed,
        unchanged=result.unchanged,
        missing=result.missing,
    )
    return UserTagLinksResponse(changed=result.changed, unchanged=result.unchanged, missing=result.missing)


@router.post("/batch/gender")
async def batch_actor_gender(req: ActorGenderBatchRequest, repo: RepoDep) -> ActorGenderBatchResponse:
    """批量写入性别; 写入字段与单条 PATCH 一样自动加锁, 不存在的演员 id 计入 missing."""
    updated, missing = await repo.batch_set_actor_gender(req.ids, req.gender)
    logger.info("actor gender batch updated", updated=updated, missing=missing)
    return ActorGenderBatchResponse(updated=updated, missing=missing)


@router.post("/batch/clear-person")
async def batch_clear_actor_person(req: ActorClearPersonBatchRequest, repo: RepoDep) -> ActorClearPersonBatchResponse:
    """批量清空人物档案并解除全部锁 (保留 ``name`` / ``gender``); 不存在的演员 id 计入 missing."""
    cleared, missing = await repo.batch_clear_actor_person(req.ids)
    logger.info("actor person batch cleared", cleared=cleared, missing=missing)
    return ActorClearPersonBatchResponse(cleared=cleared, missing=missing)


@router.get("/{actor_id}")
async def get_actor(actor_id: int, repo: RepoDep) -> ActorResponse:
    item = await repo.get_facet(FacetKind.ACTOR, actor_id)
    actor = await repo.get_actor(actor_id)
    if item is None or actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    return await _detail_response(repo, actor)


@router.patch("/{actor_id}")
async def update_actor(actor_id: int, req: ActorUpdateRequest, repo: RepoDep) -> ActorResponse:
    """更新演员规范人物字段 (不含 name/raw/field_sources); 写入字段自动加锁."""
    updates = cast("ActorPersonFields", req.model_dump(exclude_unset=True))
    if not updates:
        raise HTTPException(status_code=422, detail="没有需要修改的字段")
    if "birthday" in updates:
        raw_bday = updates["birthday"]
        if raw_bday is None or (isinstance(raw_bday, str) and not raw_bday.strip()):
            updates["birthday"] = None
        elif isinstance(raw_bday, str):
            normalized = normalize_calendar_date(raw_bday)
            if normalized is None:
                raise HTTPException(status_code=422, detail="生日必须为 YYYY-MM-DD")
            updates["birthday"] = normalized
        else:
            raise HTTPException(status_code=422, detail="生日必须为 YYYY-MM-DD")
    actor = await repo.update_actor(actor_id, mode=WriteMode.MANUAL, **updates)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    return await _detail_response(repo, actor)


@router.put("/{actor_id}/locks")
async def set_actor_locks(actor_id: int, req: ActorLocksRequest, repo: RepoDep) -> ActorResponse:
    """整体替换锁定字段集合."""
    actor = await repo.set_actor_locks(actor_id, req.fields)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    logger.info("actor locks updated", actor_id=actor_id, fields=[str(field) for field in req.fields])
    return await _detail_response(repo, actor)


@router.post("/{actor_id}/clear-person", deprecated=True)
async def clear_actor_person(actor_id: int, repo: RepoDep) -> ActorResponse:
    """已弃用: 改用 `POST /actors/batch/clear-person` 的单元素列表 (只回计数)."""
    actor = await repo.clear_actor_person(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    logger.info("actor person cleared", actor_id=actor_id)
    return await _detail_response(repo, actor)


@router.post("/{actor_id}/crop-avatar")
async def crop_actor_avatar(
    actor_id: int,
    req: CropAvatarRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> ActorResponse:
    """从当前主图 (image_urls[0]) 按像素框裁切头像, 结果前插为主图并保留原图.

    先按 EXIF 转正、再顺时针旋转 ``req.rotation``, 框相对旋转后的图像.
    """
    actor = await repo.get_actor(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    image_urls = list(actor.image_urls or [])
    if not image_urls:
        raise HTTPException(status_code=400, detail="无头像图可裁切")

    box = (req.left, req.top, req.right, req.bottom)
    try:
        cropped_url = await manual_crop_image(
            image_urls[0],
            box,
            runtime.resource_store,
            runtime.web_client,
            runtime.config.hot,
            runtime.config.cold.data_dir,
            subject="头像",
            rotation=req.rotation,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    updated = await repo.update_actor(
        actor_id, mode=WriteMode.MANUAL, image_urls=list(dict.fromkeys([cropped_url, *image_urls]))
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    logger.info("actor avatar cropped", actor_id=actor_id, box=box, rotation=req.rotation, image_url=cropped_url)
    return await _detail_response(repo, updated)


@router.post("/{actor_id}/scrape", status_code=202, deprecated=True)
async def scrape_actor(actor_id: int, repo: RepoDep, req: ActorScrapeRequest | None = None) -> TaskResponse:
    """已弃用: 改用 `POST /tasks/batch/submit` 的单元素 `actor_scrape` 提交."""
    actor = await repo.get_actor(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")
    body = req or ActorScrapeRequest()
    task = await repo.create_task(
        task_type=TaskType.ACTOR_SCRAPE, payload=ActorScrapePayload(actor_id=actor_id, use_cache=body.use_cache)
    )
    logger.info("actor scrape task created", actor_id=actor_id, task_id=task.id)
    return to_resp(TaskResponse, task)

@router.post("/{actor_id}/avatar")
async def upload_actor_avatar(
    actor_id: int,
    file: Annotated[UploadFile, File(description="本地头像图片")],
    repo: RepoDep,
    runtime: RuntimeDep,
) -> ActorAvatarUploadResponse:
    """上传本地图片作为演员头像, 同时写 .actors/ 图并改写关联影片 NFO.

    Amane 侧: 图片存入 ResourceStore, 作为 image_urls[0] 前插 (保留原图),
    以 WriteMode.MANUAL 写入并自动锁定 image_urls.
    磁盘侧: 每个关联影片目录写 ``.actors/<演员名>.jpg`` (Emby/Kodi 读本地演员头像的
    标准路径), 在 library.write_nfo 开启时重写 NFO; write_nfo() 会自动补
    ``<actor><thumb>``. 全程只写盘, 不调用 Emby API.
    """
    actor = await repo.get_actor(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(raw) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="图片超过 20MB")
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无法识别该图片") from exc
    image.thumbnail((_MAX_AVATAR_EDGE, _MAX_AVATAR_EDGE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92, optimize=True)
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]

    async def producer(dest: Path) -> bool:
        dest.write_bytes(data)
        return True

    resource = await runtime.resource_store.acquire_derived(
        f"actor-upload:{digest}", "poster", digest, producer, ext=".jpg"
    )
    if resource is None:
        raise HTTPException(status_code=500, detail="头像写入失败")
    url = f"{RESOURCE_URL_PREFIX}/{runtime.resource_store.url_hash(resource.url)}"

    old_urls = list(actor.image_urls or [])
    updated = await repo.update_actor(
        actor_id, mode=WriteMode.MANUAL, image_urls=list(dict.fromkeys([url, *old_urls]))
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="演员不存在")

    sync = await sync_actor_avatar(repo, updated, data)
    logger.info(
        "actor avatar uploaded",
        actor_id=actor_id,
        bytes=len(data),
        images=sync.images_written,
        nfos=sync.nfos_written,
    )
    return ActorAvatarUploadResponse(
        actor=await _detail_response(repo, updated),
        images_written=sync.images_written,
        nfos_written=sync.nfos_written,
        skipped=sync.skipped,
        errors=sync.errors,
    )


@router.post("/{actor_id}/translate")
async def translate_actor(
    actor_id: int,
    req: ActorTranslateRequest,
    repo: RepoDep,
    runtime: RuntimeDep,
) -> ActorTranslateResponse:
    """Lara 翻译演员简介/标语: 只返回译文不写库.

    与影片翻译一致: 前端预览后勾选才 PATCH 写回, WriteMode.MANUAL 自动锁定字段,
    避免一次点击覆盖原文, 也避免免费字符额度被误消耗.
    """
    actor = await repo.get_actor(actor_id)
    if actor is None:
        raise HTTPException(status_code=404, detail="演员不存在")

    config = runtime.config.hot.lara
    client = LaraClient(config)
    if not client.configured:
        raise HTTPException(
            status_code=400, detail="尚未配置 Lara Access Key, 请到「设置 → lara」填写"
        )

    target = (req.target or config.target_language or "zh-CN").strip()
    source = (req.source or config.source_language or "").strip() or None
    items: list[ActorTranslateItem] = []
    characters = 0
    detected: str | None = None
    for field in req.fields:
        text = getattr(actor, field, None)
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
        items.append(ActorTranslateItem(field=field, source=text, translation=translation))

    if not items:
        raise HTTPException(status_code=400, detail="简介为空, 没有可翻译的内容")
    logger.info(
        "actor translated",
        actor_id=actor_id,
        fields=[item.field for item in items],
        characters=characters,
    )
    return ActorTranslateResponse(
        target_language=target,
        source_language=detected,
        characters=characters,
        items=items,
    )
