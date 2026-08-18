import os
import time
import tempfile
import threading
import requests


class FeishuAPIError(Exception):
    """飞书 API 错误，携带状态码和响应体"""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"HTTP {status_code}: {message}")


# 导出失败的终态错误码集合
_TERMINAL_EXPORT_STATUSES = {3, 107, 108, 109, 110, 111, 122, 123, 6000}


class FeishuClient:
    """飞书 API 客户端，支持 tenant_access_token 和 user_access_token 两种模式"""

    def __init__(self, app_id: str, app_secret: str,
                 access_token_type: str = "tenant",
                 user_access_token: str = "",
                 base_url: str = "https://open.feishu.cn/open-apis"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token_type = access_token_type
        self.base_url = base_url
        self._access_token: str | None = None
        self._user_access_token = user_access_token
        self._lock = threading.Lock()

    def _refresh_tenant_token(self):
        """获取/刷新 tenant_access_token（内部方法，需在外层加锁或已确认需要刷新）"""
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }, timeout=10)
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {data}")
        self._access_token = data["tenant_access_token"]

    @property
    def access_token(self) -> str:
        """获取当前使用的 access_token"""
        if self.access_token_type == "user":
            if not self._user_access_token:
                raise RuntimeError("未配置 user_access_token")
            return self._user_access_token
        if not self._access_token:
            self._refresh_tenant_token()
        return self._access_token

    def update_user_token(self, new_token: str):
        """更新 user_access_token（OAuth 重新授权后调用）"""
        with self._lock:
            self._user_access_token = new_token

    def refresh(self):
        """刷新 token（线程安全，多线程下只刷新一次）"""
        with self._lock:
            if self.access_token_type == "user":
                raise RuntimeError("user_access_token 已过期")
            self._refresh_tenant_token()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def create_export_task(self, token: str, doc_type: str,
                           file_extension: str, sub_id: str | None = None) -> str:
        """创建导出任务，返回 ticket"""
        url = f"{self.base_url}/drive/v1/export_tasks"
        headers = {
            **self._auth_headers(),
            "Content-Type": "application/json; charset=utf-8",
        }
        body = {
            "file_extension": file_extension,
            "token": token,
            "type": doc_type,
        }
        if sub_id:
            body["sub_id"] = sub_id
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建导出任务失败: {data}")
        return data["data"]["ticket"]

    def poll_export_result(self, ticket: str, doc_token: str,
                           interval: int = 3, timeout: int = 300) -> dict:
        """轮询导出任务结果，使用指数退避策略"""
        url = f"{self.base_url}/drive/v1/export_tasks/{ticket}"
        params = {"token": doc_token}
        deadline = time.time() + timeout
        current_interval = interval

        while time.time() < deadline:
            resp = requests.get(url, headers=self._auth_headers(),
                                params=params, timeout=30)
            if resp.status_code != 200:
                raise FeishuAPIError(resp.status_code, resp.text)
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"查询导出任务失败: {data}")

            result = data["data"]["result"]
            status = result["job_status"]
            if status == 0:  # 成功
                return result
            if status in _TERMINAL_EXPORT_STATUSES:
                raise RuntimeError(
                    f"导出失败 (status={status}): {result.get('job_error_msg', '')}"
                )
            # 1=初始化, 2=处理中 → 指数退避轮询
            time.sleep(current_interval)
            current_interval = min(current_interval * 1.5, 15)

        raise TimeoutError(f"导出任务超时 ({timeout}s)")

    def download_file(self, file_token: str, save_path: str):
        """下载导出的文件到本地（原子写入：先写临时文件再 rename）"""
        url = f"{self.base_url}/drive/v1/export_tasks/file/{file_token}/download"
        resp = requests.get(url, headers=self._auth_headers(),
                            stream=True, timeout=(10, 120))
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)

        # 写入同目录临时文件，完成后 rename 保证原子性
        save_dir = os.path.dirname(save_path)
        fd, tmp_path = tempfile.mkstemp(dir=save_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            os.replace(tmp_path, save_path)
        except Exception:
            # 清理临时文件
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
