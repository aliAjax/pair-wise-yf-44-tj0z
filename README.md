# 化工装置变更与工艺安全管理：投产生效账

只使用 Python 标准库和 SQLite 的模块化项目，默认端口 `8310`。业务规则集中在 `src/rules.py`，`app.py` 只负责组装依赖和启动服务。

系统把三类纸面/现场记录接成一本**生效账**：

- 变更单（change）：每条回路的上/下限、报警方向、回差与依据版本；
- 仪表回路（instrument_loop）：仪表位号、量程、仪表版本、当前依据；
- 试验记录（test_record）：针对某个定值版本 + 仪表版本的功能试验结论。

投产提交（activation_batch）按**当前变更和回路逐项复算**，任何一条不过就挡住整批，并指出具体回路与问题码。定值或仪表版本一改，旧试验立即失效并生成待复核；并发提交同一回路只让一笔生效。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常（冲突可携带当前值）和基础校验。
- `src/rules.py`：状态机、权限、定值/量程/回差校验、投产逐项复算。
- `src/repository.py`：SQLite 建表、乐观锁、生效账与批次明细、版本失效原子操作。
- `src/service.py`：用例编排、投产复算、并发冲突、写盘重试和幂等。
- `src/http_api.py`：HTTP 路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、版本失效、并发与失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8310
```

## 核心对象与状态

| 对象 | 说明 | 状态 |
| --- | --- | --- |
| `change` | 变更单，`setpoints` 登记回路定值（方向/上下限/回差） | draft → assessed → approved → implemented（→ commissioned/rolled_back） |
| `instrument_loop` | 仪表回路：`tag`、`range_low/high`、`instrument_version` | in_service / retired |
| `test_record` | 功能试验，含定值指纹、依据变更版本、仪表版本 | recorded → invalid |
| `review_task` | 待复核（系统生成，`review-<回路>`） | pending / resolved |
| `activation_batch` | 一次投产提交，明细逐回路记录 | open / blocked / submitting / activated |
| `ledger` | 生效账，每回路至多一条 `active`（数据库部分唯一索引保证） | active / superseded |

变更单只在 `draft` 阶段允许 `revise` 定值；实施后要改参数必须开新变更单。

## 投产逐项复算

提交 `activation_batch` 的 `submit` 动作时，对每条回路复算：

1. 变更单必须 `implemented`（`CHANGE_NOT_IMPLEMENTED`）；
2. 变更单上必须有该回路的定值条目（`NO_SETPOINT`）；
3. 量程必须登记（`RANGE_MISSING`）；
4. 上/下限必须落在量程内（`RANGE_COVER`）；
5. 报警方向合法且与限值一致（`ALARM_DIRECTION_INVALID`）；
6. 回差方向：高报复位点 = 上限 − 回差，低报复位点 = 下限 + 回差；
   复位点越量程或越过对侧限值即 `HYSTERESIS_DIRECTION`；
7. 必须有针对**当前变更版本 + 仪表版本 + 定值指纹**的通过试验
   （`NO_PASSED_TEST` / `TEST_STALE` / `TEST_FAILED`）；
8. 已有同一依据生效账的回路 `ALREADY_ACTIVE`，冲突返回当前账。

通过的回路写入生效账并签认；不过的在批次明细里标 `blocked` 并附问题明细，
批次状态按明细重算（有未完成→open，全挡住→blocked，全完成→activated）。

## 版本一改、旧试验立即失效

- 仪表换型/量程变更走 `revise_instrument`：回路乐观锁升版，同事务内把对不上
  新仪表版本的试验置 `invalid`，生成（或重开）`review-<回路>` 待复核，旧生效账置 `superseded`。
- 改量程必须同时升仪表版本，否则拒绝。
- 变更单 `revise` 定值后，定值指纹（方向/上下限/回差）对不上的试验立即失效并挂待复核。
- 登记新的通过试验时，该回路待复核自动关闭。

## 并发与写盘失败

- 两名工程师并发提交同一回路：`ledger` 的部分唯一索引
  `(loop_id) WHERE status='active'` 只让一笔生效；后到方收到 409，
  响应 `details` 与批次明细里带当前生效账（签认人、批次、依据版本）。
- 逐回路独立事务，单条写盘失败只让该回路保持 `pending`，其余继续；
  重试时 `done` 的回路跳过、不重复签认，从未完成处接着做。

## 主要接口

- `GET /health`
- `POST /api/loops`：登记仪表回路（量程、仪表版本）。
- `POST /api/changes` / `POST /api/entities/<id>/actions`：变更单工作流；
  `revise` 动作在草稿阶段登记/修改定值。
- `POST /api/tests`：登记功能试验（`loop_id`、`passed`、依据版本）。
- `POST /api/batches`：建投产批次；再对批次做 `{"action":"submit"}` 复算签认。
- `GET /api/batches/<id>`：批次与每条回路的复算明细/问题码。
- `GET /api/ledger?status=active&change_id=...`：生效账清单。
- `GET /api/ledger/loops/<loop_id>`：回路详情——有效版本、来源（变更/试验）、
  历史账、待复核与未完成批次。
- `GET /api/reviews?status=pending`：待复核清单。
- `GET /api/audit`：审计时间线。

身份通过 `X-User-Id`、`X-Role` 传入；试验登记允许 verifier/engineer，
投产提交与版本变更需 engineer/admin。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

复算规则用于把变更单、回路和试验接成可追溯的生效账，不替代 HAZOP、LOPA、
法定许可、现场点检和工程师的现场确认。
