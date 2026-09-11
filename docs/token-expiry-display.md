# 新站账号凭据到期时间

`auth_mode=flow` 不使用 Labs AT，因此 `at_expires` 保持空值。管理页根据新字段显示已同步登录 Cookie 的到期时间，旧 Labs 账号仍沿用 AT 显示。

## 管理接口

`GET /api/tokens` 和 `POST /api/tokens/{id}/refresh-at` 的账号对象新增：

- `flow_cookie_expires_at`：UTC ISO 8601 日期，或 `null`。
- `flow_cookie_expiry_status`：`known`、`expired`、`session`、`incomplete`、`unavailable`、`invalid`。
- `flow_cookie_has_session_cookies`：是否包含无固定到期时间的登录 Cookie。

日期来自当前数据库中已同步 Cookie 快照，不额外访问 Google、不传回 Cookie 值、不修改认证逻辑或 `at_expires`。

范围为 `.google.com` 根路径的 `SID`，以及 `flow.google.com` 根路径的 `OSID` / `__Secure-OSID`。取这些已同步关键登录凭据中最早的明确到期时间，排除统计 Cookie、其他域名和路径。缺少 Google SID 或 Flow OSID 组时标记凭据不完整，不推算日期。

已到期条目仅在展示解析中保留，正常认证导入仍丢弃已到期 Cookie。纯会话 Cookie 不伪造日期；混合快照显示有日期条目的最早到期时间，并注明另含会话项。非法或超出日期范围的元数据降级为到期未知。

Cookie 到期时间不等于保证有效的登录期限：账号退出、上游撤销或其他验证失败都可能提前使会话不可用。页面通过说明明确区分，并将新版账号的“刷新AT”操作改为“验证会话”；验证成功不宣称 Cookie 已续期。

## 兼容与验证

不需要迁移数据库、不需要重新同步现有完整快照，也不需要更新插件。新前端连接尚未升级的后端时显示“到期未知”，不会回退到旧 AT 字段。

```powershell
python -m pytest tests -q
node --test tests/test_token_expiry_ui.cjs
```

2026-09-11 本地验证：470 项 Python 测试、6 项 JavaScript 测试通过；浏览器使用本地模拟接口检查 7 种显示状态与会话验证提示。没有提交计费生成任务。

另将新解析器仅在服务器独立诊断进程内运行，对 3.5 pre Cookie 快照进行只读核验：24 / 24 个新版账号均得到 `known` 到期日期。此核验不修改服务器代码、数据库或账号会话，不代表已部署。
