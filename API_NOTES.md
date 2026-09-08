# 接口说明

整体职责和工作流程见 README，精确类型以各 `*_routes.py` 和测试为准。所有变更请求需要同源 `X-Noveltool-Token`；调用 `/api/session` 获得令牌，不将 API key 放进这些请求。

## 观察审核

`GET /api/settings` 返回当前经过来源/数据校验的 `observations`、自动设定、人工覆盖及 `version` 指纹。观察状态是 `pending`、`accepted` 或 `rejected`。该列表不限前端当前展开的页数。

`POST /api/settings/review` 保留原来的单条/多选功能，请求包含 `expected_version`、1～512 个 `observation_ids` 和 `decision`。这条接口还可用来拒绝或恢复指定观察的待审状态。

`POST /api/settings/review/accept-all` 用于一次接纳全部当前有效待审观察，只接收：

```json
{"expected_version": "从 GET /api/settings 取得的 64 位版本指纹"}
```

示例中的文字是占位说明，实际传入原样的 `version` 字段，不手写版本指纹。此端点不接受 `observation_ids` 或筛选参数；由服务器在项目锁内选取全量集合，没有 512 条批量上限。响应是更新后的完整设定视图，并额外包含：

```json
{"bulk_review": {"accepted_count": 1204}}
```

这里只展示额外字段。没有待审项时 `accepted_count=0`，不创建审计或改变版本。成功接纳逐条状态和一条 `review_all_pending` 审计记录在同一事务内落盘。已拒绝/已接受、失效/被替代/无效来源不变，人工覆盖不变，不调用模型。

返回 409 表示正文/设定已变，客户端必须刷新并让用户再次决定，不能自动重试；422 表示输入不合法；403 表示令牌或来源检查失败。保存错误时不返回假成功，整批回滚。网络断线可能发生在服务已经提交之后，因此客户端应先 GET 核对状态，不盲目重复提交。

`GET /api/settings/history` 返回最近 100 条人工操作审计，包括本次全量接纳的 ID 与前后状态。接纳不代表锁定，也不会自动消除事实冲突或不确定结论。
