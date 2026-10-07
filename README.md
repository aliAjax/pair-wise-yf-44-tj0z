# 化工装置变更与工艺安全管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8310`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `unit`：装置运行状态；`change`：变更申请；`action_item`：风险控制行动项。
- `loop`：仪表回路，登记量程（`range_min`/`range_max`）、联锁与报警定值（`setpoint`/`alarm_limit`）、报警方向（`alarm_direction`：`high`/`low`）、回差（`hysteresis`）与依据变更（`change_id`）。定值版本 `setpoint_version` 与仪表版本 `instrument_version` 随变更自动递增。
- `loop_test`：试验记录，记录试验结果与试验时的定值/仪表版本快照。
- `review_item`：待复核项。
- `commission_batch`：投产提交批次，记录复算结果与每条回路的签认。

## 生效账规则（src/rules.py）

- 提交投产时按当前变更与回路逐项复算：量程盖不住（定值/报警限值超出量程）、回差方向不对（回差非正或复位点超出量程）、试验未过（无当前版本的有效通过试验）即挡住，并在 `failures` 中指出具体回路与原因。
- 定值或仪表版本一改，旧试验立即失效（`status` 置为 `invalid`）并生成待复核；重新试验通过后待复核关闭。
- 两名工程师同时改同一回路，后提交方收到 `409` 与当前值（`current`）和冲突字段（`conflicts`），只让一笔生效。
- 投产签认按回路逐条落盘；写盘失败后批次停在 `in_progress`，重试只签未完成回路，已签认回路不重复签。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。
- `GET /api/ledger?change_id=...`：生效账详情，展示每条回路的有效版本、来源变更与未完成项。
- `POST /api/loops`：登记回路（量程、上下限、报警方向、回差、依据变更）。
- `POST /api/loops/<id>/edit`：改定值/仪表，体为`{"data":{...},"expected_version":数字}`；版本冲突返回当前值与冲突项。
- `POST /api/loops/<id>/tests`：录入试验记录（`result` 为 `passed`/`failed`），自动快照当前版本。
- `POST /api/commission_batches`：提交投产，体为`{"change_id":"...","loop_ids":[...]}`；复算不过返回 `409` 与 `failures`。
- `POST /api/commission_batches/<id>/retry`：写盘失败后重试，只签未完成回路。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

风险分级和投产规则用于流程演示，不替代HAZOP、LOPA、法定许可和现场安全审查。
