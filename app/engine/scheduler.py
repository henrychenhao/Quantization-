"""定时调度：心跳推送 / 尾盘信号扫描 / 收盘数据同步。

三个 job 均前置"交易日判断"（真实交易日历，新浪源，本地缓存 data/trade_dates.txt；
日历不可用时退化为"非周末"）。启动方式：python main.py schedule（常驻进程）。
"""
import os
from datetime import datetime, time

from apscheduler.schedulers.blocking import BlockingScheduler

from app import db
from app.account import core as account
from app.datasource.akshare_source import AkShareSource
from app.datasource.kline_store import load_local, refresh
from app.notify import wecom
from . import signals as signal_engine


def _ensure_notify(cfg: dict) -> None:
    """推送配置初始化（幂等，独立调 job 也能推）。"""
    wecom.init(cfg.get("push", {}))

CALENDAR_CACHE = os.path.join("data", "trade_dates.txt")


# ---------- 交易日历 ----------

def refresh_trade_calendar() -> list[str]:
    """从新浪拉取交易日历并缓存到本地（覆盖未来年份，失败时调用方自行降级）。"""
    import akshare as ak
    import pandas as pd
    df = ak.tool_trade_date_hist_sina()
    dates = [d.strftime("%Y-%m-%d") for d in pd.to_datetime(df["trade_date"])]
    os.makedirs(os.path.dirname(CALENDAR_CACHE), exist_ok=True)
    with open(CALENDAR_CACHE, "w") as f:
        f.write("\n".join(dates))
    return dates


def is_trade_day(day=None) -> bool:
    """是否交易日。优先查本地日历缓存，缓存缺失/过期时自动刷新一次。"""
    day = day or datetime.now()
    key = day.strftime("%Y-%m-%d")
    try:
        with open(CALENDAR_CACHE) as f:
            dates = {x.strip() for x in f if x.strip()}
        if key in dates:
            return True
        if dates and max(dates) >= key:      # 缓存覆盖当前日期，但当天不在日历 → 非交易日
            return False
    except FileNotFoundError:
        pass
    try:                                     # 缓存缺失或未覆盖 → 刷新
        return key in set(refresh_trade_calendar())
    except Exception:
        return day.weekday() < 5              # 日历不可用：退化为非周末


# ---------- 三个定时任务 ----------

def _last_prices(cfg: dict, codes: list[str]) -> dict[str, float]:
    """用本地 K 线缓存取最新收盘价（心跳/快照用，不实时拉数）。"""
    out = {}
    for code in codes:
        df = load_local(code, cfg["data"]["kline_dir"])
        if df is not None and not df.empty:
            out[code] = float(df["close"].iloc[-1])
    return out


def job_heartbeat(cfg: dict) -> None:
    """交易日 09:35：推送心跳（自选数/现金/持仓市值/总资产）并落资产快照。"""
    _ensure_notify(cfg)
    if not is_trade_day():
        print("[heartbeat] 非交易日，跳过")
        return
    items = db.list_watchlist()
    enabled = sum(1 for w in items if w["quant_enabled"])
    codes = [w["code"] for w in items]
    prices = _last_prices(cfg, codes)
    snap = account.snapshot(prices)
    content = wecom.heartbeat_msg(len(items), enabled,
                                  snap["cash"], snap["market_value"], snap["total"])
    status = wecom.push("每日心跳", content)
    print(f"[heartbeat] 监控{len(items)}只(开启{enabled}) 总资产¥{snap['total']:,.0f} push={status}")


def job_signal_scan(cfg: dict) -> None:
    """交易日 14:50：尾盘信号扫描（四道校验 → 落库 → 推送）。"""
    _ensure_notify(cfg)
    if not is_trade_day():
        print("[signal] 非交易日，跳过")
        return
    result = signal_engine.run_signals(cfg)
    print(f"[signal] 标的{result['watch']} 生成{len(result['generated'])} 跳过{len(result['skipped'])}")
    for s in result["skipped"]:
        print(f"  - 跳过 {s['code']}: {s['reason']}")


def job_data_sync(cfg: dict) -> None:
    """交易日 17:30：收盘后全量刷新自选标的 K 线，并结算信号质量。"""
    if not is_trade_day():
        print("[sync] 非交易日，跳过")
        return
    source = AkShareSource()
    for w in db.list_watchlist():
        try:
            n = refresh(w["code"], source, cfg["data"]["kline_dir"])
            print(f"[sync] {w['code']} K线刷新 {n} 根")
        except Exception as e:
            print(f"[sync] {w['code']} 刷新失败: {e}")
    try:
        from app.engine.outcome import settle_signal_outcomes
        n = settle_signal_outcomes(cfg)
        print(f"[sync] 信号质量结算更新 {n} 条")
    except Exception as e:
        print(f"[sync] 信号质量结算失败: {e}")
    try:
        from app.engine.backup import backup_data
        backup_data(cfg)
    except Exception as e:
        print(f"[sync] 数据备份失败: {e}")


def job_intraday(cfg: dict) -> None:
    """盘中轮询（默认每 5 分钟）：实时价追加今日 K 线后跑信号引擎，触发即推送。

    只在交易时段内执行（09:30-11:30 / 13:00-15:00），非时段静默返回。
    """
    if not is_trade_day():
        print("[intraday] 非交易日，跳过")
        return
    now = datetime.now().time()
    am = time(9, 30) <= now <= time(11, 30)
    pm = time(13, 0) <= now <= time(15, 0)
    if not (am or pm):
        print("[intraday] 非交易时段，跳过")
        return
    result = signal_engine.run_signals(cfg, spot_mode=True)
    print(f"[intraday] 标的{result['watch']} 生成{len(result['generated'])} 跳过{len(result['skipped'])}")


# ---------- 启动 ----------

def start_scheduler(cfg: dict) -> None:
    sch = BlockingScheduler(timezone="Asia/Shanghai")
    jobs = cfg.get("schedule", {})
    sch.add_job(job_heartbeat, "cron", args=[cfg],
                **jobs.get("heartbeat", {"hour": 9, "minute": 35}),
                id="heartbeat", misfire_grace_time=600)
    sch.add_job(job_signal_scan, "cron", args=[cfg],
                **jobs.get("eod_signal", {"hour": 14, "minute": 50}),
                id="eod_signal", misfire_grace_time=600)
    sch.add_job(job_data_sync, "cron", args=[cfg],
                **jobs.get("data_sync", {"hour": 17, "minute": 30}),
                id="data_sync", misfire_grace_time=3600)
    intra = int(jobs.get("intraday", 0) or 0)
    if intra > 0:
        # 盘中实时扫描：交易时段内每 N 分钟一次（job 内部再过滤真实时段）
        sch.add_job(job_intraday, "cron", args=[cfg],
                    hour="9-11,13-14", minute=f"*/{intra}",
                    id="intraday", misfire_grace_time=600)
        print(f"调度器：盘中轮询每 {intra} 分钟（交易时段生效）")
    print(f"调度器已启动（交易日：心跳 09:35 / 尾盘信号 14:50 / 数据同步 17:30）"
          f"  Ctrl+C 退出")
    try:
        sch.start()
    except (KeyboardInterrupt, SystemExit):
        print("调度器已停止")