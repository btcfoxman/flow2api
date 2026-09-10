# Token updater 状态预检接口补齐

## 根因

2026-09-10 对 3.5 pre 的只读检查确认：`POST /api/plugin/check-tokens` 返回 404，OpenAPI 只有插件配置和 `update-token` 路由。同步器智能同步先查询状态，查询失败后停止该组，因此不会走到会话更新。浏览器插件直接调用 `update-token`，此前插件同步成功没有覆盖这条前置链路。

保留“查询失败不触发全账号强制登录”的保护，补齐接收端协议，而不是恢复旧的全量重登录回退。

## 协议

`POST /api/plugin/check-tokens` 使用与 `update-token` 相同的连接 Token 鉴权。请求 `{}` 或 `{"emails": []}` 查询所有已保存账号；非空 `emails` 按去除首尾空白、忽略大小写进行筛选。最多 1000 项；缺失账号不返回记录，由同步器按“尚未同步”处理。

```json
{
  "success": true,
  "tokens": [
    {
      "email": "account@example.com",
      "is_active": true,
      "needs_refresh": false,
      "sync_allowed": true
    }
  ]
}
```

- 仅返回白名单状态字段，不返回 ST、AT、Cookie、连接 Token、代理凭据或项目数据。
- `needs_refresh` 基于已保存的启用状态、ST/AT 是否存在、AT 是否在一小时内过期，以及 native_cdp 模式下 Cookie 是否完整。缺失有效期也需要刷新；无时区时间按 UTC 处理；积分为 0 不等于需要重新登录。
- 查询不会刷新 Token、启动浏览器、请求 Google、修改账号或提交任务。状态是本地快照，不是实际生成或当前网页登录成功保证。
- native_cdp 独立登录标记存在或损坏时返回 `sync_allowed=false`、`needs_refresh=false`、`sync_block_reason=independent_login`。新同步器优先跳过外部刷新，即使该账号禁用或本地同步已超时。已有 `update-token` 的 409 防覆盖保护保持不变。
- 无有效连接鉴权返回 401；非法邮件筛选返回 400；非对象请求体由框架返回 422。

## 验证与发布顺序

- Flow2API 全量回归：392 项通过；涵盖新 HTTP 路由、共享鉴权和既有会话同步保护。
- tupdater 全量回归：132 项通过；404/405 明确指出缺失接口，错误保留 HTTP 状态码；检查失败不会触发源账号重新登录。
- 在 3.5 当前容器中以内存加载候选代码、SQLite `mode=ro` 读取真实数据进行联调：34 个账号、1 个独立登录保护账号；200/401/400 行为和同步器查询、筛选、跳过逻辑均通过。该步骤没有替换运行中服务或修改数据库。
- 先发布 Flow2API pre 并检查真实 HTTP 接口，再发布 tupdater main。插件不需要修改或重建。
