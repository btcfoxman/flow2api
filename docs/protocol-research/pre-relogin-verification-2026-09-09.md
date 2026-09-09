# 2026-09-09 重新登录后的隔离验证

## 结论

源站新登录真实有效，但该会话复制到服务器另一 Native Profile 后仍不可用。本轮已排除生产并发及实际公网出口不一致；不能把完整 Cookie 快照视为可迁移会话。未提交任何新生成任务。

现有证据支持会话迁移/绑定限制为主要调查方向，但不足以断言设备绑定是唯一根因。不继续重复导入会话或消耗积分；保持源同步服务和执行服务分离，后续是否改为目标 Native Profile 独立登录，需用户确认。

## 验证顺序与证据

1. 用户确认重新登录。Playwright 快照显示源 `flow.google.com/` 的账号 43 对应账户和项目列表。
2. 源 `/fx/api/auth/session` 仍返回旧 OAuth AT（内存指纹与上轮相同），该 AT 查询 REST credits 返回 401。不能把新站 Cookie 登录成功等同于旧 Labs OAuth 已更新。
3. 源真实 Angular RPC `ngNC2` 返回 200，可解析项目结果。
4. 查询原任务 `b1074259-9507-48f6-9eb5-86d624a5437c`，RPC `as29s` 已有签名视频地址；通过源账号代理范围下载返回 206、32 字节、`video/mp4`、MP4 `ftyp` 文件头有效。因此上轮日志 26295 的轮询 401 并不代表视频未生成。
5. 临时将账号 43 的 `is_active/image_enabled/video_enabled` 设为 0，原值保存于服务器 `/app/tmp/relogin-canary-20260909/account-43-isolation.json`。确认生产 Native worker 未运行、未 busy，近期已受理 processing 任务为 0。
6. 再次确认源 RPC 200；读取 31 个 Google Cookie，完整性校验通过。快照仅经 SSH 标准输入传入隔离进程内存，不写生产 tokens 记录，不调用会自动启用账号的插件同步接口。
7. 使用已部署候选代码、原账号代理、单一目标 Profile `/app/tmp/relogin-canary-20260909/native/token-43`。11:48:20 +08，目标通过页面准备后，真实 RPC 返回 `upstream_authentication_rejected`（HTTP 401）。未调用生成接口。
8. 隔离目标浏览器关闭后，源同一 RPC 也返回 401；刷新源页落到 `/about`。不是只有页面 bootstrap/XSRF 缓存显示问题。
9. 通过双方账号配置代理分别访问相同公网地址查询服务，在内存中比较地址摘要，结果 `same_actual_public_egress=true`。未记录公网地址或代理凭证。
10. 通过源 Chrome 152 页面 CDP 的设备绑定会话观察接口，只读取脱敏元数据：存在 2 个 google.com 绑定会话，刷新端点分别为 `accounts.google.com/RotateBoundGaps` 与 `/RotateBoundCookies`；绑定 Cookie 包含 `__Host-GAPSTS` 及 `__Secure-1PSIDTS/__Secure-3PSIDTS/__Secure-1PSIDRTS/__Secure-3PSIDRTS`。未导出设备密钥、会话 ID 或挑战证明，未改变安全功能。
11. 按测试前备份、经条件校验恢复账号 43 的三个调度字段，确认全部恢复为 1。没有调用 enable API 重置错误计数，也没有覆盖账号 ST/AT、Google Cookie、代理或统计。

## 解释边界

- Google 的设备绑定会话通过设备密钥续期短期 Cookie；仅复制 Cookie 不等于复制完整会话。参见 [Chrome 官方 DBSC 文档](https://developer.chrome.com/docs/web-platform/device-bound-session-credentials)。这是机制解释，不能代替当前 Flow 请求具体触发路径的证据。
- 当前排除项：测试时生产同账号并行执行、双方实际公网 IP 不一致、不完整主域 Cookie。
- 尚未区分：设备绑定失效、跨设备登录风险撤销及其他 Google 会话轮换机制。未进行关闭安全功能、复制设备密钥等绕过尝试。
- 不采用源浏览器作为生成执行器，未变更此前要求的源同步/目标执行分离架构。

## 线上任务与代码状态

- 原任务视频存在且下载检查通过，但数据库失败状态尚未回写，签名地址也未写入日志；需要恢复可用认证后按原 task ID 查询、执行原有水印/缓存后处理，再修复任务和统计，不能重做生成。
- 本轮无新增付费生成，不等于上轮已受理任务没有扣费；事后真实余额仍未验证。
- 3.5 保持上轮候选镜像，本轮未再次部署、未提交 push；业务代码未新增修改，只补充验证脚本和研究记录。
- 本轮诊断脚本位于忽略目录 `output/playwright/`：`relogin_isolated_bridge.py`、`verify_relogin_egress.py`、`inspect_source_binding_metadata.py`、`restore_relogin_isolation.py`。

## 下一步需要的选择

建议保留多个项目分离，但由 3.5 的每账号 Native Profile 独立完成登录并持久化；同步程序不覆盖这些目标 Profile 的设备绑定会话。该方案涉及登录态管理方式调整，未获确认前不落地。

在验证目标会话可用之前，不反复要求源站重新登录并复制 Cookie，不提交新的 360p/4 秒任务。
