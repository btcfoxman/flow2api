# 新 Flow：创建项目 → 上传图片 → 参考图生成（2026-09-11）

## 真实操作结果与来源

用户在已登录的 `g9-9urm.bat` 浏览器手动新建项目、上传 PNG，再基于上传图生成新图片。源 Profile 为 `D:\tmp\g-99`，脚本代理为本机 20019；本轮没有由助手额外提交计费任务、触发插件同步或修改线上服务。

脱敏抓包目录：工作区 `output/playwright/g9-9urm-flow-native-20260911/`。请求 `req-101`、`req-134`、`req-149` 均有完整请求/响应且 ID 唯一。人工流程结果：

| 步骤 | RPC / 语义 | 结果 |
| --- | --- | --- |
| 新建项目 | `jHPbke` / AiSandbox.CreateProject | HTTP 200，返回项目 ID/标题 |
| 上传图片 | `maseQ` / FlowService.UploadImage | HTTP 200，返回媒体/工作流，项目绑定一致；原图 864×1152 |
| 参考图生成 | `ogiZ0b` / FlowService.BatchGenerateImages | HTTP 200，NARWHAL，返回新图片和工作流；结果 1376×768 |
| 下载结果 | `flow-content.google/image/{id}` | 上传图、新生成图片的实际 GET 均为 HTTP 200 |

共同入口：`POST https://flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute`。请求使用当前页面 Cookie 与 `SNlM0e` XSRF、`cfb2h` build、`FdrFJe` 页面会话，没有 Labs ST→AT 或 Bearer AT。

同时下载了当前公开前端模块 `rZcQQd`、`wO1vlb`、`XRV0Af`，用于交叉核对：`tVb` 创建项目、`R2a` 上传、`V2a` 生成、`zTa/wTa` 宽高比枚举。脚本副本保存在上述抓包目录；它们与认证凭据分开。

## 已确认的请求结构

以下值均为占位符。不能重放抓包中的验证码、页面 XSRF、Cookie 或签名链接。

### 创建项目 jHPbke

```json
["projects/*", [null, ["TITLE"]], [null, 22]]
```

响应：`["PROJECT_ID", ["TITLE"]]`。仅创建一个必要项目；同账号项目初始化受锁保护，先列出已有项目再决定创建。超时或响应未知不在当前调用内自动重建。

### 上传图片 maseQ

```json
[
  [null,22,null,null,null,"PROJECT_ID",null,null,null,null,["FRESH_UPLOAD_CAPTCHA",1]],
  "BASE64_IMAGE_BYTES",
  "image/png",
  1,
  null,null,null,null,
  "FILE_NAME.png",
  null,
  "CLIENT_SEED_A",
  "CLIENT_SEED_B"
]
```

验证码 action 明确为 **UPLOAD_IMAGE**，不是 IMAGE_GENERATION 或 VIDEO_GENERATION。字段 2 是图片字节 base64，不含 data URL 前缀；字段 4 表示用户上传。普通上传未设置隐藏、裁剪、目标文件夹等可选字段；当前适配不臆造这些字段。

响应是 `[media, workflow]`：`media[0]` 为媒体 ID，`media[1]` 为项目 ID，`media[2]` 为工作流 ID；须同时核对 `workflow[0]` 和 `workflow[4]`。不能只读到任意 name 就作为上传成功。

### 图片生成 ogiZ0b

```json
[
  null,
  [[null,null,[["UPLOADED_MEDIA_ID",null,null,null,1]],123,3,"NARWHAL",null,
    [null,22,null,null,null,"PROJECT_ID",null,null,null,null,["FRESH_GENERATION_CAPTCHA",1]],
    [[["PROMPT"]]],null,null,null,"CLIENT_SEED_A","CLIENT_SEED_B"]],
  1,
  [null,22,null,null,null,"PROJECT_ID",null,null,null,null,["FRESH_GENERATION_CAPTCHA",1]],
  ["BATCH_ID"]
]
```

- 本次模型是 `NARWHAL`，对应项目已有 `gemini-3.1-flash-image-*` 配置；不是 GEM_PIX_2。GEM_PIX_2 保留此前捕获的模型适配。
- 外层字段 5 必须是 `[batch_id]`，不是 `[[batch_id]]`。已修正先前本地适配多包一层数组的问题。
- 后续生成必须重新取得 **IMAGE_GENERATION** 验证码，不能复用上传验证码。
- 源码确认宽高比：square=1、portrait 9:16=2、landscape 16:9=3、portrait 3:4=4、landscape 4:3=5。
- 新图片地址位于 `media[6][0][13]`；同时校验工作流项目绑定及签名 URL，不能拼出未签名地址。

## Cookie、Storage 与失败归因

此次操作期间观察到 SIDCC / Secure PSIDCC 轮换、`_grecaptcha` 和 `rc::*` 状态变化，以及 `flow-prompt-box-settings` 等 UI 设置变化。没有在这段采样窗口观察到 SID / Flow OSID 值变化；这不是它们长期不变的保证。

Storage 仅记录键名、长度和哈希；这些记录不能重建设备绑定密钥或验证码状态，不作为可直接导入的认证凭据。

另有两次插件 `/api/plugin/update-token` HTTP 400，时间分别约为 14:32、15:32（UTC+8），早于本次 15:56–16:00 的手动链路，间隔约一小时，符合定时同步特征。监听器没有保存目标同步请求正文/错误正文，不能据此断言其具体拒绝原因，也不能将其视为图片生成失败。

旧监听器在长时间运行时出现 `id(request)` 复用，两个无关请求 ID 重复。上述三个关键请求不受影响；已改用 Request 对象作为映射键，并将后续监听切换到 `continued/`。旧日志保留不删、不覆盖，后续分析不得将两个重复 ID 直接一对一关联。

## 落地与验证范围

- `flow_angular.py`：新建/上传 RPC、响应绑定校验、NARWHAL、5 种宽高比、正确批次字段。
- `flow_client.py`：新站原生上传/创建；不再调用 Labs 上传条款确认或 OAuth 上传后备路径。
- `token_manager.py`：新站无项目账号可创建必要项目，失败不启用，写入前复核凭证修订。
- `browser_captcha_native_cdp.py`：创建/上传属于有副作用 RPC，超时和未知响应按结果不确定处理，不自动重放。
- 451 项 Flow2API 测试通过；新增创建、上传、NARWHAL、项目隔离、验证码 action、缺失验证码、未知结果及并发初始化只创建一次等用例。
- `analyze.py --verify-adapters` 对真实捕获的三组请求逐字段比较（仅替换凭据/图片内容并固定随机 ID），全部匹配，三组真实响应均可解析。此验证只在本地运行，没有重放外部请求。

后续已完成本机独立 Profile 同步、重启验证及本地 FlowClient 文生图/下载成功；同时保留首次实测结果不确定的记录，详见 [独立 Profile 实测](flow-native-verification-2026-09-11.md)。未 push/部署，未做 3.5 pre 单账号新同步及 API 生成实测。上述结果不等同于线上服务已恢复。未捕获模型、图片放大和完整视频链路仍需分别验收。
