# 回收分拣来料污染判定系统

提交批次检验数据（批次编号、来源区域、重量、物料类别与污染量），系统按**可版本化的接收规则**计算污染等级、处置结论、扣费明细与证据说明，并保存判定单与审计记录，支持查询与 CSV 导出。

## 功能一览

- **判定计算**：污染率 → 命中等级（A 合格 / B 轻度污染 / C 重度污染）→ 处置结论（合格接收 / 降级接收 / 拒收）→ 分类扣费明细与合计 → 中文证据说明
- **字段校验**：未知物料类别、负/零重量、负污染量、单项或总污染量超过批次重量，一律拒绝判定并返回 `422` 字段错误
- **规则版本化**：规则按 `(区域, 版本)` 存储，每区域一个启用版本，可新增版本、切换启用版本；判定单出具时保存**规则快照**，规则更新不会改写历史结论
- **持久化与审计**：判定单、规则版本、审计日志存于 SQLite；判定创建、规则新增/切换均留痕
- **页面与 API**：简洁 Web 页面（`/`）+ REST API（交互文档 `/docs`）+ CSV 导出

## 快速开始

### Docker Compose（一键启动）

```bash
docker compose up --build
```

访问 <http://localhost:8000>（页面）或 <http://localhost:8000/docs>（API 文档）。
数据保存在名为 `app-data` 的卷中（容器内 `/data/app.db`）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DB` | `data/app.db`（容器内 `/data/app.db`） | SQLite 数据库文件路径 |

首次启动自动建表并写入种子规则：`default` 区域（v1.0 启用 / v1.1 备用）与 `华东` 区域（v1.0 启用）。种子不会覆盖已有数据，也不会重置你已切换的启用版本。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/assessments` | 提交判定，返回完整判定单 |
| GET | `/api/assessments` | 查询判定单（`region` / `level` / `batch_no` / `limit` / `offset`） |
| GET | `/api/assessments/{id}` | 判定单详情（含扣费明细与证据） |
| GET | `/api/rules` | 列出全部规则版本 |
| GET | `/api/rules/{region}/active` | 查询区域当前启用规则 |
| POST | `/api/rules` | 新增规则版本（`activate: true` 可同时启用） |
| POST | `/api/rules/{region}/activate` | 切换区域启用版本 |
| GET | `/api/audit` | 查询审计记录 |
| GET | `/api/export/assessments.csv` | 导出判定单 CSV |
| GET | `/api/export/audit.csv` | 导出审计记录 CSV |
| GET | `/api/health` | 健康检查 |

## 调用示例

### 1. 提交判定

```bash
curl -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_no": "B20261005-001",
    "region": "default",
    "weight_kg": 100,
    "materials": [
      {"category": "PET", "contamination_kg": 3},
      {"category": "纸类", "contamination_kg": 2}
    ],
    "operator": "张三"
  }'
```

响应 `201`（节选）：

```json
{
  "id": "A0F9F9D8876",
  "rule_version": "v1.0",
  "contamination_ratio": 0.05,
  "level": {"code": "B", "name": "轻度污染"},
  "conclusion": "降级接收",
  "fee_lines": [
    {"category": "PET", "contamination_kg": 3.0, "fee_per_kg": 1.2, "multiplier": 1.0, "fee": 3.6},
    {"category": "纸类", "contamination_kg": 2.0, "fee_per_kg": 0.6, "multiplier": 1.0, "fee": 1.2}
  ],
  "total_fee": 4.8,
  "currency": "CNY",
  "evidence": [
    "依据规则版本 default/v1.0 判定。",
    "PET：污染 3kg × 费率 1.2 元/kg × 等级系数 1 = 3.60 元",
    "批次重量 100kg，污染总量 5kg，污染率 5.00%。",
    "污染率命中等级 B（轻度污染，阈值 ≤ 8%），处置结论：降级接收。"
  ]
}
```

### 2. 非法提交（字段错误）

```bash
curl -X POST http://localhost:8000/api/assessments \
  -H 'Content-Type: application/json' \
  -d '{"batch_no":"B002","region":"default","weight_kg":-1,
       "materials":[{"category":"锂电池","contamination_kg":3}]}'
```

响应 `422`：

```json
{
  "errors": [
    {"field": "weight_kg", "message": "批次重量必须为正数"},
    {"field": "materials[0].category", "message": "未知物料类别：锂电池，当前规则版本支持的类别：PET, HDPE, PP, 纸类, 金属, 玻璃, 织物, 其他"}
  ]
}
```

### 3. 切换区域规则版本（历史结论不受影响）

```bash
# 切换 default 区域到 v1.1（阈值更严、费率更高）
curl -X POST http://localhost:8000/api/rules/default/activate \
  -H 'Content-Type: application/json' \
  -d '{"version": "v1.1", "actor": "admin"}'

# 此后的新判定按 v1.1 计算；切换前出具的判定单仍固定为 v1.0 的结论与扣费
curl http://localhost:8000/api/assessments/A0F9F9D8876
```

### 4. 新增规则版本

```bash
curl -X POST http://localhost:8000/api/rules \
  -H 'Content-Type: application/json' \
  -d '{
    "region": "华南", "version": "v1.0", "activate": true,
    "categories": {"PET": {"fee_per_kg": 1.0, "desc": "PET 瓶类"}},
    "levels": [
      {"code": "A", "name": "合格", "max_ratio": 0.05, "conclusion": "合格接收", "fee_multiplier": 0},
      {"code": "B", "name": "超标", "max_ratio": 1.0,  "conclusion": "拒收",     "fee_multiplier": 1.0}
    ]
  }'
```

### 5. 查询与导出

```bash
curl 'http://localhost:8000/api/assessments?region=default&level=B'
curl 'http://localhost:8000/api/audit?entity=rule'
curl -OJ http://localhost:8000/api/export/assessments.csv
curl -OJ http://localhost:8000/api/export/audit.csv
```

## 规则版本化设计

- 规则以 `(region, version)` 唯一存储，每个区域恰好一个 `active` 版本。
- 出具判定时将**规则快照**与请求、结果一并写入判定单，历史单据永不重算。
- 切换启用版本只影响之后的新判定；也可在提交时显式指定 `rule_version` 复用旧版本。
- 判定创建、规则新增、规则切换均写入审计日志（操作者、动作、对象、明细、时间）。

## 项目结构

```
app/
  main.py            # FastAPI 入口：API 路由、错误处理、CSV 导出
  rules.py           # 规则引擎：种子规则、提交校验、判定计算
  storage.py         # SQLite 持久化：判定单 / 规则版本 / 审计日志
  static/index.html  # 单文件 Web 页面
tests/test_api.py    # 19 个接口与版本化行为测试
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
