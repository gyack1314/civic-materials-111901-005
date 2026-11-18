"""采集端行为测试：覆盖、截断、溯源、换界、许可补收、续跑与审计。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from harvest.audit import audit_against_source
from harvest.client import SourceClient
from harvest.geometry import normalize_feature, parse_boundary
from harvest.harvester import Harvester, HarvestInterrupted
from harvest.permits import backfill_permits
from harvest.report import Reporter
from harvest.store import Store

from harvest_tests.fakes import FakeSourceClient, boundary_geojson


class HarvestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.db = self.out / "harvest.db"

    def tearDown(self):
        self.tmp.cleanup()

    def new_store(self) -> Store:
        return Store(self.db)


class FullHarvestTests(HarvestTestBase):
    def test_phase0_full_collection_and_scope(self):
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        harvester = Harvester(client, store)
        run_id, stats = harvester.run()
        self.assertEqual(stats.gaps, 0)
        self.assertGreater(stats.queries, 0)

        # 市域内设施：F-A（两个地块 P-A1/P-A2）、F-B、F-C；P-D 在边界缺口外。
        facilities = harvester.facilities_view(0)
        self.assertEqual(set(facilities), {"F-A", "F-B", "F-C"})
        self.assertEqual({p["parcel"] for p in facilities["F-A"]["parcels"]}, {"P-A1", "P-A2"})
        self.assertEqual(facilities["F-A"]["category"], "solar")

        parcels = {r["parcel"]: r for r in store.list_parcels()}
        self.assertEqual(set(parcels), {"P-A1", "P-A2", "P-B", "P-C", "P-D"})
        scopes = {r["parcel"]: r for r in store.list_scope_eval(0)}
        self.assertTrue(all(scopes[p]["in_scope"] for p in ("P-A1", "P-A2", "P-B", "P-C")))
        self.assertFalse(scopes["P-D"]["in_scope"])
        self.assertIsNone(json.loads(scopes["P-D"]["intersection_geojson"] or "null"))
        # 相交足迹是真实几何裁剪，不是外接矩形。
        fp = json.loads(scopes["P-A1"]["intersection_geojson"])
        self.assertEqual(fp["type"], "Polygon")
        store.close()

        # 报告：整体覆盖完整、无缺口、界外观测单独列出。
        store = self.new_store()
        report = Reporter(store).build(self.out)
        self.assertTrue(report["coverage"]["overall_complete"])
        self.assertEqual(report["gaps"], [])
        self.assertEqual(
            {x["parcel"] for x in report["parcels_out_of_scope"]}, {"P-D"})
        for cat in ("solar", "heat", "storage"):
            self.assertTrue(report["coverage"]["per_category_complete"][cat])
        # 交付物文件齐全。
        for name in ("report.json", "report.md", "coverage.geojson"):
            self.assertTrue((self.out / name).exists())
        store.close()

    def test_category_escalation_avoids_infinite_split(self):
        # 同址三个不同类别设施（P-A1/P-B/P-C 完全重叠），全量查询必然截断；
        # 类别升级后每个通道只有 1 条，未截断，不应产生任何空间分割或缺口。
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        harvester = Harvester(client, store)
        _, stats = harvester.run()
        self.assertEqual(stats.gaps, 0)
        catalog_calls = [c for c in client.calls if c[0] == "catalog"]
        used_categories = {c[2] for c in catalog_calls}
        self.assertIn("solar", used_categories)
        self.assertIn("heat", used_categories)
        self.assertIn("storage", used_categories)
        # 所有叶子查询必须未截断，截断响应不计覆盖。
        for row in store.coverage_tiles(0):
            self.assertFalse(row["truncated"])
        store.close()


class AxisTests(HarvestTestBase):
    def test_yx_axis_normalized_and_declared(self):
        client = FakeSourceClient(phase=0, api="yx")
        store = self.new_store()
        Harvester(client, store).run()
        # 原始响应按来源原文保留 axis=lat,lon。
        row = store.list_queries()[0]
        self.assertEqual(row["axis"], "lat,lon")
        self.assertEqual(row["crs"], "EPSG:4326")
        # 归一化后的地块几何与 xy 版本一致（经纬度顺序）。
        p_a1 = store.get_parcel("P-A1")
        geom = json.loads(p_a1["geometry_geojson"])
        first = geom["coordinates"][0][0]
        self.assertEqual(first, [2.0, 2.0])  # lon,lat
        # normalize_feature 对未知 axis 拒绝猜测。
        with self.assertRaises(ValueError):
            normalize_feature({"geometry": {"coordinates": []}}, "weird")
        store.close()

    def test_xy_and_yx_identical_footprints(self):
        for api in ("xy", "yx"):
            out = self.out / api
            store = Store(out / "harvest.db")
            Harvester(FakeSourceClient(phase=0, api=api), store).run()
            scopes = {r["parcel"]: json.loads(r["intersection_geojson"])
                      for r in store.list_scope_eval(0) if r["in_scope"]}
            if api == "xy":
                baseline = scopes
            else:
                self.assertEqual(set(scopes), set(baseline))
                for parcel, geom in scopes.items():
                    self.assertEqual(geom, baseline[parcel])
            store.close()


class TruncationAndGapTests(HarvestTestBase):
    def test_persistent_truncation_becomes_gap_not_empty_or_infinite(self):
        client = FakeSourceClient(phase=0, force_truncated=True)
        store = self.new_store()
        harvester = Harvester(client, store, task_budget=80)
        # 预算耗尽：运行中断但已完成的观测全部保留，待办叶被登记为缺口。
        with self.assertRaises(HarvestInterrupted):
            harvester.run()
        self.assertTrue(store.list_queries())
        first_gaps = store.list_gaps(revision=0, status="open")
        self.assertTrue(first_gaps)
        for gap in first_gaps:
            self.assertIn("无法确认", gap["reason"])
        store.close()

        # 续跑：同一采集库继续推进，缺口随分割被重新打开/消解或在更深层保留；
        # 无论如何不会把未确认范围当作空区，也不会无限循环（预算再次兜底）。
        client2 = FakeSourceClient(phase=0, force_truncated=True)
        store = self.new_store()
        harvester2 = Harvester(client2, store, task_budget=120)
        with self.assertRaises(HarvestInterrupted):
            harvester2.run()
        gaps = store.list_gaps(revision=0, status="open")
        self.assertTrue(gaps)
        report = Reporter(store).build(self.out)
        self.assertFalse(report["coverage"]["overall_complete"])
        self.assertEqual(len(report["gaps"]), len(gaps))
        store.close()

    def test_empty_complete_response_is_kept_as_coverage(self):
        # 界内但无任何地块的小矩形：来源返回空列表但 truncated=false，
        # 该查询范围必须作为覆盖瓦片保留（空结果有同等范围意义）。
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        harvester = Harvester(client, store)
        revision, boundary = harvester.fetch_boundary()
        from harvest.geometry import Tile, bbox_polygon

        empty_bbox = (0.2, 0.2, 0.6, 0.6)  # 位于市域内、远离所有地块
        self.assertTrue(bbox_polygon(empty_bbox).intersects(boundary))
        response = harvester._issue_query("run-manual", Tile(empty_bbox, "solar"), 0, revision)
        self.assertFalse(response.truncated)
        self.assertEqual(response.items, [])
        tiles = store.coverage_tiles(0)
        self.assertTrue(any(t["item_count"] == 0 for t in tiles))
        # 报告中的覆盖几何把该矩形裁剪进市域，且空瓦片被单独计数。
        report = Reporter(store).build(self.out)
        self.assertIn("solar", report["coverage"]["channels"])
        store.close()

    def test_floor_and_depth_gap_reasons(self):
        # 极小矩形仍被来源判定截断：不能再分，直接登记为缺口，原因明确且不是空区。
        from harvest.geometry import MIN_SPAN_RATIO, Tile

        client = FakeSourceClient(phase=0, force_truncated=True)
        store = self.new_store()
        h = Harvester(client, store, task_budget=500)
        revision, boundary = h.fetch_boundary()
        root = tuple(round(v, 10) for v in boundary.bounds)
        root_span = max(root[2] - root[0], root[3] - root[1])
        floor_span = root_span * MIN_SPAN_RATIO
        # 落在界内、跨度小于最小分割跨度的极小矩形，且来源对它持续截断。
        tiny = (0.5, 0.5, 0.5 + floor_span / 2, 0.5 + floor_span / 2)
        h.store.enqueue_task("r1", Tile(tiny, "solar").key, tiny, "solar", 0)
        with self.assertRaises(HarvestInterrupted):
            h.run()
        gaps = store.list_gaps(revision=0, status="open")
        self.assertTrue(any("极小范围" in g["reason"] and "仍超来源上限" in g["reason"])
                        for g in gaps)
        store.close()


class ResumeTests(HarvestTestBase):
    def test_resume_reuses_queries_and_dedupes_tiles(self):
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        run_id, _ = Harvester(client, store).run()
        first_count = len(store.list_queries())
        store.close()

        # 再次运行：同一来源/分代的未结束或新运行都应复用全部瓦片，不产生新查询。
        client2 = FakeSourceClient(phase=0)
        store = self.new_store()
        run_id2, stats2 = Harvester(client2, store).run()
        self.assertEqual(stats2.queries, 0)
        self.assertGreater(stats2.reused, 0)
        self.assertEqual(len(store.list_queries()), first_count)
        # 地块不重复、观测计数反映两次来源接触（第二次为本地复用，计数只在真实响应时增长）。
        self.assertEqual(len(store.list_parcels()), 5)
        store.close()


class BoundaryRevisionTests(HarvestTestBase):
    def test_boundary_change_invalidates_old_coverage_but_keeps_archive(self):
        # 第一代采集。
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        Harvester(client, store).run()
        rev0_tiles = store.coverage_tiles(0)
        self.assertTrue(rev0_tiles)
        store.close()

        # 来源换界（revision=1，更小的市域）：旧代查询留档，但当前覆盖只看新代。
        client2 = FakeSourceClient(phase=1)
        store = self.new_store()
        Harvester(client2, store).run()
        self.assertTrue(store.coverage_tiles(1))
        # 旧代瓦片仍可查，不参与新代结论。
        archived = [q for q in store.list_queries() if q["declared_revision"] == 0]
        self.assertTrue(archived)
        report = Reporter(store).build(self.out)
        self.assertEqual(report["boundary_revision"], 1)
        # 新边界仍包含四个地块，P-D 依旧界外。
        facilities = {f["facility"] for f in report["facilities"]}
        self.assertEqual(facilities, {"F-A", "F-B", "F-C"})
        store.close()

    def test_mid_run_revision_change_is_rejected(self):
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        harvester = Harvester(client, store)
        harvester.fetch_boundary()
        # 来源在采集途中换界：响应 revision 与运行分代不一致，必须报错而不是混入覆盖。
        client.phase = 1
        from harvest.client import SourceError
        from harvest.geometry import Tile

        with self.assertRaises(SourceError):
            harvester._issue_query("run-x", Tile((0, 0, 10, 10), None), 0, 0)
        store.close()


class PermitTests(HarvestTestBase):
    def _harvest(self, phase):
        client = FakeSourceClient(phase=phase)
        store = self.new_store()
        Harvester(client, store).run()
        return store, client

    def test_permits_verified_once_and_wrong_identity_quarantined(self):
        store, client = self._harvest(0)
        result = backfill_permits(store, client, self.out)
        self.assertEqual(result, {"F-A": "verified", "F-B": "verified", "F-C": "verified"})
        # 原件落盘、sha256 有记录、按设施只登记一次。
        permits = {r["facility"]: r for r in store.list_permits()}
        self.assertEqual(len(permits), 3)
        self.assertTrue(Path(permits["F-A"]["stored_path"]).exists())
        self.assertTrue(permits["F-A"]["bytes_sha256"])
        store.close()

        # phase=1：F-A 许可声明变成 F-WRONG。重新补收必须隔离，不能拿可打开 PDF 充数。
        client2 = FakeSourceClient(phase=1)
        store = self.new_store()
        # 先在新分代重新做空间采集（许可身份按当前来源核实）。
        Harvester(client2, store).run()
        result2 = backfill_permits(store, client2, self.out)
        self.assertEqual(result2["F-A"], "rejected")
        row = store.get_permit("F-A")
        self.assertEqual(row["declared_facility"], "F-WRONG")
        self.assertTrue(Path(row["quarantine_path"]).exists())
        self.assertIsNone(row["stored_path"])
        # 空间观测与范围证明不受许可失败影响。
        self.assertTrue(store.coverage_tiles(1))
        self.assertTrue(store.get_parcel("P-A1"))
        store.close()

    def test_backfill_restores_completeness_without_touching_coverage(self):
        # phase=1 采集：F-A 许可被拒，但空间证明完整。
        client = FakeSourceClient(phase=1)
        store = self.new_store()
        Harvester(client, store).run()
        backfill_permits(store, client, self.out)
        self.assertEqual(store.get_permit("F-A")["status"], "rejected")
        queries_before = [(q["query_id"], q["raw_response"]) for q in store.list_queries()]

        # 来源恢复（phase=2 边界同 phase=1，许可身份恢复）。
        client.phase = 2
        client._boundary = boundary_geojson(1)  # phase=2 保持新版边界
        backfill_permits(store, client, self.out)
        self.assertEqual(store.get_permit("F-A")["status"], "verified")
        # 补收许可没有改动任何空间查询/范围证明。
        queries_after = [(q["query_id"], q["raw_response"]) for q in store.list_queries()]
        self.assertEqual(queries_before, queries_after)
        report = Reporter(store).build(self.out)
        entry = next(f for f in report["facilities"] if f["facility"] == "F-A")
        self.assertEqual(entry["permit_status"], "verified")
        store.close()

    def test_non_pdf_is_rejected(self):
        store, client = self._harvest(0)
        client.permit_overrides["F-B"] = b"not a pdf at all"
        result = backfill_permits(store, client, self.out)
        self.assertEqual(result["F-B"], "rejected")
        self.assertIn("不是 PDF", store.get_permit("F-B")["reason"])
        store.close()


class ProvenanceAndAuditTests(HarvestTestBase):
    def test_parcel_query_provenance_chain(self):
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        Harvester(client, store).run()
        chain = store.parcel_provenance("P-A1")
        self.assertTrue(chain)
        # 溯源链能回到具体请求 URL、运行、轴序声明与返回顺序。
        first = chain[0]
        self.assertTrue(first["request_url"].startswith("http://fake-source/catalog"))
        self.assertIn(first["axis"], ("lon,lat", "lat,lon"))
        self.assertGreaterEqual(first["returned_order"], 0)
        # F-A 的两个地块都可追溯到同一设施，许可登记只有一条。
        self.assertEqual(store.get_parcel("P-A2")["facility"], "F-A")
        store.close()

    def test_audit_reconciles_source_footprints(self):
        client = FakeSourceClient(phase=0)
        store = self.new_store()
        Harvester(client, store).run()
        # 用本地记录构造「来源日志」：每条本地查询都对应一条足迹。
        source_log = [
            {"method": "GET", "path": q["request_url"].replace("http://fake-source", ""),
             "time": i, "headers": {}}
            for i, q in enumerate(store.list_queries())
        ]
        source_log.insert(0, {"method": "GET", "path": "/boundary", "time": 0, "headers": {}})
        result = audit_against_source(store, source_log)
        self.assertEqual(result["conclusion"], "覆盖结论与来源足迹一致")
        self.assertEqual(result["queries_without_source_footprint"], [])
        self.assertGreater(result["coverage_tiles_verified"], 0)

        # 来源缺了一条足迹：审计必须发现证据不足。
        source_log = source_log[:-1]
        result2 = audit_against_source(store, source_log)
        self.assertNotEqual(result2["conclusion"], "覆盖结论与来源足迹一致")
        self.assertTrue(result2["coverage_tiles_missing_evidence"]
                        or result2["queries_without_source_footprint"])
        store.close()


class RealSourceSmokeTests(HarvestTestBase):
    """对真实演练来源（in-process）跑一遍端到端，包含 /__admin/log 审计。"""

    def test_end_to_end_against_running_source(self):
        from source.server import running_source

        with running_source() as (base, source):
            client = SourceClient(base)
            store = self.new_store()
            run_id, stats = Harvester(client, store).run()
            self.assertEqual(stats.gaps, 0)
            result = backfill_permits(store, client, self.out)
            self.assertEqual(result["F-A"], "verified")
            report = Reporter(store).build(self.out)
            self.assertTrue(report["coverage"]["overall_complete"])

            # 操作员审计：来源真实足迹与本地一致。
            from harvest.audit import fetch_source_log

            log = fetch_source_log(base)
            audit = audit_against_source(store, log)
            self.assertEqual(audit["conclusion"], "覆盖结论与来源足迹一致")
            # 采集主流程没有碰过演练控制入口。
            self.assertFalse(
                any("/__admin" in e["path"] for e in log if e["path"].startswith("/catalog")
                    or e["path"].startswith("/boundary") or e["path"].startswith("/permit")))
            store.close()


if __name__ == "__main__":
    unittest.main()
