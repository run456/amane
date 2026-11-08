"""发现视频同目录里"本来就有"的 Emby / Kodi 图片与预告片.

Amane 原本只从 ``Metadata.poster_urls`` / ``thumb_urls`` / ``trailer_urls`` 取封面与预告片
(这些是 NFO 里的外链或刮削结果), 从不读磁盘上已存在的图片文件. 但媒体服务器支持两种命名:

- 目录级 (一个文件夹一部片): ``poster.jpg`` / ``fanart.jpg`` / ``thumb.jpg`` / ``trailer.mp4``
- 影片名前缀 (同目录多部片): ``<片名>-poster.jpg`` / ``<片名>-fanart.jpg`` /
  ``<片名>-thumb.jpg`` / ``<片名>-trailer.mp4``
- 预告片子目录 (Emby / Jellyfin 官方约定): ``<影片目录>/trailers/trailer.mp4`` /
  ``<影片目录>/trailers/<片名>-trailer.mp4``. 同目录未命中时回退到此子目录,
  优先级低于同目录 (用户同目录已放 trailer.mp4 时不再读子目录).

本模块一次 ``iterdir`` 建索引, 按优先级匹配两种命名, 让响应层可以注入同源 URL:
不写数据库、不需要重新扫描, 整理后文件挪了位置也自动跟随 (端点按当前 MediaFile.path 定位).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class LocalArtifactKind(StrEnum):
    poster = "poster"
    thumb = "thumb"
    fanart = "fanart"
    trailer = "trailer"


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")

_PROXYABLE_SCHEMES = ("http://", "https://")
"""只有可代理的 http(s) 直链才改走服务端转发; HLS 播放列表交给前端 hls.js."""

# 目录级基准名, 按优先级排列.
_BASE_NAMES: dict[LocalArtifactKind, tuple[str, ...]] = {
    LocalArtifactKind.poster: ("poster", "folder", "cover", "front", "movie"),
    LocalArtifactKind.thumb: ("thumb", "landscape"),
    LocalArtifactKind.fanart: ("fanart", "backdrop", "background"),
    LocalArtifactKind.trailer: ("trailer",),
}
# <片名><后缀> 形式里的后缀.
_PREFIX_SUFFIX: dict[LocalArtifactKind, str] = {
    LocalArtifactKind.poster: "-poster",
    LocalArtifactKind.thumb: "-thumb",
    LocalArtifactKind.fanart: "-fanart",
    LocalArtifactKind.trailer: "-trailer",
}


@dataclass(frozen=True)
class LocalArtifacts:
    """一次目录扫描的结果: kind -> 磁盘路径.

    ``versions`` 记录命中文件的 ``mtime_ns``: 注入同源 URL 时作为 ``&v=`` 参数,
    用户替换 / 修改本地图片后 URL 随之变化, 前端 ``<img>`` 才会重新请求,
    否则固定 URL 会被浏览器与 React key 缓存, 改了盘上的图也看不到.
    """

    files: dict[LocalArtifactKind, Path]
    directory: Path | None = None
    stems: tuple[str, ...] = ()
    versions: dict[LocalArtifactKind, str] = field(default_factory=dict)

    def has(self, kind: LocalArtifactKind) -> bool:
        return kind in self.files

    def version(self, kind: LocalArtifactKind) -> str | None:
        return self.versions.get(kind)


def _candidate_names(kind: LocalArtifactKind, stems: Sequence[str]) -> list[str]:
    """先影片名前缀、后目录级.

    用户整理好的资料库普遍用 ``<片名>-poster.jpg`` 这类前缀名, 而 Amane 写盘 / 刮削
    只会产出目录级 ``poster.jpg`` / ``thumb.jpg`` / ``fanart.jpg``. 两者同时存在时以前缀名
    为准, 否则刮削图会顶掉用户原图 (v0.26.4 整理修复). 裸 ``<片名>.jpg`` 最容易误伤,
    放到最后兜底. 本函数同时被响应层与整理搬运复用, 保证「显示」与「复制」取同一张图.
    """
    names: list[str] = []
    for stem in stems:
        names.append(f"{stem}{_PREFIX_SUFFIX[kind]}")
        if kind is LocalArtifactKind.trailer:
            names.append(f"{stem}-preview")
    names.extend(_BASE_NAMES[kind])
    if kind is LocalArtifactKind.poster:
        names.extend(stems)  # <片名>.jpg 也常作封面
    return names


def _stems_for(video_path: Path, number: str | None) -> list[str]:
    """视频文件名 stem 优先, 番号与去掉站点前缀的番号作为补充候选."""
    stem = video_path.stem
    stems = [stem]
    if "@" in stem:
        tail = stem.rsplit("@", 1)[-1]
        if tail and tail.lower() != stem.lower():
            stems.append(tail)
    if number:
        normalized = number.strip()
        if normalized and all(normalized.lower() != s.lower() for s in stems):
            stems.append(normalized)
    return stems


def discover(video_path: Path, *, number: str | None = None) -> LocalArtifacts:
    """扫描视频所在目录一次, 返回命中的本地产物 (同步, 由调用方放进线程池).

    目录不可读时返回空结果, 不抛异常: 列表页不应因某个挂载点失效而整体报错.

    预告片额外支持 Emby / Jellyfin 官方的 ``<影片目录>/trailers/`` 子目录约定:
    同目录未命中时回退到该子目录里的 ``trailer.*`` / ``<片名>-trailer.*`` /
    ``<片名>-preview.*``. 同目录优先级高于子目录, 用户若同时放了两种也不重复.
    """
    directory = video_path.parent
    stems = tuple(_stems_for(video_path, number))
    try:
        entries = {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
    except OSError:
        return LocalArtifacts(files={}, directory=directory, stems=stems)

    # Emby / Jellyfin 本地预告片子目录: 仅在 trailer 类回退时使用, 所以单独建索引,
    # 既不污染图片类的同目录匹配, 也避免在每一轮循环里重复探测子目录是否存在.
    trailer_dir = directory / "trailers"
    trailer_entries = (
        {p.name.lower(): p for p in trailer_dir.iterdir() if p.is_file()}
        if trailer_dir.is_dir() else {}
    )

    found: dict[LocalArtifactKind, Path] = {}
    versions: dict[LocalArtifactKind, str] = {}
    for kind in LocalArtifactKind:
        exts = _VIDEO_EXTS if kind is LocalArtifactKind.trailer else _IMAGE_EXTS
        hit = _first_match(entries, kind, stems, exts)
        if hit is None and kind is LocalArtifactKind.trailer and trailer_entries:
            hit = _first_match(trailer_entries, kind, stems, exts)
        if hit is not None:
            found[kind] = hit
            try:
                # 与扫描同一线程内 stat, 避免在响应装配里阻塞事件循环.
                versions[kind] = str(hit.stat().st_mtime_ns)
            except OSError:
                pass
    return LocalArtifacts(files=found, directory=directory, stems=stems, versions=versions)


def _first_match(
    entries: dict[str, Path],
    kind: LocalArtifactKind,
    stems: Sequence[str],
    exts: Sequence[str],
) -> Path | None:
    """在给定索引里按候选名 + 扩展名匹配第一个命中."""
    for name in _candidate_names(kind, stems):
        for ext in exts:
            hit = entries.get((name + ext).lower())
            if hit is not None:
                return hit
    return None


def artifact_url(
    metadata_id: int, kind: LocalArtifactKind, version: str | None = None
) -> str:
    """同源产物 URL; 相对路径不经过图片代理, 浏览器直接请求本服务.

    ``version`` 取磁盘文件的 ``mtime_ns``: 文件被替换 / 修改后 URL 变化, 前端
    ``<img>`` 的 src / key 随之变化才会重新请求, 从而加载到最新文件. 文件没变时
    URL 保持稳定, 不会触发无谓重载. 端点忽略多余 query 参数, 无需改路由签名.
    """
    url = f"/api/metadata/{metadata_id}/artifact?kind={kind.value}"
    return f"{url}&v={version}" if version else url


def pick_proxyable_trailer(urls: Sequence[str]) -> str | None:
    """挑第一个可走服务端代理的远程直链.

    相对路径 / data: 不是外链, ``.m3u8`` 交给前端 hls.js 原有逻辑 (塞进 ``<video>`` 会直接失败).
    响应层与 ``/artifact`` 端点共用本函数, 保证「注入了 URL」与「端点肯代理」始终一致.
    """
    for url in urls:
        if not url or not url.startswith(_PROXYABLE_SCHEMES):
            continue
        path = url.split("?", 1)[0].split("#", 1)[0].casefold()
        if path.endswith(".m3u8"):
            continue
        return url
    return None
