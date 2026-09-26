# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `src/settlement_detail.py`：大额交收明细结构与单笔登记校验（数量、金额、来源单号、经办人）。
- `src/settlement_rules.py`：逐笔累计、差额/结清判断、超限整笔退回、重复来源单号与冲正退回规则。
- `src/settlement_repository.py`：交收指令、到账明细、交收审计的SQLite事务保存。
- `src/settlement_service.py`：大额交收用例编排、权限与累计视图。
- `src/settlement_http_api.py`：`/api/settlements` 路由分派。
- `static/index.html`：最小演示页面。
- `static/settlement.html`：大额交收演示页面（逐笔登记、差额展示、冲正退回）。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 大额交收接口（按笔累计）

大额交收必须**一次备齐**：数量与金额同时累计达标才结清，否则保持`processing`并返回还差多少。

- `POST /api/settlements`：建立指令，`{"reference":"...","data":{"required_quantity":1000,"required_amount":10000}}`。
- `GET /api/settlements` / `GET /api/settlements/{id}`：列表/详情，详情含累计数量金额、`remaining_quantity`/`remaining_amount`差额、全部到账明细。
- `POST /api/settlements/{id}/entries`：登记一笔到账，`{"data":{"quantity":600,"amount":6000,"source_ref":"SRC-A","operator":"张三"}}`。
  - 单笔数量或金额超出剩余值 → `422`整笔退回，不留任何记录；
  - 同一`source_ref`重复提交 → `409`只算一次；
  - 指令已`settled`/`reversed` → `409`不再接收交收；
  - 累计达标后指令置为`settled`。
- `POST /api/settlements/{id}/reverse`：冲正，`{"data":{"reverse_reason":"..."}}`；保留全部到账明细，对未完成的数量/金额生成一笔`return`退回明细。
- `GET /api/settlements/{id}/entries`：到账/退回明细列表。
- `GET /api/settlements/{id}/audit`：交收审计时间线。
- 演示页面：`GET /settlement`。大额交收接口需`settlement_officer`（或`admin`）角色。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
