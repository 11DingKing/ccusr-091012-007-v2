# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

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

## 保管期限

`apps/retention` 提供按案件类型与物资品类配置的保管期限规则（按生效日期版本化），并依据入库事实自动计算处置资格：

- `GET/POST /api/retention/rules/`、`PUT/DELETE /api/retention/rules/<id>/` — 规则版本管理；被引用后关键字段不可改、不可删，换版需新建规则
- `GET/POST /api/retention/records/`、`GET /api/retention/records/<id>/` — 按首次入库日期建档，详情含保留原因说明（`explanation`）
- `POST /api/retention/records/<id>/holds/`、`POST /api/retention/holds/<id>/release/` — 法律冻结/未结调查暂停计时，主管批准延期重算到期日；原到期日永不改动
- `GET/POST /api/retention/plans/`、`POST /api/retention/plans/generate/`、`.../approve/`、`.../execute/`、`.../cancel/` — 处置计划的生成、审批与执行
- `GET /api/retention/records/<id>/events/` — 全程审计轨迹（规则版本、措施、到期日前后值）

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
