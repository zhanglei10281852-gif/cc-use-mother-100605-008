# 设备生命周期服务（沈鼓云类远程监测平台后端）

把设备**销售交付 → 安装 → 保养 → 故障诊断 → 备件更换**串成一条可追溯的服务链。
核心目标：客户投诉时，拿到的每一份服务记录都能**对应到实际设备版本、当时的合同承诺和原始传感数据**。

## 设计要点

- **事件溯源（event sourcing）+ 哈希链**：系统中只有不可变事件（交付、安装、换件、读数、
  合同变更、工单各动作）。事件追加写入 JSONL 日志（每条 `flush + fsync`），并以
  SHA-256 逐条串联。服务重启重放日志即恢复全部状态——**未关闭的工单不会丢失**；
  日志被篡改或缺行会在启动时被 `LogIntegrityError` 检出（fail-closed）。
- **双时间戳支持离线补录**：每个现场事件同时有 `occurred_at`（现场发生时间）与
  `recorded_at`（系统入库时间）。补录过去的事件必须显式 `backfill=true`，
  且只能按现场发生顺序追加、不得改写已终态工单的历史结论。
- **换件即拓扑**：部件以序列号唯一标识，更换时校验“申报旧件 = 槽位当前件”，
  新件不得重复安装；换件形成按槽位的完整谱系，可重建任一时刻的设备配置版本。
  工单在受理时冻结当时的拓扑快照与配置版本号。
- **合同承诺按故障时刻判定**：维修费用归属只依据**故障发生时刻**有效的保修/合同状态，
  此后的保修缩水、脱保不追溯历史故障。
- **同一故障、不同阶段、不同处置权限**：权限矩阵为 `(角色 × 工单阶段 × 动作)`，
  工单归属团队构成第二道约束，跨团队必须显式转交（留有转交记录）。
- **原始传感数据只读留痕**：读数存内容摘要 + 来源 URI，诊断结论必须引用读数，
  重建视图可从结论一路回溯到原始数据。

### 工单阶段状态机

```
open ──diagnose──▶ diagnosed ──request_parts──▶ waiting_parts ──start_repair──▶ in_repair
  │                   │                              │                            │
  │                   └──────────────▶ in_repair ◀───┘                            │
  │                                                                               ▼
cancel/误报                                                                    resolved ──close──▶ closed
  ▼                                                                               │
canceled（终态）                                                        主管可退回 in_repair 返工
```
主管可在限定路径上强制退回返工；`closed/canceled` 为终态。

### 关键边界行为

| 情形 | 行为 |
|---|---|
| 离线维修补录 | 超过时限的历史事件必须带 `backfill=true`；按发生时间顺序补录；不得早于报修/上一动作；不得改写终态工单 |
| 重复工单 | 同设备 + 同故障代码且存在未关闭工单 → 拒绝建单，响应带既有工单号（HTTP 409）；客户端可带 `idempotency_key` 去重重试 |
| 重复读数 | 同设备 + 同传感器 + 同观测时刻天然幂等，返回原事件 |
| 保修范围变化 | 记录生效日、原因、操作人；历史故障仍按故障时状态结算 |
| 备件缺货 | 现货不足时禁止虚假预留；必须登记缺货补订与预计到货；到货直预留本单；取消工单/申请时现货回补 |
| 跨团队转交 | 只有客服/主管可转交；转交前后团队归属改变，原团队失去处置权；全程留痕 |
| 诊断 | 必须引用至少一条属于该设备、且观测不晚于故障时刻的原始读数 |
| 换件 | 旧件不符、新件已装他处、工单不在维修中、备件未出库——一律拒绝 |

## 目录结构

```
src/lifecycle_service/
  errors.py       领域错误（映射 HTTP 状态码）
  models.py       枚举、状态机表、命令对象
  events.py       不可变事件、时间工具、哈希计算
  store.py        只追加 JSONL 日志（fsync、重放校验）
  projection.py   读模型：设备拓扑/谱系、工单、库存、合同、历史时刻重建
  policy.py       角色×阶段权限矩阵与流转规则
  service.py      领域服务：命令校验、补录规则、幂等、编排、维修重建
  api.py          HTTP 接口（标准库，零依赖）
  cli.py          命令行（客服可直接重建维修经过）
tests/            20 个单元/端到端测试
```

## 测试与检查

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src tests run_cli.py
```

## HTTP 接口

启动：

```bash
PYTHONPATH=src python3 -m lifecycle_service.api --log data/events.jsonl --port 8080
```

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/equipment` | 设备交付登记（含初始 BOM） |
| POST | `/equipment/{sn}/install` | 安装 |
| POST | `/equipment/{sn}/parts/replace` | 换件（可关联工单） |
| POST | `/equipment/{sn}/warranty` | 保修/合同范围变化 |
| POST | `/readings` | 接入传感读数（内容摘要 + 来源 URI） |
| POST | `/stock/inbound` | 备件现货入库 |
| POST | `/work-orders` | 受理报修（重复返回 409，支持 idempotency_key） |
| POST | `/work-orders/{id}/actions` | 处置：diagnose/transfer/request_parts/reserve_part/report_shortage/part_arrived/dispatch_part/cancel_part/start_repair/resolve/close/cancel/force_stage |
| GET | `/work-orders` | 未关闭工单 |
| GET | `/work-orders/{id}` | 工单详情 |
| GET | `/work-orders/{id}/reconstruction` | **重建一次维修的完整经过** |
| GET | `/equipment/{sn}` | 设备视图（当前拓扑/保修/谱系） |

## 命令行

```bash
PYTHONPATH=src python3 -m lifecycle_service.cli --log data/events.jsonl list-open
PYTHONPATH=src python3 -m lifecycle_service.cli --log data/events.jsonl reconstruct WO-SG-200-001
PYTHONPATH=src python3 -m lifecycle_service.cli --log data/events.jsonl reconstruct WO-SG-200-001 --json
```

重建报告包含：设备身份与现场、**故障时刻配置版本与拓扑**（区别于当前版本）、
故障时费用归属及依据、原始读数（观测/入库时间、来源、摘要、补录标记）、
完整处置时间线（阶段、团队、操作人、转交、缺货、换件）、诊断与结算结论、证据链序号区间。
