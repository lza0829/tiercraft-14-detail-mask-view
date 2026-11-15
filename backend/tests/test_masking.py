"""脱敏规则版本化、快照物理隔离、失败闭导出、原文红线的回归测试。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, seed  # noqa: E402
from app.services import masking, pipeline  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(masking, "EXPORT_DIR", tmp_path / "exports")
    seed.init_db()
    pipeline.run_paste_to_detail(1)
    return tmp_path


def _detail_names():
    conn = db.connect()
    try:
        return [
            r["store_name"]
            for r in conn.execute(
                "SELECT store_name FROM layer_detail WHERE domain_id=1 ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()


def _snapshot_masked_names(batch_id):
    conn = db.connect()
    try:
        return [
            r["store_name_masked"]
            for r in conn.execute(
                "SELECT store_name_masked FROM mask_snapshot_rows WHERE batch_id=? ORDER BY id",
                (batch_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_mask_name_rule():
    assert masking.mask_name("城东店", 1, 1, "*") == "城*店"
    assert masking.mask_name("城西旗舰店", 1, 1, "*") == "城***店"
    assert masking.mask_name("城东", 1, 1, "*") == "城*"
    assert masking.mask_name("城", 1, 1, "*") == "城*"
    assert masking.mask_name("", 1, 1, "*") == ""
    assert masking.mask_name("城东店", 1, 2, "#") == "城##"  # 首尾合计不短于全长时保首掩余


def test_snapshot_frozen_by_rule_version(env):
    batch1 = masking.run_snapshot(1)
    assert batch1["rule_version"] == 1
    assert batch1["detail_rows"] == 5
    assert batch1["snapshot_rows"] == 5
    names1 = _snapshot_masked_names(batch1["id"])
    assert "城*店" in names1
    assert "城东店" not in names1 and "城西店" not in names1

    # 新建规则版本只影响之后的新快照，历史批次行保持不变
    masking.create_rule("店名井号掩码", 1, 1, "#")
    batch2 = masking.run_snapshot(1)
    assert batch2["rule_version"] == 2
    assert "城#店" in _snapshot_masked_names(batch2["id"])
    assert _snapshot_masked_names(batch1["id"]) == names1
    assert masking.get_batch(batch1["id"])["rule_version"] == 1

    # 明细原表仍是原文，未被回写
    assert set(_detail_names()) == {"城东店", "城西店"}


def test_enable_older_rule_version(env):
    masking.create_rule("店名井号掩码", 1, 1, "#")
    masking.enable_rule(1)
    batch = masking.run_snapshot(1)
    assert batch["rule_version"] == 1
    assert "城*店" in _snapshot_masked_names(batch["id"])


def test_export_success_fail_closed(env):
    batch = masking.run_snapshot(1)
    rec = masking.run_export(1, batch["id"])
    assert rec["status"] == "成功"
    assert rec["rule_version"] == 1
    # 同源：导出记录行数 = 批次登记 = 明文明细行数
    assert rec["snapshot_rows"] == rec["detail_rows"] == 5
    assert rec["sample_checked"] == 5

    path = masking.export_file_path(rec)
    assert path.exists()
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == rec["content_hash"]
    text = data.decode("utf-8")
    assert "城*店" in text
    assert "城东店" not in text and "城西店" not in text
    # 临时目录不留半截文件
    assert list((env / "exports" / "tmp").iterdir()) == []
    # 明细原表未被回写
    assert set(_detail_names()) == {"城东店", "城西店"}


def test_export_fails_on_plaintext_leak(env):
    batch = masking.run_snapshot(1)
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE mask_snapshot_rows SET store_name_masked='城东店' WHERE batch_id=? AND rowid=(SELECT min(rowid) FROM mask_snapshot_rows WHERE batch_id=?)",
            (batch["id"], batch["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    rec = masking.run_export(1, batch["id"])
    assert rec["status"] == "失败"
    assert "明文" in rec["fail_reason"]
    assert rec["file_path"] == ""
    # 不落正式文件、不写成功导出记录
    exports_dir = env / "exports"
    officials = [p for p in exports_dir.glob("*.csv")] if exports_dir.exists() else []
    assert officials == []
    assert [r["status"] for r in masking.list_exports(1)] == ["失败"]


def test_export_fails_on_count_mismatch(env):
    batch = masking.run_snapshot(1)
    conn = db.connect()
    try:
        conn.execute(
            "DELETE FROM mask_snapshot_rows WHERE rowid=(SELECT min(rowid) FROM mask_snapshot_rows WHERE batch_id=?)",
            (batch["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    rec = masking.run_export(1, batch["id"])
    assert rec["status"] == "失败"
    assert "不一致" in rec["fail_reason"]
    assert rec["file_path"] == ""


def test_export_fails_when_detail_rebuilt(env):
    batch = masking.run_snapshot(1)
    pipeline.run_paste_to_detail(1)  # 重建明细层，溯源指针失效
    rec = masking.run_export(1, batch["id"])
    assert rec["status"] == "失败"
    assert "重新生成快照" in rec["fail_reason"]


def test_rule_change_does_not_rewrite_history(env):
    batch1 = masking.run_snapshot(1)
    rec1 = masking.run_export(1, batch1["id"])
    path1 = masking.export_file_path(rec1)
    data1 = path1.read_bytes()

    masking.create_rule("店名井号掩码", 1, 1, "#")
    batch2 = masking.run_snapshot(1)
    rec2 = masking.run_export(1, batch2["id"])

    text2 = masking.export_file_path(rec2).read_bytes().decode("utf-8")
    assert "城#店" in text2
    # 历史导出文件与记录原样保留
    assert masking.export_file_path(rec1).read_bytes() == data1
    kept = masking.get_export(rec1["id"])
    assert kept["rule_version"] == 1
    assert kept["content_hash"] == rec1["content_hash"]


def test_reconcile_and_layers_stay_on_plaintext(env):
    masking.run_snapshot(1)
    pipeline.run_detail_to_summary(1)
    report = pipeline.run_reconcile(1)
    assert report["status"] == "一致"
    assert report["detail_count"] == 5
    # 分层各层仍是原文
    detail_rows = pipeline.layer_rows("detail", 1)
    assert {r["store_name"] for r in detail_rows} == {"城东店", "城西店"}
    # 内部只读脱敏视图是脱敏结果，但不影响明细原表
    view = masking.masked_view(1)
    assert view and all(r["store_name_masked"] != "城东店" for r in view)
    assert {r["store_name_masked"] for r in view} == {"城*店"}
    assert set(_detail_names()) == {"城东店", "城西店"}
