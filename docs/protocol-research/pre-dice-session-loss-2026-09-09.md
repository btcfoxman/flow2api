# 独立 Profile 冷启动登录丢失：DICE OAuth 诊断

## 新证据

在 3.5 目标账号 43 的 Chromium `chrome://signin-internals` 中直接读取到：

- Account Consistency: DICE。
- Signin Status: Not Signed In；TokenService Load Status: Load credentials finished with success。
- Accounts in Token Service: No token in Token Service。
- RefreshToken Received: `Missing authorization code due to OAuth outage in Dice.`，时间 `2026-09-09T04:25:11.302Z`。
- 冷启动时账号协调器已不再 blocked，状态 OK；根域 SID/PSID 随后缺失、RPC/OAuth 返回 401。

这与之前“同进程真实图片/视频成功，但正常退出后重启失效”的观察吻合。不是 Cookie SQLite 文件没有写入的充分证据，也不是密码存储解密错误的诊断。

## 上游机制与结论边界

Chromium 的 [DICE 响应处理实现](https://chromium.googlesource.com/chromium/src/+/ed60de37f0a454d6585d7f3c43ca3913c91a64af/chrome/browser/signin/dice_response_handler.cc) 在无授权码的 OAuth outage 情况下，使用内存中的锁临时暂停账号协调；锁不等于持久化浏览器刷新凭据。[相关浏览器回归测试](https://chromium.googlesource.com/chromium/src/+/HEAD/chrome/browser/signin/dice_browsertest.cc) 也验证了锁定和超时解除行为。[账号协调逻辑](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/components/signin/core/browser/account_reconcilor.cc) 存在清空网页账号会话的路径。

由这些证据推断，浏览器账号协调是本次冷启动会话丢失的高度匹配机制。仍需重新登录后实际冷启动验收，不能把推断写成“所有历史 429 的唯一原因”。不通过重复回放旧 Cookie 或关闭网站风控来处理。

## 专用 Profile 修复

将没有既有 Chrome 账号的服务专用 Profile 设为仅网页登录：启动前保留整个 Preferences 的其他字段，仅设置 `signin.allowed=false` 与 `signin.allowed_on_next_startup=false`。这是浏览器自身登录/同步的偏好设置，不是禁止 Google 网页登录；[Chrome 官方说明](https://support.google.com/chrome/a/answer/2657289?hl=en) 区分了浏览器登录/同步这一设置。

- 已有 `account_info` 或 Chrome 主账号的 Profile 不自动迁移，避免退出用户的既有 Chrome 账号。
- Cookie、Storage、设备绑定、网站登录、验证码及代理均不关闭、不清空、不伪造。
- 保存原先两个偏好值的私有备份；原子替换配置；损坏配置拒绝覆盖。
- Flow2API Native 浏览器与 tupdater 的全部持久化启动入口分别落实，不新增跨项目依赖。
- 不修改 g1 普通浏览器或插件使用者的 Chrome 设置。

容器内无真实账号的临时 Profile 实测：`Account Consistency=None`、`Account Reconcilor State=Inactive`、`Gaia cookies state=Allowed`。这证明设置已生效，**不等于真实网页登录冷启动已验收**。已经被撤销的登录仍需用户重新登录后才能继续验证。

## 已发布基线

Flow2API `ad77e5e` 已通过本地 339 项测试、Linux CI 并实际部署到 3.5，健康接口 200。tupdater `f4e743c` 已成功构建、远程拉取并启动；此前 GHCR 静态凭据拒绝已修复。插件本地 1.2.2 包全部 16 个文件与源码一致，13 项 Node 测试通过。

本文件所述 DICE 专用 Profile 修复是在该基线上继续增加；实际发布与真实账号冷启动验收需另行记录。任务池观察到 57 条等待任务，无效会话退避开始生效，但目前不能称任务处理能力已全面恢复。
