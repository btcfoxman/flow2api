# Flow 新站认证迁移（本地实测通过，生产待验收）

## 原因与边界

此前同步先调用 Labs `/auth/session` 换 OAuth AT，再调用旧 credits 接口。Flow 新站已经登录时，Labs 授权仍可过期，因此旧同步会在写入 Cookie 和启用账号之前失败。

新账号路径使用显式 `auth_mode=flow`，不再要求 Labs ST/AT。原数据库账号默认保留 `labs`，仅在新站同步通过身份验证后迁移，避免无验证地切换全部线上账号。源与目标 Profile 分离，服务器独立登录标记依然阻止外部覆盖。

## 请求与验证链路

`源 Flow 页面身份 → 完整 scoped Cookie + 同出口目标代理 → 临时隔离目标浏览器验证身份/余额/项目 → 按已验证邮箱更新账号 → 目标真实 Profile 再验证 → 按自动启用配置决定启用`

插件 `/api/plugin/update-token` 新请求字段：

- `auth_mode: "flow"`
- `email`：源页面取得，接收端必须独立核对，不作为可信查找授权
- `google_cookies`：保留 domain/path/secure/httpOnly/sameSite/expires，排除分区及非允许域
- `captcha_proxy_url`：必须明确提供目标可访问的同出口地址

不发送 OAuth AT、Labs ST、页面 XSRF、reCAPTCHA 或设备绑定密钥。旧数据库 ST 的非空唯一约束使用 `flow:` 前缀的非秘密账号标识兼容；它不是凭证，禁止送往 OAuth 接口。

成功确认必须包含 `auth_mode=flow`、`flow_identity_verified=true`、`native_session_verified=true`、`account_active=true`，并确认 Cookie/代理。保存但目标未通过预检、账号仍禁用或旧版本缺少确认字段时，客户端不得显示成功。

## 已确认协议来源

- 2026-09-08 脱敏人工操作记录：`flow-angular-capture-2026-09-08.md` / `flow-angular-rpc-2026-09-08.json`。
- 2026-09-11 官方新站公开前端（本地研究副本在工作区 `output/playwright/flow-native-auth-20260911/frontend.js`）：`oPEP7c` 是邮箱兜底字段；`SNlM0e` 是页面 XSRF，`cfb2h`、`FdrFJe` 是页面 bootstrap，均不是 OAuth AT。
- `nzlxg` = `/VideoFxService.GetCredits`，请求 `[]`；响应字段 1 为余额，字段 2 为 paygate tier。零余额需结合有效 tier 识别，不把空响应视为成功。
- `UpteDb` 查询已有项目；`ogiZ0b` 为已捕获 GEM_PIX_2 单图请求。图片结果从媒体结构提取项目绑定与带签名 URL，不自行拼接未签名地址。

## 安全与兼容

候选导入先在临时 Profile 验证，不以客户端邮箱直接覆盖数据库。临时验证也受浏览器池容量限制。已有 worker 忙碌时拒绝同步；停止与写入受同一账号锁保护。凭证修订变化使验证缓存失效；失败不伪造 AT 有效期、不强制启用、不重放旧 Cookie 绕过设备绑定。

旧 Labs 客户端不能覆盖已迁移的新站账号。管理端修改新站账号备注/并发不再先调用 ST→AT；手动刷新改为新站预检。其他旧账号和非 native 模式保留显式兼容路径，不能误称为已迁移。

## 尚未完成的验收与接口

1. 已完成真实源浏览器只读验证及本机独立目标 Profile 的单账号同步、重启验证；3.5 pre 服务器端仍待验收。详见 [独立 Profile 实测](flow-native-verification-2026-09-11.md)。
2. 后续人工操作已补齐新站图片上传 `maseQ`、项目创建 `jHPbke` 的成功证据并落地适配，详见 [创建/上传/生成链路](flow-image-upload-2026-09-11.md)。部署后仍须验收目标 Profile。
3. 已捕获 GEM_PIX_2、NARWHAL、Abra 参考/编辑视频协议。NARWHAL 网页人工生成、本地独立目标 Profile 的 FlowClient 文生图及下载均已有成功；生产 API、视频仍待实测。另一次本地图片实测结果不确定，失败记录保留。未捕获模型、图片放大不冒用旧 OAuth 接口。
4. 尚未 push/部署；桌面批量脚本保持停止。插件本地构建不更新实际已加载目录，须服务端升级和单账号验收后再启用批量。

## 本地验证记录

- Flow2API：`python -m pytest tests -q --tb=short`，458 passed（本轮最终回归）。
- flow2api_tupdater：项目 venv `python -m pytest tests -q --tb=short`，141 passed。
- 插件：`node --test tests/defaults.test.cjs tests/session_sync.test.cjs`，17 passed；包含无 Labs Cookie 同步、缺失目标确认失败、未确认源身份不发请求。
- 三库 `git diff --check` 通过；插件和 tupdater 前端 JavaScript 语法检查通过。
- 插件本地私有构建：工作区 `output/releases/Flow2API-Token-Updater-1.3.0-20260911-131942` 及同名 ZIP。未替换浏览器实际加载目录，缓存配置未动。
- 已进行两次最多一次尝试的本地图片实测：一次结果不确定，一次生成/下载成功；只读余额始终为 50。未验证 3.5 pre 新路径，不能替代部署后验收。

## 真实源浏览器只读核验（2026-09-11 13:57，UTC+8）

用户手动打开 `g9-9urm.bat`。连接其现有 `D:\tmp\g-99` Profile / CDP 9226，脚本代理 20019；没有启动其他脚本或主动触发插件同步。

- 实际页面 `https://flow.google.com/`，账号 `urmiladevi354555@gmail.com`，PRO；`oPEP7c` 邮箱与 UI 一致，SNlM0e/cfb2h/FdrFJe 均存在。
- `.google.com` SID、Flow OSID 均存在；只读取并展示元数据，未导出凭证值。
- 在原浏览器执行 `nzlxg([])`：HTTP 200，单帧，余额 50、tier=1。响应字段 3/4 均为 number（枚举），字段 5 null、字段 6 为 50。当前总余额解析与真实结果相符。
- 在同一页面执行 `UpteDb(["projects/*",21,null,null,null,null,[1]])`：HTTP 200，4 个项目，ID/标题结构符合当前适配。
- 上述请求仅新站同源 Cookie + 页面 XSRF，没有执行 Labs OAuth、没有发送 Bearer AT、没有生成计费任务。
- 监听记录目录：工作区 `output/playwright/g9-9urm-flow-native-20260911/`。用户后续已完成新建项目、上传及 NARWHAL 图生图，详见上述补充链路文档。
