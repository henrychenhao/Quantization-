"""策略池：动态注册表 + 参数覆盖 + 自定义副本（registry / db / API 校验）。

设计要点：
- 调参存「与默认值的差异」（strategy_params 表），副本初始参数亦为差异，
  副本后续调参叠加在初始差异之上（同走 strategy_params 表）；
- 每次调用 all_strategies() 重新实例化，保存后即时生效；
- API 校验：参数名白名单（base 的 default_params）、副本 base 须内置、code 自动避让。
"""
import json

import pytest

from app import db
from app.strategy import registry
from app.web import app as webapp


# ---------- registry 动态化 ----------

def test_builtin_defaults():
    """无任何覆盖时：内置 9 策略齐全，参数等于类默认。"""
    insts = registry.all_strategies()
    assert set(insts) == set(registry.BUILT_IN)
    for code, s in insts.items():
        assert s.params == registry.BUILT_IN[code].default_params


def test_param_override_takes_effect():
    """调参存差异表：实例即时生效，未覆盖参数保持默认。"""
    db.set_strategy_params("rsi", {"n": 7})
    s = registry.get("rsi")
    assert s.params["n"] == 7
    for k, v in registry.BUILT_IN["rsi"].default_params.items():
        if k != "n":
            assert s.params[k] == v


def test_param_reset_restores_defaults():
    db.set_strategy_params("rsi", {"n": 7, "low": 25})
    db.del_strategy_params("rsi")
    assert registry.get("rsi").params == registry.BUILT_IN["rsi"].default_params


def test_get_unknown_raises():
    with pytest.raises(KeyError):
        registry.get("no_such_strategy")


def test_custom_copy_lifecycle():
    """副本：创建 → 可取（base_code/name/参数合并）→ 叠加调参 → 删除后消失。"""
    db.add_strategy_custom("macd_c1", "macd", "MACD 敏感版", {"fast": 8})
    insts = registry.all_strategies()
    assert "macd_c1" in insts and "macd" in insts      # 副本与原策略并存
    s = insts["macd_c1"]
    assert s.base_code == "macd"
    assert s.name == "MACD 敏感版"
    assert s.params["fast"] == 8
    assert s.params["slow"] == registry.BUILT_IN["macd"].default_params["slow"]
    # 副本后续调参叠加在初始差异之上
    db.set_strategy_params("macd_c1", {"fast": 10})
    assert registry.get("macd_c1").params["fast"] == 10
    # 删除后消失，strategy_params 同步清理
    db.del_strategy_custom("macd_c1")
    assert "macd_c1" not in registry.all_strategies()
    assert db.get_strategy_params("macd_c1") == {}


def test_custom_copy_bad_base_skipped():
    """base 非法（不在内置表）的脏数据行被跳过，不影响其余策略。"""
    db.add_strategy_custom("ghost_c1", "no_such_base", "幽灵", {})
    insts = registry.all_strategies()
    assert "ghost_c1" not in insts
    assert "macd" in insts


# ---------- API 层（直接调用路由函数，无需起服务） ----------

def test_api_list_builtins():
    d = webapp.api_strategies()
    codes = [i["code"] for i in d["items"]]
    assert set(registry.BUILT_IN) <= set(codes)
    rsi = next(i for i in d["items"] if i["code"] == "rsi")
    assert rsi["category"] == "均值回归"
    assert rsi["defaults"] == registry.BUILT_IN["rsi"].default_params
    assert rsi["base"] == "rsi" and rsi["is_custom"] is False


def test_api_watchers_counted():
    db.add_watchlist("000977", "浪潮信息", ["rsi", "grid"])
    d = webapp.api_strategies()
    rsi = next(i for i in d["items"] if i["code"] == "rsi")
    assert rsi["watchers"] == 1


def test_api_params_validation_and_effect():
    """非法参数名 400、未知策略 404、合法调参 200 且全链路生效。"""
    r = webapp.api_strategy_params("rsi", webapp.StrategyParamsReq(params={"n": 9, "bad_key": 1}))
    assert r.status_code == 400
    r = webapp.api_strategy_params("no_such", webapp.StrategyParamsReq(params={"n": 9}))
    assert r.status_code == 404
    ok = webapp.api_strategy_params("rsi", webapp.StrategyParamsReq(params={"n": 9}))
    assert ok["params"]["n"] == 9
    assert registry.get("rsi").params["n"] == 9


def test_api_custom_codegen_avoids_conflict():
    """副本 code 自动生成（base_c1、base_c2 …），非法 base / 空名 / 非法参数 400。"""
    r = webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="nope", name="x", params={}))
    assert r.status_code == 400
    r = webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="grid", name="", params={}))
    assert r.status_code == 400
    r = webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="grid", name="网格密档", params={"bad": 1}))
    assert r.status_code == 400
    d1 = webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="grid", name="网格密档", params={"n_levels": 8}))
    assert d1["code"] == "grid_c1" and d1["params"]["n_levels"] == 8
    d2 = webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="grid", name="网格疏档", params={}))
    assert d2["code"] == "grid_c2"
    assert d2["params"]["n_levels"] == 5      # 未覆盖参数继承默认


def test_api_custom_delete_unbinds_watchlist():
    """删除副本：绑定该副本的自选同步解绑，其余绑定保留；内置不可删（404）。"""
    webapp.api_strategy_custom_add(webapp.CustomStrategyReq(base="rsi", name="RSI 快版", params={"n": 7}))
    db.add_watchlist("000977", "浪潮信息", ["rsi_c1", "rsi"])
    assert webapp.api_strategy_custom_del("rsi_c1")["ok"]
    w = next(x for x in db.list_watchlist() if x["code"] == "000977")
    assert json.loads(w["strategies"]) == ["rsi"]
    assert webapp.api_strategy_custom_del("rsi_c1").status_code == 404
    assert webapp.api_strategy_custom_del("rsi").status_code == 404
