"""限量目录的可解释采集主流程。

覆盖语义（可向资料员解释的核心规则）：
1. 收集范围是市域多边形与地块真实几何的相交；bbox 只是向来源定位的工具，
   矩形超出市域的部分在交付时裁剪掉。
2. 只有 truncated=false 的响应才证明该矩形+该类别通道「已完整观察」；
   空列表的未截断响应同等证明该范围无该类设施，必须保留。
3. truncated=true 只说明「还有结果」，不产生覆盖。处置顺序：
   a. 先用契约允许的 category 附加筛选逐类查询（类别互斥，逐类成功即逐类覆盖）；
   b. 某类别仍截断时，把矩形沿长边二分后递归；
   c. 矩形已小到最小跨度、达到深度上限或任务预算仍截断的，登记为「缺口」——
      既不无限分割，也绝不当作空区。
4. 来源 revision（响应中随边界分代变化）决定查询属于哪一代覆盖；换界后旧代
   留档可查，不参与当前覆盖，需要按新边界重新取得未截断响应。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .client import CatalogResponse, SourceClient, SourceError
from .geometry import (
    DEFAULT_TASK_BUDGET,
    MAX_DEPTH,
    MIN_SPAN_RATIO,
    Tile,
    geometry_intersects_boundary,
    intersection_footprint,
    min_span,
    parse_boundary,
    round_bbox,
    split_bbox,
)
from .store import Store


@dataclass
class HarvestStats:
    queries: int = 0
    truncated: int = 0
    empty_complete: int = 0
    parcels_seen: int = 0
    gaps: int = 0
    reused: int = 0
    errors: int = 0


class HarvestInterrupted(RuntimeError):
    """运行被终止（任务预算耗尽或外部中断），已完成的观测全部保留。"""


class Harvester:
    def __init__(
        self,
        client: SourceClient,
        store: Store,
        *,
        task_budget: int = DEFAULT_TASK_BUDGET,
        output_dir: str | None = None,
    ):
        self.client = client
        self.store = store
        self.task_budget = task_budget
        self.output_dir = output_dir
        self.stats = HarvestStats()

    # ---------- 边界 ----------
    def fetch_boundary(self) -> tuple[int, object]:
        """取得当前市域边界并落库；返回 (revision, 边界多边形)。"""
        geojson = self.client.get_boundary()
        boundary, revision = parse_boundary(geojson)
        if revision is None:
            raise SourceError("/boundary 未声明 revision，无法建立覆盖分代")
        self.store.save_boundary(revision, geojson)
        return int(revision), boundary

    # ---------- 单次查询的统一处理 ----------
    def _issue_query(self, run_id: str, tile: Tile, depth: int, revision: int) -> CatalogResponse:
        # 续跑/重跑：同一瓦片（矩形+通道）已有成功响应则直接复用，来源足迹不重复。
        existing = self.store.completed_query_for_tile(tile.key)
        if existing is not None and existing["declared_revision"] == revision:
            self.stats.reused += 1
            cached_payload = json.loads(existing["raw_response"])
            return CatalogResponse(
                payload=cached_payload,
                revision=existing["declared_revision"],
                axis=existing["axis"],
                crs=existing["crs"],
                categories=list(cached_payload.get("categories", [])),
                truncated=bool(existing["truncated"]),
                items=list(cached_payload.get("items", [])),
                raw_text=existing["raw_response"],
                request_url=existing["request_url"],
            )

        response = self.client.get_catalog(tile.bbox, tile.category)
        self.stats.queries += 1
        if response.revision != revision:
            # 采集中途来源换界/修订：本次响应不能算在当前分代上，直接报错保留现场。
            raise SourceError(
                f"查询 {tile.key} 返回 revision={response.revision}，"
                f"与运行开始时的 {revision} 不一致；请用新边界重新发起运行"
            )
        query_id = self.store.record_query(
            run_id, tile, response.request_url, response.axis, response.crs,
            response.revision, response.truncated, len(response.items), response.raw_text,
        )
        if not response.truncated:
            # 未截断响应（含空结果）成为覆盖瓦片，关闭同范围或被其包含的历史缺口。
            self.store.resolve_gaps_for_tile(tile.bbox, tile.category, query_id)
            self.store.resolve_contained_gaps(revision, tile.bbox, tile.category, query_id)
            if not response.items:
                self.stats.empty_complete += 1
        else:
            self.stats.truncated += 1

        # 轴序归一化后落地块观测；原始响应已按来源原文保存，可回查。
        from .geometry import normalize_feature

        for order, raw_feature in enumerate(response.items):
            feature = normalize_feature(raw_feature, response.axis)
            props = feature.get("properties") or {}
            for field in ("parcel", "facility", "category", "permit"):
                if field not in props:
                    raise SourceError(f"目录 Feature 缺少契约属性 {field!r}：{response.request_url}")
            self.store.upsert_parcel(props, feature["geometry"], query_id, order)
            self.stats.parcels_seen += 1
        return response

    # ---------- 主循环 ----------
    def run(self, run_id: str | None = None) -> tuple[str, HarvestStats]:
        revision, boundary = self.fetch_boundary()
        root_bbox = round_bbox(*boundary.bounds)
        floor = min_span(root_bbox)

        existing_run = self.store.latest_open_run(self.client.base_url, self.client.api, revision)
        run_id = run_id or existing_run or f"run-{uuid.uuid4().hex[:12]}"
        if existing_run is None:
            self.store.begin_run(run_id, self.client.base_url, self.client.api, revision)
            # 根任务：全量通道。边界 bbox 即起始查询范围。
            self.store.enqueue_task(
                run_id, Tile(root_bbox, None).key, root_bbox, None, 0
            )
        # 续跑时把历史开放缺口按原深度重新入队。
        for gap in self.store.open_gap_tiles(revision):
            gbbox = round_bbox(gap["minx"], gap["miny"], gap["maxx"], gap["maxy"])
            self.store.enqueue_task(
                run_id, Tile(gbbox, gap["category"]).key, gbbox, gap["category"], gap["depth"]
            )

        interrupted: HarvestInterrupted | None = None
        try:
            self._loop(run_id, revision, boundary, root_bbox, floor)
        except HarvestInterrupted as exc:
            interrupted = exc
        # 预算耗尽或上次中断留下的待办叶范围统一登记为缺口：
        # 它们是「来源限制下无法确认」的区域，绝不能被当成空区或被遗忘。
        self.stats.gaps += self._sweep_pending_as_gaps(run_id, revision, floor)
        # 地块与市域的真实相交判定（含接触），不依赖中心点或外接矩形。
        self._evaluate_scope(revision, boundary)
        self.store.finish_run(
            run_id, "completed" if self.stats.gaps == 0 else "completed_with_gaps",
            note=f"queries={self.stats.queries} gaps={self.stats.gaps}",
        )
        if interrupted is not None:
            # 本次运行到此为止；缺口与观测已保存，重跑同一命令会从未决处继续。
            raise interrupted
        return run_id, self.stats

    def _loop(self, run_id: str, revision: int, boundary, root_bbox, floor: float) -> None:
        budget = self.task_budget
        while True:
            task = self.store.take_next_task(run_id)
            if task is None:
                return
            bbox = round_bbox(task["minx"], task["miny"], task["maxx"], task["maxy"])
            category = task["category"]
            depth = task["depth"]
            tile = Tile(bbox, category)
            try:
                response = self._issue_query(run_id, tile, depth, revision)
                if not response.truncated:
                    self.store.update_task(run_id, tile.key, "done")
                    continue

                # 截断：先尝试契约允许的类别筛选（仅全量通道需要升级）。
                if category is None:
                    categories = response.categories or self._declared_categories(run_id, tile)
                    if not categories:
                        raise SourceError("来源未声明 categories，无法用类别筛选消解截断")
                    self.store.update_task(run_id, tile.key, "escalated")
                    for cat in categories:
                        child = Tile(bbox, cat)
                        if self.store.enqueue_task(run_id, child.key, bbox, cat, depth):
                            budget -= 1
                    self._check_budget(run_id, budget)
                    continue

                # 单类别仍截断：空间二分。
                children = self._spatial_children(bbox, boundary, floor, depth)
                if children is None:
                    self._register_gap(run_id, revision, bbox, category,
                                       self._gap_reason(bbox, floor, depth), depth)
                    self.store.update_task(run_id, tile.key, "gap")
                    continue
                self.store.update_task(run_id, tile.key, "split")
                for child_bbox in children:
                    child = Tile(child_bbox, category)
                    if self.store.enqueue_task(run_id, child.key, child_bbox, category, depth + 1):
                        budget -= 1
                self._check_budget(run_id, budget)
            except KeyboardInterrupt:
                # 任务回到 pending，运行保持 running，下次同命令续跑。
                self.store.update_task(run_id, tile.key, "pending", "运行被人工终止")
                raise
            except SourceError as exc:
                self.stats.errors += 1
                # 可恢复错误（如来源暂时不可用、采集中途换界）：任务回到队列，
                # 运行保持 running，同代后续运行会继续；本轮中断退出。
                self.store.update_task(run_id, tile.key, "pending", str(exc))
                raise HarvestInterrupted(str(exc)) from exc

    def _declared_categories(self, run_id: str, tile: Tile) -> list[str]:
        rows = self.store.list_queries(run_id)
        for row in reversed(rows):
            cats = json.loads(row["raw_response"]).get("categories")
            if cats:
                return list(cats)
        return []

    def _spatial_children(self, bbox, boundary, floor: float, depth: int):
        """返回可继续查询的子矩形；不可再分时返回 None（调用方登记缺口）。"""
        minx, miny, maxx, maxy = bbox
        if depth >= MAX_DEPTH or max(maxx - minx, maxy - miny) <= floor:
            return None
        children = split_bbox(bbox)
        # 与市域不相交的子矩形无需查询：它落在边界缺口/界外，本就不属于收集范围。
        children = [c for c in children if self._intersects(c, boundary)]
        return children or None

    @staticmethod
    def _intersects(bbox, boundary) -> bool:
        from .geometry import intersects_boundary

        return intersects_boundary(bbox, boundary)

    def _check_budget(self, run_id: str, budget: int) -> None:
        if budget < 0:
            # 预算耗尽不丢数据：排队中的任务保留 pending，由 sweep 统一登记缺口；
            # 运行保持 running 状态，已完成的观测全部落库，下一次运行直接续跑。
            raise HarvestInterrupted("任务预算耗尽；已完成观测已保存，可再次运行继续收集")

    def _sweep_pending_as_gaps(self, run_id: str, revision: int, floor: float) -> int:
        """把仍未完成（pending/in_progress）的任务范围登记为缺口。

        预算耗尽或上次中断都会留下这类任务；它们是「来源限制下无法确认」的区域，
        绝不能被当成空区或被遗忘。重跑时这些任务会重新入队继续推进。
        """
        rows = self.store.conn.execute(
            "SELECT * FROM tasks WHERE run_id=? AND status IN ('pending','in_progress')",
            (run_id,)).fetchall()
        count = 0
        for row in rows:
            bbox = round_bbox(row["minx"], row["miny"], row["maxx"], row["maxy"])
            reason = (f"任务预算耗尽而中断：该范围（类别={row['category'] or '全量'}，"
                      f"深度={row['depth']}）仍处于截断链上，未取得未截断响应，"
                      "重跑可继续推进；在取得完整响应前属于无法确认区域")
            self.store.open_gap(run_id, revision, bbox, row["category"], reason, row["depth"])
            self.store.update_task(run_id, row["tile_key"], "gap", reason)
            count += 1
        return count

    def _register_gap(self, run_id: str, revision: int, bbox, category, reason: str,
                      depth: int) -> None:
        self.store.open_gap(run_id, revision, bbox, category, reason, depth)
        self.stats.gaps += 1

    @staticmethod
    def _gap_reason(bbox, floor: float, depth: int) -> str:
        minx, miny, maxx, maxy = bbox
        span = max(maxx - minx, maxy - miny)
        if depth >= MAX_DEPTH:
            return f"达到最大分割深度 {MAX_DEPTH}，该类别在此范围仍超来源上限"
        return (f"矩形跨度 {span:.3g} 已不大于最小分割跨度 {floor:.3g}（根跨度×{MIN_SPAN_RATIO:g}），"
                "该类别在此极小范围仍超来源上限，无法继续定位")

    # ---------- 市域归属 ----------
    def _evaluate_scope(self, revision: int, boundary) -> None:
        for row in self.store.list_parcels():
            geometry = json.loads(row["geometry_geojson"])
            in_scope = geometry_intersects_boundary(geometry, boundary)
            footprint = intersection_footprint(geometry, boundary) if in_scope else None
            self.store.save_scope_eval(row["parcel"], revision, in_scope, footprint)

    # ---------- 设施视图 ----------
    def facilities_view(self, revision: int) -> dict[str, dict]:
        """按设施主键聚合：所有相交地块都是覆盖证明，许可按设施登记一次。"""
        return build_facilities_view(self.store, revision)


def build_facilities_view(store: Store, revision: int) -> dict[str, dict]:
    """纯本地聚合，不发起任何网络访问，供报告与续跑复用。"""
    scopes = {r["parcel"]: r for r in store.list_scope_eval(revision)}
    facilities: dict[str, dict] = {}
    for parcel_row in store.list_parcels():
        parcel = parcel_row["parcel"]
        scope = scopes.get(parcel)
        if scope is None or not scope["in_scope"]:
            continue
        facility = parcel_row["facility"]
        entry = facilities.setdefault(facility, {
            "facility": facility,
            "category": parcel_row["category"],
            "permit_url": parcel_row["permit_url"],
            "parcels": [],
            "conflict": False,
        })
        entry["parcels"].append({
            "parcel": parcel,
            "intersection": json.loads(scope["intersection_geojson"]),
            "first_query_id": parcel_row["first_query_id"],
            "last_query_id": parcel_row["last_query_id"],
            "observations": parcel_row["observation_count"],
        })
        entry["conflict"] = entry["conflict"] or bool(parcel_row["geometry_conflict"])
    # 许可状态并入。
    for permit_row in store.list_permits():
        entry = facilities.get(permit_row["facility"])
        if entry is not None:
            entry["permit_status"] = permit_row["status"]
            entry["permit_reason"] = permit_row["reason"]
            entry["permit_path"] = permit_row["stored_path"]
    return facilities
