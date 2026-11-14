from __future__ import annotations

import json

from app.db import connect, rows_to_dicts


STEP_LABEL = {
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
}


def list_domains() -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(conn.execute("SELECT * FROM domains ORDER BY id").fetchall())
    finally:
        conn.close()


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
        }
    finally:
        conn.close()


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
            out.append(d)
        return out
    finally:
        conn.close()


def run_paste_to_detail(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_detail WHERE domain_id=?", (domain_id,))
        paste = conn.execute(
            "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        out = drop = 0
        for row in paste:
            store_name = (row["store_name"] or "").strip()
            amount = float(row["amount"])
            if not store_name or amount < 0:
                drop += 1
                continue
            conn.execute(
                """
                INSERT INTO layer_detail(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    row["biz_day"],
                    row["store_code"],
                    store_name,
                    row["category"],
                    amount,
                    int(row["qty"]),
                    "已清洗",
                ),
            )
            out += 1
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count)
            VALUES (?, 'paste_to_detail', '成功', ?, ?, ?, ?)
            """,
            (domain_id, f"清洗完成，剔除门店名为空或金额为负的行", len(paste), out, drop),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def run_detail_to_summary(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
        detail = conn.execute(
            "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        buckets: dict[tuple, dict] = {}
        for row in detail:
            key = (row["biz_day"], row["store_code"])
            b = buckets.setdefault(
                key,
                {"biz_day": row["biz_day"], "store_code": row["store_code"], "amount": 0.0, "qty": 0, "n": 0},
            )
            b["amount"] += float(row["amount"])
            b["qty"] += int(row["qty"])
            b["n"] += 1
        for b in buckets.values():
            conn.execute(
                """
                INSERT INTO layer_summary(
                  domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
            )
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count)
            VALUES (?, 'detail_to_summary', '成功', ?, ?, ?, 0)
            """,
            (domain_id, "按业务日+门店编码汇总", len(detail), len(buckets)),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary = conn.execute(
            "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchone()
        summary_line_sum = int(summary["s"])
        summary_amount = float(summary["a"])
        detail_amount = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
                (domain_id,),
            ).fetchone()["a"]
        )
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = round(summary_amount - detail_amount, 4)
        status = "一致" if key_mismatch == 0 and abs(amount_drift) < 0.0001 else "存在差异"
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
            }
            for r in reports
        ],
        "主题域": [{"编号": d["id"], "名称": d["name"], "业务键": d["biz_key"]} for d in domains],
    }
