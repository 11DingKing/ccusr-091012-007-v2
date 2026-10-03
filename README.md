# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 保管期限规则子系统

针对「不同案件类型、不同物资类别适用不同保存期限」，系统提供基于入库事实的
期限计算与处置资格管理：

- **带生效日期与版本的规则**：规则按「案件类型 × 品类（空为该案件类型通用规则）」
  配置保管年限或永久保管；换版时旧版本置为 `superseded` 但记录保留，版本链
  （`replaces/superseded_by`）可回溯。物资在入库时**快照当时适用的规则版本**，
  后续换版不影响存量物资。
- **期限口径**：原始到期日 = 入库日 + 保管年限 − 1 天（届满次日起具备销毁资格，
  跨年、闰年由日期运算处理，2 月 29 日在平年落到 2 月 28 日）。原始到期日一旦
  生成永不改变。
- **暂停（法律冻结 / 未结调查）**：暂停区间内自然日不计入保管期间，到期日按暂停
  天数顺延；多个区间取并集，重叠不重复计时；解除后自动重算。到期日之后才开始的
  冻结不补天数，但仍阻止销毁。未解除暂停期间最终到期日标记为「暂不可预计」。
- **主管延期**：以批准时认定的到期日为基准重算，链式记录每次延期（`seq`、
  `base_due_date`、`new_due_date`、批准人、理由、时间），原到期日与历次快照保留。
- **处置计划与可追溯**：每次生成计划都新增一行 `DisposalPlan`，内含原始/目标/
  预计到期日、暂停与延期天数、状态、**仍需保管的原因**和完整**计算时间线**；
  主管可通过 API 直接看到某件物资为何仍被保留、何时可进入销毁流程。所有计算均
  支持传入基准日复算，规则换版、跨年、多重延期均可追溯。

### 主要 API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET/POST /api/retention/rules/` | 规则列表 / 发布规则版本（换版传 `replaces`，需主管权限） |
| `GET  /api/retention/rules/<id>/` | 规则详情与版本链 |
| `GET  /api/retention/rules/resolve/?case_type=&category=&on_date=` | 按日期解析适用规则（品类专用优先） |
| `GET/POST /api/custody-items/` | 保管物资登记（按入库日自动快照规则）/ 列表 |
| `GET  /api/custody-items/<id>/schedule/?as_of=` | **为何仍被保留 / 何时可销毁**（含时间线，可按历史日复算） |
| `GET  /api/custody-items/<id>/timeline/` | 物资全量追溯：规则、暂停、延期、历次计划、销毁申请 |
| `POST /api/custody-items/<id>/holds/` | 登记法律冻结 / 未结调查（暂停期限） |
| `POST /api/custody-holds/<id>/lift/` | 解除暂停（期限顺延重算） |
| `POST /api/custody-items/<id>/extensions/` | 主管批准延期（重算到期日，原到期日保留） |
| `POST /api/custody-items/<id>/generate-plan/` | 单件生成处置计划 |
| `POST /api/disposal-plans/` | 批量生成处置计划（支持 `as_of/item_ids/case_type/category`，需主管） |
| `POST /api/custody-items/<id>/disposal-requests/` | 期限届满后发起销毁流程（未满期返回 409 及原因） |
| `POST /api/disposal-requests/<id>/decision/` | 主管审批；批准时再次复核冻结/到期状态 |

批量计划也可由调度任务离线执行：

```bash
python manage.py generate_disposal_plans --as-of 2026-10-03
```


## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
