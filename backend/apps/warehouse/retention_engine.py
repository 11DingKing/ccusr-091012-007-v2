"""
保管期限计算引擎。

术语与口径
----------
- 原始到期日 original_due_date = 入库日 + 保管年限 − 1 天，即保管期限内的最后一日；
  次日起具备处置资格。减 1 天是为了让「保管 N 年」覆盖 N 个完整年度（跨年、闰年由
  date 运算自然处理；2 月 29 日入库在平年落到 2 月 28 日，见 add_years）。
- 暂停（法律冻结 / 未结调查）：暂停区间内的自然日不计入保管期间，到期日按暂停天数
  顺延。多个暂停区间取并集，重叠部分不重复计时。仅「到期日之前已经开始」的暂停能
  顺延期限——到期日之后才开始的冻结只阻止销毁、不补天数（通过未解除暂停状态体现）。
- 延期（主管批准）：以批准时认定的到期日为基准向后加批准确认的天数，生成新的目标
  到期日；原到期日及历次到期日均作为快照保留，永不覆盖。多次延期链式累加。
- 去重口径：批准延期时，该时点之前的全部暂停影响已固化进当次 base_due_date 快照；
  最终预计到期日只对「最后一次延期批准日之后」开始的暂停再次顺延，避免重复计时。
- 最终预计到期日 = 延期后目标日，再叠加其后暂停区间做不动点顺延；存在未解除暂停时
  为 None（到期日暂不可预计）。

引擎所有函数都接受 as_of 注入，保证规则换版、跨年、多重延期等场景可复算、可追溯。
"""
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q

from apps.core.exceptions import BusinessException
from .retention_models import (
    DisposalPlan,
    RetentionExtension,
    RetentionRule,
)

DAY = timedelta(days=1)
# 不动点迭代的保护性上限（正常情况下迭代次数不超过暂停区间数 + 1）
_MAX_FIXPOINT_ITERATIONS = 1000


def add_years(day, years):
    """日期加整年；2 月 29 日在平年落到 2 月 28 日。"""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return day.replace(year=day.year + years, day=28)


def resolve_rule(case_type, category_id, on_date):
    """
    解析某日期适用的规则版本：品类专用规则优先于通用规则（category 为空）；
    生效日期不得晚于 on_date；同优先级取生效日最新、版本最高。
    找不到时抛 BusinessException。
    """
    from django.db.models import Case, IntegerField, Value, When

    category_q = (
        Q(category_id=category_id) | Q(category__isnull=True)
        if category_id else Q(category__isnull=True)
    )
    rule = (
        RetentionRule.objects.filter(case_type=case_type, effective_date__lte=on_date)
        .filter(category_q)
        .annotate(specificity=Case(
            When(category__isnull=True, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        ))
        .order_by('-specificity', '-effective_date', '-version')
        .first()
    )
    if rule is None:
        raise BusinessException(
            f'{on_date} 前不存在案件类型 {case_type} 的有效保管期限规则，无法起算期限',
            code=422,
        )
    return rule


def _merge_intervals(intervals):
    """合并 [(start, end)] 日期闭区间并集，end=None 表示开放区间。"""
    closed = sorted((s, e) for s, e in intervals if e is not None)
    merged = []
    for start, end in closed:
        if not merged or start > merged[-1][1] + DAY:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    open_starts = sorted(s for s, e in intervals if e is None)
    return merged, open_starts


def _count_days_before(merged, cutoff):
    """合并区间中严格早于 cutoff 的自然日总数（闭区间按天计）。"""
    total = 0
    for start, end in merged:
        seg_end = min(end, cutoff - DAY)
        if seg_end >= start:
            total += (seg_end - start).days + 1
    return total


def apply_suspensions(target_due, intervals, intake_date, as_of):
    """
    对目标到期日施加暂停顺延，返回 (projected_due_or_None, suspension_days)。

    区间内、到期日之前的每一天都使到期日后移一天；后移又可能让更多区间日落入
    「到期日之前」，因此用不动点迭代求最终到期日。未解除暂停先按截至 as_of 已
    经过的天数参与顺延（反映已实际暂停的时长），同时令最终到期日返回 None。
    到期日之后才开始的暂停不补天数（不动点不会因此移动）。
    """
    relevant = [
        (s, e) for s, e in intervals
        if e is None or (e >= intake_date and s <= target_due)
    ]
    # 区间起点不得早于入库日（入库前不存在保管期限，其冻结日不计顺延）；
    # 完全落在入库日之前的区间直接丢弃。
    clipped = []
    for s, e in relevant:
        s = max(s, intake_date)
        if e is not None and e < s:
            continue
        clipped.append((s, e))
    relevant = clipped
    merged, open_starts = _merge_intervals(relevant)
    # 未解除区间按截至 as_of 已经过的部分并入计数（与已结束区间合并以去重）
    count_intervals = list(merged)
    for s in open_starts:
        if s <= as_of:
            count_intervals.append([s, as_of])
    count_intervals = _merge_intervals(count_intervals)[0]

    effective = target_due
    for _ in range(_MAX_FIXPOINT_ITERATIONS):
        counted = _count_days_before(count_intervals, effective)
        candidate = target_due + timedelta(days=counted)
        if candidate == effective:
            break
        effective = candidate
    else:  # pragma: no cover - 数学上必然收敛，此处仅作保护
        raise RuntimeError('暂停顺延计算未收敛')

    days = (effective - target_due).days
    open_started = any(s <= target_due for s in open_starts)
    if open_started:
        return None, days
    return effective, days


def _validate_holds(holds):
    for hold in holds:
        if hold.end_date is not None and hold.end_date < hold.start_date:
            raise BusinessException(
                f'暂停记录（{hold.get_hold_type_display()}，{hold.authority}）解除日期早于开始日期，数据异常'
            )


def compute_schedule(item, as_of=None):
    """
    计算物资在 as_of（默认今天）的完整期限视图。返回 dict：
    original_due_date / target_due_date / projected_due_date /
    suspension_days / extension_days / indefinite_hold /
    state / eligible_for_disposal / retained_reasons / timeline
    """
    as_of = as_of or date.today()
    rule = item.rule_version
    holds = list(item.holds.order_by('start_date', 'created_at'))
    _validate_holds(holds)
    extensions = list(item.extensions.select_related('approver').order_by('seq'))

    timeline = [{
        'event': 'intake',
        'date': item.intake_date.isoformat(),
        'detail': f'物资 {item.code} 入库，期限自入库事实（入库日）起算',
    }, {
        'event': 'rule_applied',
        'rule_id': rule.id,
        'version': rule.version,
        'case_type': rule.case_type,
        'category_id': rule.category_id,
        'retention_years': None if rule.is_permanent else rule.retention_years,
        'is_permanent': rule.is_permanent,
        'effective_date': rule.effective_date.isoformat(),
        'detail': f'适用入库时已生效的规则快照 V{rule.version}（{rule.effective_date} 生效），规则换版不影响本物资',
    }]

    result = {
        'rule': rule,
        'as_of_date': as_of,
        'original_due_date': None,
        'target_due_date': None,
        'projected_due_date': None,
        'suspension_days': 0,
        'extension_days': 0,
        'indefinite_hold': False,
        'state': DisposalPlan.STATE_NORMAL,
        'eligible_for_disposal': False,
        'retained_reasons': [],
        'timeline': timeline,
    }

    if rule.is_permanent:
        result['state'] = DisposalPlan.STATE_PERMANENT
        result['retained_reasons'].append({
            'code': 'permanent_rule',
            'message': f'规则 V{rule.version} 规定该类物资永久保管，不产生到期日',
        })
        timeline.append({'event': 'permanent', 'detail': '永久保管规则，期限不届满'})
        _append_active_hold_reasons(result, holds, as_of)
        return result

    original_due = add_years(item.intake_date, rule.retention_years) - DAY
    target_due = original_due
    result['original_due_date'] = original_due
    result['target_due_date'] = target_due
    timeline.append({
        'event': 'original_due',
        'date': original_due.isoformat(),
        'detail': f'入库日 {item.intake_date} + {rule.retention_years} 年 − 1 天 = 原始到期日（任何暂停/延期均不改变此日期）',
    })

    for hold in holds:
        timeline.append(_hold_timeline_entry(hold))

    # 以 as_of 视角截取当时已经发生的暂停事实：
    # - 开始日晚于 as_of 的暂停当时尚未发生，忽略；
    # - 解除日晚于 as_of（或未解除）的，在当时视为开放区间；
    # - 其余按完整闭区间计入。
    view_intervals = []
    for hold in holds:
        if hold.start_date > as_of:
            continue
        end = hold.end_date
        if end is None or end > as_of:
            end = None
        view_intervals.append((hold.start_date, end))

    # 延期链：批准日之前的暂停影响已固化进各次 base_due_date 快照
    for ext in extensions:
        timeline.append({
            'event': 'extension',
            'seq': ext.seq,
            'approved_at': ext.approved_at.date().isoformat(),
            'base_due_date': ext.base_due_date.isoformat(),
            'extension_days': ext.extension_days,
            'new_due_date': ext.new_due_date.isoformat(),
            'approver': ext.approver.username if ext.approver else None,
            'reason': ext.reason,
            'detail': (
                f'第 {ext.seq} 次主管批准延期：以批准时认定到期日 {ext.base_due_date} 为基准，'
                f'+{ext.extension_days} 天，重算为 {ext.new_due_date}'
            ),
        })
    if extensions:
        target_due = extensions[-1].new_due_date
        result['target_due_date'] = target_due
        result['extension_days'] = sum(e.extension_days for e in extensions)
        horizon = extensions[-1].approved_at.date()
    else:
        horizon = None

    # 最终顺延只统计最后一次延期批准日之后开始的暂停（之前的已在延期快照中）
    tail_intervals = [
        (s, e) for s, e in view_intervals
        if horizon is None or s > horizon
    ]
    projected, tail_suspension_days = apply_suspensions(
        target_due, tail_intervals, item.intake_date, as_of
    )
    # 批准日之前的暂停影响已固化进延期链：目标到期日相对原始到期日的增量，
    # 扣除累计延期天数，即为历史暂停顺延天数。
    embedded_suspension_days = (
        (target_due - original_due).days - result['extension_days']
    )
    suspension_days = embedded_suspension_days + tail_suspension_days
    result['projected_due_date'] = projected
    result['suspension_days'] = suspension_days

    timeline.append({
        'event': 'suspension_total',
        'days': suspension_days,
        'embedded_days': embedded_suspension_days,
        'tail_days': tail_suspension_days,
        'after': horizon.isoformat() if horizon else None,
        'detail': (
            f'冻结/调查暂停累计顺延 {suspension_days} 天'
            f'（其中 {embedded_suspension_days} 天已在延期批准时计入到期日，'
            f'最后一次延期（{horizon}）后新增 {tail_suspension_days} 天；重叠区间不重复计）'
            if horizon else
            f'冻结/调查暂停合计顺延 {suspension_days} 天（重叠区间不重复计）'
        ),
    })
    timeline.append({
        'event': 'projected_due',
        'date': projected.isoformat() if projected else None,
        'detail': '预计最终到期日' if projected else '存在未解除暂停，最终到期日暂不可预计，解除后顺延重算',
    })

    _evaluate_state(result, holds, as_of, original_due, target_due, projected)
    timeline.append({
        'event': 'evaluation',
        'as_of': as_of.isoformat(),
        'state': result['state'],
        'eligible_for_disposal': result['eligible_for_disposal'],
    })
    return result


def _hold_timeline_entry(hold):
    return {
        'event': 'hold',
        'hold_id': hold.id,
        'hold_type': hold.hold_type,
        'hold_type_display': hold.get_hold_type_display(),
        'reference_no': hold.reference_no,
        'authority': hold.authority,
        'start_date': hold.start_date.isoformat(),
        'end_date': hold.end_date.isoformat() if hold.end_date else None,
        'status': 'active' if hold.is_active else 'lifted',
        'detail': str(hold),
    }


def _append_active_hold_reasons(result, holds, as_of):
    active = [h for h in holds if h.is_active_on(as_of)]
    if not active:
        return
    result['indefinite_hold'] = True
    for hold in active:
        result['retained_reasons'].append({
            'code': hold.hold_type,
            'hold_id': hold.id,
            'message': (
                f'{hold.get_hold_type_display()}中，期限暂停：{hold.authority}'
                f'{("，文书/案号 " + hold.reference_no) if hold.reference_no else ""}'
                f'，自 {hold.start_date} 起未解除'
            ),
        })


def _evaluate_state(result, holds, as_of, original_due, target_due, projected):
    """根据基准日判定处置资格与状态，填充 retained_reasons。"""
    reasons = result['retained_reasons']
    _append_active_hold_reasons(result, holds, as_of)
    active_hold = result['indefinite_hold']

    # 法律冻结/未结调查具有最高优先级：即使名义期限已届满（含到期日后才开始的
    # 冻结，不补天数但仍阻止销毁），只要暂停未解除，就不能进入销毁流程。
    if active_hold:
        result['state'] = DisposalPlan.STATE_SUSPENDED
        reasons.append({
            'code': 'indefinite_suspension',
            'message': '暂停解除后期限方可继续计算/进入销毁流程，当前销毁资格被冻结/调查阻断',
        })
        return

    if projected is not None and as_of > projected:
        # 名义期限届满且无活动暂停 → 具备处置资格
        result['state'] = DisposalPlan.STATE_ELIGIBLE
        result['eligible_for_disposal'] = True
        return

    effective_due = projected
    days_left = (effective_due - as_of).days
    if target_due > original_due and as_of >= original_due:
        result['state'] = DisposalPlan.STATE_EXTENDED
    elif as_of >= original_due:
        result['state'] = DisposalPlan.STATE_TOLLED
    reasons.append({
        'code': 'within_retention_term',
        'message': f'尚在保管期限内，预计 {effective_due} 期限届满（剩余 {days_left} 天，届满次日起可申请销毁）',
        'projected_due_date': effective_due.isoformat(),
        'days_remaining': days_left,
    })


def assert_disposal_eligible(item, as_of=None):
    """销毁流程准入校验，供申请接口复用。"""
    schedule = compute_schedule(item, as_of=as_of)
    if not schedule['eligible_for_disposal']:
        messages = '；'.join(r['message'] for r in schedule['retained_reasons']) or '期限尚未届满'
        raise BusinessException(f'物资 {item.code} 暂不能进入销毁流程：{messages}', code=409)
    return schedule


@transaction.atomic
def grant_extension(item, days, reason, approver, approved_at, created_by=None):
    """
    主管批准延期：以批准日的期限视图认定基准到期日，向后加 days 天。
    处于未解除冻结/调查时拒绝（期限本就暂停，延期没有依据）；
    延期后到期日不得早于批准日。原到期日与历次快照保留不变。
    """
    if days <= 0:
        raise BusinessException('延期天数必须为正整数')
    approved_day = approved_at.date()
    schedule = compute_schedule(item, as_of=approved_day)
    if schedule['indefinite_hold']:
        raise BusinessException('物资处于未解除的法律冻结/调查中，期限正在暂停，不能批准延期')

    base_due = schedule['projected_due_date']
    new_due = base_due + timedelta(days=days)
    if new_due < approved_day:
        raise BusinessException(f'延期后到期日 {new_due} 早于批准日期 {approved_day}，延期无意义')

    seq = item.extensions.count() + 1
    return RetentionExtension.objects.create(
        item=item,
        seq=seq,
        base_due_date=base_due,
        new_due_date=new_due,
        extension_days=days,
        approver=approver,
        reason=reason,
        approved_at=approved_at,
        created_by=created_by or approver,
    )


@transaction.atomic
def generate_plan(item, as_of=None, persist=True):
    """
    按入库事实与当前全部事实计算处置资格；persist=True 时新增一行 DisposalPlan
    （历史计划全部保留，可追溯每次计算结论），否则只返回计算结果（预览）。
    """
    as_of = as_of or date.today()
    schedule = compute_schedule(item, as_of=as_of)
    if not persist:
        return schedule

    return DisposalPlan.objects.create(
        item=item,
        as_of_date=as_of,
        rule_version=schedule['rule'],
        original_due_date=schedule['original_due_date'],
        target_due_date=schedule['target_due_date'],
        projected_due_date=schedule['projected_due_date'],
        suspension_days=schedule['suspension_days'],
        extension_days=schedule['extension_days'],
        indefinite_hold=schedule['indefinite_hold'],
        state=schedule['state'],
        eligible_for_disposal=schedule['eligible_for_disposal'],
        retained_reasons=schedule['retained_reasons'],
        timeline=schedule['timeline'],
    )


@transaction.atomic
def release_rule_version(rule, effective_date, created_by, **updates):
    """
    规则换版：基于 rule 发布新版本。旧版本置为 superseded 并指向新版，
    记录保留——历史物资仍按入库时快照版本计算，历史日期仍可按旧版解析。
    """
    if effective_date <= rule.effective_date:
        raise BusinessException(
            f'新生效日期 {effective_date} 必须晚于被替代版本的生效日期 {rule.effective_date}'
        )
    is_permanent = updates.get('is_permanent', rule.is_permanent)
    retention_years = updates.get('retention_years', rule.retention_years)
    if not is_permanent and not retention_years:
        raise BusinessException('非永久保管规则必须指定保管年限')

    new_rule = RetentionRule.objects.create(
        case_type=rule.case_type,
        category=rule.category,
        retention_years=None if is_permanent else retention_years,
        is_permanent=is_permanent,
        effective_date=effective_date,
        version=rule.version + 1,
        status=RetentionRule.STATUS_ACTIVE,
        replaces=rule,
        remark=updates.get('remark', rule.remark),
        created_by=created_by,
    )
    rule.status = RetentionRule.STATUS_SUPERSEDED
    rule.superseded_by = new_rule
    rule.save(update_fields=['status', 'superseded_by', 'updated_at'])
    return new_rule
