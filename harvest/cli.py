"""命令行入口：harvest run / permits / report / audit / provenance。

典型流程（与 docs/source-contract.md 的本地来源配合）：
    python -I -m harvest run     --base http://127.0.0.1:8765 --out ./out
    python -I -m harvest permits --base http://127.0.0.1:8765 --out ./out
    python -I -m harvest report  --out ./out
    python -I -m harvest audit   --base http://127.0.0.1:8765 --out ./out
所有命令都可重复执行：空间采集支持终止后继续，许可支持补收，已验证原件不重复下载。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import audit_against_source, fetch_source_log
from .client import SourceClient
from .harvester import Harvester, HarvestInterrupted
from .permits import backfill_permits
from .report import Reporter
from .store import Store


def _paths(out: str) -> tuple[Path, Path]:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir, out_dir / "harvest.db"


def cmd_run(args: argparse.Namespace) -> int:
    out_dir, db_path = _paths(args.out)
    store = Store(db_path)
    try:
        client = SourceClient(args.base, api=args.api, timeout=args.timeout)
        harvester = Harvester(client, store, task_budget=args.task_budget)
        try:
            run_id, stats = harvester.run()
        except HarvestInterrupted as exc:
            print(f"[中断] {exc}", file=sys.stderr)
            print("已完成观测已保存；重新执行同一命令即可继续收集。", file=sys.stderr)
            return 2
        print(f"运行 {run_id} 结束：新查询 {stats.queries} 次（复用 {stats.reused} 次），"
              f"截断 {stats.truncated} 次，空范围完整响应 {stats.empty_complete} 个，"
              f"地块观测 {stats.parcels_seen} 条，开放缺口 {stats.gaps} 个，错误 {stats.errors} 个")
        if stats.gaps:
            print("注意：存在来源限制下无法确认的缺口，详见 report 命令输出。", file=sys.stderr)
        return 0
    finally:
        store.close()


def cmd_permits(args: argparse.Namespace) -> int:
    out_dir, db_path = _paths(args.out)
    store = Store(db_path)
    try:
        client = SourceClient(args.base, api=args.api, timeout=args.timeout)
        result = backfill_permits(store, client, out_dir, only_pending=args.only_pending)
    finally:
        store.close()
    for facility, status in sorted(result.items()):
        print(f"{facility}: {status}")
    bad = [f for f, s in result.items() if s == "rejected"]
    pending = [f for f, s in result.items() if s == "pending"]
    if bad:
        print(f"身份不符或无法解析的许可已隔离：{', '.join(bad)}（等待来源核实，未充数）",
              file=sys.stderr)
    if pending:
        print(f"暂缺许可：{', '.join(pending)}；空间观测不受影响，可稍后重跑本命令补收。",
              file=sys.stderr)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    out_dir, db_path = _paths(args.out)
    store = Store(db_path)
    try:
        report = Reporter(store).build(out_dir)
    finally:
        store.close()
    rev = report["boundary_revision"]
    print(f"报告已写入 {out_dir}/report.json、report.md、coverage.geojson")
    print(f"边界 revision={rev}，整体覆盖完整：{report['coverage']['overall_complete']}，"
          f"开放缺口 {len(report['gaps'])} 个，市域内设施 {len(report['facilities'])} 个")
    for entry in report["facilities"]:
        print(f"  设施 {entry['facility']}（{entry['category']}）：地块 "
              f"{[p['parcel'] for p in entry['parcels']]}，许可 {entry.get('permit_status', '未登记')}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    out_dir, db_path = _paths(args.out)
    store = Store(db_path)
    try:
        source_log = fetch_source_log(args.base)
        result = audit_against_source(store, source_log)
    finally:
        store.close()
    out_path = Path(out_dir) / "audit.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"审计结果已写入 {out_path}")
    print(f"本地产品查询 {result['local_product_queries']} 条，来源产品足迹 "
          f"{result['source_product_footprints']} 条，匹配 {result['matched_queries']} 条")
    print(f"覆盖瓦片已证实 {result['coverage_tiles_verified']} 个，"
          f"开放缺口 {len(result['open_gaps'])} 个")
    print(f"结论：{result['conclusion']}")
    return 0 if result["conclusion"] == "覆盖结论与来源足迹一致" else 3


def cmd_provenance(args: argparse.Namespace) -> int:
    _, db_path = _paths(args.out)
    store = Store(db_path)
    try:
        parcel_row = store.get_parcel(args.parcel)
        if parcel_row is None:
            print(f"本地没有地块 {args.parcel} 的观测", file=sys.stderr)
            return 1
        chain = store.parcel_provenance(args.parcel)
        print(f"地块 {args.parcel} -> 设施 {parcel_row['facility']}，"
              f"类别 {parcel_row['category']}，共被 {parcel_row['observation_count']} 次查询返回：")
        for link in chain:
            print(f"  query#{link['query_id']} 运行 {link['run_id']} "
                  f"类别={link['category'] or '全量'} truncated={bool(link['truncated'])} "
                  f"顺序={link['returned_order']} axis={link['axis']} crs={link['crs']}")
            print(f"    {link['request_url']}")
        scope = next((r for r in store.list_scope_eval(
            max(store.list_boundary_revisions())) if r["parcel"] == args.parcel), None)
        if scope is not None:
            print(f"市域归属（revision={scope['boundary_revision']}）："
                  f"{'在收集范围内（真实几何相交）' if scope['in_scope'] else '界外'}")
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harvest", description="城市低碳设施空间目录限量采集")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行/继续空间采集")
    p_run.add_argument("--base", required=True, help="来源基址，如 http://127.0.0.1:8765")
    p_run.add_argument("--out", required=True, help="采集保存目录（数据库与原件独立管理）")
    p_run.add_argument("--api", choices=("xy", "yx"), default="xy",
                       help="目录接口坐标版本，响应 axis/crs 仍以来源声明为准")
    p_run.add_argument("--timeout", type=float, default=15.0)
    p_run.add_argument("--task-budget", type=int, default=2000,
                       help="单次运行任务预算；超限保留待办与缺口而非无限分割")
    p_run.set_defaults(func=cmd_run)

    p_perm = sub.add_parser("permits", help="下载并核对许可原件，可反复补收")
    p_perm.add_argument("--base", required=True)
    p_perm.add_argument("--out", required=True)
    p_perm.add_argument("--api", choices=("xy", "yx"), default="xy")
    p_perm.add_argument("--timeout", type=float, default=15.0)
    p_perm.add_argument("--only-pending", action="store_true",
                        help="只补 pending，不重试 rejected（默认 rejected 也重试，以便来源修订后恢复）")
    p_perm.set_defaults(func=cmd_permits)

    p_rep = sub.add_parser("report", help="生成覆盖、归属、许可交付物")
    p_rep.add_argument("--out", required=True)
    p_rep.set_defaults(func=cmd_report)

    p_aud = sub.add_parser("audit", help="（操作员）用来源查询足迹核对覆盖结论")
    p_aud.add_argument("--base", required=True)
    p_aud.add_argument("--out", required=True)
    p_aud.set_defaults(func=cmd_audit)

    p_pro = sub.add_parser("provenance", help="追查地块来自哪次查询")
    p_pro.add_argument("--parcel", required=True)
    p_pro.add_argument("--out", required=True)
    p_pro.set_defaults(func=cmd_provenance)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
