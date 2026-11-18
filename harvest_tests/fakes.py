"""采集端测试辅助：可注入的假来源客户端与临时环境。

不接触 /__admin 之外的演练控制；FakeClient 实现与 SourceClient 相同的产品方法签名，
用于构造真实来源中不存在的极端情形（持续截断、坐标版本、空范围）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harvest.client import CatalogResponse  # noqa: E402

DATA = ROOT / "data"


def boundary_geojson(phase: int = 0) -> dict[str, Any]:
    name = "boundary2.geojson" if phase else "boundary.geojson"
    return json.loads((DATA / name).read_text())


def parcel_rows() -> list[dict[str, Any]]:
    return json.loads((DATA / "parcels.geojson").read_text())["features"]


class FakeSourceClient:
    """与 SourceClient 同构的内存来源，行为按契约模拟。"""

    def __init__(
        self,
        *,
        phase: int = 0,
        api: str = "xy",
        limit: int = 2,
        force_truncated: bool = False,
        boundary: dict[str, Any] | None = None,
    ):
        self.api = api
        self.base_url = "http://fake-source"
        self.phase = phase
        self.limit = limit
        self.force_truncated = force_truncated
        self._boundary = boundary or boundary_geojson(phase)
        self.calls: list[tuple[str, tuple, str | None]] = []
        self.permit_overrides: dict[str, bytes] = {}

    # --- 产品入口 ---
    def get_boundary(self) -> dict[str, Any]:
        self.calls.append(("boundary", (), None))
        return json.loads(json.dumps(self._boundary))

    def get_catalog(self, bbox, category=None) -> CatalogResponse:
        from shapely.geometry import box, shape

        self.calls.append(("catalog", tuple(bbox), category))
        region = box(*[float(v) for v in bbox])
        rows = [
            f for f in parcel_rows()
            if shape(f["geometry"]).intersects(region)
            and (not category or f["properties"]["category"] == category)
        ]
        truncated = self.force_truncated or len(rows) > self.limit
        items = json.loads(json.dumps(rows[: self.limit]))
        if self.api == "yx":
            for item in items:
                item["geometry"]["coordinates"] = [
                    [[b, a] for a, b in ring] for ring in item["geometry"]["coordinates"]
                ]
        payload = {
            "revision": self.phase,
            "axis": "lat,lon" if self.api == "yx" else "lon,lat",
            "crs": "EPSG:4326",
            "categories": ["solar", "heat", "storage"],
            "truncated": truncated,
            "items": items,
        }
        return CatalogResponse(
            payload=payload, revision=self.phase, axis=payload["axis"], crs="EPSG:4326",
            categories=payload["categories"], truncated=truncated, items=items,
            raw_text=json.dumps(payload, ensure_ascii=False),
            request_url=f"http://fake-source/catalog?bbox={','.join(map(str, bbox))}"
                        f"&api={self.api}" + (f"&category={category}" if category else ""),
        )

    def get_permit(self, permit_url_or_path: str) -> tuple[bytes, str]:
        key = permit_url_or_path.rsplit("/", 1)[-1]
        self.calls.append(("permit", (key,), None))
        if key in self.permit_overrides:
            return self.permit_overrides[key], f"http://fake-source/permit/{key}"
        actual = "F-WRONG" if self.phase == 1 and key == "F-A" else key
        return (DATA / f"{actual}.pdf").read_bytes(), f"http://fake-source/permit/{actual}"
