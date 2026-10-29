"""配置加载：单一配置源 config.yaml（main.py 与 Web 共用）。"""
import yaml

DEFAULT_CONFIG = {
    "data": {"kline_dir": "data/kline", "log_dir": "logs"},
    "fees": {"commission": 0.00025, "min_commission": 5, "stamp_tax": 0.0005, "slippage": 0.001},
    "account": {"initial_cash": 100000},
    "push": {"channel": "wecom", "wecom_webhook": ""},
    "watchlist": [],
}


def load_config(path: str = "config.yaml") -> dict:
    cfg = DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        for k, v in user_cfg.items():
            if isinstance(v, dict):
                cfg.setdefault(k, {}).update(v)
            else:
                cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg