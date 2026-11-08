"""Lara Translate 专用 REST 客户端.

协议依据官方 SDK ``translated/lara-python`` 1.17.2 的实现:

- ``POST /v2/auth``: 用 Access Key 做 HMAC-SHA256 challenge 换取 JWT
- ``POST /v2/translate``: 文本字段名为 ``q``, 响应是逐行 NDJSON, 最后一条含完整译文

SDK 本身依赖 ``requests`` (同步), 这里改用项目已有的 ``httpx2`` 异步实现,
既避免在事件循环里阻塞, 也不必为一次翻译引入新依赖 (改 ``uv.lock`` 会让镜像构建失败).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

import httpx2 as httpx

from ..config.manager import LaraConfig

SDK_NAME = "amane"
SDK_VERSION = "0.26.11"
_AUTH_PATH = "/v2/auth"
_TRANSLATE_PATH = "/v2/translate"
_MAX_DETAIL = 200

# token 按 (base_url, key id, secret) 缓存; 401 时清掉重取.
_TOKENS: dict[str, str] = {}


class LaraError(Exception):
    """Lara 调用失败; 消息可直接作为 API detail 返回."""


def _cache_key(config: LaraConfig) -> str:
    raw = f"{config.base_url}|{config.access_key_id}|{config.access_key_secret}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _challenge(
    secret: str, method: str, path: str, content_md5: str, content_type: str, date: str
) -> str:
    payload = f"{method}\n{path}\n{content_md5}\n{content_type}\n{date}"
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _error_detail(body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:_MAX_DETAIL]
    if isinstance(parsed, dict):
        for key in ("message", "error", "detail"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value[:_MAX_DETAIL]
    return text[:_MAX_DETAIL]


class LaraClient:
    """每次翻译一个实例; 构造开销可忽略, JWT 走模块级缓存."""

    def __init__(self, config: LaraConfig, *, timeout_s: float = 60.0) -> None:
        self._config = config
        self._timeout = httpx.Timeout(timeout_s)

    @property
    def configured(self) -> bool:
        return bool(self._config.access_key_id.strip() and self._config.access_key_secret.strip())

    async def translate(
        self,
        text: str,
        *,
        target: str | None = None,
        source: str | None = None,
    ) -> tuple[str, str | None]:
        """返回 (译文, 检测到的源语言). ``source`` 为空则交由 Lara 自动检测."""
        if not self.configured:
            raise LaraError("尚未配置 Lara Access Key")
        body: dict[str, object] = {
            "q": text,
            "target": target or self._config.target_language or "zh-CN",
        }
        if source:
            body["source"] = source
        style = self._config.style.strip()
        if style:
            body["style"] = style
        payload = await self._post_json(_TRANSLATE_PATH, body, retry_on_401=True)
        translation = payload.get("translation")
        if not isinstance(translation, str):
            raise LaraError("Lara 未返回译文")
        detected = payload.get("source_language")
        return translation, detected if isinstance(detected, str) else None

    async def _post_json(
        self, path: str, body: dict[str, object], *, retry_on_401: bool = False
    ) -> dict:
        url = f"{self._config.base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {await self._token()}"}
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            response = await client.post(url, headers=headers, json=body)
            if response.status_code == 401 and retry_on_401:
                _TOKENS.pop(_cache_key(self._config), None)
                headers["Authorization"] = f"Bearer {await self._token()}"
                response = await client.post(url, headers=headers, json=body)
        if response.status_code >= 400:
            raise LaraError(f"Lara {response.status_code}: {_error_detail(response.content)}")
        return self._last_json(response.content)

    @staticmethod
    def _last_json(raw: bytes) -> dict:
        """``/v2/translate`` 逐行返回 NDJSON; 取最后一条带 translation 的记录."""
        result: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict) and parsed.get("translation") is not None:
                result = parsed
        if not result:
            raise LaraError("Lara 返回内容无法解析")
        return result

    async def _token(self) -> str:
        key = _cache_key(self._config)
        cached = _TOKENS.get(key)
        if cached:
            return cached

        date = datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT")
        content_type = "application/json"
        # 签名基于字节: 必须紧凑 JSON 且不转义非 ASCII, 否则 challenge 对不上.
        body_bytes = json.dumps(
            {"id": self._config.access_key_id}, ensure_ascii=False, separators=(",", ":")
        ).encode()
        content_md5 = base64.b64encode(hashlib.md5(body_bytes).digest()).decode()
        challenge = _challenge(
            self._config.access_key_secret, "POST", _AUTH_PATH, content_md5, content_type, date
        )
        headers = {
            "Authorization": f"Lara:{challenge}",
            "X-Lara-Date": date,
            "Content-Type": content_type,
            "Content-MD5": content_md5,
            "X-Lara-SDK-Name": SDK_NAME,
            "X-Lara-SDK-Version": SDK_VERSION,
        }
        url = f"{self._config.base_url.rstrip('/')}{_AUTH_PATH}"
        async with httpx.AsyncClient(timeout=self._timeout, trust_env=False) as client:
            response = await client.post(url, headers=headers, content=body_bytes)
        if response.status_code >= 400:
            raise LaraError(
                f"Lara 认证失败 {response.status_code}: {_error_detail(response.content)}"
            )
        try:
            token = response.json().get("token")
        except ValueError as exc:
            raise LaraError("Lara 认证响应无法解析") from exc
        if not isinstance(token, str) or not token:
            raise LaraError("Lara 认证未返回 token")
        _TOKENS[key] = token
        return token
