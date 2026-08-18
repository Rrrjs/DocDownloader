import re
import configparser
from pathlib import Path


# 匹配飞书文档 URL 的正则（支持 .feishu.cn 和 .larksuite.com）
URL_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9\-]+\."
    r"(?:feishu\.cn|larksuite\.com)/"
    r"(docs|docx|wiki|sheets|bitable|base|mindnotes)/"
    r"([A-Za-z0-9]+)"
)

# 暂不支持的类型（wiki 需要额外解析实际文档类型）
UNSUPPORTED_TYPES = {"wiki"}

# URL 路径类型 → 飞书 API type 参数映射
URL_TO_API_TYPE = {
    "docs": "doc",
    "docx": "docx",
    "sheets": "sheet",
    "bitable": "bitable",
    "base": "bitable",
    "mindnotes": "mindnote",
}


def parse_url_file(url_file_path: str) -> str | None:
    """从 .url 快捷方式文件中提取 URL"""
    config = configparser.ConfigParser()
    config.read(url_file_path, encoding="utf-8")
    try:
        return config.get("InternetShortcut", "url")
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None


def classify_url(url: str) -> tuple[str, str] | None:
    """
    根据 URL 判断文档类型和 token
    返回 (doc_type, token) 或 None（不支持/无法识别）
    """
    m = URL_PATTERN.search(url)
    if not m:
        return None
    path_type, token = m.group(1), m.group(2)
    if path_type in UNSUPPORTED_TYPES:
        return None
    api_type = URL_TO_API_TYPE.get(path_type)
    if not api_type:
        return None
    return api_type, token


def scan_single_file(file_path: str, supported_types: set[str]) -> list[dict]:
    """
    扫描单个 .url 文件
    返回 [{path, url, doc_type, token}, ...] 或空列表
    """
    url = parse_url_file(file_path)
    if not url:
        print(f"  ⚠ 无法解析: {file_path}")
        return []
    info = classify_url(url)
    if not info:
        print(f"  ⚠ 不支持的文件类型: {file_path}")
        return []
    doc_type, token = info
    if doc_type not in supported_types:
        print(f"  ⚠ 未配置导出格式的类型 ({doc_type}): {file_path}")
        return []
    return [{"path": file_path, "url": url, "doc_type": doc_type, "token": token}]


def scan_folder(folder: str, supported_types: set[str]) -> list[dict]:
    """
    扫描文件夹中的 .url 文件
    返回 [{path, url, doc_type, token}, ...]
    只返回 doc_type 在 supported_types 中的条目
    """
    results = []
    skipped = 0
    folder_path = Path(folder)

    for url_file in folder_path.rglob("*.url"):
        url = parse_url_file(str(url_file))
        if not url:
            continue
        info = classify_url(url)
        if not info:
            skipped += 1
            continue
        doc_type, token = info
        if doc_type not in supported_types:
            skipped += 1
            continue
        results.append({
            "path": str(url_file),
            "url": url,
            "doc_type": doc_type,
            "token": token,
        })

    if skipped > 0:
        print(f"  （跳过 {skipped} 个不支持的文件类型）")

    return results
