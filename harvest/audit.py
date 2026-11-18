"""用来源实际留下的查询足迹核对本地覆盖结论。

重要边界：/__admin/log 是演练来源的操作员调试入口，采集主流程（client/harvester）
绝不调用。本模块只在人工执行 `harvest audit` 时使用，用来回答：
本地声称的每一次未截断覆盖，是否真的在来源上留下了对应的产品查询足迹。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

from .geometry import parse_boundary
from .store import Store

PRODUCT_PATHS = ("/boundary", "/catalog", "/permit/")


def fetch_source_log(base_url: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    """操作员审计专用：回查来源实际请求日志。"""
    url = base_url.rstrip("/") + "/__admin/log"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"来源日志接口返回 {resp.status}")
        return json.loads(resp.read().decode("utf-8"))


def _footprint_index(source_log: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for entry in source_log:
        # 来源日志记录的是 path?query；统一取路径+查询部分作为足迹键。
        key = urllib.parse.urlsplit(entry["path"]).path + (
            "?" + urllib.parse.urlsplit(entry["path"]).query
            if urllib.parse.urlsplit(entry["path"]).query else "")
        if not key.startswith(PRODUCT_PATHS):
            continue
        index.setdefault(key, []).append(entry)
    return index


def _request_key(full_url: str) -> str:
    parts = urllib.parse.urlsplit(full_url)
    return parts.path + (("?" + parts.query) if parts.query else "")


def audit_against_source(store: Store, source_log: list[dict[str, Any]]) -> dict[str, Any]:
    """把本地查询留痕与来源足迹逐条对账。"""
    footprint = _footprint_index(source_log)
    local_queries = store.list_queries()
    matched, missing = [], []
    for row in local_queries:
        key = _request_key(row["request_url"])
        seen = key in footprint
        (matched if seen else missing).append({
            "query_id": row["query_id"],
            "run_id": row["run_id"],
            "request_url": row["request_url"],
            "truncated": bool(row["truncated"]),
            "tile_bbox": [row["minx"], row["miny"], row["maxx"], row["maxy"]],
            "category": row["category"],
        })

    # 反向检查：来源收到的产品查询是否都在本地有记录（防止绕过采集库直连）。
    local_urls = {_request_key(row["request_url"]) for row in local_queries}
    orphan_footprints = sorted(
        path for path in footprint if path not in local_urls
    )

    revision = max(store.list_boundary_revisions())
    boundary_geojson = store.get_boundary(revision)
    boundary, _ = parse_boundary(boundary_geojson)

    # 覆盖瓦片的「足迹证据」：每张未截断瓦片必须至少有一条来源足迹与之对应。
    tile_evidence = []
    for tile in store.coverage_tiles(revision):
        evidence = footprint.get(_request_key(tile["request_url"]), [])
        tile_evidence.append({
            "tile_key": tile["tile_key"],
            "category": tile["category"],
            "truncated": False,
            "source_hits": len(evidence),
            "times": [e["time"] for e in evidence],
            "verified": bool(evidence),
        })

    gaps = store.list_gaps(revision=revision, status="open")
    return {
        "boundary_revision": revision,
        "rule": "覆盖结论只采信 truncated=false 且能在来源 /__admin/log 找到实际足迹的查询",
        "local_product_queries": len(local_queries),
        "source_product_footprints": sum(len(v) for v in footprint.values()),
        "matched_queries": len(matched),
        "queries_without_source_footprint": missing,
        "source_footprints_not_in_local_store": orphan_footprints,
        "coverage_tiles_verified": sum(1 for t in tile_evidence if t["verified"]),
        "coverage_tiles_missing_evidence": [t for t in tile_evidence if not t["verified"]],
        "open_gaps": [
            {"gap_id": g["gap_id"], "category": g["category"],
             "bbox": [g["minx"], g["miny"], g["maxx"], g["maxy"]], "reason": g["reason"]}
            for g in gaps
        ],
        "conclusion": (
            "覆盖结论与来源足迹一致"
            if not missing and all(t["verified"] for t in tile_evidence)
            else "存在无法用来源足迹证实的本地查询，覆盖结论不可信"
        ),
    }
