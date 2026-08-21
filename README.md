扫描文件夹中的 `.url` 快捷方式，识别文件类型，调用飞书导出 API 批量下载文档。
支持：doc、docx、sheet、bitable、mindnote

开发者：Rrrjs

GUI预览
![image](https://raw.githubusercontent.com/Rrrjs/DocDownloader/refs/heads/main/preview/screenshot.png)

## 文件结构

| 文件 | 说明 |
|---|---|
| `main.py` | 入口，主下载循环 |
| `config.py` | 配置加载 |
| `url_parser.py` | URL 解析与文件夹扫描 |
| `feishu_api.py` | 飞书 API 调用（支持 tenant/user 鉴权） |
| `auth_helper.py` | OAuth 授权模块（浏览器自动获取 token） |
| `utils.py` | 工具函数（文件名清理、重名处理等） |
| `requirements.txt` | Python 依赖 |
| `config.json` | 凭据与导出配置（不提交 Git） |

## 使用方法

```bash
# 安装依赖
pip install -r requirements.txt

# 运行后按提示输入文件夹路径
python main.py
```

要求 Python 3.10+

## 鉴权模式

支持两种鉴权模式，通过 `access_token_type` 切换：

### user 模式（推荐）

以用户身份访问文档，拥有用户本人的所有文档权限，无需额外配置应用权限。

```json
{
    "feishu": {
        "app_id": "cli_xxxxxxxxxx",
        "app_secret": "xxxxxxxxxxxxxxxx",
        "access_token_type": "user",
        "user_access_token": ""
    }
}
```

获取 token 方式（二选一）：

**方式一：自动获取（推荐）**
`user_access_token` 留空，运行 `python main.py` 后会自动打开浏览器，在飞书页面授权即可，token 会自动写入 `config.json`。

**方式二：手动获取**
1. 打开 [飞书 API 调试台](https://open.feishu.cn/api-explorer/)
2. 选择任意 API，点击「获取 token」→「user_access_token」
3. 授权后复制 `user_access_token` 填入配置

前置条件：在飞书开放平台 → 你的应用 → 安全设置中，添加重定向 URL：`http://localhost:18234/callback`

token 有效期 2 小时，过期后运行中会自动重新授权。

### tenant 模式

以应用身份访问文档，需要在飞书开放平台配置应用权限。

```json
{
    "feishu": {
        "app_id": "cli_xxxxxxxxxx",
        "app_secret": "xxxxxxxxxxxxxxxx",
        "access_token_type": "tenant"
    }
}
```

## 配置说明 (config.json)

| 字段 | 说明 | 默认值 |
|---|---|---|
| `feishu.app_id` | 飞书应用 App ID | 必填 |
| `feishu.app_secret` | 飞书应用 App Secret | 必填 |
| `feishu.access_token_type` | 鉴权模式：`user` 或 `tenant` | `tenant` |
| `feishu.user_access_token` | 用户 token（user 模式必填，留空则自动授权） | 空 |
| `export_types` | key 为 URL 中识别的文件类型，value 为传给 API 的导出格式 | 见下方 |
| `base_url` | 飞书 API 基础地址（私有化部署可改） | `https://open.feishu.cn/open-apis` |
| `poll_interval_seconds` | 轮询导出结果的初始间隔（秒） | `3` |
| `poll_timeout_seconds` | 导出任务超时时间（秒） | `300` |
| `conflict_policy` | 文件重名策略：`rename`/`overwrite`/`skip` | `rename` |

默认 `export_types`：
```json
{
    "doc": "docx",
    "docx": "docx",
    "sheet": "xlsx",
    "bitable": "xlsx",
    "mindnote": "pdf"
}
```

## URL 识别规则

| URL 格式 | 类型 | 默认导出格式 |
|---|---|---|
| `https://xxx.feishu.cn/docs/{token}` | doc（旧版文档） | docx |
| `https://xxx.feishu.cn/docx/{token}` | docx（新版文档） | docx |
| `https://xxx.feishu.cn/sheets/{token}` | sheet（电子表格） | xlsx |
| `https://xxx.feishu.cn/bitable/{token}` | bitable（多维表格） | xlsx |
| `https://xxx.feishu.cn/base/{token}` | bitable（多维表格旧路径） | xlsx |
| `https://xxx.feishu.cn/mindnotes/{token}` | mindnote（思维笔记） | pdf |

同时支持 `larksuite.com` 域名（国际版）。

暂不支持：wiki（需要额外解析实际文档类型）。

## 工作流程

1. 扫描文件夹中所有 `.url` 快捷方式
2. 解析 URL 识别文件类型和 token
3. 过滤出 `export_types` 中配置的类型
4. 用户确认后开始下载
5. 对每个文件：创建导出任务 → 轮询结果 → 下载保存（原子写入）
6. 下载的文件保存在对应快捷方式的同目录下
7. token 过期时自动重新授权，继续下载剩余文件
8. 完成后汇总成功/失败数量，列出失败文件

## 注意事项

- `config.json` 包含飞书应用凭据，已通过 `.gitignore` 排除
- 下载采用原子写入（先写临时文件再 rename），中断不会产生坏文件
- 文件名中的 `\/:*?"<>|` 等非法字符会自动替换为 `_`
- 轮询采用指数退避策略（以3s为例 3s → 4.5s → 6.75s → ... 最大 15s），减少 API 请求
