"""入口：A股量化系统命令行。

用法：
    python main.py backtest --code 513530 --strategy double_ma \
        --start 2023-10-21 --end 2026-10-21 --capital 100000
    python main.py serve --port 8000        # 启动 Web 回测工作台
"""
import argparse

from app.backtest.engine import Fees, run_backtest
from app.backtest.report import print_report
from app.config import load_config
from app.datasource.akshare_source import AkShareSource
from app.datasource.kline_store import get_or_fetch
from app.strategy.registry import get as get_strategy


def cmd_backtest(args, cfg: dict) -> None:
    source = AkShareSource()
    kline_dir = cfg["data"]["kline_dir"]

    df = get_or_fetch(args.code, source, kline_dir, start=args.start, end=args.end)
    if df is None or df.empty:
        raise SystemExit(f"未取到 {args.code} 的行情数据，请检查代码与网络。")

    strategy = get_strategy(args.strategy)
    df = strategy.compute(df)                       # 先算好指标
    res = run_backtest(df, strategy, args.capital,
                       Fees(**cfg["fees"]), code=args.code)
    print_report(res, args.capital)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="quant", description="A股量化系统 (P1)")
    sub = p.add_subparsers(dest="command", required=True)

    bt = sub.add_parser("backtest", help="单策略回测")
    bt.add_argument("--code", required=True, help="标的代码，如 513530 / 002156")
    bt.add_argument("--strategy", default="double_ma", help="策略 code，默认 double_ma")
    bt.add_argument("--start", default=None, help="起始日 YYYY-MM-DD，默认全量")
    bt.add_argument("--end", default=None, help="结束日 YYYY-MM-DD，默认最新")
    bt.add_argument("--capital", type=float, default=100000.0, help="初始资金，默认 100000")

    sv = sub.add_parser("serve", help="启动 Web 控制台（回测工作台）")
    sv.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    sv.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")

    sc = sub.add_parser("schedule", help="启动定时调度（心跳/尾盘信号/数据同步，常驻）")
    return p


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config()
    if args.command == "backtest":
        cmd_backtest(args, cfg)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "schedule":
        cmd_schedule(cfg)


def cmd_schedule(cfg: dict) -> None:
    from app.engine.scheduler import start_scheduler
    start_scheduler(cfg)


def cmd_serve(args) -> None:
    import uvicorn
    print(f"回测工作台启动: http://{args.host}:{args.port}  (Ctrl+C 退出)")
    uvicorn.run("app.web.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()