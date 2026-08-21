"""与界面无关的飞书文档下载服务。"""

import base64
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from auth_helper import get_user_token_by_oauth, save_token_to_config
from feishu_api import FeishuAPIError, FeishuClient, RateLimitError, RequestLimiter
from utils import prepare_file_name, resolve_save_path


TERMINAL_STATUSES = {"ok", "skipped", "failed", "cancelled"}


def is_token_error(exc: Exception) -> bool:
    return (
        isinstance(exc, FeishuAPIError) and exc.status_code == 401
    ) or (
        isinstance(exc, RuntimeError) and "99991663" in str(exc)
    )


def create_client(cfg: dict, on_wait=None) -> FeishuClient:
    feishu_cfg = cfg.get("feishu", {})
    return FeishuClient(
        app_id=feishu_cfg.get("app_id", ""),
        app_secret=feishu_cfg.get("app_secret", ""),
        access_token_type=feishu_cfg.get("access_token_type", "tenant"),
        user_access_token=feishu_cfg.get("user_access_token", ""),
        base_url=cfg.get("base_url", "https://open.feishu.cn/open-apis"),
        request_limiter=RequestLimiter(
            stage_seconds=int(cfg.get("request_window_seconds", 60)),
            on_wait=on_wait,
        ),
    )


def token_expired(token: str) -> bool:
    if not token:
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return not exp or time.time() >= exp
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return False


def refresh_auth(client: FeishuClient, cfg: dict, config_path: str):
    feishu_cfg = cfg.setdefault("feishu", {})
    if feishu_cfg.get("access_token_type", "tenant") == "user":
        try:
            tokens = client.refresh_user_token(feishu_cfg.get("refresh_token", ""))
        except (FeishuAPIError, RuntimeError):
            tokens = get_user_token_by_oauth(
                feishu_cfg.get("app_id", ""), feishu_cfg.get("app_secret", "")
            )
        save_token_to_config(
            config_path, tokens["user_access_token"], tokens["refresh_token"]
        )
        feishu_cfg["user_access_token"] = tokens["user_access_token"]
        feishu_cfg["refresh_token"] = tokens["refresh_token"]
        client.update_user_token(tokens["user_access_token"])
    else:
        client.refresh()


def download_one(client: FeishuClient, item: dict, file_extension: str,
                 cfg: dict, cancel_event: threading.Event | None = None) -> dict:
    if cancel_event and cancel_event.is_set():
        return {"status": "cancelled", "name": os.path.basename(item["path"]), "size": 0, "error": "已取消"}

    token = item["token"]
    ticket = client.create_export_task(token, item["doc_type"], file_extension, cancel_event=cancel_event)
    result = client.poll_export_result(
        ticket, token, cfg.get("poll_interval_seconds", 3),
        cfg.get("poll_timeout_seconds", 300), cancel_event=cancel_event,
    )
    file_token = result["file_token"]
    file_name, warning = prepare_file_name(result["file_name"], file_extension)
    file_size = result.get("file_size", 0)
    save_path = resolve_save_path(
        os.path.dirname(item["path"]), file_name, cfg.get("conflict_policy", "rename")
    )
    if save_path is None:
        return {"status": "skipped", "name": file_name, "size": file_size, "warning": "文件已存在，已跳过"}
    client.download_file(file_token, save_path, cancel_event=cancel_event)
    return {"status": "ok", "name": file_name, "size": file_size, "warning": warning}


def download_with_retry(client: FeishuClient, item: dict, file_extension: str,
                        cfg: dict, config_path: str,
                        auth_lock: threading.Lock,
                        cancel_event: threading.Event | None = None) -> dict:
    token_before = client.user_access_token if client.access_token_type == "user" else None
    try:
        return download_one(client, item, file_extension, cfg, cancel_event)
    except RateLimitError:
        raise
    except (FeishuAPIError, RuntimeError) as exc:
        if not is_token_error(exc):
            raise

        if cancel_event and cancel_event.is_set():
            return {"status": "cancelled", "name": os.path.basename(item["path"]), "size": 0, "error": "已取消"}
        with auth_lock:
            if client.access_token_type == "user":
                current_token = cfg.get("feishu", {}).get("user_access_token", "")
                if current_token and current_token != token_before and not token_expired(current_token):
                    client.update_user_token(current_token)
                    return download_one(client, item, file_extension, cfg, cancel_event)
            try:
                return download_one(client, item, file_extension, cfg, cancel_event)
            except (FeishuAPIError, RuntimeError) as retry_exc:
                if not is_token_error(retry_exc):
                    raise
                refresh_auth(client, cfg, config_path)
                return download_one(client, item, file_extension, cfg, cancel_event)


def ensure_authenticated(client: FeishuClient, cfg: dict, config_path: str,
                         auth_lock: threading.Lock):
    feishu_cfg = cfg.setdefault("feishu", {})
    if feishu_cfg.get("access_token_type", "tenant") != "user":
        _ = client.access_token
        return
    with auth_lock:
        current_token = feishu_cfg.get("user_access_token", "")
        if current_token and not token_expired(current_token):
            client.update_user_token(current_token)
            return
        refresh_auth(client, cfg, config_path)


def run_download(items: list[dict], cfg: dict, config_path: str,
                 callback: Callable[[dict], None] | None = None,
                 cancel_event: threading.Event | None = None) -> dict[int, dict]:
    """执行一批下载；每个输入项目始终返回一个终态结果。"""
    callback = callback or (lambda event: None)
    cancel_event = cancel_event or threading.Event()
    results: dict[int, dict] = {}
    client = create_client(
        cfg,
        lambda seconds: callback({"kind": "log", "level": "throttle", "message": "API请求过速，冷却中"}),
    )
    export_types = cfg.get("export_types", {})
    auth_lock = threading.Lock()
    try:
        ensure_authenticated(client, cfg, config_path, auth_lock)
        callback({"kind": "log", "level": "success", "message": "鉴权成功"})
    except Exception as exc:
        callback({"kind": "error", "message": f"鉴权失败：{exc}"})
        for index, item in enumerate(items, 1):
            results[index] = {
                "index": index, "status": "failed",
                "name": os.path.basename(item["path"]), "size": 0,
                "error": f"鉴权失败：{exc}",
            }
        return results
    count_lock = threading.Lock()
    results_lock = threading.Lock()
    processed = 0

    def emit(event):
        callback(event)

    def one(index: int, item: dict):
        nonlocal processed
        name = os.path.basename(item["path"])
        thread_name = threading.current_thread().name
        emit({"kind": "status", "index": index, "thread": thread_name, "status": "exporting", "name": name, "processed": processed})
        try:
            while True:
                if cancel_event.is_set():
                    result = {"status": "cancelled", "name": name, "size": 0, "error": "已取消"}
                    break
                if item.get("doc_type") not in export_types:
                    result = {"status": "failed", "name": name, "size": 0, "error": "未配置导出格式"}
                    break
                try:
                    result = download_with_retry(
                        client, item, export_types[item["doc_type"]], cfg,
                        config_path, auth_lock, cancel_event,
                    )
                    break
                except RateLimitError:
                    pass
        except Exception as exc:
            result = {"status": "failed", "name": name, "size": 0, "error": str(exc)}
        with count_lock:
            if result["status"] in TERMINAL_STATUSES:
                processed += 1
            current = processed
        result["index"] = index
        with results_lock:
            results[index] = result
        if result.get("warning"):
            emit({"kind": "log", "level": "warning", "message": result["warning"]})
        if result["status"] == "failed":
            emit({"kind": "log", "level": "error", "message": f"下载失败：{name}：{result.get('error', '')}"})
        emit({"kind": "status", "index": index, "thread": thread_name, "status": result["status"], "name": result.get("name", name), "error": result.get("error", ""), "processed": current})
        return index, result

    try:
        if cfg.get("multi_thread", False):
            worker_count = max(1, int(cfg.get("thread_count", 3)))
            emit({"kind": "log", "level": "info", "message": f"多线程下载已启用：{worker_count} 个线程"})
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [executor.submit(one, index, item) for index, item in enumerate(items, 1)]
                for future in as_completed(futures):
                    index, result = future.result()
                    results[index] = result
        else:
            emit({"kind": "log", "level": "info", "message": "单线程下载已启用"})
            for index, item in enumerate(items, 1):
                one(index, item)
    except Exception as exc:
        emit({"kind": "error", "message": str(exc)})
        for index, item in enumerate(items, 1):
            results.setdefault(index, {
                "index": index, "status": "failed",
                "name": os.path.basename(item["path"]), "size": 0,
                "error": str(exc),
            })
    return results
