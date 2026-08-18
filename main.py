import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from config import load_config, get_feishu_cfg, get_export_types
from url_parser import scan_folder, scan_single_file
from feishu_api import FeishuClient, FeishuAPIError
from auth_helper import get_user_token_by_oauth, save_token_to_config
from utils import resolve_save_path, prepare_file_name

console = Console()


# ────────────────────── 下载核心逻辑 ──────────────────────

def download_one(client: FeishuClient, token: str, doc_type: str,
                 file_extension: str, save_dir: str,
                 conflict_policy: str, poll_interval: int,
                 poll_timeout: int) -> tuple[bool, str, int, str]:
    """下载单个文件，返回 (成功?, 文件名, 文件大小bytes, 警告信息)"""
    ticket = client.create_export_task(token, doc_type, file_extension)
    result = client.poll_export_result(ticket, token, poll_interval, poll_timeout)
    file_token = result["file_token"]
    file_name, warn = prepare_file_name(result["file_name"], file_extension)
    file_size = result.get("file_size", 0)

    save_path = resolve_save_path(save_dir, file_name, conflict_policy)
    if save_path is None:
        return True, file_name, file_size, warn

    client.download_file(file_token, save_path)
    return True, file_name, file_size, warn


def handle_token_expired(client: FeishuClient, token_type: str,
                         feishu_cfg: dict, config_path: str):
    """处理 token 过期"""
    if token_type == "user":
        tokens = get_user_token_by_oauth(
            feishu_cfg["app_id"], feishu_cfg["app_secret"]
        )
        save_token_to_config(config_path, tokens["user_access_token"],
                             tokens["refresh_token"])
        client.update_user_token(tokens["user_access_token"])
    else:
        client.refresh()


def download_with_retry(client: FeishuClient, item: dict, file_extension: str,
                        save_dir: str, conflict_policy: str,
                        poll_interval: int, poll_timeout: int,
                        token_type: str, feishu_cfg: dict,
                        config_path: str,
                        reauth_lock: threading.Lock | None = None) -> tuple[bool, str, int, str]:
    """带重试的下载单个文件，返回 (成功?, 文件名, 大小, 警告)"""
    token = item["token"]
    doc_type = item["doc_type"]
    try:
        return download_one(client, token, doc_type, file_extension,
                            save_dir, conflict_policy, poll_interval, poll_timeout)
    except (FeishuAPIError, RuntimeError) as e:
        is_token_expired = (
            (isinstance(e, FeishuAPIError) and e.status_code == 401)
            or (isinstance(e, RuntimeError) and "99991663" in str(e))
        )
        if not is_token_expired:
            raise

        if reauth_lock:
            # 多线程：只有一个线程执行重新授权，其他线程等锁释放后直接重试
            with reauth_lock:
                # 检查是否已被别的线程刷新过（token 变了就不用再刷）
                try:
                    return download_one(client, token, doc_type, file_extension,
                                        save_dir, conflict_policy, poll_interval, poll_timeout)
                except (FeishuAPIError, RuntimeError):
                    handle_token_expired(client, token_type, feishu_cfg, config_path)
                    return download_one(client, token, doc_type, file_extension,
                                        save_dir, conflict_policy, poll_interval, poll_timeout)
        else:
            # 单线程：直接重新授权
            handle_token_expired(client, token_type, feishu_cfg, config_path)
            return download_one(client, token, doc_type, file_extension,
                                save_dir, conflict_policy, poll_interval, poll_timeout)


# ────────────────────── 多线程下载 ──────────────────────

def format_size(size_bytes: int) -> str:
    if size_bytes <= 0:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    return f"{size_bytes / (1024 * 1024):.1f}MB"


def run_multi_thread(client: FeishuClient, items: list, export_types: dict,
                     conflict_policy: str, poll_interval: int, poll_timeout: int,
                     token_type: str, feishu_cfg: dict,
                     config_path: str, thread_count: int):
    """多线程下载，每个线程一个槽位实时显示状态，完成后按序号输出"""
    total = len(items)
    results = {}
    lock = threading.Lock()
    reauth_lock = threading.Lock()  # 重新授权锁，防止多线程同时弹出授权窗口

    # 每个线程槽位的状态：(线程名, 文件名, 状态文本, 状态类型)
    # 状态类型: "wait"=等待, "export"=导出中, "download"=下载中, "ok"=完成, "fail"=失败
    slots = [("Thread" + str(i + 1), "-", "", "wait") for i in range(thread_count)]
    completed_count = [0]
    log_lines = []  # 共享日志，显示在表格下方

    def build_table() -> Table:
        """根据当前槽位状态构建表格"""
        table = Table(show_header=False, pad_edge=False, expand=True,
                      border_style="dim")
        table.add_column("线程", width=10)
        table.add_column("状态", width=8)
        table.add_column("文件", ratio=1)
        table.add_column("大小", justify="right", width=10)

        for tid, name, detail, stype in slots:
            if stype == "wait":
                table.add_row(f"[dim]{tid}[/dim]", "[dim]WAIT[/dim]",
                              "[dim]-[/dim]", "")
            elif stype == "export":
                table.add_row(tid, "[yellow]EXPORT[/yellow]", name, "")
            elif stype == "download":
                table.add_row(tid, "[yellow]LOAD[/yellow]", name, f"[dim]{detail}[/dim]")
            elif stype == "ok":
                table.add_row(tid, "[green]OK[/green]",
                              f"[green]{name}[/green]",
                              f"[green]{detail}[/green]")
            elif stype == "fail":
                table.add_row(tid, "[red]FAIL[/red]",
                              f"[red]{name}[/red]",
                              f"[red]{detail}[/red]")
        return table

    def worker(slot_id: int, index: int, item: dict):
        doc_type = item["doc_type"]
        file_extension = export_types[doc_type]
        save_dir = os.path.dirname(item["path"])
        name = os.path.basename(item["path"])
        tid = slots[slot_id][0]

        # 开始导出
        with lock:
            slots[slot_id] = (tid, name, "", "export")

        try:
            ok, file_name, file_size, warn = download_with_retry(
                client, item, file_extension, save_dir,
                conflict_policy, poll_interval, poll_timeout,
                token_type, feishu_cfg, config_path,
                reauth_lock=reauth_lock
            )
            with lock:
                if ok:
                    size_str = format_size(file_size)
                    slots[slot_id] = (tid, file_name, size_str, "ok")
                    results[index] = ("ok", item["path"], file_name, file_size, "")
                else:
                    slots[slot_id] = (tid, name, "", "fail")
                    results[index] = ("fail", item["path"], name, 0, "")
                if warn:
                    log_lines.append(f"[yellow]WARN[/yellow] {warn}")
                completed_count[0] += 1
                progress.advance(task_id)
        except Exception as e:
            with lock:
                slots[slot_id] = (tid, name, str(e)[:40], "fail")
                results[index] = ("fail", item["path"], name, 0, str(e))
                completed_count[0] += 1
                progress.advance(task_id)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]下载中"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )
    task_id = progress.add_task("下载", total=total)

    def build_display():
        from rich.console import Group
        # 只显示最近 3 条日志，轮流滚动
        recent = log_lines[-3:] if log_lines else []
        log_table = Table(show_header=False, pad_edge=False, expand=True)
        log_table.add_column("日志", ratio=1)
        for line in recent:
            log_table.add_row(line)
        return Group(
            progress.make_tasks_table(progress.tasks),
            build_table(),
            log_table,
        )

    with Live(build_display(), console=console, refresh_per_second=4,
              screen=True) as live:
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = []
            for i, item in enumerate(items, 1):
                slot_id = (i - 1) % thread_count
                fut = executor.submit(worker, slot_id, i, item)
                futures.append(fut)

            import time
            done = False
            while not done:
                done = all(f.done() for f in futures)
                live.update(build_display())
                time.sleep(0.25)

            for fut in futures:
                fut.result()

    # 最终按序号输出全部结果（用 Table 保证对齐）
    result_table = Table(show_header=True, pad_edge=False, expand=True)
    result_table.add_column("#", width=4, justify="right")
    result_table.add_column("状态", width=6)
    result_table.add_column("文件", ratio=1)
    result_table.add_column("大小", justify="right", width=10)
    for i in range(1, total + 1):
        status, path, file_name, file_size, err = results.get(
            i, ("fail", "", "", 0, "未处理")
        )
        if status == "ok":
            size_str = format_size(file_size)
            result_table.add_row(str(i), "[green]OK[/green]", file_name, size_str)
        else:
            result_table.add_row(
                str(i), "[red]FAIL[/red]", f"[red]{file_name}[/red]",
                f"[red]{err}[/red]"
            )
    console.print(result_table)

    return results


# ────────────────────── 单线程下载 ──────────────────────

def run_single_thread(client: FeishuClient, items: list, export_types: dict,
                      conflict_policy: str, poll_interval: int, poll_timeout: int,
                      token_type: str, feishu_cfg: dict,
                      config_path: str):
    """单线程逐个下载"""
    total = len(items)
    results = {}

    for i, item in enumerate(items, 1):
        doc_type = item["doc_type"]
        file_extension = export_types[doc_type]
        save_dir = os.path.dirname(item["path"])
        name = os.path.basename(item["path"])

        console.print(f"\n[bold][{i}/{total}][/bold] {name}")
        console.print(f"  类型: {doc_type}  导出格式: {file_extension}")

        try:
            ok, file_name, file_size, warn = download_with_retry(
                client, item, file_extension, save_dir,
                conflict_policy, poll_interval, poll_timeout,
                token_type, feishu_cfg, config_path
            )
            if ok:
                size_str = format_size(file_size)
                console.print(f"  [green]OK[/green]   {file_name} ({size_str})")
                results[i] = ("ok", item["path"], file_name, file_size, "")
            else:
                results[i] = ("fail", item["path"], name, 0, "")
        except Exception as e:
            console.print(f"  [red]FAIL[/red]   {e}")
            results[i] = ("fail", item["path"], name, 0, str(e))

    return results


# ────────────────────── 主流程 ──────────────────────

def init_client(cfg: dict, feishu_cfg: dict, config_path: str) -> FeishuClient:
    """初始化飞书客户端，user 模式无 token 时自动触发 OAuth"""
    token_type = cfg["feishu"].get("access_token_type", "tenant")
    client = FeishuClient(
        app_id=feishu_cfg.get("app_id", ""),
        app_secret=feishu_cfg.get("app_secret", ""),
        access_token_type=token_type,
        user_access_token=feishu_cfg.get("user_access_token", ""),
        base_url=cfg.get("base_url", "https://open.feishu.cn/open-apis"),
    )

    if token_type == "user":
        if not feishu_cfg.get("user_access_token"):
            console.print("未配置 user_access_token，启动浏览器授权...")
            tokens = get_user_token_by_oauth(
                feishu_cfg["app_id"], feishu_cfg["app_secret"]
            )
            save_token_to_config(config_path, tokens["user_access_token"],
                                 tokens["refresh_token"])
            cfg = load_config(config_path)
            feishu_cfg = cfg["feishu"]
            client = FeishuClient(
                app_id=feishu_cfg["app_id"],
                app_secret=feishu_cfg["app_secret"],
                access_token_type="user",
                user_access_token=feishu_cfg["user_access_token"],
                base_url=cfg.get("base_url", "https://open.feishu.cn/open-apis"),
            )
        console.print(f"鉴权模式: [cyan]user（用户身份）[/cyan]")
        console.print(f"user_access_token: {feishu_cfg['user_access_token'][:20]}...")
    else:
        console.print(f"鉴权模式: [cyan]tenant（应用身份）[/cyan]")
        console.print("获取 tenant_access_token ...")
        _ = client.access_token
        console.print("access_token 获取成功")

    return client


def main():
    console.print("=" * 50)
    console.print("  飞书云文档批量下载工具", style="bold")
    console.print("=" * 50)

    path = console.input("\n请输入文件夹路径或单个 .url 文件路径: ").strip().strip('"')
    if not path:
        console.print("错误: 路径不能为空", style="red")
        return

    # 兼容 PyInstaller 单文件模式：exe 所在目录
    base_dir = os.path.dirname(os.path.abspath(__import__("sys").executable if getattr(__import__("sys"), 'frozen', False) else __file__))
    config_path = os.path.join(base_dir, "config.json")

    cfg = load_config(config_path)
    feishu_cfg = get_feishu_cfg(cfg)
    export_types = get_export_types(cfg)
    poll_interval = cfg["poll_interval_seconds"]
    poll_timeout = cfg["poll_timeout_seconds"]
    token_type = feishu_cfg.get("access_token_type", "tenant")
    conflict_policy = cfg["conflict_policy"]
    multi_thread = cfg.get("multi_thread", False)
    thread_count = cfg.get("thread_count", 3)
    supported_types = set(export_types.keys())

    # 扫描
    if os.path.isfile(path) and path.lower().endswith(".url"):
        console.print(f"扫描单个文件: {path}")
        items = scan_single_file(path, supported_types)
    elif os.path.isdir(path):
        console.print(f"扫描文件夹: {path}")
        items = scan_folder(path, supported_types)
    else:
        console.print(f"错误: 路径不存在或不是 .url 文件/文件夹 → {path}", style="red")
        return
    console.print(f"找到 [bold]{len(items)}[/bold] 个匹配的快捷方式")

    if not items:
        console.print("没有需要下载的文件，退出。")
        return

    # 用户确认
    mode_str = f"多线程({thread_count}线程)" if multi_thread else "单线程"
    policy_map = {"rename": "重命名", "overwrite": "覆盖", "skip": "跳过"}
    policy_str = policy_map.get(conflict_policy, conflict_policy)
    console.print(f"  模式: {mode_str}  |  重名策略: {policy_str}")
    confirm = console.input(
        f"即将下载 {len(items)} 个文件，是否继续？(Y/n): "
    ).strip().lower()
    if confirm and confirm != "y":
        console.print("已取消。")
        return

    # 初始化客户端
    client = init_client(cfg, feishu_cfg, config_path)

    # 下载
    console.print()
    if multi_thread:
        results = run_multi_thread(
            client, items, export_types, conflict_policy,
            poll_interval, poll_timeout, token_type, feishu_cfg,
            config_path, thread_count
        )
    else:
        results = run_single_thread(
            client, items, export_types, conflict_policy,
            poll_interval, poll_timeout, token_type, feishu_cfg,
            config_path
        )

    # 汇总
    success = sum(1 for s, *_ in results.values() if s == "ok")
    fail = sum(1 for s, *_ in results.values() if s == "fail")
    failed_paths = [p for s, p, *_ in results.values() if s == "fail"]

    console.print(f"\n{'='*50}")
    console.print(
        f"完成! 成功: [green]{success}[/green], "
        f"失败: [red]{fail}[/red], 总计: {len(items)}"
    )
    if failed_paths:
        console.print(f"\n[red]失败文件列表:[/red]")
        for p in failed_paths:
            console.print(f"  - {p}")

    # 询问删除
    if success > 0:
        downloaded_paths = [
            item["path"] for item in items
            if item["path"] not in failed_paths
        ]
        if downloaded_paths:
            del_confirm = console.input(
                f"\n是否删除已下载的 {len(downloaded_paths)} 个 .url 快捷方式？(y/N): "
            ).strip().lower()
            if del_confirm == "y":
                deleted = 0
                for p in downloaded_paths:
                    try:
                        os.remove(p)
                        deleted += 1
                    except Exception as e:
                        console.print(f"  删除失败: {p} ({e})", style="yellow")
                console.print(f"已删除 {deleted} 个 .url 文件")
            else:
                console.print("已跳过删除。")


if __name__ == "__main__":
    main()
