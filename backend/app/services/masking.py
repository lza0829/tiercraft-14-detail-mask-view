"""脱敏与对外导出。

隔离形态（择一）：独立脱敏快照表 mask_snapshot_rows，按「规则版本+批次」固化行，
与明文明细物理隔离；历史批次永不改写，规则变更只影响之后新生成的快照/导出。

失败闭形态（择一）：先写临时目录，抽样校验通过后原子改名（os.replace）落正式文件；
抽样检出明文店名则整次失败——不落正式文件、不写成功导出记录（只写失败记录）。

红线：贴源层、明细原表、分层任务与层间对账一律基于原文；本模块只读 layer_detail，
生成快照与导出时绝不回写明细原表。
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import random
import uuid
from datetime import datetime
from pathlib import Path

from app.db import DATA_DIR, connect, rows_to_dicts
from app.services.pipeline import get_domain

EXPORT_DIR = DATA_DIR / "exports"
SAMPLE_MIN = 20
SAMPLE_RATIO = 0.2


def mask_name(name: str, keep_head: int, keep_tail: int, mask_char: str) -> str:
    """店名保留首尾、中间掩码。与 v_detail_masked 视图的 SQL 实现保持一致。"""
    if not name:
        return name
    n = len(name)
    if n <= keep_head + keep_tail:
        return name[:keep_head] + mask_char * max(n - keep_head, 1)
    return name[:keep_head] + mask_char * (n - keep_head - keep_tail) + name[n - keep_tail:]


# ---------- 规则版本 ----------

def list_rules() -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(
            conn.execute("SELECT * FROM mask_rules ORDER BY version DESC").fetchall()
        )
    finally:
        conn.close()


def _current_rule(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mask_rules WHERE enabled=1 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def create_rule(name: str, keep_head: int, keep_tail: int, mask_char: str) -> dict:
    """新建一版规则并启用。旧版本保留不动，历史快照/导出不受影响。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("规则名称不能为空")
    keep_head = int(keep_head)
    keep_tail = int(keep_tail)
    if keep_head < 0 or keep_tail < 0 or keep_head + keep_tail < 1:
        raise ValueError("保留首尾字符数不能为负，且至少保留一位")
    if not mask_char or len(mask_char) != 1:
        raise ValueError("掩码字符必须为单个字符")
    conn = connect()
    try:
        row = conn.execute("SELECT COALESCE(MAX(version),0) AS v FROM mask_rules").fetchone()
        version = int(row["v"]) + 1
        conn.execute("UPDATE mask_rules SET enabled=0")
        cur = conn.execute(
            """
            INSERT INTO mask_rules(version, name, keep_head, keep_tail, mask_char, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (version, name, keep_head, keep_tail, mask_char),
        )
        conn.commit()
        return dict(
            conn.execute("SELECT * FROM mask_rules WHERE id=?", (int(cur.lastrowid),)).fetchone()
        )
    finally:
        conn.close()


def enable_rule(version: int) -> dict:
    """启用既有版本（只影响之后新生成的快照/导出，不改写历史）。"""
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM mask_rules WHERE version=?", (int(version),)).fetchone()
        if not row:
            raise ValueError("规则版本不存在")
        conn.execute("UPDATE mask_rules SET enabled=0")
        conn.execute("UPDATE mask_rules SET enabled=1 WHERE version=?", (int(version),))
        conn.commit()
        return dict(
            conn.execute("SELECT * FROM mask_rules WHERE version=?", (int(version),)).fetchone()
        )
    finally:
        conn.close()


# ---------- 脱敏快照（按规则版本+批次固化，物理隔离） ----------

def run_snapshot(domain_id: int) -> dict:
    """按当前启用规则生成一批脱敏快照。

    只读 layer_detail 原文，绝不回写；明细行数与快照行数同一趟读取、同一事务登记（同源）。
    """
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        rule = _current_rule(conn)
        if not rule:
            raise ValueError("未启用任何脱敏规则，请先在「脱敏规则」页启用一版规则")
        detail = conn.execute(
            "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        if not detail:
            raise ValueError("明细层为空，请先在「分层任务」执行贴源到明细")
        kh, kt, mc = int(rule["keep_head"]), int(rule["keep_tail"]), rule["mask_char"]
        rows = []
        for r in detail:
            plain = r["store_name"] or ""
            rows.append(
                (
                    int(rule["version"]),
                    domain_id,
                    r["biz_day"],
                    r["store_code"],
                    mask_name(plain, kh, kt, mc),
                    r["category"],
                    float(r["amount"]),
                    int(r["qty"]),
                    int(r["id"]),
                    hashlib.sha256(plain.encode("utf-8")).hexdigest(),
                )
            )
        cur = conn.execute(
            """
            INSERT INTO mask_batches(domain_id, rule_version, detail_rows, snapshot_rows, status, message)
            VALUES (?, ?, ?, ?, '成功', ?)
            """,
            (
                domain_id,
                int(rule["version"]),
                len(detail),
                len(rows),
                f"按规则 v{rule['version']}（{rule['name']}）固化 {len(rows)} 行",
            ),
        )
        batch_id = int(cur.lastrowid)
        conn.executemany(
            """
            INSERT INTO mask_snapshot_rows(
              batch_id, rule_version, domain_id, biz_day, store_code, store_name_masked,
              category, amount, qty, source_detail_id, source_name_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(batch_id, *row) for row in rows],
        )
        # 防御：快照行数必须与明文明细行数一致，否则整批回滚
        inserted = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM mask_snapshot_rows WHERE batch_id=?", (batch_id,)
            ).fetchone()["c"]
        )
        if inserted != len(detail):
            conn.rollback()
            raise ValueError("快照行数与明文明细行数不一致，批次已回滚")
        conn.commit()
        return get_batch(batch_id)
    finally:
        conn.close()


def get_batch(batch_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM mask_batches WHERE id=?", (int(batch_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_batches(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM mask_batches ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mask_batches WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


def list_snapshot_rows(batch_id: int) -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(
            conn.execute(
                "SELECT * FROM mask_snapshot_rows WHERE batch_id=? ORDER BY id", (int(batch_id),)
            ).fetchall()
        )
    finally:
        conn.close()


# ---------- 对外导出（失败闭） ----------

def run_export(domain_id: int, batch_id: int) -> dict:
    """失败闭导出：抽样校验通过才落正式文件（临时目录写入 + 原子改名）。

    任何校验失败：整次失败，不落正式文件、不写成功导出记录，只写失败记录。
    """
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        batch = conn.execute("SELECT * FROM mask_batches WHERE id=?", (int(batch_id),)).fetchone()
        if not batch or int(batch["domain_id"]) != int(domain_id):
            raise ValueError("快照批次不存在")
        batch = dict(batch)
        if batch["status"] != "成功":
            raise ValueError("快照批次未成功，不能导出")
        rule = conn.execute(
            "SELECT * FROM mask_rules WHERE version=?", (int(batch["rule_version"]),)
        ).fetchone()
        if not rule:
            raise ValueError("批次对应的规则版本不存在")
        rule = dict(rule)
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM mask_snapshot_rows WHERE batch_id=? ORDER BY id", (batch["id"],)
            ).fetchall()
        )

        def fail(reason: str, sample_checked: int = 0) -> dict:
            cur = conn.execute(
                """
                INSERT INTO export_records(
                  domain_id, batch_id, rule_version, status, fail_reason,
                  snapshot_rows, detail_rows, sample_checked
                ) VALUES (?, ?, ?, '失败', ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    batch["id"],
                    int(batch["rule_version"]),
                    reason,
                    int(batch["snapshot_rows"]),
                    int(batch["detail_rows"]),
                    sample_checked,
                ),
            )
            conn.commit()
            return dict(
                conn.execute(
                    "SELECT * FROM export_records WHERE id=?", (int(cur.lastrowid),)
                ).fetchone()
            )

        # 同源校验：快照表实际行数须与批次登记的快照行数、明文明细行数一致
        if len(rows) != int(batch["snapshot_rows"]) or int(batch["snapshot_rows"]) != int(
            batch["detail_rows"]
        ):
            return fail(
                f"快照行数（{len(rows)}）与批次登记不一致"
                f"（登记快照 {batch['snapshot_rows']} 行 / 明细 {batch['detail_rows']} 行）"
            )

        # 抽样明文检查（按批次号确定性抽样；检出明文即整次失败）
        k = min(len(rows), max(SAMPLE_MIN, int(len(rows) * SAMPLE_RATIO)))
        sample = random.Random(batch["id"]).sample(rows, k) if len(rows) > k else list(rows)
        kh, kt, mc = int(rule["keep_head"]), int(rule["keep_tail"]), rule["mask_char"]
        for r in sample:
            src = conn.execute(
                "SELECT store_name FROM layer_detail WHERE id=?", (int(r["source_detail_id"]),)
            ).fetchone()
            plain = (src["store_name"] if src else "") or ""
            if hashlib.sha256(plain.encode("utf-8")).hexdigest() != r["source_name_hash"]:
                return fail("抽样溯源失败：明细层已重建或快照被改动，请重新生成快照", k)
            if plain and r["store_name_masked"] == plain:
                return fail(f"抽样检出明文店名「{plain}」，整次导出失败", k)
            if r["store_name_masked"] != mask_name(plain, kh, kt, mc):
                return fail("抽样快照行与批次规则版本不符，疑似被篡改", k)

        # 内存完整物化导出内容：全部来自已固化快照，不触碰明文明细
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(["业务日", "门店编码", "门店名称", "品类", "金额", "件数"])
        for r in rows:
            writer.writerow(
                [
                    r["biz_day"],
                    r["store_code"],
                    r["store_name_masked"],
                    r["category"],
                    r["amount"],
                    r["qty"],
                ]
            )
        data = buf.getvalue().encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()

        # 先写临时目录，原子改名落正式文件
        tmp_dir = EXPORT_DIR / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        fname = (
            f"明细脱敏-域{domain_id}-批次{batch['id']}"
            f"-规则v{batch['rule_version']}-{stamp}-{uuid.uuid4().hex[:6]}.csv"
        )
        final_path = EXPORT_DIR / fname
        tmp_path = tmp_dir / (fname + ".tmp")
        try:
            with open(tmp_path, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, final_path)
            dir_fd = os.open(EXPORT_DIR, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return fail(f"写出正式文件失败：{exc}", k)

        cur = conn.execute(
            """
            INSERT INTO export_records(
              domain_id, batch_id, rule_version, status, fail_reason,
              snapshot_rows, detail_rows, sample_checked, file_path, content_hash
            ) VALUES (?, ?, ?, '成功', '', ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                batch["id"],
                int(batch["rule_version"]),
                int(batch["snapshot_rows"]),
                int(batch["detail_rows"]),
                k,
                f"exports/{fname}",
                digest,
            ),
        )
        conn.commit()
        return dict(
            conn.execute("SELECT * FROM export_records WHERE id=?", (int(cur.lastrowid),)).fetchone()
        )
    finally:
        conn.close()


def list_exports(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM export_records ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM export_records WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        return rows_to_dicts(rows)
    finally:
        conn.close()


def get_export(export_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM export_records WHERE id=?", (int(export_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def export_file_path(record: dict) -> Path:
    return EXPORT_DIR.parent / record["file_path"]


# ---------- 内部只读脱敏视图（非对账/分层数据源） ----------

def masked_view(domain_id: int | None = None, limit: int = 200) -> list[dict]:
    conn = connect()
    try:
        sql = "SELECT * FROM v_detail_masked"
        args: tuple = ()
        if domain_id is not None:
            sql += " WHERE domain_id=?"
            args = (domain_id,)
        sql += " ORDER BY id LIMIT ?"
        args = (*args, int(limit))
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()
