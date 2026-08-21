import json

# 默认值
DEFAULTS = {
    "poll_interval_seconds": 3,
    "poll_timeout_seconds": 300,
    "conflict_policy": "rename",
    "base_url": "https://open.feishu.cn/open-apis",
    "multi_thread": False,
    "thread_count": 3,
    "requests_per_minute": 100,
    "request_window_seconds": 60,
    "delete_source_after_success": False,
}

VALID_POLICIES = ("rename", "overwrite", "skip")


def load_config(config_path: str) -> dict:
    """从 JSON 文件加载配置，并填充默认值"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for key, val in DEFAULTS.items():
        cfg.setdefault(key, val)
    # conflict_policy 校验
    if cfg["conflict_policy"] not in VALID_POLICIES:
        print(f"  ⚠ 未知的 conflict_policy: {cfg['conflict_policy']}，使用默认值 rename")
        cfg["conflict_policy"] = "rename"
    return cfg


def get_feishu_cfg(cfg: dict) -> dict:
    """获取飞书凭据配置"""
    return cfg.get("feishu", {})


def get_export_types(cfg: dict) -> dict:
    """获取导出类型映射，如 {"doc": "docx", "docx": "docx"}"""
    return cfg.get("export_types", {})
