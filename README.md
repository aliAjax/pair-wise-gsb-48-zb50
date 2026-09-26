# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算和公司行动调整、冲突检查。
- `src/settlement.py`：交收明细解析与按笔累计判断（整笔退回、达标结清）。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面（含按笔累计交收面板）。
- `tests/`：完整流程、规则计算、按笔累计交收和失败场景测试。

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
- `GET /api/records/{id}/settlements`：交收明细与累计进度（已到账、还差多少）。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 按笔累计交收

- `settle`动作按笔登记交收，每笔包含数量`delivered_quantity`、金额`cash_paid`、来源单号`source_ref`和经办人`operator`（默认同调用人）。
- 累计达到结清要求后指令进入`settled`；未达标保持`settling`（处理中），记录与进度接口会显示还差多少。
- 单笔数量或金额超出剩余部分时整笔退回，不计入累计。
- 同一来源单号重复提交只入账一次，重复提交返回当前记录。
- 已结清（`settled`）或已冲正（`reversed`）的指令不再接收交收。
- 冲正（`reverse`）保留每笔到账明细，未完成部分以`returned_quantity`/`returned_amount`退回。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
