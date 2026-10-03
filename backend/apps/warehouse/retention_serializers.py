"""
保管期限规则相关序列化器。
"""
from datetime import date

from rest_framework import serializers

from .retention_models import (
    CustodyHold,
    CustodyItem,
    DisposalPlan,
    DisposalRequest,
    RetentionExtension,
    RetentionRule,
)
from .retention_engine import resolve_rule


class RetentionRuleSerializer(serializers.ModelSerializer):
    """规则序列化器"""
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    replaces_id = serializers.IntegerField(read_only=True)
    superseded_by_id = serializers.IntegerField(read_only=True)
    version_chain = serializers.SerializerMethodField()

    class Meta:
        model = RetentionRule
        fields = [
            'id', 'case_type', 'case_type_display', 'category', 'category_name',
            'retention_years', 'is_permanent', 'effective_date', 'version',
            'status', 'status_display', 'replaces_id', 'superseded_by_id',
            'version_chain', 'remark', 'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'version', 'status', 'created_at', 'updated_at']

    def get_version_chain(self, obj):
        """沿 replaces 向前回溯版本链 id（当前版本 → 最早版本）"""
        chain = []
        node = obj
        seen = set()
        while node is not None and node.id not in seen:
            seen.add(node.id)
            chain.append({
                'id': node.id,
                'version': node.version,
                'effective_date': node.effective_date.isoformat(),
                'status': node.status,
            })
            node = node.replaces
        return chain


class RetentionRuleCreateSerializer(serializers.Serializer):
    """新建规则版本（首个版本或显式指定 replaces 的换版均可）"""
    case_type = serializers.ChoiceField(choices=RetentionRule.CASE_TYPE_CHOICES, required=True,
                                        error_messages={'required': '请选择案件类型'})
    category = serializers.IntegerField(required=False, allow_null=True)
    retention_years = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=100)
    is_permanent = serializers.BooleanField(required=False, default=False)
    effective_date = serializers.DateField(required=True, error_messages={'required': '请填写生效日期'})
    replaces = serializers.IntegerField(required=False, allow_null=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_category(self, value):
        if value:
            from .models import Category
            if not Category.objects.filter(pk=value).exists():
                raise serializers.ValidationError('品类不存在')
        return value

    def validate_effective_date(self, value):
        # 生效日期通常面向未来；允许补录历史规则（换版追溯），但不允许晚于今天 1 年以上的明显误录
        return value

    def validate(self, data):
        is_permanent = data.get('is_permanent', False)
        years = data.get('retention_years')
        if not is_permanent and not years:
            raise serializers.ValidationError('非永久保管规则必须指定保管年限')
        if is_permanent:
            data['retention_years'] = None

        replaces_id = data.get('replaces')
        base = None
        if replaces_id:
            base = RetentionRule.objects.filter(pk=replaces_id).first()
            if base is None:
                raise serializers.ValidationError({'replaces': '被替代的规则版本不存在'})
            if data['case_type'] != base.case_type or data.get('category') != base.category_id:
                raise serializers.ValidationError('换版不能改变案件类型与适用品类')
            if data['effective_date'] <= base.effective_date:
                raise serializers.ValidationError(
                    f'新生效日期必须晚于上一版本生效日期 {base.effective_date}'
                )

        # 同一生效日期 + 同适用范围不得重复出版本
        dup_qs = RetentionRule.objects.filter(
            case_type=data['case_type'],
            category_id=data.get('category'),
            effective_date=data['effective_date'],
        )
        if base:
            dup_qs = dup_qs.exclude(pk=base.pk)
        if dup_qs.exists():
            raise serializers.ValidationError('该案件类型/品类在此生效日期已存在规则版本')

        # 版本号：同范围内已有版本则取 max + 1
        scope_qs = RetentionRule.objects.filter(
            case_type=data['case_type'], category_id=data.get('category')
        )
        if base is not None:
            data['_version'] = base.version + 1
        else:
            latest = scope_qs.order_by('-version').first()
            data['_version'] = (latest.version + 1) if latest else 1

        # 同范围内生效日期必须单调（不能在版本序列中间插入造成倒挂）
        if base is None and latest is not None:
            if data['effective_date'] < latest.effective_date:
                raise serializers.ValidationError(
                    f'新生效日期不得早于现行版本生效日期 {latest.effective_date}；如需请走补录流程'
                )
        data['_base'] = base
        return data


class CustodyItemSerializer(serializers.ModelSerializer):
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    registered_by_name = serializers.CharField(source='created_by.username', read_only=True)
    rule_version_info = serializers.SerializerMethodField()

    class Meta:
        model = CustodyItem
        fields = [
            'id', 'code', 'name', 'goods', 'category', 'category_name',
            'case_type', 'case_type_display', 'case_no', 'intake_date',
            'rule_version', 'rule_version_info', 'location', 'remark',
            'is_active', 'created_by', 'registered_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'rule_version', 'created_at', 'updated_at']

    def get_rule_version_info(self, obj):
        rule = obj.rule_version
        return {
            'id': rule.id,
            'version': rule.version,
            'effective_date': rule.effective_date.isoformat(),
            'retention_years': None if rule.is_permanent else rule.retention_years,
            'is_permanent': rule.is_permanent,
        }


class CustodyItemCreateSerializer(serializers.Serializer):
    """登记保管物资：按入库事实自动解析并快照入库时适用规则版本"""
    code = serializers.CharField(max_length=50, required=True)
    name = serializers.CharField(max_length=200, required=True)
    goods = serializers.IntegerField(required=False, allow_null=True)
    category = serializers.IntegerField(required=False, allow_null=True)
    case_type = serializers.ChoiceField(choices=RetentionRule.CASE_TYPE_CHOICES, required=True,
                                        error_messages={'required': '请选择案件类型'})
    case_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    intake_date = serializers.DateField(required=True, error_messages={'required': '请填写入库日期'})
    location = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_code(self, value):
        if CustodyItem.objects.filter(code=value).exists():
            raise serializers.ValidationError('物资编号已存在')
        return value

    def validate_goods(self, value):
        if value:
            from .models import Goods
            goods = Goods.objects.filter(pk=value).first()
            if goods is None:
                raise serializers.ValidationError('关联库存货物不存在')
            self.context['_goods_obj'] = goods
        return value

    def validate_category(self, value):
        if value:
            from .models import Category
            if not Category.objects.filter(pk=value).exists():
                raise serializers.ValidationError('品类不存在')
        return value

    def validate_intake_date(self, value):
        if value > date.today():
            raise serializers.ValidationError('入库日期不能晚于今天')
        return value

    def validate(self, data):
        goods_obj = self.context.get('_goods_obj')
        category_id = data.get('category')
        if not category_id and goods_obj:
            variety = getattr(goods_obj, 'variety', None)
            category_id = variety.category_id if variety else None
            data['category'] = category_id
        # 以入库日解析规则（规则在入库之后换版不影响本物资）
        data['_rule'] = resolve_rule(data['case_type'], category_id, data['intake_date'])
        return data


class CustodyHoldSerializer(serializers.ModelSerializer):
    hold_type_display = serializers.CharField(source='get_hold_type_display', read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    lifted_by_name = serializers.CharField(source='lifted_by.username', read_only=True)

    class Meta:
        model = CustodyHold
        fields = [
            'id', 'item', 'hold_type', 'hold_type_display', 'reference_no',
            'authority', 'reason', 'start_date', 'end_date', 'is_active',
            'approver', 'approver_name', 'lifted_by', 'lifted_by_name', 'lifted_at',
            'created_by', 'created_by_name', 'created_at',
        ]
        read_only_fields = ['id', 'end_date', 'lifted_by', 'lifted_at', 'created_at']


class CustodyHoldCreateSerializer(serializers.Serializer):
    hold_type = serializers.ChoiceField(choices=CustodyHold.TYPE_CHOICES, required=True)
    reference_no = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    authority = serializers.CharField(max_length=200, required=True,
                                      error_messages={'required': '请填写决定机关/办案单位'})
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    start_date = serializers.DateField(required=True, error_messages={'required': '请填写暂停开始日期'})
    approver = serializers.IntegerField(required=False, allow_null=True)

    def validate_approver(self, value):
        if value:
            from apps.authentication.models import User
            if not User.objects.filter(pk=value).exists():
                raise serializers.ValidationError('批准人不存在')
        return value

    def validate_start_date(self, value):
        if value > date.today():
            raise serializers.ValidationError('暂停开始日期不能晚于今天')
        return value


class RetentionExtensionSerializer(serializers.ModelSerializer):
    approver_name = serializers.CharField(source='approver.username', read_only=True)

    class Meta:
        model = RetentionExtension
        fields = [
            'id', 'item', 'seq', 'base_due_date', 'new_due_date',
            'extension_days', 'approver', 'approver_name', 'reason',
            'approved_at', 'created_by',
        ]
        read_only_fields = fields


class ExtensionCreateSerializer(serializers.Serializer):
    days = serializers.IntegerField(required=True, min_value=1, max_value=3650,
                                    error_messages={'required': '请填写延期天数'})
    reason = serializers.CharField(required=True, error_messages={'required': '请填写延期理由'})
    approver = serializers.IntegerField(required=True, error_messages={'required': '请选择批准主管'})
    approved_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_approver(self, value):
        from apps.authentication.models import User
        user = User.objects.filter(pk=value).first()
        if user is None:
            raise serializers.ValidationError('批准主管不存在')
        if not user.is_admin:
            raise serializers.ValidationError('延期须由主管（管理员角色）批准')
        return value


class DisposalPlanSerializer(serializers.ModelSerializer):
    state_display = serializers.CharField(source='get_state_display', read_only=True)
    rule_version = serializers.IntegerField(source='rule_version_id', read_only=True)
    item_code = serializers.CharField(source='item.code', read_only=True)
    item_name = serializers.CharField(source='item.name', read_only=True)

    class Meta:
        model = DisposalPlan
        fields = [
            'id', 'item', 'item_code', 'item_name', 'generated_at', 'as_of_date',
            'rule_version', 'original_due_date', 'target_due_date', 'projected_due_date',
            'suspension_days', 'extension_days', 'indefinite_hold',
            'state', 'state_display', 'eligible_for_disposal',
            'retained_reasons', 'timeline',
        ]


class DisposalRequestSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    requested_by_name = serializers.CharField(source='requested_by.username', read_only=True)
    decided_by_name = serializers.CharField(source='decided_by.username', read_only=True)

    class Meta:
        model = DisposalRequest
        fields = [
            'id', 'item', 'status', 'status_display', 'reason',
            'requested_by', 'requested_by_name', 'created_at',
            'decided_by', 'decided_by_name', 'decided_at', 'decision_remark',
        ]
        read_only_fields = ['id', 'status', 'requested_by', 'created_at',
                            'decided_by', 'decided_at', 'decision_remark']


class DisposalDecisionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=[DisposalRequest.STATUS_APPROVED, DisposalRequest.STATUS_REJECTED])
    decision_remark = serializers.CharField(required=False, allow_blank=True, default='')
