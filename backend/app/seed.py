from __future__ import annotations

import json
from pathlib import Path

from app.db import FIXTURES_DIR, connect


SCHEMA = """
CREATE TABLE IF NOT EXISTS domains (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  biz_key TEXT NOT NULL,
  fixture_file TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS layer_paste (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  raw_json TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_detail (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  clean_flag TEXT NOT NULL,
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_summary (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  amount_sum REAL NOT NULL,
  qty_sum INTEGER NOT NULL,
  line_count INTEGER NOT NULL,
  UNIQUE(domain_id, biz_day, store_code),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS layer_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  in_count INTEGER NOT NULL DEFAULT 0,
  out_count INTEGER NOT NULL DEFAULT 0,
  drop_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  paste_count INTEGER NOT NULL,
  detail_count INTEGER NOT NULL,
  summary_line_sum INTEGER NOT NULL,
  key_mismatch INTEGER NOT NULL,
  amount_drift REAL NOT NULL,
  status TEXT NOT NULL,
  detail_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 脱敏规则：按版本固化，只增不改；启用哪一版只影响之后新生成的快照/导出
CREATE TABLE IF NOT EXISTS mask_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  keep_head INTEGER NOT NULL DEFAULT 1,
  keep_tail INTEGER NOT NULL DEFAULT 1,
  mask_char TEXT NOT NULL DEFAULT '*',
  enabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 脱敏快照批次：明细行数与快照行数在同一趟读取、同一事务内登记（同源）
CREATE TABLE IF NOT EXISTS mask_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  detail_rows INTEGER NOT NULL,
  snapshot_rows INTEGER NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(domain_id) REFERENCES domains(id)
);

-- 脱敏快照表：按规则版本+批次固化行，与明文明细物理隔离；只存脱敏结果与溯源指针，不存明文店名
CREATE TABLE IF NOT EXISTS mask_snapshot_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  domain_id INTEGER NOT NULL,
  biz_day TEXT NOT NULL,
  store_code TEXT NOT NULL,
  store_name_masked TEXT NOT NULL,
  category TEXT NOT NULL,
  amount REAL NOT NULL,
  qty INTEGER NOT NULL,
  source_detail_id INTEGER NOT NULL,
  source_name_hash TEXT NOT NULL,
  FOREIGN KEY(batch_id) REFERENCES mask_batches(id)
);

-- 导出记录：规则版本、成败、失败原因、快照行数等均由本趟批次同源派生
CREATE TABLE IF NOT EXISTS export_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id INTEGER NOT NULL,
  batch_id INTEGER NOT NULL,
  rule_version INTEGER NOT NULL,
  status TEXT NOT NULL,
  fail_reason TEXT NOT NULL DEFAULT '',
  snapshot_rows INTEGER NOT NULL,
  detail_rows INTEGER NOT NULL,
  sample_checked INTEGER NOT NULL DEFAULT 0,
  file_path TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES mask_batches(id)
);

-- 内部只读脱敏视图：按当前启用规则即时脱敏，仅供查看；
-- 禁止作为层间对账或分层任务的数据源（它们一律基于 layer_detail 原文）。
-- 注意：本视图不是快照，历史批次的脱敏结果以 mask_snapshot_rows 为准。
CREATE VIEW IF NOT EXISTS v_detail_masked AS
SELECT
  d.id AS id,
  d.domain_id AS domain_id,
  d.biz_day AS biz_day,
  d.store_code AS store_code,
  CASE
    WHEN length(d.store_name) = 0 THEN ''
    WHEN length(d.store_name) <= r.keep_head + r.keep_tail THEN
      substr(d.store_name, 1, r.keep_head) ||
      replace(hex(zeroblob(max(length(d.store_name) - r.keep_head, 1))), '00', r.mask_char)
    ELSE
      substr(d.store_name, 1, r.keep_head) ||
      replace(hex(zeroblob(length(d.store_name) - r.keep_head - r.keep_tail)), '00', r.mask_char) ||
      substr(d.store_name, length(d.store_name) - r.keep_tail + 1, r.keep_tail)
  END AS store_name_masked,
  d.category AS category,
  d.amount AS amount,
  d.qty AS qty,
  d.clean_flag AS clean_flag,
  r.version AS rule_version
FROM layer_detail d
JOIN mask_rules r ON r.enabled = 1;
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT COUNT(*) AS c FROM domains").fetchone()
        if int(row["c"]) == 0:
            _seed_domain(conn)
        rule_row = conn.execute("SELECT COUNT(*) AS c FROM mask_rules").fetchone()
        if int(rule_row["c"]) == 0:
            _seed_mask_rule(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_mask_rule(conn) -> None:
    conn.execute(
        """
        INSERT INTO mask_rules(version, name, keep_head, keep_tail, mask_char, enabled)
        VALUES (1, '店名保留首尾中间掩码', 1, 1, '*', 1)
        """
    )


def _seed_domain(conn) -> None:
    fixture = "门店日销售-贴源.jsonl"
    cur = conn.execute(
        """
        INSERT INTO domains(name, biz_key, fixture_file, description)
        VALUES (?, ?, ?, ?)
        """,
        (
            "门店日销售",
            "业务日+门店编码",
            fixture,
            "演示贴源装载、明细清洗与按门店日汇总",
        ),
    )
    domain_id = int(cur.lastrowid)
    path = FIXTURES_DIR / fixture
    if not path.exists():
        # 仓库内相对路径兜底
        alt = Path(__file__).resolve().parents[2] / "fixtures" / fixture
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        conn.execute(
            """
            INSERT INTO layer_paste(
              domain_id, biz_day, store_code, store_name, category, amount, qty, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                obj.get("业务日") or "",
                obj.get("门店编码") or "",
                obj.get("门店名称") or "",
                obj.get("品类") or "",
                float(obj.get("金额") or 0),
                int(obj.get("件数") or 0),
                json.dumps(obj, ensure_ascii=False),
            ),
        )
