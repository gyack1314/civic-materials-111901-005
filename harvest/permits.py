"""许可原件下载、页内身份核对与补收。

契约要点：
- 许可为独立 PDF 原件，页内文字声明 facility，不以 URL 就认定归属；
- 一个设施的许可只登记一次（permit 表主键为 facility），同址多地块共享同一登记；
- 下载返回的设施代码与目录不符时隔离留证、等待核实，不能拿可打开的 PDF 充数；
- 暂缺/被拒不影响任何空间观测；之后补收成功只更新 permit 表，
  使设施转为「资料齐备」，不改动范围证明（catalog_queries/scope_eval 不被触碰）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .client import SourceClient, SourceError
from .store import Store, now

FACILITY_DECL = re.compile(r"facility\s*[=:]\s*([A-Za-z0-9_.\-]+)")


class PermitError(RuntimeError):
    pass


def _current_revision(store: Store) -> int:
    revisions = store.list_boundary_revisions()
    if not revisions:
        raise PermitError("采集库中还没有市域边界，请先运行空间采集")
    return max(revisions)


def in_scope_facilities(store: Store, revision: int) -> dict[str, str]:
    """当前市域内设施 -> 许可相对地址（按设施去重，多地块只登记一次）。"""
    scopes = {row["parcel"] for row in store.list_scope_eval(revision) if row["in_scope"]}
    result: dict[str, str] = {}
    for row in store.list_parcels():
        if row["parcel"] in scopes:
            result.setdefault(row["facility"], row["permit_url"])
    return result


def extract_declared_facility(pdf_bytes: bytes) -> str | None:
    """解析 PDF 页内文字，提取声明的 facility 代码。

    延迟导入 fitz：只有处理许可时才需要 PyMuPDF，空间采集不依赖它。
    """
    import fitz

    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
    match = FACILITY_DECL.search(text)
    return match.group(1) if match else None


def backfill_permits(
    store: Store,
    client: SourceClient,
    output_dir: str | Path,
    *,
    only_pending: bool = False,
) -> dict[str, str]:
    """对市域内设施登记并核对许可；返回 facility -> 最终状态。

    only_pending=False（默认）时，pending 与 rejected 都会重试——来源修订后
    被拒许可可能恢复身份（契约 phase=2 恢复 F-A）；verified 绝不重复下载、
    也不允许被覆盖。
    """
    output_dir = Path(output_dir)
    permit_dir = output_dir / "permits"
    quarantine_dir = output_dir / "quarantine"
    permit_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    revision = _current_revision(store)
    # 换界/来源修订后，旧分代验证过的许可不能继续直接采信，转 pending 重新核实；
    # 当前分代已验证的原件不重复下载。
    store.reset_stale_permits(revision)
    facilities = in_scope_facilities(store, revision)
    result: dict[str, str] = {}
    for facility, permit_url in sorted(facilities.items()):
        store.upsert_permit_request(facility, permit_url)
        row = store.get_permit(facility)
        if row["status"] == "verified" and row["verified_revision"] == revision:
            result[facility] = "verified"
            continue
        if only_pending and row["status"] != "pending":
            result[facility] = row["status"]
            continue
        result[facility] = _fetch_and_verify(
            store, client, facility, permit_url, permit_dir, quarantine_dir, revision
        )
    return result


def _fetch_and_verify(
    store: Store,
    client: SourceClient,
    facility: str,
    permit_url: str,
    permit_dir: Path,
    quarantine_dir: Path,
    revision: int,
) -> str:
    try:
        pdf_bytes, http_url = client.get_permit(permit_url)
    except SourceError as exc:
        store.update_permit(facility, "pending", reason=f"下载失败，待补收：{exc}")
        return "pending"

    if not pdf_bytes.startswith(b"%PDF-"):
        store.update_permit(facility, "rejected", reason="响应内容不是 PDF 文件")
        return "rejected"

    digest = hashlib.sha256(pdf_bytes).hexdigest()
    try:
        declared = extract_declared_facility(pdf_bytes)
    except Exception as exc:  # fitz 解析失败等：文件不可作为原件采信
        qpath = quarantine_dir / f"{facility}-{int(now()*1000)}.pdf"
        qpath.write_bytes(pdf_bytes)
        store.update_permit(
            facility, "rejected", quarantine_path=str(qpath), bytes_sha256=digest,
            http_url=permit_url, reason=f"PDF 无法解析，隔离待核实：{exc}",
        )
        return "rejected"

    if declared is None:
        qpath = quarantine_dir / f"{facility}-{int(now()*1000)}.pdf"
        qpath.write_bytes(pdf_bytes)
        store.update_permit(
            facility, "rejected", quarantine_path=str(qpath), declared_facility=None,
            bytes_sha256=digest, http_url=permit_url,
            reason="PDF 页内未声明 facility 代码，无法确认归属，隔离待核实",
        )
        return "rejected"

    if declared != facility:
        # 身份不符：原件隔离留证（保留追查线索），状态保持 rejected 等待来源核实。
        qpath = quarantine_dir / f"{facility}-declared-{declared}-{int(now()*1000)}.pdf"
        qpath.write_bytes(pdf_bytes)
        store.update_permit(
            facility, "rejected", quarantine_path=str(qpath), declared_facility=declared,
            bytes_sha256=digest, http_url=permit_url,
            reason=f"页内声明 facility={declared}，与目录登记的 {facility} 不符，等待核实",
        )
        return "rejected"

    target = permit_dir / f"{facility}.pdf"
    target.write_bytes(pdf_bytes)
    store.update_permit(
        facility, "verified", stored_path=str(target), declared_facility=declared,
        bytes_sha256=digest, http_url=permit_url, reason=None,
        verified_revision=revision,
    )
    return "verified"
