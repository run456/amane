"""发现视频同目录里"本来就有"的 Emby / Kodi 图片与预告片.

Amane 原本只从 ``Metadata.poster_urls`` / ``thumb_urls`` / ``trailer_urls`` 取封面与预告片
(这些是 NFO 里的外链或刮削结果), 从不读磁盘上已存在的图片文件. 但媒体服务器支持两种命名:

- 目录级 (一个文件夹一部片): ``poster.jpg`` / ``fanart.jpg`` / ``thumb.jpg`` / ``trailer.mp4``
- 影片名前缀 (同目录多部片): ``<片名>-poster.jpg`` / ``<片名>-fanart.jpg`` /
  ``<片名>-thumb.jpg`` / ``<片名>-trailer.mp4``

本模块一次 ``iterdir`` 建索引, 按优先级匹配两种命名, 让响应层可以注入同源 URL:
不写数据库、不需要重新扫描, 整理后文件挪了位置也自动跟随 (端点按当前 MediaFile.path 定位).
"""

from __future__ import annotations

from dataclasses import dataclass
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
    """一次目录扫描的结果: kind -> 磁盘路径."""

    files: dict[LocalArtifactKind, Path]
    directory: Path | None = None
    stems: tuple[str, ...] = ()

    def has(self, kind: LocalArtifactKind) -> bool:
        return kind in self.files


def _candidate_names(kind: LocalArtifactKind, stems: Sequence[str]) -> list[str]:
    """先目录级、后影片名前缀; 前缀名再补几个常见别名."""
    names = list(_BASE_NAMES[kind])
    for stem in stems:
        names.append(f"{stem}{_PREFIX_SUFFIX[kind]}")
        if kind is LocalArtifactKind.poster:
            names.append(stem)  # <片名>.jpg 也常作封面
        if kind is LocalArtifactKind.trailer:
            names.append(f"{stem}-preview")
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
    """
    directory = video_path.parent
    stems = tuple(_stems_for(video_path, number))
    try:
        entries = {p.name.lower(): p for p in directory.iterdir() if p.is_file()}
    except OSError:
        return LocalArtifacts(files={}, directory=directory, stems=stems)

    found: dict[LocalArtifactKind, Path] = {}
    for kind in LocalArtifactKind:
        exts = _VIDEO_EXTS if kind is LocalArtifactKind.trailer else _IMAGE_EXTS
        for name in _candidate_names(kind, stems):
            hit: Path | None = None
            for ext in exts:
                hit = entries.get((name + ext).lower())
                if hit is not None:
                    break
            if hit is not None:
                found[kind] = hit
                break
    return LocalArtifacts(files=found, directory=directory, stems=stems)


def artifact_url(metadata_id: int, kind: LocalArtifactKind) -> str:
    """同源产物 URL; 相对路径不经过图片代理, 浏览器直接请求本服务."""
    return f"/api/metadata/{metadata_id}/artifact?kind={kind.value}"


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
