# Flow 浏览器协议观察（2026-08-31）

## 采集范围

- 浏览器标签：`g2-2ron`
- Chrome Profile：`D:/tmp/g-42`
- 浏览器出口：`socks5://127.0.0.1:20002`
- 页面：Flow 项目页
- 原始记录：`output/playwright/flow-g2-2ron-20260831-164507/`
- 原始记录可能包含账号、项目、提示词和媒体元数据，已通过 `.gitignore` 排除，禁止直接提交。
- 本文和同目录 JSON 仅保留协议字段、模型目录和脱敏结果。

## 已验证请求链

```text
Flow 项目初始化
  -> flow.projectInitialData（返回 modelConfig）
  -> flow/appConfig + flow/models/statuses
  -> flow/uploadImage
  -> video:batchAsyncGenerateVideoReferenceImages
  -> video:batchCheckAsyncVideoGenerationStatus（约 10 秒一次）
  -> flowWorkflows/{workflowId}（更新 primaryMediaId）
```

R2V 360p 的关键提交结构：

```json
{
  "mediaGenerationContext": {
    "batchId": "<stable-per-launch>",
    "audioFailurePreference": "BLOCK_SILENCED_VIDEOS"
  },
  "clientContext": {
    "projectId": "<project-id>",
    "tool": "PINHOLE",
    "userPaygateTier": "PAYGATE_TIER_ONE",
    "sessionId": ";<milliseconds>",
    "recaptchaContext": "<redacted>"
  },
  "requests": [
    {
      "outputSpec": {"resolution": "VIDEO_RESOLUTION_360P"},
      "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT",
      "textInput": {"structuredPrompt": {"parts": [{"text": "<prompt>"}]}},
      "videoModelKey": "abra_r2v_8s_360p",
      "seed": 12345,
      "metadata": {},
      "referenceImages": [
        {"mediaId": "<media-id>", "imageUsageType": "IMAGE_USAGE_TYPE_ASSET"}
      ]
    }
  ],
  "useV2ModelConfig": true
}
```

轮询请求保持精简：

```json
{
  "media": [
    {"name": "<media-id>", "projectId": "<project-id>"}
  ]
}
```

## 实操结果

| 提交时间 | 模型 | 分辨率 | 比例 | 参考图 | 最终状态 | 耗时 |
| --- | --- | --- | --- | ---: | --- | ---: |
| 16:47:09 | `abra_r2v_6s_360p` | 360p | 9:16 | 2 | SUCCESSFUL | 36 秒 |
| 16:51:15 | `abra_r2v_8s_360p` | 360p | 9:16 | 1 | SUCCESSFUL | 35 秒 |

两个提交和全部轮询均为 HTTP 200。模型健康接口同时返回 `abra = MODEL_HEALTH_STATUS_HEALTHY`。

提交响应采用新的 `workflows + media` 结构，轮询响应采用 `media` 结构；项目客户端需要把它们归一化为内部 `operations`。第一次提交响应直接返回 `remainingCredits=6`，第二次余额降到 0 时该字段被省略，最终成功轮询才明确返回 `remainingCredits=0`。因此不能把“响应里没有余额字段”解释成余额没有变化。

17:51:58 的后续页面操作仅触发两次会话刷新请求，均为 HTTP 200；没有出现上传、模型配置、生成提交或状态轮询请求，因此这一操作没有带来新的协议差异。

## 最新模型目录

项目初始化数据把 `abra` 显示为 **Omni 1.1 Flash**，包含 34 个 usage：

- T2V：4/6/8/10 秒，720p 默认键与 `_360p` 键。
- R2V：4/6/8/10 秒，720p 默认键与 `_360p` 键。
- 单首帧 I2V：`abra_i2v_*`，4/6/8/10 秒，720p 与 360p。
- 首尾帧 I2V：`omni_flash_i2v_*_first_last`，4/6/8/10 秒，720p 与 360p。
- 视频编辑：`abra_edit` 与 `abra_edit_360p`。

模型目录给出的精确耗点如下，三个 service tier 的价格一致：

| 时长 | 720p | 360p |
| ---: | ---: | ---: |
| 4 秒 | 7 | 4 |
| 6 秒 | 10 | 5 |
| 8 秒 | 12 | 6 |
| 10 秒 | 15 | 7 |

视频编辑为 720p 20 点、360p 10 点。R2V 最多 7 张参考图、5 个音频引用、3 个角色；视频编辑最多 5 张参考图、3 个音频引用、3 个角色，输入视频最长 10 秒。所有 34 个 usage 都声明会输出音频，并支持横屏和竖屏。

精确键、需求和端点映射见 `flow-omni-1.1-flash-2026-08-31.json`。

页面说明 360p 用于预览和提示测试，可后续重塑至 720p；360p 视频不能直接扩展。因此 360p 不能只修改 `outputSpec`，还必须使用对应的 `_360p` 模型键。

## 与原代码的差异

采集前的实现存在以下偏差：

1. R2V 固定发送 `VIDEO_RESOLUTION_720P`。
2. `abra_r2v_6s` / `abra_r2v_8s` 在用户选择 360p 时仍使用无后缀上游键。
3. 缺少 T2V/R2V 全部 360p 模型。
4. 缺少 Omni 单首帧和首尾帧 I2V 的 16 个上游键。
5. 原始浏览器抓包目录未被忽略，存在误提交项目/账号元数据的风险。

本轮实现保留无后缀键作为 720p 默认，并增加显式 `_720p` 公共别名与真实 `_360p` 上游键。T2V、R2V、StartImage、StartAndEndImage 都会同步发送匹配的 `outputSpec.resolution`。

账号调度不再统一按 15 点门槛判断：先按解析后的模型读取精确耗点，再扣除该账号尚未完成任务已经预占的点数。异步任务或客户端断开后，预占会跟随后台轮询直到最终成功/失败并刷新余额，防止同一份缓存余额被并发任务重复使用。没有精确价格的旧模型仍沿用管理员配置的全局门槛。

项目自带测试页也已同步：按 Omni T2V、R2V、单首帧 I2V、首尾帧 I2V、视频编辑分类展示模型，覆盖默认 720p、显式 `_720p` 和 `_360p` 公共键，并按模型校验图片数量与编辑视频必填项，避免后端已支持但测试入口仍提交旧参数。

## 429 判别记录

不要把所有 429 都映射成“视频生成服务繁忙”：

1. 先看上游错误码和错误体，区分流量控制、reCAPTCHA 风控、账号配额和模型参数错误。
2. 再看提交前后的 `/v1/credits` 与轮询结果。
3. 只有流量控制才进入全局冷却；模型键/分辨率不匹配应直接修正请求；配额耗尽应切换有额度账号。

本次第二个成功任务完成后 `remainingCredits` 为 0，随后 `/v1/credits` 不再返回 `credits` 字段。因此该时间点之后同账号的新失败不能单独作为代理或模型故障证据。

这也解释了部分 429 的表现：若只依赖定时余额刷新，字段省略或并发提交会让数据库短时间保留旧余额，随后继续选择同一账号。精确耗点门槛和并发余额预占用于关闭这个窗口；真实流量控制仍保留共享冷却逻辑，二者分别处理。

## 后续待实操确认

- T2V 360p 与 720p 的完整提交体。
- 单首帧 I2V 360p 与 720p。
- 首尾帧 I2V 360p 与 720p。
- 视频编辑 360p 与 720p。
- 多输出（x2/x3/x4）实际提交时是一个请求包含多项，还是前端拆分为多个批次。
- 新账号浏览器 Profile、reCAPTCHA 获取和生成请求是否始终共享同一代理出口。
