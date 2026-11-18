"""可解释交付：市域覆盖、设施/地块归属、许可状态。

交付物（写入运行目录）：
- report.json：机器可读的完整结论；
- report.md：资料员可读的中文说明，明确哪些空间已有未截断来源响应、
  哪些区域因来源限制仍无法确认（缺口），而不是只看去重后的总数；
- coverage.geojson：覆盖几何（按类别通道的已覆盖/缺口/地块相交足迹）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shapely.geometry import shape
from shapely.ops import unary_union

from .geometry import (
    bbox_polygon,
    boundary_area,
    clip_to_boundary,
    parse_boundary,
    to_multipolygon,
)
from .harvester import build_facilities_view
from .store import Store

AREA_EPSILON = 1e-12


def _feature(geom, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "properties": properties,
            "geometry": geom.__geo_interface__ if geom is not None else None}


class Reporter:
    def __init__(self, store: Store):
        self.store = store

    def build(self, output_dir: str | Path) -> dict[str, Any]:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        revision = max(self.store.list_boundary_revisions())
        boundary_geojson = self.store.get_boundary(revision)
        boundary, _ = parse_boundary(boundary_geojson)
        total_area = boundary_area(boundary)

        tiles = self.store.coverage_tiles(revision)
        # 按类别通道分组：category 为 NULL 是全量通道，其余是单类别通道。
        channels: dict[str, list] = {}
        for row in tiles:
            channels.setdefault(row["category"] or "*", []).append(row)

        channel_report = {}
        for channel, rows in sorted(channels.items()):
            bboxes = [(r["minx"], r["miny"], r["maxx"], r["maxy"]) for r in rows]
            covered = clip_to_boundary(bboxes, boundary)
            covered_area = float(covered.area) if covered is not None else 0.0
            uncovered = boundary.difference(covered) if covered is not None else boundary
            channel_report[channel] = {
                "tile_count": len(rows),
                "empty_tiles": sum(1 for r in rows if r["item_count"] == 0),
                "covered_area": round(covered_area, 12),
                "coverage_fraction": round(covered_area / total_area, 6) if total_area else None,
                "uncovered_area": round(float(uncovered.area), 12),
                "complete": bool(uncovered.is_empty or uncovered.area <= AREA_EPSILON),
            }

        # 缺口（当前分代、仍开放）。
        gaps = self.store.list_gaps(revision=revision, status="open")
        gap_geoms, gap_items = [], []
        for gap in gaps:
            poly = bbox_polygon((gap["minx"], gap["miny"], gap["maxx"], gap["maxy"])).intersection(boundary)
            if not poly.is_empty:
                gap_geoms.append(poly)
            gap_items.append({
                "gap_id": gap["gap_id"], "category": gap["category"], "reason": gap["reason"],
                "bbox": [gap["minx"], gap["miny"], gap["maxx"], gap["maxy"]],
                "opened_at": gap["opened_at"], "run_id": gap["run_id"],
            })
        gap_union = unary_union(gap_geoms) if gap_geoms else None

        declared_categories = self._declared_categories()
        # 整体覆盖结论：来源声明的每个类别通道都未截断覆盖，且没有开放缺口。
        per_category = {c: channel_report.get(c) for c in declared_categories}
        categories_complete = all(
            (per_category.get(c) or {}).get("complete", False) for c in declared_categories
        )
        overall_complete = categories_complete and not gaps

        facilities = build_facilities_view(self.store, revision)

        report = {
            "boundary_revision": revision,
            "boundary_area": round(total_area, 12),
            "declared_categories": declared_categories,
            "coverage": {
                "overall_complete": overall_complete,
                "rule": "覆盖仅由 truncated=false 的查询矩形裁剪到市域多边形构成；空结果查询同等计为覆盖",
                "channels": channel_report,
                "per_category_complete": {c: (per_category.get(c) or {}).get("complete", False)
                                          for c in declared_categories},
            },
            "truncated_queries_kept_as_footprints_only": [
                {"query_id": r["query_id"], "bbox": [r["minx"], r["miny"], r["maxx"], r["maxy"]],
                 "category": r["category"], "request_url": r["request_url"]}
                for r in self.store.truncated_queries(revision)
            ],
            "gaps": gap_items,
            "facilities": sorted(facilities.values(), key=lambda x: x["facility"]),
            "permits": [dict(r) for r in self.store.list_permits()],
            "parcels_out_of_scope": self._out_of_scope(revision),
            "runs": [dict(r) for r in self.store.conn.execute(
                "SELECT run_id,started_at,finished_at,status,note FROM runs ORDER BY started_at")],
        }
        (output_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_geojson(output_dir, boundary, channels, gap_union, revision, facilities)
        self._write_markdown(output_dir, report)
        return report

    def _declared_categories(self) -> list[str]:
        row = self.store.conn.execute(
            "SELECT raw_response FROM catalog_queries ORDER BY query_id LIMIT 1").fetchone()
        if row is None:
            return []
        return list(json.loads(row["raw_response"]).get("categories", []))

    def _out_of_scope(self, revision: int) -> list[dict[str, Any]]:
        scopes = {r["parcel"]: r for r in self.store.list_scope_eval(revision)}
        result = []
        for row in self.store.list_parcels():
            scope = scopes.get(row["parcel"])
            if scope is not None and not scope["in_scope"]:
                result.append({"parcel": row["parcel"], "facility": row["facility"],
                               "category": row["category"],
                               "reason": "地块真实几何与当前市域多边形不相交（含接触判定），仅保留来源观测"})
        return result

    def _write_geojson(self, output_dir, boundary, channels, gap_union, revision, facilities) -> None:
        features = [_feature(boundary, {"kind": "boundary", "revision": revision})]
        for channel, rows in sorted(channels.items()):
            bboxes = [(r["minx"], r["miny"], r["maxx"], r["maxy"]) for r in rows]
            covered = clip_to_boundary(bboxes, boundary)
            if covered is not None:
                features.append(_feature(to_multipolygon(covered),
                                         {"kind": "covered", "channel": channel}))
        if gap_union is not None:
            features.append(_feature(to_multipolygon(gap_union), {"kind": "gap"}))
        for entry in facilities.values():
            for parcel in entry["parcels"]:
                from shapely.geometry import shape
                features.append(_feature(shape(parcel["intersection"]), {
                    "kind": "parcel_intersection", "facility": entry["facility"],
                    "parcel": parcel["parcel"], "category": entry["category"],
                }))
        collection = {"type": "FeatureCollection", "features": features}
        (output_dir / "coverage.geojson").write_text(
            json.dumps(collection, ensure_ascii=False), encoding="utf-8")

    def _write_markdown(self, output_dir: Path, report: dict[str, Any]) -> None:
        lines = [
            "# 低碳设施采集覆盖报告",
            "",
            f"- 市域边界分代 revision：{report['boundary_revision']}",
            f"- 市域面积（坐标单位平方）：{report['boundary_area']}",
            f"- 整体覆盖结论：{'**已完整覆盖（无截断、无缺口）**' if report['coverage']['overall_complete'] else '**存在未确认区域，见缺口**'}",
            "- 覆盖规则：仅当某空间范围取得 truncated=false 的来源响应才算已观察；",
            "  空列表的未截断响应同样证明该范围无此类设施。截断响应只留查询足迹，不计覆盖。",
            "",
            "## 分类别覆盖",
            "",
            "| 类别通道 | 未截断查询数 | 其中空结果 | 已覆盖面积 | 覆盖比例 | 是否完整 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
        for channel, info in report["coverage"]["channels"].items():
            lines.append(
                f"| {channel} | {info['tile_count']} | {info['empty_tiles']} | "
                f"{info['covered_area']} | {info['coverage_fraction']} | "
                f"{'是' if info['complete'] else '否'} |")
        lines += ["", "## 缺口（来源限制下仍无法确认的范围，不是空区）", ""]
        if report["gaps"]:
            lines.append("| gap | 类别 | 范围 bbox | 原因 |")
            lines.append("| --- | --- | --- | --- |")
            for gap in report["gaps"]:
                lines.append(
                    f"| {gap['gap_id']} | {gap['category'] or '全量'} | {gap['bbox']} | {gap['reason']} |")
        else:
            lines.append("当前分代无开放缺口。")
        lines += ["", "## 市域内设施与地块归属", ""]
        if report["facilities"]:
            lines.append("| 设施 | 类别 | 相交地块 | 地块数 | 许可状态 | 身份冲突 |")
            lines.append("| --- | --- | --- | ---: | --- | --- |")
            for entry in report["facilities"]:
                lines.append(
                    f"| {entry['facility']} | {entry['category']} | "
                    f"{', '.join(p['parcel'] for p in entry['parcels'])} | "
                    f"{len(entry['parcels'])} | {entry.get('permit_status', '未登记')} | "
                    f"{'是' if entry['conflict'] else '否'} |")
        else:
            lines.append("当前分代未确认任何市域内设施。")
        lines += ["", "## 许可原件", ""]
        if report["permits"]:
            lines.append("| 设施 | 状态 | 原件/隔离路径 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for p in report["permits"]:
                path = p["stored_path"] or p["quarantine_path"] or ""
                lines.append(f"| {p['facility']} | {p['status']} | {path} | {p['reason'] or ''} |")
        else:
            lines.append("尚无许可登记。")
        if report["parcels_out_of_scope"]:
            lines += ["", "## 界外观测（保留但不计入收集范围）", ""]
            for item in report["parcels_out_of_scope"]:
                lines.append(f"- {item['parcel']}（{item['facility']} / {item['category']}）：{item['reason']}")
        lines.append("")
        (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
