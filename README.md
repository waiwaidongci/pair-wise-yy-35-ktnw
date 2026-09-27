# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/incidents`：登记坏损剂量计事件（dosimetrist）
- `GET /api/items/{id}/incidents`：事件列表
- `POST /api/items/{id}/incidents/{incident_id}/remeasure`：备用剂量计复核回写（dosimetrist，必须由登记人之外的另一名监测员执行，提交`expected_version`）
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 坏损剂量计补测

剂量计遗失（lost）、未回收（not_recovered）或故障（malfunction）时不再按零剂量登记：

- 事件记录剂量计编号（`dosimeter_id`）、佩戴周期（`wearing_period`）和异常类型（`anomaly_type`），并冻结原读数（`original_dose`）。
- 同一佩戴周期同一剂量计编号只保留一条有效（open）处理，由数据库唯一索引保证；事件结案后该周期编号方可再次登记。
- 登记后原读数立即退出年度累计（`counted_dose`为`null`，`annual_cumulative_dose`剔除该周期，`dose_source=original_reading|pending_remeasurement|backup_dosimeter`）。
- 另一名监测员使用备用剂量计（`backup_dosimeter_id`，编号不得与原计相同）复核，把替代剂量（`replacement_dose`，允许为0）回写该周期，事件结案后按`原始剂量+Σ替代剂量-Σ原读数`计入年度累计。
- 补测未完成的事件出现在`blockers`中并阻塞周期结案；重复补测、版本冲突均返回409。
- 登记与回写分别产生`incident_register`、`incident_remeasure`审计事件，继续挂接原有SHA-256审计链，权限矩阵不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
