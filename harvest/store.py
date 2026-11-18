"""本地 SQLite 持久化。

设计原则：
- 每一次 /catalog 调用都留痕（含截断响应），未截断的查询才是覆盖「瓦片」；
- 地块（parcel）是观测主键，设施（facility）是归属分组；parcel × query 的多对多
  溯源链保证「来自哪个地块的哪次查询」可追查；
- 边界按 revision 分代：旧代查询原样留档，但不参与当前覆盖结论；
- 许可按设施登记一次，状态与空间观测独立，补收许可不改动任何空间证明。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS boundaries (
    revision INTEGER PRIMARY KEY,
    fetched_at REAL NOT NULL,
    geojson TEXT NOT NULL,
    boundary_hash TEXT NOT NULL,
    superseded_at REAL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    finished_at REAL,
    source_base TEXT NOT NULL,
    api TEXT NOT NULL,
    boundary_revision INTEGER,
    status TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    tile_key TEXT NOT NULL,
    minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
    category TEXT,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    enqueued_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, tile_key)
);
CREATE TABLE IF NOT EXISTS catalog_queries (
    query_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    tile_key TEXT NOT NULL,
    minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
    category TEXT,
    request_url TEXT NOT NULL,
    axis TEXT NOT NULL,
    crs TEXT NOT NULL,
    declared_revision INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    raw_response TEXT NOT NULL,
    requested_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS parcel_observations (
    parcel TEXT PRIMARY KEY,
    facility TEXT NOT NULL,
    category TEXT NOT NULL,
    permit_url TEXT NOT NULL,
    geometry_geojson TEXT NOT NULL,
    first_query_id INTEGER,
    last_query_id INTEGER,
    observation_count INTEGER NOT NULL DEFAULT 1,
    geometry_conflict INTEGER NOT NULL DEFAULT 0,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS parcel_query (
    parcel TEXT NOT NULL,
    query_id INTEGER NOT NULL,
    returned_order INTEGER NOT NULL,
    PRIMARY KEY (parcel, query_id)
);
CREATE TABLE IF NOT EXISTS scope_eval (
    parcel TEXT PRIMARY KEY,
    boundary_revision INTEGER NOT NULL,
    in_scope INTEGER NOT NULL,
    intersection_geojson TEXT,
    evaluated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS permits (
    facility TEXT PRIMARY KEY,
    permit_url TEXT NOT NULL,
    status TEXT NOT NULL,
    stored_path TEXT,
    quarantine_path TEXT,
    declared_facility TEXT,
    http_url TEXT,
    bytes_sha256 TEXT,
    reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    verified_revision INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (
    gap_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    boundary_revision INTEGER NOT NULL,
    minx REAL NOT NULL, miny REAL NOT NULL, maxx REAL NOT NULL, maxy REAL NOT NULL,
    category TEXT,
    depth INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at REAL NOT NULL,
    resolved_at REAL,
    resolved_query_id INTEGER
);
"""


def now() -> float:
    return time.time()


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _init_schema(self) -> None:
        # executescript 会先隐式提交挂起事务再执行脚本；先建表，再用独立查询读版本。
        self.conn.executescript(SCHEMA)
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
            self.conn.commit()
        elif int(row["value"]) != SCHEMA_VERSION:
            raise RuntimeError(f"采集库 schema 版本 {row['value']} 与程序 {SCHEMA_VERSION} 不兼容")
        self._migrate()

    def _migrate(self) -> None:
        """轻量列迁移：旧采集库补齐后加列，数据不丢。"""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(permits)")}
        if "verified_revision" not in cols:
            self.conn.execute("ALTER TABLE permits ADD COLUMN verified_revision INTEGER")
            self.conn.commit()
        gap_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(gaps)")}
        if "depth" not in gap_cols:
            self.conn.execute("ALTER TABLE gaps ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    # ---------- 边界分代 ----------
    def save_boundary(self, revision: int, geojson: dict[str, Any]) -> str:
        raw = json.dumps(geojson, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        with self.tx() as c:
            existing = c.execute("SELECT boundary_hash FROM boundaries WHERE revision=?", (revision,)).fetchone()
            if existing is None:
                c.execute(
                    "INSERT INTO boundaries(revision,fetched_at,geojson,boundary_hash) VALUES(?,?,?,?)",
                    (revision, now(), raw, h),
                )
            elif existing["boundary_hash"] != h:
                # 同号但几何变了：来源契约不允许静默覆盖，留新行需人工裁决，这里直接报错。
                raise RuntimeError(f"边界 revision={revision} 的几何与已存版本不一致，拒绝覆盖采集库")
        return h

    def get_boundary(self, revision: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT geojson FROM boundaries WHERE revision=?", (revision,)).fetchone()
        return json.loads(row["geojson"]) if row else None

    def list_boundary_revisions(self) -> list[int]:
        return [r["revision"] for r in self.conn.execute("SELECT revision FROM boundaries ORDER BY revision")]

    # ---------- 运行 ----------
    def begin_run(self, run_id: str, source_base: str, api: str, boundary_revision: int | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO runs(run_id,started_at,source_base,api,boundary_revision,status) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, now(), source_base, api, boundary_revision, "running"),
            )

    def finish_run(self, run_id: str, status: str, note: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, note=? WHERE run_id=?",
                (now(), status, note, run_id),
            )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()

    def latest_open_run(self, source_base: str, api: str, revision: int) -> str | None:
        """找到同一来源、同一 api、同一边界分代下尚未结束的运行，用于终止后继续。"""
        row = self.conn.execute(
            "SELECT run_id FROM runs WHERE source_base=? AND api=? AND boundary_revision=? "
            "AND status='running' ORDER BY started_at DESC LIMIT 1",
            (source_base, api, revision),
        ).fetchone()
        return row["run_id"] if row else None

    # ---------- 任务队列 ----------
    def enqueue_task(
        self,
        run_id: str,
        tile_key: str,
        bbox: tuple[float, float, float, float],
        category: str | None,
        depth: int,
    ) -> bool:
        try:
            with self.tx() as c:
                c.execute(
                    "INSERT INTO tasks(run_id,tile_key,minx,miny,maxx,maxy,category,depth,"
                    "status,enqueued_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, tile_key, *bbox, category, depth, "pending", now(), now()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def take_next_task(self, run_id: str) -> sqlite3.Row | None:
        with self.tx() as c:
            row = c.execute(
                "SELECT * FROM tasks WHERE run_id=? AND status='pending' "
                "ORDER BY depth, tile_key LIMIT 1",
                (run_id,),
            ).fetchone()
            if row is not None:
                c.execute(
                    "UPDATE tasks SET status='in_progress', attempts=attempts+1, updated_at=? "
                    "WHERE run_id=? AND tile_key=?",
                    (now(), run_id, row["tile_key"]),
                )
            return row

    def update_task(self, run_id: str, tile_key: str, status: str, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET status=?, last_error=?, updated_at=? WHERE run_id=? AND tile_key=?",
                (status, error, now(), run_id, tile_key),
            )

    def task_counts(self, run_id: str) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) n FROM tasks WHERE run_id=? GROUP BY status", (run_id,)
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def completed_query_for_tile(self, tile_key: str) -> sqlite3.Row | None:
        """该瓦片是否已有成功查询（任意运行），供同边界分代续跑/重跑时免重复请求。"""
        return self.conn.execute(
            "SELECT * FROM catalog_queries WHERE tile_key=? ORDER BY query_id DESC LIMIT 1",
            (tile_key,),
        ).fetchone()

    # ---------- 查询留痕 ----------
    def record_query(
        self,
        run_id: str,
        tile: "Tileish",
        request_url: str,
        axis: str,
        crs: str,
        declared_revision: int,
        truncated: bool,
        item_count: int,
        raw_response: str,
    ) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO catalog_queries(run_id,tile_key,minx,miny,maxx,maxy,category,"
                "request_url,axis,crs,declared_revision,truncated,item_count,raw_response,requested_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id, tile.key, *tile.bbox, tile.category, request_url, axis, crs,
                    declared_revision, 1 if truncated else 0, item_count, raw_response, now(),
                ),
            )
            return int(cur.lastrowid)

    def list_queries(self, run_id: str | None = None) -> list[sqlite3.Row]:
        if run_id:
            return list(self.conn.execute(
                "SELECT * FROM catalog_queries WHERE run_id=? ORDER BY query_id", (run_id,)))
        return list(self.conn.execute("SELECT * FROM catalog_queries ORDER BY query_id"))

    # ---------- 地块观测与溯源 ----------
    def upsert_parcel(
        self,
        properties: dict[str, Any],
        geometry_geojson: dict[str, Any],
        query_id: int,
        returned_order: int,
    ) -> None:
        parcel = properties["parcel"]
        facility = properties["facility"]
        category = properties["category"]
        permit_url = properties["permit"]
        geom_raw = json.dumps(geometry_geojson, ensure_ascii=False, sort_keys=True)
        with self.tx() as c:
            row = c.execute("SELECT * FROM parcel_observations WHERE parcel=?", (parcel,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO parcel_observations(parcel,facility,category,permit_url,geometry_geojson,"
                    "first_query_id,last_query_id,observation_count,first_seen_at,last_seen_at)"
                    " VALUES(?,?,?,?,?,?,?,1,?,?)",
                    (parcel, facility, category, permit_url, geom_raw, query_id, query_id, now(), now()),
                )
            else:
                conflict = row["geometry_conflict"]
                if row["geometry_geojson"] != geom_raw:
                    conflict = 1
                # 身份字段若发生变化，同样标注冲突并保留原值，交付报告提示人工核实。
                if (row["facility"], row["category"], row["permit_url"]) != (facility, category, permit_url):
                    conflict = 1
                c.execute(
                    "UPDATE parcel_observations SET last_query_id=?, observation_count=observation_count+1,"
                    "geometry_conflict=?, last_seen_at=? WHERE parcel=?",
                    (query_id, conflict, now(), parcel),
                )
            c.execute(
                "INSERT OR IGNORE INTO parcel_query(parcel,query_id,returned_order) VALUES(?,?,?)",
                (parcel, query_id, returned_order),
            )

    def list_parcels(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM parcel_observations ORDER BY parcel"))

    def get_parcel(self, parcel: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM parcel_observations WHERE parcel=?", (parcel,)).fetchone()

    def parcel_provenance(self, parcel: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT q.*, pq.returned_order FROM parcel_query pq "
            "JOIN catalog_queries q ON q.query_id=pq.query_id "
            "WHERE pq.parcel=? ORDER BY q.query_id", (parcel,)))

    # ---------- 市域归属判定 ----------
    def save_scope_eval(
        self, parcel: str, revision: int, in_scope: bool, intersection: dict[str, Any] | None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO scope_eval(parcel,boundary_revision,in_scope,intersection_geojson,evaluated_at)"
                " VALUES(?,?,?,?,?) ON CONFLICT(parcel) DO UPDATE SET "
                "boundary_revision=excluded.boundary_revision,in_scope=excluded.in_scope,"
                "intersection_geojson=excluded.intersection_geojson,evaluated_at=excluded.evaluated_at",
                (parcel, revision, 1 if in_scope else 0,
                 json.dumps(intersection, ensure_ascii=False) if intersection else None, now()),
            )

    def list_scope_eval(self, revision: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM scope_eval WHERE boundary_revision=?", (revision,)))

    # ---------- 许可 ----------
    def upsert_permit_request(self, facility: str, permit_url: str) -> None:
        with self.tx() as c:
            row = c.execute("SELECT facility FROM permits WHERE facility=?", (facility,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO permits(facility,permit_url,status,attempts,updated_at)"
                    " VALUES(?,?, 'pending', 0, ?)",
                    (facility, permit_url, now()),
                )

    def update_permit(
        self,
        facility: str,
        status: str,
        *,
        stored_path: str | None = None,
        quarantine_path: str | None = None,
        declared_facility: str | None = None,
        http_url: str | None = None,
        bytes_sha256: str | None = None,
        reason: str | None = None,
        verified_revision: int | None = None,
    ) -> None:
        with self.tx() as c:
            # 状态直接覆盖（不用 COALESCE）：被拒/待补收时旧的通过路径必须失效，
            # 不能让一份未核实的文件继续挂在 stored_path 上充数。
            c.execute(
                "UPDATE permits SET status=?, stored_path=?,"
                "quarantine_path=COALESCE(?,quarantine_path), declared_facility=?,"
                "http_url=COALESCE(?,http_url), bytes_sha256=COALESCE(?,bytes_sha256),"
                "reason=?, attempts=attempts+1, updated_at=?,"
                "verified_revision=COALESCE(?,verified_revision) WHERE facility=?",
                (status, stored_path, quarantine_path, declared_facility, http_url,
                 bytes_sha256, reason, now(), verified_revision, facility),
            )

    def reset_stale_permits(self, revision: int) -> list[str]:
        """换界/修订后，旧分代的 verified 不再可直接采信，转 pending 等待重新核实。

        已下载的原件文件保留不删（留档），但状态不再是 verified。
        """
        with self.tx() as c:
            rows = c.execute(
                "SELECT facility FROM permits WHERE status='verified' "
                "AND (verified_revision IS NULL OR verified_revision != ?)",
                (revision,),
            ).fetchall()
            c.execute(
                "UPDATE permits SET status='pending', "
                "reason='边界分代变化，旧许可验证不再直接采信，等待重新核实', updated_at=? "
                "WHERE status='verified' AND (verified_revision IS NULL OR verified_revision != ?)",
                (now(), revision),
            )
            return [r["facility"] for r in rows]

    def list_permits(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM permits ORDER BY facility"))

    def get_permit(self, facility: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM permits WHERE facility=?", (facility,)).fetchone()

    # ---------- 缺口 ----------
    def open_gap(
        self,
        run_id: str,
        revision: int,
        bbox: tuple[float, float, float, float],
        category: str | None,
        reason: str,
        depth: int = 0,
    ) -> int:
        minx, miny, maxx, maxy = bbox
        with self.tx() as c:
            # 同范围同通道已有开放缺口（如续跑重试后仍截断）则复用，不重复登记。
            existing = c.execute(
                "SELECT gap_id FROM gaps WHERE status='open' AND boundary_revision=? "
                "AND minx=? AND miny=? AND maxx=? AND maxy=? "
                "AND COALESCE(category,'*')=COALESCE(?, '*')",
                (revision, minx, miny, maxx, maxy, category),
            ).fetchone()
            if existing is not None:
                return int(existing["gap_id"])
            cur = c.execute(
                "INSERT INTO gaps(run_id,boundary_revision,minx,miny,maxx,maxy,category,"
                "depth,reason,status,opened_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, revision, *bbox, category, depth, reason, "open", now()),
            )
            return int(cur.lastrowid)

    def resolve_gaps_for_tile(self, tile_key_bbox: tuple[float, float, float, float],
                              category: str | None, query_id: int) -> int:
        """某瓦片取得未截断响应后，关闭落在同一范围/通道上的旧缺口。"""
        minx, miny, maxx, maxy = tile_key_bbox
        with self.tx() as c:
            cur = c.execute(
                "UPDATE gaps SET status='resolved', resolved_at=?, resolved_query_id=? "
                "WHERE status='open' AND minx=? AND miny=? AND maxx=? AND maxy=? "
                "AND COALESCE(category,'*')=COALESCE(?, '*')",
                (now(), query_id, minx, miny, maxx, maxy, category),
            )
            return cur.rowcount

    def resolve_contained_gaps(self, revision: int,
                               bbox: tuple[float, float, float, float],
                               category: str | None, query_id: int) -> int:
        """未截断父/子矩形覆盖了缺口矩形时，关闭被其包含的同通道开放缺口。"""
        minx, miny, maxx, maxy = bbox
        with self.tx() as c:
            cur = c.execute(
                "UPDATE gaps SET status='resolved', resolved_at=?, resolved_query_id=? "
                "WHERE status='open' AND boundary_revision=? "
                "AND COALESCE(category,'*')=COALESCE(?, '*') "
                "AND minx>=? AND miny>=? AND maxx<=? AND maxy<=?",
                (now(), query_id, revision, category, minx, miny, maxx, maxy),
            )
            return cur.rowcount

    def list_gaps(self, revision: int | None = None, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM gaps WHERE 1=1"
        params: list[Any] = []
        if revision is not None:
            sql += " AND boundary_revision=?"
            params.append(revision)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY gap_id"
        return list(self.conn.execute(sql, params))

    def open_gap_tiles(self, revision: int) -> list[sqlite3.Row]:
        """当前分代仍开放的缺口（可能来自之前的运行），续跑时按原深度重新入队。"""
        return list(self.conn.execute(
            "SELECT DISTINCT minx,miny,maxx,maxy,category,depth FROM gaps "
            "WHERE boundary_revision=? AND status='open'", (revision,)))

    # ---------- 覆盖瓦片 ----------
    def coverage_tiles(self, revision: int) -> list[sqlite3.Row]:
        """当前边界分代下所有未截断查询：覆盖结论的唯一依据（含空结果查询）。"""
        return list(self.conn.execute(
            "SELECT * FROM catalog_queries WHERE declared_revision=? AND truncated=0 ORDER BY query_id",
            (revision,),
        ))

    def truncated_queries(self, revision: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM catalog_queries WHERE declared_revision=? AND truncated=1 ORDER BY query_id",
            (revision,),
        ))
