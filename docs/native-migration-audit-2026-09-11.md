# Flow 新站迁移补漏（2026-09-11）

## 核查结论与修复

1. **视频状态漏判**：新站适配器把状态 1 当作完成，且未识别失败 / 取消状态。现按已录制官方前端枚举处理，只有 3 产生成功结果；失败和取消立即结束轮询，不进入成功统计或重新提交。
2. **备份仍依赖旧 ST**：管理页以前导出列表中的 `st`，Flow 账号的该值只是内部身份键。现使用独立管理员接口导出真实 Cookie 结构，按 `auth_mode` 导入，导入必须经过 native 身份预检，独立登录账号禁止覆盖。
3. **Cookie 边界与有效期**：Flow OSID 必须具有根路径和非空值；兼容 `expires`、`expirationDate`、`expiry`。`-1` / 缺省表示会话 Cookie，`0` 是已过期时间，不能转成无限期会话。展示可保留过期快照，认证导入仍过滤过期项。
4. **智能同步刷新判定**：`check-tokens` 使用新站关键 Cookie 有效期判断是否接近到期，附带到期元数据，不依赖不存在的 Labs AT，也不向插件状态查询返回原始 Cookie。
5. **同步器遗漏**：配套 flow2api_tupdater 补齐协议登录管理接口、对外取会话结构、同账号确认及新版错误码；默认不再要求 Labs 授权。

## 已录制的视频状态证据

来源：本工作区 `output/playwright/g9-9urm-flow-native-20260911/frontend-wO1vlb.js`，函数 `nM` 的状态映射；由 `oM` 读取媒体视频信息中的状态对象，`wL` 读取枚举。

该文件 SHA-256：`51ad972c43d3c590f3429793a0d5a0b0fced2dd17115f9e117a725cad97679f4`。

| wire 状态 | 官方前端含义 | Flow2API 行为 |
| --- | --- | --- |
| 1 | pending | 等待，不计成功 |
| 2 | pending | 执行中轮询，不计成功 |
| 3 | success | 成功结果处理 |
| 4、7 | failed | 结束任务，计失败 |
| 5 | canceled | 结束任务，不计成功 |
| 6 | scheduled | 排队轮询 |

未知枚举继续拒绝猜测。此修复纠正 `Unrecognized Flow media status 4` 的错误分类，不代表解决了状态 4 对应的上游生成失败原因，更不能据此宣称所有 429 已消失。

## 管理接口与安全

- `GET /api/tokens/export`：管理员鉴权，返回 JSON 数组，禁止缓存。Flow 行包含 `auth_mode=flow` 与 `google_cookies`，ST/AT 为 `null`；Labs 行保留旧结构。导出前明确提示文件含登录凭据。
- `POST /api/tokens/import`：旧 Labs 文件继续支持；Flow 行要求完整 Cookie、`native_cdp` 和目标可访问的同出口代理，验证邮箱及目标会话后才计成功。未通过项明确显示失败，保留请求的禁用状态与并发配置。
- 独立登录保护不解除，凭据导入不等于绕过设备绑定。原始 Cookie 仅由专用管理员导出接口返回，不进入普通列表或状态查询。
- 到期展示字段见 [新站凭据到期说明](token-expiry-display.md)。Cookie 到期时间不是保证登录有效的期限；会话验证也不等于自动续期。

## 验证与边界

本地 Python 全量回归 478 项、两组 Node UI 测试 11 项通过，覆盖状态、终止轮询、原生导入导出、旧账号兼容、独立登录保护、有效期和错误信息脱敏。配套跨项目契约脚本位于工作区 `output/diagnostics/native-audit-20260911/contract.py`：使用真实发送器、HTTP 路由、鉴权、TokenManager 和临时 SQLite，只模拟上游浏览器/Google，8 项检查通过。

```powershell
python -m pytest tests -q
node --test tests/test_token_expiry_ui.cjs tests/test_native_transfer_ui.cjs
```

尚未录制或尚未适配的新模型、纯文生视频及部分放大链路仍应明确报不支持，不能悄悄回退 Labs 或猜测协议。本次没有新增这些模型的支持，也未进行线上部署或计费生成验收。
