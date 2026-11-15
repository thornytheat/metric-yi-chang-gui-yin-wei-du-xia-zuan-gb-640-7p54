# 接口 anomalyattr

服务端口 `18209`，容器内 `8000`。所有金额与因子以字符串返回，避免 JSON 浮点误差。

## 页面

| 路径 | 内容 |
|---|---|
| `/` | 指标接入、四账、原因码、批次刻度 |
| `/anomalies` | 水位时间轴、确认点与候选点、事件清单 |
| `/review` | 复盘卡、敏感性矩阵、同批异常、逐日证据 |
| `/attribution` | 三因子分解、门店贡献、窗口覆盖 |
| `/drilldown` | 渠道分解与门店明细 |
| `/batches` | 批次刻度、运行清单、同源重跑 |
| `/report` | 运行报告与导出口 |

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活与库大小 |
| GET | `/api/import/samples` | 样例清单（名称、字节、sha256） |
| POST | `/api/import/preview` | 只采信：编码、列映射、诊断前 20 条 |
| POST | `/api/import` | 整批导入：采信 → 归一 → 质量 → 落库 → 清单 |
| GET | `/api/contract` | 字段定义、派生字段、规则、阈值、节假日 |
| POST | `/api/contract/preview` | 单行试算，返回归一后字段或原因码 |
| GET | `/api/quarantine` | 隔离明细、原因分布、三账 |
| GET | `/api/quarantine.csv` | 隔离行 CSV（行号 + 原因码 + 原值） |
| GET | `/api/anomalies` | 检测结果；`status` / `channel` / `hotel` 可筛选 |
| GET | `/api/anomalies/timeline` | 日期刻度 + 异常标记 + 水位 |
| GET | `/api/attribution` | `dimension=hotel_code\|channel`、`cur_days`、`base_days` |
| POST | `/api/attribution/rerun` | 同源重跑，命中同一批次时 `reused=true` |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/batches/{run_id}` | 运行清单、参数摘要、账目备注 |
| GET | `/api/reconcile` | 四账与逐条检查 |
| GET | `/api/report` | 报告结构与结论 |
| GET | `/api/export/{kind}` | `points.csv` / `anomalies.csv` / `attribution.csv` / `manifest.json` |
| GET | `/api/review` | 复盘卡、敏感性九组合、同批异常；`run_id` 可指定版本 |
| GET | `/api/review/{event_id}` | 单张复盘卡（含逐日证据与同向对照） |
| GET | `/api/review/sensitivity` | 单组合明细：`window` 14-42、`threshold` 1-10 |
| GET | `/api/review/cards.csv` | 复盘卡 CSV（BOM + 标记码） |
| POST | `/api/review/mark` | 按版本钉住复核结论（`run_id`+`event_id`+`verdict`），新版本不覆盖 |
| GET | `/api/review/marks` | 某版本的已复核结论清单；`run_id` 可指定版本 |
| POST | `/api/revisions` | 修订切换：补报/撤回整版重算，四账+分解全平才一次性换版 |
| GET | `/api/revisions` | 修订版本链（含成功与失败记录、影响面、核对结果） |
| GET | `/api/revisions/{run_id}/lineage` | 某版本沿父链回溯的版本谱系 |

## 失败返回

| 场景 | 状态码 | `error` |
|---|---|---|
| 样例不存在 | 404 | `E_SAMPLE_NOT_FOUND` |
| 空文件 | 400 | `E_EMPTY_FILE` |
| 缺必需列 | 400 | `E_MISSING_COLUMNS` |
| 末行截断 | 400 | `E_TRUNCATED` |
| 仅表头 / 全空行 / 0 记录 | 422 | `E_HEADER_ONLY` / `E_ALL_BLANK` / `E_NO_ROWS` |
| 维度非法 / 窗口越界 | 400 | `E_BAD_DIMENSION` / `E_BAD_WINDOW` |
| 批次不存在 | 404 | `E_BATCH_NOT_FOUND` |
| 还没有批次 | 404 | `E_NO_RUN` |
| 导出类型未知 | 400 | `E_BAD_EXPORT` |
| 复盘窗口或阈值越界 | 400 | `E_BAD_AXIS` |
| 复盘卡不存在 | 404 | `E_CARD_NOT_FOUND` |
| 当前批次没有有效点 | 400 | `E_NO_POINTS` |
| 没有可修订的基线版本 | 404 | `E_NO_RUN` |
| 修订模式非法 | 400 | `E_BAD_REVISION_MODE` |
| 修订文件空态/缺列/截断 | 400/422 | 沿用 M1 的 `E_EMPTY_FILE`/`E_HEADER_ONLY`/`E_MISSING_COLUMNS`/`E_TRUNCATED` 等 |

错误体固定为 `{"error": "...", "message": "..."}`，空态额外带 `empty_state`。

## 修订与版本化

门店渠道日报存在补报与撤回，记录到达顺序与快照版本不一致。修订走独立的版本化链路，
不就地覆盖当前批次：

- `POST /api/revisions`（multipart：`file`、`mode=partial|full`、可选 `parent_run_id`）：
  - **partial（默认）**：修订文件只含受影响业务键，未触及事实从父版本承接，整版重算。
  - **full**：只用修订文件事实整版替换，承接为 0。
  - 撤回用可选列 `op_flag=WITHDRAW`（兼容 `REVOKE`/`撤销` 等，见 `app/config.py`），
    该业务键整键不再采信，记 `W_WITHDRAWN`。
- 影响面按业务键 `(门店, 渠道, 业务日)` 与快照事实比对，分新增/改写/撤回/未变四类，
  并给受影响业务日与门店×渠道清单；28 天基线、LMDI 分解与下钻份额随整版重算，不做半版更新。
- 切换在单事务内完成，先落候选版本再核对**行账、点账、金额账、分解账**；任一不平整体回滚，
  当前版本沿用旧版，并登记一条 `status=failed` 的失败修订。
- 幂等：同一修订文件（sha256 + 模式）重试命中同一记录，成功复用或回放失败原因，**不新增版本**。
- 新版本有独立 `run_id`（`rev-` 前缀）与完整 points/quarantine，旧版本不可变；
  所有 `run_id` 参数接口（异常、归因、对账、导出、复盘、隔离）都能回看任意历史版本。
- 复核结论通过 `/api/review/mark` 钉在具体 `run_id` + `event_id` 上，换版不迁移、不覆盖。

修订响应的 `revision_result` 字段：`status`（`switched`/`failed`）、`switched`、`reused`、
`parent_run_id`、`root_run_id`、`revision_no`、`mode`、`affected`（影响面）、
`verification`（四类核对逐条结论）、`fail_reason`、`current_run_id`。
