"""
保管期限计算与事件记录

核心规则：
- 到期日按入库事实（首次入库日期）起算，适用起算日当日生效的规则版本；
- 法律冻结、未结调查暂停计时，解除后按暂停天数顺延；
- 主管批准的延期重算当前到期日；
- 任何措施都不会改动原到期日。
"""
import calendar
import logging
from datetime import date, timedelta

from django.db.models import Max
from django.utils import timezone

from apps.core.exceptions import BusinessException
from .models import DisposalPlan, RetentionEvent, RetentionHold, RetentionRecord, RetentionRule

logger = logging.getLogger('apps')

# 暂停计时的措施类型
PAUSE_HOLD_TYPES = ['legal_freeze', 'investigation']

HOLD_TYPE_LABELS = dict(RetentionHold.TYPE_CHOICES)


def add_months(d, months):
    """日期按月偏移，月末自动钳制（如 1月31日 + 1个月 → 2月28/29日），自然处理跨年。"""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def select_rule(case_type, category_id, on_date):
    """选取起算日当日生效的规则版本（生效日期不晚于起算日的最新启用版本）。"""
    return (RetentionRule.objects
            .filter(case_type=case_type, category_id=category_id,
                    is_active=True, effective_from__lte=on_date)
            .order_by('-effective_from', '-id')
            .first())


def log_event(record, event_type, actor=None, detail=None):
    """写入审计事件。"""
    return RetentionEvent.objects.create(
        record=record,
        event_type=event_type,
        actor=actor,
        detail=detail or {},
    )


def paused_days(record, as_of):
    """暂停措施累计天数：已解除的按实际区间计，进行中的计至 as_of。"""
    total = 0
    for hold in record.holds.filter(hold_type__in=PAUSE_HOLD_TYPES):
        end = hold.end_date or as_of
        if end > hold.start_date:
            total += (end - hold.start_date).days
    return total


def extension_due_date(record):
    """所有批准延期中最晚的延期后到期日，无延期返回 None。"""
    return (record.holds
            .filter(hold_type='extension')
            .aggregate(value=Max('extended_due_date'))['value'])


def compute_current_due_date(record, as_of=None):
    """当前到期日 = max(原到期日, 最晚延期后到期日) + 暂停累计天数。原到期日不参与运算、永不改动。"""
    as_of = as_of or timezone.localdate()
    base = record.original_due_date
    extended = extension_due_date(record)
    if extended and extended > base:
        base = extended
    return base + timedelta(days=paused_days(record, as_of))


def active_pause_holds(record):
    """进行中的暂停措施（法律冻结/未结调查）。"""
    return record.holds.filter(hold_type__in=PAUSE_HOLD_TYPES, end_date__isnull=True)


def is_eligible(record, as_of=None):
    """是否具备处置资格：保管中、无进行中的暂停措施、已到当前到期日。"""
    as_of = as_of or timezone.localdate()
    return (record.status == 'in_custody'
            and not active_pause_holds(record).exists()
            and compute_current_due_date(record, as_of) <= as_of)


def refresh_current_due_date(record, as_of=None):
    """重算并保存当前到期日快照，返回是否发生变化。"""
    new_due = compute_current_due_date(record, as_of)
    if new_due != record.current_due_date:
        record.current_due_date = new_due
        record.save(update_fields=['current_due_date', 'updated_at'])
        return True
    return False


def explain(record, as_of=None):
    """说明物资当前为何仍被保留、何时可进入处置流程。"""
    as_of = as_of or timezone.localdate()
    current_due = compute_current_due_date(record, as_of)
    holds = list(active_pause_holds(record))

    if record.status == 'disposed':
        state = 'disposed'
        reason = '物资已处置'
    elif holds:
        labels = '、'.join(dict.fromkeys(HOLD_TYPE_LABELS[h.hold_type] for h in holds))
        state = 'suspended'
        reason = f'存在进行中的{labels}，期限暂停计算，解除后按暂停天数顺延'
    elif current_due <= as_of:
        state = 'eligible'
        reason = '已到当前到期日，可进入处置流程'
    else:
        state = 'in_custody'
        reason = f'当前到期日为 {current_due.isoformat()}，尚未到期'

    return {
        'state': state,
        'reason': reason,
        'anchor_date': record.anchor_date,
        'original_due_date': record.original_due_date,
        'extended_due_date': extension_due_date(record),
        'paused_days': paused_days(record, as_of),
        'current_due_date': current_due,
        'is_eligible': state == 'eligible',
    }


def create_record(goods, case_type, actor=None):
    """按入库事实为货物建立保管期限记录。"""
    first_in = goods.stock_ins.order_by('stock_in_time').first()
    if not first_in:
        raise BusinessException('该货物尚无入库记录，无法建立保管期限')

    anchor = timezone.localtime(first_in.stock_in_time).date()
    category_id = goods.variety.category_id
    rule = select_rule(case_type, category_id, anchor)
    if not rule:
        raise BusinessException('该案件类型与品类在入库日期无生效的保管期限规则')

    original_due = add_months(anchor, rule.retention_months)
    record = RetentionRecord.objects.create(
        goods=goods,
        case_type=case_type,
        rule=rule,
        anchor_date=anchor,
        original_due_date=original_due,
        current_due_date=original_due,
        created_by=actor,
    )
    log_event(record, 'created', actor, detail={
        'goods_id': goods.id,
        'goods_name': goods.name,
        'case_type': case_type,
        'rule_id': rule.id,
        'rule_effective_from': rule.effective_from.isoformat(),
        'retention_months': rule.retention_months,
        'anchor_date': anchor.isoformat(),
        'original_due_date': original_due.isoformat(),
    })
    logger.info(f"Retention record created: goods={goods.code} rule={rule.id} due={original_due}")
    return record


def place_hold(record, hold_type, reason, actor=None, start_date=None, extended_due_date=None):
    """登记暂停/延期措施并重算当前到期日。"""
    if record.status != 'in_custody':
        raise BusinessException('物资已处置，无法登记暂停或延期')

    start_date = start_date or timezone.localdate()
    if start_date > timezone.localdate():
        raise BusinessException('开始日期不能晚于今天')

    previous_due = compute_current_due_date(record)
    approved_by = None

    if hold_type == 'extension':
        if not extended_due_date:
            raise BusinessException('请填写延期后到期日')
        if extended_due_date <= previous_due:
            raise BusinessException(f'延期后到期日必须晚于当前到期日（{previous_due.isoformat()}）')
        approved_by = actor
    else:
        label = HOLD_TYPE_LABELS[hold_type]
        if record.holds.filter(hold_type=hold_type, end_date__isnull=True).exists():
            raise BusinessException(f'已存在进行中的{label}，请先解除')
        extended_due_date = None

    hold = RetentionHold.objects.create(
        record=record,
        hold_type=hold_type,
        reason=reason,
        start_date=start_date,
        extended_due_date=extended_due_date,
        approved_by=approved_by,
        created_by=actor,
    )
    refresh_current_due_date(record)
    log_event(record, 'hold_placed', actor, detail={
        'hold_id': hold.id,
        'hold_type': hold_type,
        'reason': reason,
        'start_date': start_date.isoformat(),
        'extended_due_date': extended_due_date.isoformat() if extended_due_date else None,
        'previous_current_due_date': previous_due.isoformat(),
        'current_due_date': record.current_due_date.isoformat(),
    })
    logger.info(f"Retention hold placed: record={record.id} type={hold_type}")
    return hold


def release_hold(hold, actor=None, release_reason='', end_date=None):
    """解除暂停措施并按暂停天数顺延当前到期日。批准延期不可解除。"""
    if hold.hold_type == 'extension':
        raise BusinessException('批准延期不可解除')
    if hold.end_date is not None:
        raise BusinessException('该措施已解除')

    end_date = end_date or timezone.localdate()
    if end_date < hold.start_date:
        raise BusinessException('解除日期不能早于开始日期')
    if end_date > timezone.localdate():
        raise BusinessException('解除日期不能晚于今天')

    record = hold.record
    previous_due = compute_current_due_date(record)
    hold.end_date = end_date
    hold.released_by = actor
    hold.release_reason = release_reason
    hold.save(update_fields=['end_date', 'released_by', 'release_reason'])
    refresh_current_due_date(record)
    log_event(record, 'hold_released', actor, detail={
        'hold_id': hold.id,
        'hold_type': hold.hold_type,
        'start_date': hold.start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'paused_days': (end_date - hold.start_date).days,
        'release_reason': release_reason,
        'previous_current_due_date': previous_due.isoformat(),
        'current_due_date': record.current_due_date.isoformat(),
    })
    logger.info(f"Retention hold released: record={record.id} hold={hold.id}")
    return hold


def create_plan(record, planned_date, method, actor=None, remark='', source='manual'):
    """为具备处置资格的记录生成处置计划。"""
    if record.status != 'in_custody':
        raise BusinessException('物资已处置，无法生成处置计划')
    if record.disposal_plans.filter(status__in=['pending', 'approved']).exists():
        raise BusinessException('该物资已存在未完成的处置计划')
    if not is_eligible(record):
        raise BusinessException('该物资当前不具备处置资格')

    plan = DisposalPlan.objects.create(
        record=record,
        planned_date=planned_date,
        method=method,
        remark=remark,
        created_by=actor,
    )
    log_event(record, 'plan_created', actor, detail={
        'plan_id': plan.id,
        'source': source,
        'planned_date': planned_date.isoformat(),
        'method': method,
    })
    logger.info(f"Disposal plan created: record={record.id} source={source}")
    return plan


def generate_disposal_plans(actor=None, as_of=None):
    """为所有已到期且未排计划的保管记录自动生成处置计划。"""
    as_of = as_of or timezone.localdate()
    created = []
    for record in RetentionRecord.objects.filter(status='in_custody'):
        if record.disposal_plans.filter(status__in=['pending', 'approved']).exists():
            continue
        if not is_eligible(record, as_of):
            continue
        created.append(create_plan(
            record, as_of, 'destruction',
            actor=actor, remark='系统自动生成', source='auto',
        ))
    return created
