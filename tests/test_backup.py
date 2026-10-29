"""数据备份测试：目录结构 / 数据库一致性 / 保留策略清理。

备份根目录 monkeypatch 到临时目录，绝不触碰真实 data/backup。
"""
import os
import sqlite3

from app import db
from app.engine import backup as backup_mod

CFG = {"data": {"kline_dir": "data/kline"}}


def test_backup_creates_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_mod, "BACKUP_ROOT", str(tmp_path))
    # 造一个真实数据：账本有流水，K 线目录有文件
    db.add_trade(None, "600519", "buy", 100.0, 200, 5.0)
    kdir = tmp_path / "src_kline"
    kdir.mkdir()
    (kdir / "600519.parquet").write_bytes(b"dummy")

    r = backup_mod.backup_data({**CFG, "data": {"kline_dir": str(kdir)}}, keep=7)
    assert r["db_ok"] is True
    assert r["kline_files"] == 1
    assert os.path.isfile(os.path.join(r["path"], "quant.db"))
    # 备份库可读且包含业务数据（在线备份一致性）
    bak = sqlite3.connect(os.path.join(r["path"], "quant.db"))
    trades = bak.execute("SELECT COUNT(*) FROM trade").fetchone()[0]
    bak.close()
    assert trades == 1


def test_backup_keeps_recent_only(tmp_path, monkeypatch):
    """今天+昨天往前共 6 个日期目录，keep=5 → 只清最早 1 个。"""
    monkeypatch.setattr(backup_mod, "BACKUP_ROOT", str(tmp_path))
    for d in ("2026-10-24", "2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28"):
        os.makedirs(os.path.join(str(tmp_path), d))
    r = backup_mod.backup_data(CFG, keep=5)
    remain = sorted(x for x in os.listdir(str(tmp_path)) if len(x) == 10)
    assert remain == ["2026-10-25", "2026-10-26", "2026-10-27", "2026-10-28",
                      "2026-10-29"]
    assert r["cleaned"] == ["2026-10-24"]


def test_backup_same_day_idempotent(tmp_path, monkeypatch):
    """同一天重复备份：覆盖当天目录，不产生第二个同名目录。"""
    monkeypatch.setattr(backup_mod, "BACKUP_ROOT", str(tmp_path))
    backup_mod.backup_data(CFG)
    backup_mod.backup_data(CFG)
    day_dirs = [x for x in os.listdir(str(tmp_path)) if len(x) == 10]
    assert len(day_dirs) == 1


def test_cleanup_skips_non_date_dirs(tmp_path, monkeypatch):
    """非日期命名的目录不参与清理（避免误删）；keep=1 时只留今天。"""
    monkeypatch.setattr(backup_mod, "BACKUP_ROOT", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "README"))
    for d in ("2026-10-20", "2026-10-21"):
        os.makedirs(os.path.join(str(tmp_path), d))
    backup_mod.backup_data(CFG, keep=1)
    assert os.path.isdir(os.path.join(str(tmp_path), "README"))
    assert sorted(x for x in os.listdir(str(tmp_path)) if len(x) == 10) == ["2026-10-29"]