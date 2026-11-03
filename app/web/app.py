"""FastAPI 应用：回测工作台 + 自选监控 + 信号中心 + 账户账本。

启动：main.py serve
"""
import json
import os

import pandas as pd
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db
from app.account import core as account
from app.backtest.engine import Fees, run_backtest
from app.backtest.report import compute_metrics
from app.config import load_config
from app.datasource import industry_map, sector
from app.datasource.akshare_source import AkShareSource
from app.datasource.base import is_etf
from app.datasource.kline_store import get_or_fetch
from app.engine import signals as signal_engine
from app.engine.outcome import strategy_stats
from app.engine.recommend import CATEGORY, recommend
from app.notify import wecom
from app.strategy import registry

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE, "static")
TEMPLATE_DIR = os.path.join(BASE, "templates")

app = FastAPI(title="A股半自动量化")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)

cfg = load_config()
db.init_db()
db.seed_watchlist(cfg.get("watchlist", []))
db.init_cash(cfg.get("account", {}).get("initial_cash", 100000))
wecom.init(cfg.get("push", {}))

def _strategy_meta() -> list[dict]:
    """页面用策略列表（动态：含调参副本与调参后参数），供回测/自选/信号页渲染。"""
    return [{"code": k, "name": s.name, "params": dict(s.params)}
            for k, s in registry.all_strategies().items()]


def _to(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if isinstance(v, pd.Timestamp):
            out[k] = v.strftime("%Y-%m-%d")
        elif hasattr(v, "item"):
            out[k] = v.item()
        else:
            out[k] = v
    return out


def _last_prices(codes: list[str]) -> dict[str, float]:
    """用本地 K 线缓存取最新收盘价（避免每次实时拉数）。"""
    out = {}
    for code in codes:
        df = get_or_fetch(code, AkShareSource(), cfg["data"]["kline_dir"])
        if df is not None and not df.empty:
            out[code] = float(df["close"].iloc[-1])
    return out


# ---------- 页面 ----------

@app.get("/")
def root():
    return RedirectResponse("/backtest")


@app.get("/backtest")
def page_backtest(request: Request):
    return templates.TemplateResponse(request, "backtest.html", {
        "active": "backtest", "strategies": _strategy_meta(), "watchlist": _watch_presets(),
    })


@app.get("/watchlist")
def page_watchlist(request: Request):
    return templates.TemplateResponse(request, "watchlist.html", {
        "active": "watchlist", "strategies": _strategy_meta(),
    })


@app.get("/signals")
def page_signals(request: Request):
    return templates.TemplateResponse(request, "signals.html", {"active": "signals", "strategies": _strategy_meta()})


@app.get("/account")
def page_account(request: Request):
    pos = account.positions()
    prices = _last_prices(list(pos.keys()))
    positions = [
        {"code": c, "name": _name_of(c), "shares": p["shares"], "avg_cost": p["avg_cost"],
         "last_price": prices.get(c, 0), "market_value": prices.get(c, 0) * p["shares"]}
        for c, p in pos.items()
    ]
    market_value = sum(p["market_value"] for p in positions)
    cash_now = account.cash()
    init_cash = float(cfg.get("account", {}).get("initial_cash", 100000))
    total_now = cash_now + market_value
    pnl = total_now - init_cash
    snap_rows = [{"date": s["snap_date"], "cash": float(s["cash"]),
                  "market_value": float(s["market_value"]), "total": float(s["total"])}
                 for s in db.list_snapshots(90)]
    return templates.TemplateResponse(request, "account.html", {
        "active": "account", "cash": cash_now, "market_value": market_value,
        "total": total_now, "init_cash": init_cash, "pnl": pnl,
        "pnl_pct": (pnl / init_cash) if init_cash else 0,
        "positions": positions,
        "trades": db.list_trades(), "snapshots": snap_rows,
    })


# ---------- 页面：AI 分析 ----------

@app.get("/ai")
def page_ai(request: Request):
    return templates.TemplateResponse(request, "ai.html", {
        "active": "ai",
        "watchlist": [{"code": w["code"], "name": w["name"] or w["code"]} for w in db.list_watchlist()],
        "llm_ready": bool((cfg.get("llm", {}).get("api_key") or "").strip()),
        "llm_model": cfg.get("llm", {}).get("model", "deepseek-chat"),
    })


# ---------- API: watchlist ----------

class WatchAdd(BaseModel):
    code: str
    name: str = ""
    strategies: list[str] = []


@app.get("/api/watchlist")
def api_watchlist():
    pos = account.positions()
    items = [{
        "id": w["id"], "code": w["code"], "name": w["name"],
        "quant_enabled": bool(w["quant_enabled"]),
        "strategies": json.loads(w["strategies"] or "[]"),
        "industry": w["industry"] or "",
        "is_etf": is_etf(w["code"]),
        "shares": pos.get(w["code"], {}).get("shares"),
    } for w in db.list_watchlist()]
    return {"items": items}


@app.post("/api/watchlist")
def api_watch_add(req: WatchAdd):
    code = req.code.strip()
    if not code:
        return JSONResponse(status_code=400, content={"error": "代码不能为空"})
    name = req.name.strip()
    if not name:
        # 自动取名：从新浪实时接口拿真实证券名称
        try:
            quotes = AkShareSource().get_spots([code])
            name = quotes.get(code, {}).get("name", "") or code
        except Exception:
            name = code
    try:
        db.add_watchlist(code, name, req.strategies)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    return {"ok": True}


@app.get("/api/watchlist/quotes")
def api_watchlist_quotes():
    """自选标的实时行情（新浪批量接口），供页面轮询刷新；顺带补齐缺失名称。"""
    codes = [w["code"] for w in db.list_watchlist()]
    quotes = AkShareSource().get_spots(codes)
    # 名称缺失/为代码的存量行，用行情名称补全
    for w in db.list_watchlist():
        q = quotes.get(w["code"])
        if q and q.get("name") and (not w["name"] or w["name"] == w["code"]):
            db.update_watchlist(w["id"], name=q["name"])
    return {"items": [
        {"code": c, **q} for c, q in quotes.items()
    ]}


@app.post("/api/watchlist/{wid}/toggle")
def api_watch_toggle(wid: int):
    row = _watch_row(wid)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "自选不存在"})
    db.update_watchlist(wid, quant_enabled=1 if not row["quant_enabled"] else 0)
    return {"ok": True}


@app.delete("/api/watchlist/{wid}")
def api_watch_del(wid: int):
    db.remove_watchlist(wid)
    return {"ok": True}


class ApplyStrategies(BaseModel):
    strategies: list[str]


@app.post("/api/watchlist/{wid}/strategies")
def api_watch_strategies(wid: int, body: ApplyStrategies):
    row = _watch_row(wid)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "自选不存在"})
    db.update_watchlist(wid, strategies=body.strategies)
    return {"ok": True, "strategies": body.strategies}


class SetIndustry(BaseModel):
    industry: str = ""


@app.post("/api/watchlist/{wid}/industry")
def api_watch_industry(wid: int, body: SetIndustry):
    """设置所属行业（AI 板块联动用，可空）。"""
    if _watch_row(wid) is None:
        return JSONResponse(status_code=404, content={"error": "自选不存在"})
    db.update_watchlist(wid, industry=body.industry.strip())
    return {"ok": True, "industry": body.industry.strip()}


@app.get("/api/industries")
def api_industries():
    """同花顺行业名单（约 90 个），供自选页选择所属行业。"""
    return {"items": sector.get_industries()}


@app.post("/api/watchlist/{wid}/auto_industry")
def api_watch_auto_industry(wid: int):
    """单只自动识别所属行业（同花顺成分映射）。

    映射缓存缺失时自动构建一次（约 30 秒）；识别失败（ETF/新股）返回 400。
    """
    row = _watch_row(wid)
    if row is None:
        return JSONResponse(status_code=404, content={"error": "自选不存在"})
    industry = industry_map.detect_industry(row["code"])
    if industry is None and not industry_map.map_status()["built"]:
        m = industry_map.build_map()
        industry = m.get(row["code"])
    if industry is None:
        return JSONResponse(status_code=400,
                            content={"error": f"未能识别 {row['code']} 的行业（ETF 无行业概念或不在映射中）"})
    db.update_watchlist(wid, industry=industry)
    return {"ok": True, "industry": industry}


@app.post("/api/industries/auto_all")
def api_industries_auto_all():
    """构建/加载全市场行业映射，并覆盖式回填全部非 ETF 自选的行业。

    幂等：映射已构建时直接复用（秒回）；force 重抓约 30 秒。
    """
    m = industry_map.build_map()
    if not m:
        return JSONResponse(status_code=503,
                            content={"error": "行业映射构建失败（行业名单接口不可用），请稍后重试"})
    updated, unknown = [], []
    for w in db.list_watchlist():
        if is_etf(w["code"]):
            continue
        ind = m.get(w["code"])
        if ind:
            if (w["industry"] or "") != ind:
                db.update_watchlist(w["id"], industry=ind)
            updated.append({"code": w["code"], "name": w["name"], "industry": ind})
        else:
            unknown.append(w["code"])
    return {"ok": True, "stocks": len(m), "detected": len(updated),
            "items": updated, "unknown": unknown}


@app.get("/api/recommend")
def api_recommend(code: str, capital: float = 100000.0):
    try:
        return recommend(cfg, code, capital)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"推荐计算失败：{e}"})


# ---------- API: signals ----------

class RunSignals(BaseModel):
    dry_run: bool = False


@app.get("/api/signals")
def api_signals():
    db.expire_stale_signals()       # 惰性过期：打开信号中心即见最新状态
    items = []
    for s in db.list_signals():
        items.append({**dict(s), "name": _name_of(s["code"])})
    return {"items": items}


@app.get("/api/signal_stats")
def api_signal_stats():
    """各策略近期战绩（已结算信号聚合），供信号中心展示与 AI 引用。"""
    return {"items": strategy_stats()}


@app.get("/api/signal_frequency")
def api_signal_frequency(days: int = 30):
    """近 N 天信号触发频次（策略×标的）：信号通胀体检，复盘用。"""
    names = {c: s.name for c, s in registry.all_strategies().items()}
    items = []
    for r in db.signal_frequency(max(1, min(days, 365))):
        items.append({**dict(r), "name": _name_of(r["code"]),
                      "strategy_name": names.get(r["strategy"], r["strategy"])})
    return {"items": items, "days": days}


@app.get("/api/health/source")
def api_health_source():
    """数据源健康探测：新浪实时接口拉一只权重股，返回可用性与耗时。"""
    import time as _t
    t0 = _t.time()
    try:
        quotes = AkShareSource().get_spots(["000001"])
        price = quotes.get("000001", {}).get("price", 0)
        ok = price > 0
    except Exception:
        ok = False
    return {"source": "新浪", "ok": ok, "latency_ms": round((_t.time() - t0) * 1000)}


@app.get("/api/scheduler_state")
def api_scheduler_state():
    """调度器最近执行状态 + 各任务计划时间（页面透明化展示）。"""
    import json as _json
    import os as _os
    state: dict = {}
    try:
        with open(_os.path.join("data", "scheduler_state.json")) as f:
            state = _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        pass
    jobs = cfg.get("schedule", {})

    def _fmt(j, default):
        j = jobs.get(j, default)
        return f"{int(j.get('hour', 0)):02d}:{int(j.get('minute', 0)):02d}"

    return {
        "state": state,
        "plan": {
            "heartbeat": "交易日 " + _fmt("heartbeat", {"hour": 9, "minute": 35}),
            "signal": "交易日 " + _fmt("eod_signal", {"hour": 14, "minute": 50}),
            "sync": "交易日 " + _fmt("data_sync", {"hour": 17, "minute": 30}),
            "intraday": f"交易时段每 {jobs.get('intraday', 0) or '—'} 分钟"
                        if jobs.get("intraday") else "已关闭",
        },
    }


@app.post("/api/signals/run")
def api_signals_run(body: RunSignals):
    try:
        result = signal_engine.run_signals(cfg, dry_run=body.dry_run)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    return result


class FollowReq(BaseModel):
    price: float
    shares: int
    fee: float = 0.0
    note: str = ""


@app.post("/api/signals/{sid}/follow")
def api_signal_follow(sid: int, body: FollowReq):
    try:
        return account.follow_signal(sid, body.price, body.shares, body.fee, body.note)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


class UpdateTradeReq(BaseModel):
    price: float
    shares: int
    fee: float = 0.0
    note: str | None = None


class ManualTradeReq(BaseModel):
    code: str
    side: str
    price: float
    shares: int
    fee: float = 0.0
    note: str = ""


@app.post("/api/trades")
def api_manual_trade(body: ManualTradeReq):
    """手动记账：不依赖信号直接记一笔成交，现金与持仓按全部流水重推。"""
    try:
        return account.manual_trade(body.code.strip(), body.side, body.price,
                                    body.shares, body.fee, body.note)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.put("/api/trades/{tid}")
def api_update_trade(tid: int, body: UpdateTradeReq):
    """账本纠错：修改一笔流水后全量重推现金与持仓。"""
    try:
        return account.update_trade(tid, body.price, body.shares, body.fee, body.note)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})


@app.post("/api/signals/{sid}/ignore")
def api_signal_ignore(sid: int):
    db.set_signal_status(sid, "ignored")
    return {"ok": True}


# ---------- API: backtest ----------

class BacktestReq(BaseModel):
    code: str
    strategies: list[str]
    start: str | None = None
    end: str | None = None
    capital: float = 100000.0


@app.post("/api/backtest")
def api_backtest(req: BacktestReq):
    try:
        df = get_or_fetch(req.code, AkShareSource(), cfg["data"]["kline_dir"],
                          start=req.start, end=req.end)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"行情获取失败：{e}"})
    if df is None or df.empty:
        return JSONResponse(status_code=400, content={"error": "无行情数据"})

    fees = Fees(**cfg.get("fees", {}))
    rows, curves, drawdown = [], {}, {}
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    benchmark = None

    insts = registry.all_strategies()
    for sc in req.strategies:
        if sc not in insts:
            continue
        strategy = insts[sc]
        sdf = strategy.compute(df)
        res = run_backtest(sdf, strategy, req.capital, fees, code=req.code)
        m = compute_metrics(res, req.capital)
        rows.append({
            **_to(m), "code": sc, "name": strategy.name,
            "trades": [{"date": d.strftime("%Y-%m-%d"), "side": s,
                        "price": round(p, 3), "shares": n}
                       for (d, s, p, n) in res.trades],
        })
        eq = res.equity
        curves[sc] = [round(v, 2) for v in eq.tolist()]
        peak = eq.cummax()
        drawdown[sc] = [round(v * 100, 2) for v in ((eq - peak) / peak).tolist()]
        if benchmark is None:
            benchmark = [round(v, 2) for v in res.benchmark_equity.tolist()]

    return {
        "dates": dates, "rows": rows, "curves": curves,
        "benchmark": benchmark or [], "drawdown": drawdown,
        "kline": {"start": df.index[0].strftime("%Y-%m-%d"),
                  "end": df.index[-1].strftime("%Y-%m-%d"), "days": len(df)},
    }


# ---------- API: strategies（策略池） ----------

class StrategyParamsReq(BaseModel):
    params: dict[str, float]


class CustomStrategyReq(BaseModel):
    base: str
    name: str
    params: dict[str, float] = {}


@app.get("/strategies")
def page_strategies(request: Request):
    """策略池页：内置策略调参 + 自定义副本管理（数据由 /api/strategies 拉取）。"""
    return templates.TemplateResponse(request, "strategies.html", {"active": "strategies"})


@app.get("/api/strategies")
def api_strategies():
    """策略池：内置（含当前生效参数）+ 自定义副本；含每策略被自选绑定数。"""
    from app.llm.analyst import _STRATEGY_KNOWLEDGE
    bind_cnt: dict[str, int] = {}
    for w in db.list_watchlist():
        for sc in json.loads(w["strategies"] or "[]"):
            bind_cnt[sc] = bind_cnt.get(sc, 0) + 1
    items = []
    for code, s in registry.all_strategies().items():
        base = getattr(s, "base_code", None)
        items.append({
            "code": code, "name": s.name, "base": base or code,
            "is_custom": bool(base),
            "category": CATEGORY.get(base or code, "—"),
            "params": dict(s.params), "defaults": dict(type(s).default_params),
            "doc": _STRATEGY_KNOWLEDGE.get(base or code, ""),
            "watchers": bind_cnt.get(code, 0),
        })
    return {"items": items}


def _coerce_param(defaults: dict, k: str, v) -> float | int:
    """参数值转数字；默认值为整数且新值也是整数时保持 int（展示口径一致）。"""
    v = float(v)
    if isinstance(defaults.get(k), int) and v.is_integer():
        return int(v)
    return v


@app.put("/api/strategies/{code}/params")
def api_strategy_params(code: str, body: StrategyParamsReq):
    """策略调参：只存与默认值的差异（内置与副本统一走 strategy_params 表），
    保存后全链路（信号/回测/推荐/AI）即时生效。"""
    customs = {c["code"]: c for c in db.list_strategy_customs()}
    if code not in registry.BUILT_IN and code not in customs:
        return JSONResponse(status_code=404, content={"error": f"策略 {code} 不存在"})
    base = customs[code]["base"] if code in customs else code
    defaults = registry.BUILT_IN[base].default_params
    bad = [k for k in body.params if k not in defaults]
    if bad:
        return JSONResponse(status_code=400,
                            content={"error": f"非法参数名：{', '.join(bad)}（可选：{', '.join(defaults)}）"})
    cur = db.get_strategy_params(code)
    cur.update({k: _coerce_param(defaults, k, v) for k, v in body.params.items()})
    db.set_strategy_params(code, cur)
    return {"ok": True, "params": dict(registry.get(code).params)}


@app.delete("/api/strategies/{code}/params")
def api_strategy_params_reset(code: str):
    """恢复默认参数：清空该策略（内置或副本）的参数覆盖。"""
    customs = {c["code"] for c in db.list_strategy_customs()}
    if code not in registry.BUILT_IN and code not in customs:
        return JSONResponse(status_code=404, content={"error": f"策略 {code} 不存在"})
    db.del_strategy_params(code)
    return {"ok": True, "params": dict(registry.get(code).params)}


@app.post("/api/strategies/custom")
def api_strategy_custom_add(body: CustomStrategyReq):
    """从内置策略另存调参副本：新 code 新名，可与原策略并存、绑定不同标的。"""
    base = body.base.strip()
    if base not in registry.BUILT_IN:
        return JSONResponse(status_code=400,
                            content={"error": f"基础策略 {base} 须为内置策略：{', '.join(registry.BUILT_IN)}"})
    name = body.name.strip()
    if not name:
        return JSONResponse(status_code=400, content={"error": "副本名称不能为空"})
    defaults = registry.BUILT_IN[base].default_params
    bad = [k for k in body.params if k not in defaults]
    if bad:
        return JSONResponse(status_code=400,
                            content={"error": f"非法参数名：{', '.join(bad)}（可选：{', '.join(defaults)}）"})
    exist = {c["code"] for c in db.list_strategy_customs()} | set(registry.BUILT_IN)
    n = 1
    new_code = f"{base}_c{n}"
    while new_code in exist:
        n += 1
        new_code = f"{base}_c{n}"
    body.params = {k: _coerce_param(defaults, k, v) for k, v in body.params.items()}
    db.add_strategy_custom(new_code, base, name, body.params)
    return {"ok": True, "code": new_code, "name": name,
            "params": dict(registry.get(new_code).params)}


@app.delete("/api/strategies/custom/{code}")
def api_strategy_custom_del(code: str):
    """删除副本：顺带清理自选页对该副本的绑定（内置策略不可删）。"""
    if next((c for c in db.list_strategy_customs() if c["code"] == code), None) is None:
        return JSONResponse(status_code=404, content={"error": f"副本 {code} 不存在"})
    db.del_strategy_custom(code)
    for w in db.list_watchlist():
        sts = json.loads(w["strategies"] or "[]")
        if code in sts:
            db.update_watchlist(w["id"], strategies=[s for s in sts if s != code])
    return {"ok": True}


# ---------- API: AI 分析 ----------

class AIChatReq(BaseModel):
    messages: list[dict]        # [{"role": "user"|"assistant", "content": "..."}]
    code: str | None = None    # 当前选中的诊断标的（注入个股上下文）


@app.post("/api/ai/chat")
def api_ai_chat(body: AIChatReq):
    """AI 对话：注入本地数据上下文后调用 DeepSeek，SSE 流式返回。"""
    from app.llm.client import LLMClient, LLMNotConfigured
    from app.llm import analyst

    try:
        client = LLMClient(cfg.get("llm", {}))
    except LLMNotConfigured as e:
        return JSONResponse(status_code=400, content={"error": str(e)})

    try:
        sys_prompt = analyst.system_prompt(body.code, cfg)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"分析上下文构建失败：{e}"})

    # 多轮记忆：保留最近 12 条，首轮 system 由服务端重建（保证数据最新）
    msgs = [{"role": "system", "content": sys_prompt}] + (body.messages or [])[-12:]

    def gen():
        try:
            for chunk in client.stream_chat(msgs):
                yield f"data: {json.dumps({'text': chunk}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- helpers ----------

def _watch_presets() -> list[dict]:
    rows = db.list_watchlist()
    return [{"code": r["code"], "name": r["name"] or r["code"]} for r in rows] or \
           [{"code": c, "name": c} for c in ["513530", "159107", "515050"]]


def _watch_row(wid: int):
    for r in db.list_watchlist():
        if r["id"] == wid:
            return r
    return None


def _name_of(code: str) -> str:
    for r in db.list_watchlist():
        if r["code"] == code:
            return r["name"] or code
    return code