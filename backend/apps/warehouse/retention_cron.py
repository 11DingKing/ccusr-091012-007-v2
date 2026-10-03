"""
保管期限周期任务，可由管理命令、cron 或进程内调度器调用。

- generate_disposal_plans：按入库事实为全部（或筛选范围）在管物资（重新）计算
  处置资格并落库计划；每次执行均新增计划行，历史行保留用于追溯。
- 本任务只给出「可进入销毁流程」的资格结论，不自动销毁；销毁仍须主管审批。
"""
import logging
from datetime import date

from .retention_engine import generate_plan
from .retention_models import CustodyItem, DisposalPlan

logger = logging.getLogger('apps')


def generate_disposal_plans(as_of=None, case_type=None, category_id=None, item_ids=None):
    """批量生成处置计划，返回统计 dict。"""
    as_of = as_of or date.today()
    qs = CustodyItem.objects.filter(is_active=True).select_related('rule_version')
    if case_type:
        qs = qs.filter(case_type=case_type)
    if category_id:
        qs = qs.filter(category_id=category_id)
    if item_ids:
        qs = qs.filter(id__in=item_ids)

    stats = {
        'as_of': as_of.isoformat(),
        'total': 0,
        'eligible': 0,
        'suspended': 0,
        'extended': 0,
        'tolled': 0,
        'normal': 0,
        'permanent': 0,
    }
    for item in qs:
        plan = generate_plan(item, as_of=as_of, persist=True)
        stats['total'] += 1
        if plan.eligible_for_disposal:
            stats['eligible'] += 1
        if plan.state in stats:
            stats[plan.state] += 1
    logger.info('处置计划生成完成：%s', stats)
    return stats


def latest_plan_index():
    """每件物资的最新计划，供看板/清单使用（只读，不落新行）。"""
    latest_ids = {}
    for plan in DisposalPlan.objects.order_by('item_id', '-generated_at'):
        latest_ids.setdefault(plan.item_id, plan.id)
    return DisposalPlan.objects.filter(id__in=latest_ids.values()).select_related('item')
