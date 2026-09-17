# 服务器统一账号登录（2026-09-17）

## 目标与边界

将 tupdater 中的服务器账号管理、远程登录和周期检测能力整合进 Flow2API，
不嵌套另一个同步服务、数据库或源浏览器。一个账号的人工登录、身份校验、额度检测、
项目接口、图片/视频生成全部使用 `NativeCdpAccountBrowser` 的同一个持久化 Profile。

控制台 `/manage` 的“账号登录 / 新增”进入 `/account-login`；账号行也提供直达入口。
数据与会话状态复用现有 Token 数据库和 `SessionAvailability`，不再产生另一份“源会话状态”。
生成 API、模型名称和异步任务接口保持兼容。

这消除的是跨 Profile 的凭据复制与状态分歧，不是 Google 重新认证要求。
平台撤销会话、需要验证码或其他人工验证时，仍需管理员重新登录；不会自动输入 Google 密码，
不保存密码，也不伪造 Cookie 保留期限为授权有效期。

## 使用与迁移

1. 部署新的 **headed** 镜像，启用 `native_cdp`，保留原来的数据和浏览器 Profile 挂载。
2. 登录 Flow2API 控制台，选择已有账号（复用现有生成端 Profile）或创建待登录账号。
3. 配置该服务器可访问的账号代理；已有本地 Profile 的代理绑定不能直接改为另一出口。
4. 打开登录窗口。账号有已分配/执行中的任务或正在检查时会拒绝打开，请待其结束；不会中断任务。
5. 在画面中确认登录的是选定邮箱，进入 `https://flow.google.com/`。
6. 点击“完成登录并验证”：撤销画面访问 → 优雅关闭登录浏览器并落盘 →
   用同一 Profile 在生成显示环境重新打开 → 验证邮箱及额度、项目 RPC → 成功后恢复接单。
7. 所有账号完成迁移和业务验收后，再停用独立 tupdater 的同步任务与旧 18002 管理入口。
   本次代码变更不会自行停止旧服务、删除其数据、批量改动账号或启动本机 BAT。

首次打开会将账号转为本地持有；原 Profile 文件保留，数据库不再保留其外部 Cookie 快照。
本地 ownership marker 在登录前写入（**它仅代表管理权，不代表登录成功**），
外部插件、tupdater 和导入接口不能再覆盖此账号的会话。
取消、超时、重启中断或校验失败都保持账号暂停，不自动启用。
旧导入账号未执行上述迁移前，仍保留原来的兼容同步行为。

## 自动检测与调度

- 默认每 300 秒复检已启用、本地管理的账号；`NATIVE_ACCOUNT_CHECK_INTERVAL_SECONDS` 可设为 60–3600。
- 使用与生成端相同的浏览器、代理、凭据版本和验证结果，复用并发检查合并机制；不额外生成媒体或消耗生成积分。
- 检查忙碌账号时跳过，失败遵循现有回退间隔；不会自动启用手动暂停的账号。
- 后台检测仍需打开该账号的服务器浏览器，因此受浏览器池大小、排队和网络时延影响，300 秒不是精确时限。
- 超过 15 分钟未重新成功验证，界面标为“需复检”，保留最后检测时间；不把历史成功当成当前成功。
- 登录占用同时保护调度待执行计数、图片/视频并发槽及 CDP 入口。其他账号继续工作，
  但登录窗口也占一个浏览器池席位；浏览器池为 1 时没有额外生成浏览器容量。
- Profile 目录、账号代理及运行用户须持久且一致。重启后的状态从“待验证”开始，实际检测后才显示通过。

## 远程登录与安全

无需新增 6080 端口或设置 `VNC_PASSWORD`。管理员只访问 Flow2API 原地址，例如
`http://192.168.3.5:4020/account-login`（实际端口以现有部署映射为准）。
正式使用建议通过 HTTPS 反向代理，转发同源 `/account-login/websockify` 的 WebSocket Upgrade。

- 每次登录独立 Xvfb 显示环境，不暴露生成端共享桌面，也不展示其他账号的窗口。
- 全局最多一个人工登录窗口，20 分钟硬超时，绑定创建它的管理员登录会话。
- 画面凭据是短期随机票据，HttpOnly / SameSite=Strict，HTTPS 下设置 Secure；不会把管理员 Token 放 URL。
- VNC 仅监听 127.0.0.1，每次生成独立的临时 RFB 密码（8 字符协议限制），临时文件仅服务用户可读。
- WebSocket 校验同源、有效管理员、窗口归属和期限；固定代理当前窗口的本地端口，不接受客户端目标地址。
- 登出、改管理员密码、结束窗口或验证阶段都会撤销访问。资源路径限于 noVNC 的 core/vendor 模块。
- 登录浏览器先优雅关闭，随后终止并回收 VNC、窗口管理器和 Xvfb；异常/取消也执行清理。
- `/tmp` 仅允许顶层媒体文件，禁止 Profile、Cookie、隐藏文件和符号链接经媒体下载接口暴露。

## 部署与持久化

`Dockerfile.headed` 增加 `x11vnc`、`novnc`，继续只暴露原 4020 应用端口。
保持单个应用 worker（浏览器池、管理员会话、登录租约均由应用进程管理）。
保持 `init: true` 和现有优雅停止时间，不使用强制 kill 替代正常停止。
部署前检查 `flow2api_account_login_active` 指标；登录窗口启动、登录、验证或清理期间均拒绝重启。
该检查与已有任务检查一样不是原子停流；正式维护期间仍不要同时打开新登录窗口。

默认 Profile 根目录仍为 `/app/tmp/native_cdp_profiles`；沿用现有 `/app/tmp` 持久卷，
没有悄悄迁移目录、复制另一项目的 Profile 或覆盖 Google 的设备相关状态。
自定义 `NATIVE_CDP_PROFILE_ROOT` 时必须保证该目录也持久化并与账号数据库一起备份。

新增接口均要求控制台管理员鉴权：

| 接口 | 作用 |
| --- | --- |
| `POST /api/accounts` | 按邮箱、账号代理创建暂停的本地账号 |
| `GET /api/account-login` | 环境能力、当前登录窗口、剩余时间 |
| `POST /api/accounts/{id}/login` | 预约账号并打开同 Profile 登录窗口 |
| `POST /api/account-login/{session}/viewer` | 为所属管理员签发同源画面票据 |
| `POST /api/account-login/{session}/finish` | 保存、实际验证、成功后恢复接单 |
| `POST /api/account-login/{session}/cancel` | 保存并关闭，保持暂停 |

## 验证

`python -m pytest -q` 包含调度互斥、错误/取消清理、所有权与代理绑定、凭据隔离、
管理员鉴权、Origin、WebSocket 撤销、媒体目录防泄露等回归。
`test_real_isolated_desktop_requires_rfb_password_and_reaps_all_children` 在具备 Linux headed 依赖时
真实启动临时显示环境、检查 RFB 必须认证和子进程回收；Windows 自动跳过。
pre CI 在测试阶段安装依赖，执行此检查后才构建发布镜像。

本地 UI 检查使用合成账号/模拟 API，不等于 Google 登录或 3.5 pre 生成验收。
线上验收需要在部署后选择单账号完成实际登录、验证、图片/最低消费视频生成，
再经历正常浏览器回收和服务重启复检，最后逐步迁移其他账号。

参考：[noVNC RFB API](https://github.com/novnc/noVNC/blob/master/docs/API.md)、
[x11vnc 官方选项](https://github.com/LibVNC/x11vnc/blob/master/doc/OPTIONS.md)。
