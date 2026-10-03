"""
保管期限规则相关 API。

主管视角的关键接口：
- GET  /api/custody-items/<id>/schedule/   某件物资为何仍被保留、何时可销毁（含完整时间线）
- POST /api/custody-items/<id>/holds/      登记法律冻结/未结调查（暂停期限）
- POST /api/custody-holds/<id>/lift/       解除暂停（期限顺延重算）
- POST /api/custody-items/<id>/extensions/ 主管批准延期（重算到期日，原到期日保留）
- POST /api/custody-items/<id>/generate-plan/  生成处置计划
- POST /api/custody-items/<id>/disposal-requests/  期限届满后发起销毁流程
规则侧：
- GET/POST /api/retention/rules/、GET /api/retention/rules/<id>/
- GET /api/retention/rules/resolve/  按日期解析适用规则版本（换版可追溯）
"""
from datetime import date, datetime

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.exceptions import BusinessException
from apps.core.response import success_response, error_response
from .retention_engine import (
    assert_disposal_eligible,
    compute_schedule,
    generate_plan,
    grant_extension,
    release_rule_version,
    resolve_rule,
)
from .retention_models import (
    CustodyHold,
    CustodyItem,
    DisposalPlan,
    DisposalRequest,
    RetentionRule,
)
from .retention_serializers import (
    CustodyHoldCreateSerializer,
    CustodyHoldSerializer,
    CustodyItemCreateSerializer,
    CustodyItemSerializer,
    DisposalDecisionSerializer,
    DisposalPlanSerializer,
    DisposalRequestSerializer,
    ExtensionCreateSerializer,
    RetentionExtensionSerializer,
    RetentionRuleCreateSerializer,
    RetentionRuleSerializer,
)


def _parse_date(raw, field='as_of'):
    if not raw:
        return date.today()
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise BusinessException(f'{field} 日期格式应为 YYYY-MM-DD')


def _paginate(request, queryset):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = min(max(int(request.query_params.get('page_size', 10)), 1), 200)
    total = queryset.count()
    return queryset[(page - 1) * page_size:page * page_size], total, page, page_size


def _require_admin(user):
    if not user.is_admin:
        raise BusinessException('该操作需要主管（管理员）权限', code=403)


def _first_error(serializer):
    errors = serializer.errors
    value = list(errors.values())[0]
    if isinstance(value, dict):
        value = list(value.values())[0]
    if isinstance(value, list):
        value = value[0]
    return str(value)


# ==================== 保管期限规则 ====================

class RetentionRuleListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = RetentionRule.objects.select_related('category', 'created_by').all()
        case_type = request.query_params.get('case_type')
        if case_type:
            qs = qs.filter(case_type=case_type)
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category_id=category)
        scope = request.query_params.get('scope')  # general / specific
        if scope == 'general':
            qs = qs.filter(category__isnull=True)
        elif scope == 'specific':
            qs = qs.filter(category__isnull=False)
        status_ = request.query_params.get('status')
        if status_:
            qs = qs.filter(status=status_)
        qs = qs.order_by('case_type', 'category_id', '-effective_date', '-version')
        items, total, page, page_size = _paginate(request, qs)
        return success_response(data={
            'list': RetentionRuleSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        _require_admin(request.user)
        serializer = RetentionRuleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        base = data['_base']
        if base is not None:
            rule = release_rule_version(
                base, data['effective_date'], request.user,
                retention_years=data.get('retention_years'),
                is_permanent=data.get('is_permanent', False),
                remark=data.get('remark', ''),
            )
        else:
            rule = RetentionRule.objects.create(
                case_type=data['case_type'],
                category_id=data.get('category'),
                retention_years=None if data.get('is_permanent') else data['retention_years'],
                is_permanent=data.get('is_permanent', False),
                effective_date=data['effective_date'],
                version=data['_version'],
                remark=data.get('remark', ''),
                created_by=request.user,
            )
        return success_response(data=RetentionRuleSerializer(rule).data, message='规则版本已发布')


class RetentionRuleDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        rule = RetentionRule.objects.select_related('category', 'created_by', 'replaces').filter(pk=pk).first()
        if rule is None:
            return error_response(message='规则不存在', code=404)
        data = RetentionRuleSerializer(rule).data
        data['successors'] = RetentionRuleSerializer(
            [s for s in rule.successors.all()], many=True
        ).data
        return success_response(data=data)


class RetentionRuleResolveView(APIView):
    """按日期解析适用规则（品类专用优先于通用），供登记与核查使用"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        case_type = request.query_params.get('case_type')
        if not case_type:
            return error_response(message='请提供 case_type')
        category_id = request.query_params.get('category')
        on_date = _parse_date(request.query_params.get('on_date'), field='on_date')
        rule = resolve_rule(case_type, category_id, on_date)
        return success_response(data={
            'rule': RetentionRuleSerializer(rule).data,
            'resolved_on': on_date.isoformat(),
            'note': '品类专用规则优先；规则按生效日期匹配，入库时快照不受后续换版影响',
        })


# ==================== 保管物资 ====================

class CustodyItemListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = CustodyItem.objects.select_related('category', 'rule_version', 'created_by').all()
        code = request.query_params.get('code')
        if code:
            qs = qs.filter(code__icontains=code)
        case_type = request.query_params.get('case_type')
        if case_type:
            qs = qs.filter(case_type=case_type)
        case_no = request.query_params.get('case_no')
        if case_no:
            qs = qs.filter(case_no__icontains=case_no)
        category = request.query_params.get('category')
        if category:
            qs = qs.filter(category_id=category)
        if request.query_params.get('is_active') in ('true', 'false'):
            qs = qs.filter(is_active=request.query_params.get('is_active') == 'true')
        qs = qs.order_by('-intake_date', '-created_at')
        items, total, page, page_size = _paginate(request, qs)
        return success_response(data={
            'list': CustodyItemSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        serializer = CustodyItemCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        goods = serializer.context.get('_goods_obj')
        item = CustodyItem.objects.create(
            code=data['code'], name=data['name'], goods=goods,
            category_id=data.get('category'),
            case_type=data['case_type'], case_no=data.get('case_no', ''),
            intake_date=data['intake_date'], rule_version=data['_rule'],
            location=data.get('location', ''), remark=data.get('remark', ''),
            created_by=request.user,
        )
        return success_response(data=CustodyItemSerializer(item).data, message='保管物资登记成功')


class CustodyItemDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        item = CustodyItem.objects.select_related('category', 'rule_version').filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        return success_response(data=CustodyItemSerializer(item).data)


class CustodyScheduleView(APIView):
    """主管核查接口：某件物资为何仍被保留 / 何时可以进入销毁流程"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        item = CustodyItem.objects.select_related('rule_version').filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        as_of = _parse_date(request.query_params.get('as_of'))
        schedule = compute_schedule(item, as_of=as_of)
        return success_response(data=_schedule_payload(item, schedule))


def _schedule_payload(item, schedule):
    return {
        'item': CustodyItemSerializer(item).data,
        'as_of_date': schedule['as_of_date'].isoformat(),
        'original_due_date': schedule['original_due_date'].isoformat()
            if schedule['original_due_date'] else None,
        'target_due_date': schedule['target_due_date'].isoformat()
            if schedule['target_due_date'] else None,
        'projected_due_date': schedule['projected_due_date'].isoformat()
            if schedule['projected_due_date'] else None,
        'suspension_days': schedule['suspension_days'],
        'extension_days': schedule['extension_days'],
        'indefinite_hold': schedule['indefinite_hold'],
        'state': schedule['state'],
        'state_display': dict(DisposalPlan.STATE_CHOICES)[schedule['state']],
        'eligible_for_disposal': schedule['eligible_for_disposal'],
        'retained_reasons': schedule['retained_reasons'],
        'timeline': schedule['timeline'],
    }


class CustodyTimelineView(APIView):
    """物资全量追溯：规则版本、入库、冻结/调查、延期、历次计划与销毁申请"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        item = CustodyItem.objects.select_related('rule_version').filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        return success_response(data={
            'item': CustodyItemSerializer(item).data,
            'holds': CustodyHoldSerializer(item.holds.all(), many=True).data,
            'extensions': RetentionExtensionSerializer(item.extensions.all(), many=True).data,
            'plans': DisposalPlanSerializer(item.plans.all()[:50], many=True).data,
            'disposal_requests': DisposalRequestSerializer(item.disposal_requests.all(), many=True).data,
        })


# ==================== 暂停（冻结 / 调查） ====================

class CustodyHoldCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = CustodyItem.objects.filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        serializer = CustodyHoldCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data

        approver_id = data.get('approver')
        approver = None
        if approver_id:
            from apps.authentication.models import User
            approver = User.objects.filter(pk=approver_id).first()

        hold = CustodyHold.objects.create(
            item=item, hold_type=data['hold_type'],
            reference_no=data.get('reference_no', ''), authority=data['authority'],
            reason=data.get('reason', ''), start_date=data['start_date'],
            approver=approver, created_by=request.user,
        )
        return success_response(data=CustodyHoldSerializer(hold).data,
                                message='暂停已登记，保管期限自暂停起始日起停止计算')


class CustodyHoldLiftView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        hold = CustodyHold.objects.select_related('item').filter(pk=pk).first()
        if hold is None:
            return error_response(message='暂停记录不存在', code=404)
        if hold.end_date is not None:
            return error_response(message='该暂停记录已解除，不能重复解除')
        end_date = _parse_date(request.data.get('end_date'), field='end_date')
        if end_date < hold.start_date:
            return error_response(message='解除日期不能早于暂停开始日期')
        if end_date > date.today():
            return error_response(message='解除日期不能晚于今天')
        hold.end_date = end_date
        hold.lifted_by = request.user
        hold.lifted_at = timezone.now()
        hold.save(update_fields=['end_date', 'lifted_by', 'lifted_at'])
        schedule = compute_schedule(hold.item, as_of=end_date)
        return success_response(data={
            'hold': CustodyHoldSerializer(hold).data,
            'projected_due_date': schedule['projected_due_date'].isoformat()
                if schedule['projected_due_date'] else None,
            'suspension_days': schedule['suspension_days'],
            'message': '暂停已解除，期限按暂停天数顺延重算',
        }, message='暂停解除，期限已重算')


# ==================== 主管延期 ====================

class ExtensionCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = CustodyItem.objects.select_related('rule_version').filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        _require_admin(request.user)
        serializer = ExtensionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data

        from apps.authentication.models import User
        approver = User.objects.get(pk=data['approver'])
        approved_at = data.get('approved_at') or timezone.now()
        if timezone.is_naive(approved_at):
            approved_at = timezone.make_aware(approved_at)
        try:
            extension = grant_extension(
                item, days=data['days'], reason=data['reason'],
                approver=approver, approved_at=approved_at, created_by=request.user,
            )
        except BusinessException as exc:
            return error_response(message=exc.message, code=exc.code)
        schedule = compute_schedule(item, as_of=approved_at.date())
        return success_response(data={
            'extension': RetentionExtensionSerializer(extension).data,
            'original_due_date': schedule['original_due_date'].isoformat()
                if schedule['original_due_date'] else None,
            'projected_due_date': schedule['projected_due_date'].isoformat()
                if schedule['projected_due_date'] else None,
        }, message='延期已批准，到期日已重算（原到期日保留可查）')


# ==================== 处置计划 ====================

class DisposalPlanListGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = DisposalPlan.objects.select_related('item', 'rule_version').all()
        item_id = request.query_params.get('item')
        if item_id:
            qs = qs.filter(item_id=item_id)
        state = request.query_params.get('state')
        if state:
            qs = qs.filter(state=state)
        if request.query_params.get('eligible') == 'true':
            qs = qs.filter(eligible_for_disposal=True)
        if request.query_params.get('latest') == 'true':
            # 每件物资只保留最新一行
            latest_ids = {}
            for plan in qs.order_by('item_id', '-generated_at'):
                latest_ids.setdefault(plan.item_id, plan.id)
            qs = DisposalPlan.objects.filter(id__in=latest_ids.values())
        qs = qs.order_by('-generated_at')
        items, total, page, page_size = _paginate(request, qs)
        return success_response(data={
            'list': DisposalPlanSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        """批量/单件生成处置计划。请求体：{as_of?, item_ids?, case_type?, category?}"""
        _require_admin(request.user)
        as_of = _parse_date(request.data.get('as_of'))
        qs = CustodyItem.objects.filter(is_active=True).select_related('rule_version')
        item_ids = request.data.get('item_ids')
        if item_ids:
            qs = qs.filter(id__in=item_ids)
        if request.data.get('case_type'):
            qs = qs.filter(case_type=request.data['case_type'])
        if request.data.get('category'):
            qs = qs.filter(category_id=request.data['category'])

        plans, failed = [], []
        for item in qs:
            try:
                plans.append(generate_plan(item, as_of=as_of, persist=True))
            except BusinessException as exc:
                failed.append({'item': item.code, 'reason': exc.message})
        return success_response(data={
            'as_of_date': as_of.isoformat(),
            'generated_count': len(plans),
            'eligible_count': sum(1 for p in plans if p.eligible_for_disposal),
            'suspended_count': sum(1 for p in plans if p.state == DisposalPlan.STATE_SUSPENDED),
            'plans': DisposalPlanSerializer(plans, many=True).data,
            'failed': failed,
        }, message=f'已生成 {len(plans)} 份处置计划')


class DisposalPlanDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        plan = DisposalPlan.objects.select_related('item').filter(pk=pk).first()
        if plan is None:
            return error_response(message='处置计划不存在', code=404)
        return success_response(data=DisposalPlanSerializer(plan).data)


class CustodyGeneratePlanView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        item = CustodyItem.objects.select_related('rule_version').filter(pk=pk).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        as_of = _parse_date(request.data.get('as_of'))
        plan = generate_plan(item, as_of=as_of, persist=True)
        return success_response(data=DisposalPlanSerializer(plan).data, message='处置计划已生成')


# ==================== 销毁流程申请 ====================

class DisposalRequestListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        qs = DisposalRequest.objects.select_related('item', 'requested_by').all()
        item_id = pk or request.query_params.get('item')
        if item_id:
            qs = qs.filter(item_id=item_id)
        status_ = request.query_params.get('status')
        if status_:
            qs = qs.filter(status=status_)
        items, total, page, page_size = _paginate(request, qs.order_by('-created_at'))
        return success_response(data={
            'list': DisposalRequestSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request, pk=None):
        item_id = pk or request.data.get('item')
        item = CustodyItem.objects.select_related('rule_version').filter(pk=item_id).first()
        if item is None:
            return error_response(message='保管物资不存在', code=404)
        as_of = _parse_date(request.data.get('as_of'))
        try:
            schedule = assert_disposal_eligible(item, as_of=as_of)
        except BusinessException as exc:
            return error_response(message=exc.message, code=exc.code)

        pending = item.disposal_requests.filter(status=DisposalRequest.STATUS_PENDING).exists()
        if pending:
            return error_response(message='该物资已有待审批的销毁申请，请勿重复发起')

        disposal = DisposalRequest.objects.create(
            item=item, reason=request.data.get('reason', ''),
            requested_by=request.user,
        )
        return success_response(data={
            'request': DisposalRequestSerializer(disposal).data,
            'basis': _schedule_payload(item, schedule),
        }, message='期限已届满，销毁申请已发起')


class DisposalRequestDecisionView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        disposal = DisposalRequest.objects.select_related('item', 'item__rule_version').filter(pk=pk).first()
        if disposal is None:
            return error_response(message='销毁申请不存在', code=404)
        _require_admin(request.user)
        if disposal.status != DisposalRequest.STATUS_PENDING:
            return error_response(message='该申请已审批，不能重复审批')
        serializer = DisposalDecisionSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        if serializer.validated_data['status'] == DisposalRequest.STATUS_APPROVED:
            try:
                assert_disposal_eligible(disposal.item)
            except BusinessException as exc:
                return error_response(
                    message=f'审批时复核未通过：{exc.message}', code=409
                )
        disposal.status = serializer.validated_data['status']
        disposal.decision_remark = serializer.validated_data.get('decision_remark', '')
        disposal.decided_by = request.user
        disposal.decided_at = timezone.now()
        disposal.save(update_fields=['status', 'decision_remark', 'decided_by', 'decided_at'])
        if disposal.status == DisposalRequest.STATUS_APPROVED:
            disposal.item.is_active = False
            disposal.item.save(update_fields=['is_active', 'updated_at'])
        return success_response(data=DisposalRequestSerializer(disposal).data,
                                message='销毁申请已批准' if disposal.status == 'approved' else '销毁申请已拒绝')
