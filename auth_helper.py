import json
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

import requests

AUTH_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
REDIRECT_PORT = 18234
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"


class CallbackHandler(BaseHTTPRequestHandler):
    """处理 OAuth 回调的 HTTP handler"""

    auth_code: str | None = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/callback":
            params = parse_qs(parsed.query)
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            if code:
                CallbackHandler.auth_code = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    "✅ 授权成功！请返回终端继续操作，可以关闭此页面。".encode("utf-8")
                )
                return
            if error:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    f"❌ 授权失败: {error}".encode("utf-8")
                )
                return
        self.send_response(400)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


def get_user_token_by_oauth(app_id: str, app_secret: str) -> dict:
    """
    通过浏览器 OAuth 授权获取 user_access_token
    返回 {"user_access_token": "...", "refresh_token": "..."}
    """
    # 重置状态，防止上次残留
    CallbackHandler.auth_code = None

    # 1) 构造授权 URL
    encoded_redirect = quote(REDIRECT_URI, safe="")
    auth_url = (
        f"{AUTH_URL}"
        f"?client_id={app_id}"
        f"&response_type=code"
        f"&redirect_uri={encoded_redirect}"
        f"&prompt=consent"
    )
    print(f"\n正在打开浏览器进行授权...")
    print(f"如果浏览器没有自动打开，请手动访问：\n{auth_url}\n")
    webbrowser.open(auth_url)

    # 2) 启动本地服务器等待回调
    server = HTTPServer(("localhost", REDIRECT_PORT), CallbackHandler)
    server.timeout = 120  # 2 分钟超时
    server.handle_request()

    if not CallbackHandler.auth_code:
        raise RuntimeError("授权超时或失败，未收到回调")

    print("收到授权码，正在换取 token...")

    # 3) 用授权码换取 user_access_token
    resp = requests.post(TOKEN_URL, json={
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": app_secret,
        "code": CallbackHandler.auth_code,
        "redirect_uri": REDIRECT_URI,
    }, headers={
        "Content-Type": "application/json; charset=utf-8",
    }, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"换取 token 失败: {data}")

    return {
        "user_access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
    }


def save_token_to_config(config_path: str, user_token: str, refresh_token: str):
    """将获取的 token 写入 config.json"""
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["feishu"]["user_access_token"] = user_token
    cfg["feishu"]["refresh_token"] = refresh_token
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)
    print(f"✅ token 已保存到 config.json")
