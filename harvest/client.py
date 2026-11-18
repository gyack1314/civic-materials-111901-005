"""来源 HTTP 客户端。

只访问 docs/source-contract.md 声明的产品入口：
    GET /boundary、GET /catalog、GET /permit/<code>
/__admin/* 属于演练控制，产品采集流程绝不调用；审计需要时由 harvest.audit 单独使用。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


class SourceError(RuntimeError):
    """来源返回了无法继续处理的响应。"""


@dataclass
class CatalogResponse:
    """一次 /catalog 调用的完整可追溯结果。"""

    payload: dict[str, Any]
    revision: int
    axis: str
    crs: str
    categories: list[str]
    truncated: bool
    items: list[dict[str, Any]]
    raw_text: str
    request_url: str


class SourceClient:
    def __init__(
        self,
        base_url: str,
        *,
        api: str = "xy",
        timeout: float = 15.0,
        retries: int = 3,
        backoff: float = 0.5,
        opener: Any | None = None,
    ):
        if api not in ("xy", "yx"):
            raise ValueError("api 只接受契约定义的 'xy' 或 'yx'")
        self.base_url = base_url.rstrip("/")
        self.api = api
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        # opener 可注入（测试用）；默认走真实 urllib。
        self._opener = opener or urllib.request.build_opener()

    def _get(self, url: str) -> tuple[int, bytes, dict[str, str]]:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"Accept": "application/json,application/pdf"})
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
            except urllib.error.HTTPError as exc:
                # 4xx（除 429）是请求本身的问题，重试没有意义。
                if exc.code == 429 or exc.code >= 500:
                    last_exc = exc
                else:
                    raise SourceError(f"来源返回 HTTP {exc.code}：{url}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
            time.sleep(self.backoff * (2**attempt))
        raise SourceError(f"来源多次不可用，放弃请求：{url}；最后错误：{last_exc!r}")

    def get_boundary(self) -> dict[str, Any]:
        status, body, _ = self._get(f"{self.base_url}/boundary")
        if status != 200:
            raise SourceError(f"/boundary 状态码 {status}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError("/boundary 不是合法 GeoJSON") from exc

    def get_catalog(
        self, bbox: tuple[float, float, float, float], category: str | None = None
    ) -> CatalogResponse:
        # bbox 输入始终按经纬度（lon,lat），与 api 版本无关。
        query = {"bbox": ",".join(str(v) for v in bbox), "api": self.api}
        if category:
            query["category"] = category
        url = f"{self.base_url}/catalog?{urllib.parse.urlencode(query)}"
        status, body, _ = self._get(url)
        if status != 200:
            raise SourceError(f"/catalog 状态码 {status}")
        raw_text = body.decode("utf-8")
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"/catalog 不是合法 JSON：{url}") from exc
        for field in ("revision", "axis", "crs", "categories", "truncated", "items"):
            if field not in payload:
                raise SourceError(f"/catalog 响应缺少契约字段 {field!r}：{url}")
        if payload["axis"] not in ("lon,lat", "lat,lon"):
            raise SourceError(f"未知 axis 声明 {payload['axis']!r}，拒绝猜测坐标顺序")
        return CatalogResponse(
            payload=payload,
            revision=payload["revision"],
            axis=payload["axis"],
            crs=payload["crs"],
            categories=list(payload["categories"]),
            truncated=bool(payload["truncated"]),
            items=list(payload["items"]),
            raw_text=raw_text,
            request_url=url,
        )

    def get_permit(self, permit_url_or_path: str) -> tuple[bytes, str]:
        """下载许可原件，返回 (字节内容, 实际请求 URL)。

        契约中的 permit 是相对路径（/permit/F-A），按来源基址拼接；
        完整 URL 也允许，但其 host 必须与来源一致，避免被字段带离授权来源。
        """
        parsed = urllib.parse.urlsplit(permit_url_or_path)
        if parsed.scheme:
            if urllib.parse.urlsplit(self.base_url).netloc != parsed.netloc:
                raise SourceError(f"许可地址指向非授权来源主机：{permit_url_or_path}")
            url = permit_url_or_path
        else:
            path = permit_url_or_path if permit_url_or_path.startswith("/") else f"/{permit_url_or_path}"
            url = f"{self.base_url}{path}"
        status, body, headers = self._get(url)
        if status != 200:
            raise SourceError(f"许可下载状态码 {status}：{url}")
        content_type = headers.get("content-type", "")
        if "pdf" not in content_type.lower() and not body.startswith(b"%PDF-"):
            raise SourceError(f"许可响应不是 PDF（Content-Type={content_type!r}）：{url}")
        return body, url
