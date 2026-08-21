import os
import time
import tempfile
import threading
import requests


class RateLimitError(RuntimeError):
    """飞书返回 99991400，当前 60 秒阶段暂停。"""


class RequestLimiter:
    """按固定 60 秒阶段暂停，不预先消耗请求额度。"""

    def __init__(self, stage_seconds=60, on_wait=None):
        self.stage_seconds = max(1, int(stage_seconds))
        self.on_wait = on_wait
        self._stage_started = time.monotonic()
        self._paused_until = 0.0
        self._notified_until = 0.0
        self._lock = threading.Lock()

    def acquire(self, cancel_event=None):
        while True:
            with self._lock:
                remaining = self._paused_until - time.monotonic()
                notify = remaining > 0 and self._paused_until != self._notified_until
                if notify:
                    self._notified_until = self._paused_until
                if remaining <= 0:
                    self._paused_until = 0.0
                    self._stage_started = time.monotonic()
                    return
            if notify and self.on_wait:
                self.on_wait(remaining)
            if cancel_event and cancel_event.wait(remaining):
                raise RuntimeError("速率限制冷却已取消")
            if not cancel_event:
                time.sleep(remaining)

    def pause_current_stage(self):
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._stage_started
            stages = int(elapsed // self.stage_seconds) + 1
            stage_end = self._stage_started + stages * self.stage_seconds
            self._paused_until = stage_end


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
                 base_url: str = "https://open.feishu.cn/open-apis",
                 request_limiter: RequestLimiter | None = None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token_type = access_token_type
        self.base_url = base_url
        self._access_token: str | None = None
        self._user_access_token = user_access_token
        self._lock = threading.Lock()
        self.request_limiter = request_limiter

    def _request(self, method, url, **kwargs):
        cancel_event = kwargs.pop("cancel_event", None)
        request_func = getattr(requests, method.lower())
        if self.request_limiter is not None:
            self.request_limiter.acquire(cancel_event)
        response = request_func(url, **kwargs)
        if self._response_is_rate_limited(response):
            if self.request_limiter is not None:
                self.request_limiter.pause_current_stage()
            raise RateLimitError("飞书接口触发 99991400")
        return response

    @staticmethod
    def _response_is_rate_limited(response):
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                return False
            data = response.json()
            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            return str(data.get("code")) == "99991400" or str(nested.get("code")) == "99991400"
        except (ValueError, AttributeError):
            return False

    def _refresh_tenant_token(self):
        """获取/刷新 tenant_access_token（内部方法，需在外层加锁或已确认需要刷新）"""
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        resp = self._request("POST",url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }, timeout=10)
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)
        data = resp.json() if resp.text else {}
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

    @property
    def user_access_token(self) -> str:
        """返回当前 user_access_token，用于并发刷新判断"""
        return self._user_access_token

    def refresh_user_token(self, refresh_token: str) -> dict:
        """使用 refresh_token 换取新的 user_access_token"""
        if not refresh_token:
            raise RuntimeError("未配置 refresh_token")
        url = "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token"
        resp = self._request("POST",url, json={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=10)
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"刷新 user_access_token 失败: {data}")
        result = data.get("data", data)
        access_token = result.get("access_token")
        if not access_token:
            raise RuntimeError(f"刷新 user_access_token 返回数据异常: {data}")
        return {
            "user_access_token": access_token,
            "refresh_token": result.get("refresh_token", refresh_token),
        }

    def refresh(self):
        """刷新 token（线程安全，多线程下只刷新一次）"""
        with self._lock:
            if self.access_token_type == "user":
                raise RuntimeError("user_access_token 已过期")
            self._refresh_tenant_token()

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}"}

    def create_export_task(self, token: str, doc_type: str,
                           file_extension: str, sub_id: str | None = None,
                           cancel_event=None) -> str:
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
        resp = self._request("POST", url, headers=headers, json=body, timeout=30, cancel_event=cancel_event)
        if resp.status_code != 200:
            raise FeishuAPIError(resp.status_code, resp.text)
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"创建导出任务失败: {data}")
        return data["data"]["ticket"]

    def poll_export_result(self, ticket: str, doc_token: str,
                           interval: int = 3, timeout: int = 300,
                           cancel_event=None) -> dict:
        """轮询导出任务结果，使用指数退避策略"""
        url = f"{self.base_url}/drive/v1/export_tasks/{ticket}"
        params = {"token": doc_token}
        deadline = time.time() + timeout
        current_interval = interval

        while time.time() < deadline:
            resp = self._request("GET", url, headers=self._auth_headers(),
                                params=params, timeout=30, cancel_event=cancel_event)
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
            if cancel_event and cancel_event.wait(current_interval):
                raise RuntimeError("下载已取消")
            if not cancel_event:
                time.sleep(current_interval)
            current_interval = min(current_interval * 1.5, 15)

        raise TimeoutError(f"导出任务超时 ({timeout}s)")

    def download_file(self, file_token: str, save_path: str, cancel_event=None):
        """下载导出的文件到本地（原子写入：先写临时文件再 rename）"""
        url = f"{self.base_url}/drive/v1/export_tasks/file/{file_token}/download"
        resp = self._request("GET", url, headers=self._auth_headers(),
                            stream=True, timeout=(10, 120), cancel_event=cancel_event)
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
