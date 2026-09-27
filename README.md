# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、关闭不变量和坏损剂量计规则。
- `src/repository.py`：SQLite建表、事务、版本控制、审计链和补测事件存取。
- `src/service.py`：权限检查、用例编排、并发控制、审计和年度累计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、失败测试和坏损剂量计补测测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`，可带`dosimeter_no`、`wear_period`（YYYY-MM）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 坏损剂量计补测

- `POST /api/items/{id}/dosimeter-incidents`：登记遗失（`lost`）、未回收（`not_returned`）或故障（`malfunction`）事件，必须包含`dosimeter_no`、`wear_period`、`anomaly_type`；同周期同编号只保留一条`open`有效处理（数据库唯一索引）。登记后原读数保留但退出年度累计（`dose_status=excluded`，`effective_dose=0`）。
- `POST /api/dosimeter-incidents/{id}/remeasure`：由**另一名监测员**（`actor`不能与登记人相同）用备用剂量计（编号不能与原剂量计相同）复核，提交`replacement_dose`、`spare_dosimeter_no`、`expected_version`，替代剂量回写该周期（原读数不覆盖，留在`quantity`）。
- `GET /api/dosimeter-incidents?status=open|resolved`、`GET /api/dosimeter-incidents/{id}`、`GET /api/items/{id}/dosimeter-incidents`
- `GET /api/dose/annual?year=YYYY`：年度累计，待补测周期按0计、补测完成后按替代剂量计。
- 补测未完成的事件不能结案（转换到`closed`返回409）；详情和列表输出`dose_source`（来源）、`replacement_dose`（替代剂量）和`block_reasons`/`block_reason`（阻塞原因）。

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。登记与补测仅dosimetrist/radiation_officer可操作，复核人必须不同于登记人；全部操作追加到原有SHA-256审计链，既有角色与审计行为不变。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
