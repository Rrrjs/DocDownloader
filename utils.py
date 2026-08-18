import os
import re


# Windows 文件名非法字符
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')


def sanitize_filename(file_name: str) -> str:
    """
    清理文件名中的非法字符。
    \\ / : * ? " < > | 及控制字符 → 下划线
    首尾空格和点号也会清理
    """
    cleaned = _INVALID_CHARS.sub("_", file_name)
    cleaned = cleaned.strip(" .")
    return cleaned if cleaned else "unnamed"


def resolve_save_path(save_dir: str, file_name: str,
                      policy: str = "rename") -> str | None:
    """
    生成保存路径，根据 policy 处理重名：
    - rename: 自动追加序号（report(1).docx）
    - overwrite: 直接覆盖
    - skip: 返回 None 跳过
    """
    path = os.path.join(save_dir, file_name)

    if not os.path.exists(path):
        return path

    if policy == "overwrite":
        print(f"  ⚠ 文件已存在，将覆盖: {file_name}")
        return path

    if policy == "skip":
        print(f"  ⏭ 文件已存在，跳过: {file_name}")
        return None

    # rename 策略
    base, ext = os.path.splitext(file_name)
    counter = 1
    while True:
        new_name = f"{base}({counter}){ext}"
        path = os.path.join(save_dir, new_name)
        if not os.path.exists(path):
            print(f"  ⚠ 文件已存在，重命名为: {new_name}")
            return path
        counter += 1


def ensure_extension(file_name: str, file_extension: str) -> str:
    """确保文件名以正确的扩展名结尾"""
    expected_ext = f".{file_extension}"
    if not file_name.endswith(expected_ext):
        return f"{file_name}{expected_ext}"
    return file_name


def prepare_file_name(raw_name: str, file_extension: str) -> tuple[str, str]:
    """
    统一处理文件名：补扩展名 → 清理非法字符
    返回 (最终文件名, 警告信息)，无警告时警告为空字符串
    """
    name = ensure_extension(raw_name, file_extension)
    clean = sanitize_filename(name)
    warn = ""
    if clean != name:
        warn = f"文件名含非法字符，已清理: {clean}"
    return clean, warn
