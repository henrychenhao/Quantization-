"""推送通道：企微群机器人 webhook（主）+ Server酱（预留）。

- 未配置 webhook 时，推送降级为"写 push_log 表 + 终端打印"（模拟模式），
  功能链路完整可测，配置 key 后即真实触达。
- 消息模板与参考系统对齐：心跳 / 交易信号。
"""
import os

import requests

from app import db

# 推送目标（init() 后生效；直接 import 调用的入口也应先 init）
wecom_webhook = os.environ.get("WECOM_WEBHOOK", "")


def init(push_cfg: dict | None = None) -> None:
    """初始化推送配置（幂等）。各入口（Web/调度器/信号引擎）调用一次即可。"""
    global wecom_webhook
    if push_cfg and push_cfg.get("wecom_webhook"):
        wecom_webhook = str(push_cfg["wecom_webhook"]).strip()
    elif os.environ.get("WECOM_WEBHOOK"):
        wecom_webhook = os.environ["WECOM_WEBHOOK"].strip()


def _send_wecom(content: str) -> str:
    """发送 markdown 消息到企微群机器人。返回状态串。"""
    if not wecom_webhook:
        return "mock"
    r = requests.post(wecom_webhook, json={"msgtype": "markdown", "markdown": {"content": content}},
                      timeout=8)
    ok = r.ok and r.json().get("errcode") == 0
    return "ok" if ok else f"fail:{r.text[:80]}"


def push(title: str, content: str, channel: str = "wecom") -> str:
    """统一入口：发送 + 落 push_log。返回发送状态。"""
    if channel == "wecom":
        status = _send_wecom(content)
    else:
        status = "mock"
    db.log_push(channel, title, content, status)
    return status


def heartbeat_msg(code_count: int, enabled_count: int, cash: float,
                  market_value: float, total: float) -> str:
    return (f"【每日心跳】<font color=\"info\">监控 {code_count} 只自选（开启 {enabled_count} 只）</font>\n"
            f"现金 {cash:,.0f} · 持仓市值 {market_value:,.0f} · 总资产 {total:,.0f}")


def signal_msg(code: str, name: str, side: str, strategy_name: str, reason: str,
               ref_price: float, suggest_shares: int, amount: float,
               cash: float | None = None) -> str:
    arrow = "🔴 拟买入" if side == "buy" else "🟢 拟卖出"
    return (f"【交易信号】{arrow} · {code} {name}\n"
            f"建议 {suggest_shares} 股（{suggest_shares // 100} 手）· 预计 {amount:,.0f} 元\n"
            f"现价 {ref_price:.2f} · {strategy_name}\n"
            f"触发：{reason}\n"
            + (f"可用现金 {cash:,.0f} · " if cash is not None else "")
            + "请自行决策下单，成交后在系统确认记账")