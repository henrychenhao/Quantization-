"""数据备份：把账本库与 K 线目录快照到 data/backup/。

策略：按日期建目录（data/backup/YYYY-MM-DD/），同一天多次运行覆盖当天目录；
保留最近 keep 份（默认 14 天），自动清理更早目录。由收盘数据同步 job 调用。

数据库用 SQLite 在线备份（sqlite3.backup）保证与并发写入安全一致；
K 线 parquet 整目录快照拷贝。
"""
import os
import shutil
import sqlite3
from datetime import datetime

from app import db

BACKUP_ROOT = os.path.join("data", "backup")


def _cleanup_dir(keep: int) -> list[str]:
    """清理超过 keep 份的最早备份目录（按 YYYY-MM-DD 名排序）。返回被清理目录名。"""
    if keep <= 0 or not os.path.isdir(BACKUP_ROOT):
        return []
    dirs = sorted(
        d for d in os.listdir(BACKUP_ROOT)
        if len(d) == 10 and os.path.isdir(os.path.join(BACKUP_ROOT, d))
    )
    removed = []
    for d in dirs[:max(0, len(dirs) - keep)]:
        shutil.rmtree(os.path.join(BACKUP_ROOT, d), ignore_errors=True)
        removed.append(d)
    return removed


def backup_data(cfg: dict, keep: int = 14) -> dict:
    """执行一次全量备份，返回备份摘要。keep<=0 表示不清理旧备份。"""
    day = datetime.now().strftime("%Y-%m-%d")
    target = os.path.join(BACKUP_ROOT, day)
    os.makedirs(target, exist_ok=True)

    # 1) 数据库：SQLite 在线备份（与运行中写入并发安全）
    db_bak = os.path.join(target, "quant.db")
    db_ok = False
    try:
        src = sqlite3.connect(db.DB_PATH)
        dst = sqlite3.connect(db_bak)
        with dst:
            src.backup(dst)
        dst.close()
        src.close()
        db_ok = True
    except Exception as e:
        print(f"[backup] 数据库备份失败: {e}")

    # 2) K 线 parquet：整目录快照（当日覆盖）
    kline_dir = cfg["data"]["kline_dir"]
    kline_bak = os.path.join(target, "kline")
    kline_files = 0
    if os.path.isdir(kline_dir):
        if os.path.isdir(kline_bak):
            shutil.rmtree(kline_bak)
        shutil.copytree(kline_dir, kline_bak)
        kline_files = len(os.listdir(kline_bak))

    cleaned = _cleanup_dir(keep)
    print(f"[backup] {target}: db={'OK' if db_ok else 'FAIL'} kline={kline_files} 分片"
          f" 清理 {len(cleaned)} 份旧备份")
    return {"path": target, "db_ok": db_ok, "kline_files": kline_files, "cleaned": cleaned}