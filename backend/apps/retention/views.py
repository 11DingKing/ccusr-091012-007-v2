"""
保管期限视图
"""
import logging
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from apps.core.response import success_response, error_response
from apps.core.exceptions import BusinessException
from apps.warehouse.models import Goods
from . import services
from .models import DisposalPlan, RetentionHold, RetentionRecord, RetentionRule
from .serializers import (
    DisposalPlanCreateSerializer, DisposalPlanSerializer,
    RetentionEventSerializer, RetentionHoldCreateSerializer, RetentionHoldSerializer,
    RetentionRecordCreateSerializer, RetentionRecordDetailSerializer,
    RetentionRecordListSerializer, RetentionRuleCreateSerializer, RetentionRuleSerializer,
)

logger = logging.getLogger('apps')


def _first_error(errors):
    """取序列化器的第一条错误信息"""
    first_error = list(errors.values())[0]
    if isinstance(first_error, list):
        first_error = first_error[0]
    return str(first_error)


def _paginate(request, items, total):
    """按现有接口风格分页"""
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 10))
    start = (page - 1) * page_size
    end = start + page_size
    return items[start:end], {
        'total': total,
        'page': page,
        'page_size': page_size,
    }


# ==================== 保管期限规则 ====================

class RetentionRuleListView(APIView):
    """保管期限规则列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = RetentionRule.objects.select_related('category').all()

        case_type = request.query_params.get('case_type')
        if case_type:
            queryset = queryset.filter(case_type=case_type)
        category = request.query_params.get('category')
        if category:
            queryset = queryset.filter(category_id=category)

        total = queryset.count()
        page_items, page_info = _paginate(request, queryset, total)
        serializer = RetentionRuleSerializer(page_items, many=True)

        return success_response(data={'list': serializer.data, **page_info})

    def post(self, request):
        """新建规则版本（仅管理员）"""
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        serializer = RetentionRuleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        rule = RetentionRule.objects.create(
            case_type=data['case_type'],
            category_id=data['category'],
            retention_months=data['retention_months'],
            effective_from=data['effective_from'],
            is_active=data['is_active'],
            remark=data['remark'],
            created_by=request.user,
        )

        logger.info(f"User {request.user.username} created retention rule {rule.id}")

        return success_response(data=RetentionRuleSerializer(rule).data, message='创建成功')


class RetentionRuleDetailView(APIView):
    """保管期限规则详情视图"""
    permission_classes = [IsAuthenticated]

    def put(self, request, pk):
        """更新规则（仅管理员；被引用后关键字段不可改，换版请新建）"""
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        try:
            rule = RetentionRule.objects.get(pk=pk)
        except RetentionRule.DoesNotExist:
            return error_response(message='规则不存在', code=404)

        serializer = RetentionRuleCreateSerializer(data=request.data, context={'instance': rule})
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        if rule.is_referenced:
            immutable_changed = (
                rule.case_type != data['case_type']
                or rule.category_id != data['category']
                or rule.retention_months != data['retention_months']
                or rule.effective_from != data['effective_from']
            )
            if immutable_changed:
                return error_response(message='规则已被保管记录引用，关键字段不可修改，请新建规则版本')

        rule.case_type = data['case_type']
        rule.category_id = data['category']
        rule.retention_months = data['retention_months']
        rule.effective_from = data['effective_from']
        rule.is_active = data['is_active']
        rule.remark = data['remark']
        rule.save()

        logger.info(f"User {request.user.username} updated retention rule {rule.id}")

        return success_response(data=RetentionRuleSerializer(rule).data, message='更新成功')

    def delete(self, request, pk):
        """删除规则（仅管理员；被引用的规则不可删除）"""
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        try:
            rule = RetentionRule.objects.get(pk=pk)
        except RetentionRule.DoesNotExist:
            return error_response(message='规则不存在', code=404)

        if rule.is_referenced:
            return error_response(message='规则已被保管记录引用，无法删除')

        rule.delete()

        logger.info(f"User {request.user.username} deleted retention rule {pk}")

        return success_response(message='删除成功')


# ==================== 保管期限记录 ====================

class RetentionRecordListView(APIView):
    """保管期限记录列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = RetentionRecord.objects.select_related('goods', 'rule').all()

        case_type = request.query_params.get('case_type')
        if case_type:
            queryset = queryset.filter(case_type=case_type)
        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)

        eligible = request.query_params.get('eligible')
        if eligible in ('true', '1', 'false', '0'):
            want = eligible in ('true', '1')
            items = [r for r in queryset if services.is_eligible(r) == want]
            page_items, page_info = _paginate(request, items, len(items))
        else:
            page_items, page_info = _paginate(request, queryset, queryset.count())

        serializer = RetentionRecordListSerializer(page_items, many=True)

        return success_response(data={'list': serializer.data, **page_info})

    def post(self, request):
        """按入库事实为货物建立保管期限记录"""
        serializer = RetentionRecordCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        goods = Goods.objects.get(pk=serializer.validated_data['goods'])
        try:
            record = services.create_record(
                goods, serializer.validated_data['case_type'], actor=request.user
            )
        except BusinessException as e:
            return error_response(message=e.message)

        logger.info(f"User {request.user.username} created retention record for goods {goods.code}")

        return success_response(data=RetentionRecordDetailSerializer(record).data, message='创建成功')


class RetentionRecordDetailView(APIView):
    """保管期限记录详情视图（含保留原因说明）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            record = (RetentionRecord.objects
                      .select_related('goods', 'rule')
                      .prefetch_related('holds')
                      .get(pk=pk))
        except RetentionRecord.DoesNotExist:
            return error_response(message='保管记录不存在', code=404)

        return success_response(data=RetentionRecordDetailSerializer(record).data)


class RetentionRecordEventListView(APIView):
    """保管期限事件列表（审计轨迹）"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            record = RetentionRecord.objects.get(pk=pk)
        except RetentionRecord.DoesNotExist:
            return error_response(message='保管记录不存在', code=404)

        events = record.events.select_related('actor').all()
        serializer = RetentionEventSerializer(events, many=True)

        return success_response(data=serializer.data)


class RetentionRecordHoldView(APIView):
    """保管措施登记视图"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """登记法律冻结/未结调查/批准延期（延期需主管批准）"""
        try:
            record = RetentionRecord.objects.get(pk=pk)
        except RetentionRecord.DoesNotExist:
            return error_response(message='保管记录不存在', code=404)

        serializer = RetentionHoldCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        if data['hold_type'] == 'extension' and not request.user.is_admin:
            return error_response(message='延期需主管批准', code=403)

        try:
            hold = services.place_hold(
                record,
                data['hold_type'],
                data['reason'],
                actor=request.user,
                start_date=data.get('start_date'),
                extended_due_date=data.get('extended_due_date'),
            )
        except BusinessException as e:
            return error_response(message=e.message)

        logger.info(f"User {request.user.username} placed {hold.hold_type} on record {record.id}")

        return success_response(data=RetentionHoldSerializer(hold).data, message='登记成功')


class RetentionHoldReleaseView(APIView):
    """保管措施解除视图"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """解除法律冻结/未结调查（仅管理员）"""
        if not request.user.is_admin:
            return error_response(message='解除措施需主管权限', code=403)

        try:
            hold = RetentionHold.objects.select_related('record').get(pk=pk)
        except RetentionHold.DoesNotExist:
            return error_response(message='措施不存在', code=404)

        end_date = None
        end_date_raw = request.data.get('end_date')
        if end_date_raw:
            end_date = parse_date(end_date_raw)
            if not end_date:
                return error_response(message='解除日期格式无效')

        try:
            services.release_hold(
                hold,
                actor=request.user,
                release_reason=request.data.get('release_reason', ''),
                end_date=end_date,
            )
        except BusinessException as e:
            return error_response(message=e.message)

        logger.info(f"User {request.user.username} released hold {hold.id}")

        return success_response(data=RetentionHoldSerializer(hold).data, message='解除成功')


# ==================== 处置计划 ====================

class DisposalPlanListView(APIView):
    """处置计划列表视图"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = DisposalPlan.objects.select_related('record__goods').all()

        status = request.query_params.get('status')
        if status:
            queryset = queryset.filter(status=status)

        total = queryset.count()
        page_items, page_info = _paginate(request, queryset, total)
        serializer = DisposalPlanSerializer(page_items, many=True)

        return success_response(data={'list': serializer.data, **page_info})

    def post(self, request):
        """为具备处置资格的记录生成处置计划"""
        serializer = DisposalPlanCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        record = RetentionRecord.objects.get(pk=data['record'])
        planned_date = data.get('planned_date') or timezone.localdate()
        if planned_date < timezone.localdate():
            return error_response(message='计划处置日期不能早于今天')

        try:
            plan = services.create_plan(
                record, planned_date, data['method'],
                actor=request.user, remark=data['remark'],
            )
        except BusinessException as e:
            return error_response(message=e.message)

        logger.info(f"User {request.user.username} created disposal plan {plan.id}")

        return success_response(data=DisposalPlanSerializer(plan).data, message='创建成功')


class DisposalPlanGenerateView(APIView):
    """批量生成处置计划视图（仅管理员）"""
    permission_classes = [IsAuthenticated]

    def post(self, request):
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        plans = services.generate_disposal_plans(actor=request.user)

        logger.info(f"User {request.user.username} generated {len(plans)} disposal plans")

        return success_response(
            data={'created_count': len(plans), 'list': DisposalPlanSerializer(plans, many=True).data},
            message=f'成功生成 {len(plans)} 个处置计划'
        )


class DisposalPlanApproveView(APIView):
    """处置计划批准视图（仅管理员）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        try:
            plan = DisposalPlan.objects.select_related('record').get(pk=pk)
        except DisposalPlan.DoesNotExist:
            return error_response(message='处置计划不存在', code=404)

        if plan.status != 'pending':
            return error_response(message='仅待审批的计划可批准')

        plan.status = 'approved'
        plan.approved_by = request.user
        plan.approved_at = timezone.now()
        plan.save(update_fields=['status', 'approved_by', 'approved_at', 'updated_at'])
        services.log_event(plan.record, 'plan_approved', request.user, detail={'plan_id': plan.id})

        logger.info(f"User {request.user.username} approved disposal plan {plan.id}")

        return success_response(data=DisposalPlanSerializer(plan).data, message='批准成功')


class DisposalPlanExecuteView(APIView):
    """处置计划执行视图（仅管理员）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        try:
            plan = DisposalPlan.objects.select_related('record').get(pk=pk)
        except DisposalPlan.DoesNotExist:
            return error_response(message='处置计划不存在', code=404)

        if plan.status != 'approved':
            return error_response(message='计划尚未批准，无法执行')

        record = plan.record
        if record.status != 'in_custody':
            return error_response(message='物资已处置')
        if services.active_pause_holds(record).exists():
            return error_response(message='存在进行中的暂停措施，无法执行处置')

        now = timezone.now()
        plan.status = 'executed'
        plan.executed_by = request.user
        plan.executed_at = now
        plan.save(update_fields=['status', 'executed_by', 'executed_at', 'updated_at'])

        record.status = 'disposed'
        record.disposed_at = now
        record.save(update_fields=['status', 'disposed_at', 'updated_at'])
        services.log_event(record, 'plan_executed', request.user, detail={'plan_id': plan.id})

        logger.info(f"User {request.user.username} executed disposal plan {plan.id}")

        return success_response(data=DisposalPlanSerializer(plan).data, message='执行成功')


class DisposalPlanCancelView(APIView):
    """处置计划取消视图（仅管理员）"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not request.user.is_admin:
            return error_response(message='无权限操作', code=403)

        try:
            plan = DisposalPlan.objects.select_related('record').get(pk=pk)
        except DisposalPlan.DoesNotExist:
            return error_response(message='处置计划不存在', code=404)

        if plan.status not in ['pending', 'approved']:
            return error_response(message='仅待审批或已批准的计划可取消')

        plan.status = 'cancelled'
        plan.cancel_reason = request.data.get('cancel_reason', '')
        plan.save(update_fields=['status', 'cancel_reason', 'updated_at'])
        services.log_event(plan.record, 'plan_cancelled', request.user, detail={
            'plan_id': plan.id,
            'cancel_reason': plan.cancel_reason,
        })

        logger.info(f"User {request.user.username} cancelled disposal plan {plan.id}")

        return success_response(data=DisposalPlanSerializer(plan).data, message='取消成功')
