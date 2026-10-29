"""pytest 公共设施：隔离数据库 + 公共 fixture。

关键点：必须在 import app.db 之前设置 QUANT_DB_PATH，保证全部测试
读写独立临时库，绝不触碰 data/quant.db 真实业务库。
"""
import os
import sys
import tempfile

# 项目根加入 sys.path（保证 `import app` 可用）
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 隔离数据库（在 import app.db 之前完成）
_TEST_DIR = tempfile.mkdtemp(prefix="quant-test-")
os.environ["QUANT_DB_PATH"] = os.path.join(_TEST_DIR, "test.db")

import pandas as pd
import pytest

from app import db


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前建表、测试后清空全部业务表，保证用例相互独立。"""
    db.init_db()
    db.set_cash(0.0)
    yield
    with db.db() as c:
        for t in ("watchlist", "signal", "trade", "asset_snapshot", "push_log"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM account")


def make_df(closes, dates=None, high=None, low=None, open_=None) -> pd.DataFrame:
    """构造单标的 K 线 DataFrame（回测/策略测试用，无需网络）。"""
    closes = [float(x) for x in closes]
    n = len(closes)
    if dates is None:
        dates = pd.bdate_range("2025-01-01", periods=n)
    df = pd.DataFrame({
        "open": open_ if open_ is not None else closes,
        "high": high if high is not None else closes,
        "low": low if low is not None else closes,
        "close": closes,
        "volume": [100000] * n,
    }, index=pd.DatetimeIndex(dates))
    return df


@pytest.fixture
def seeded_account():
    """初始资金 100000 的干净账本（现金与初始资金一致）。"""
    db.set_cash(100000.0)
    with db.db() as c:
        c.execute("UPDATE account SET initial_cash=100000 WHERE id=1")
    return 100000.0