"""
保管期限序列化器
"""
from rest_framework import serializers
from apps.warehouse.models import Category, Goods
from . import services
from .models import (
    CASE_TYPE_CHOICES, DisposalPlan, RetentionEvent,
    RetentionHold, RetentionRecord, RetentionRule,
)


class RetentionRuleSerializer(serializers.ModelSerializer):
    """保管期限规则序列化器"""
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    is_referenced = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)

    class Meta:
        model = RetentionRule
        fields = [
            'id', 'case_type', 'case_type_display', 'category', 'category_name',
            'retention_months', 'effective_from', 'is_active', 'is_referenced',
            'remark', 'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class RetentionRuleCreateSerializer(serializers.Serializer):
    """保管期限规则创建/更新序列化器"""
    case_type = serializers.ChoiceField(choices=CASE_TYPE_CHOICES, error_messages={
        'required': '请选择案件类型',
        'invalid_choice': '无效的案件类型',
    })
    category = serializers.IntegerField(error_messages={
        'required': '请选择物资品类',
    })
    retention_months = serializers.IntegerField(min_value=1, max_value=1200, error_messages={
        'required': '请填写保管月数',
        'min_value': '保管月数至少为1个月',
        'max_value': '保管月数不能超过1200个月',
    })
    effective_from = serializers.DateField(error_messages={
        'required': '请选择生效日期',
        'invalid': '生效日期格式无效',
    })
    is_active = serializers.BooleanField(required=False, default=True)
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value

    def validate(self, data):
        instance = self.context.get('instance')
        queryset = RetentionRule.objects.filter(
            case_type=data['case_type'],
            category_id=data['category'],
            effective_from=data['effective_from'],
        )
        if instance:
            queryset = queryset.exclude(pk=instance.pk)
        if queryset.exists():
            raise serializers.ValidationError('该案件类型与品类在此生效日期已存在规则版本')
        return data


class RetentionHoldSerializer(serializers.ModelSerializer):
    """保管措施序列化器"""
    hold_type_display = serializers.CharField(source='get_hold_type_display', read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    released_by_name = serializers.CharField(source='released_by.username', read_only=True)

    class Meta:
        model = RetentionHold
        fields = [
            'id', 'record', 'hold_type', 'hold_type_display', 'reason',
            'start_date', 'end_date', 'is_active', 'extended_due_date',
            'approved_by', 'approved_by_name', 'created_by', 'created_by_name',
            'released_by', 'released_by_name', 'release_reason', 'created_at'
        ]


class RetentionHoldCreateSerializer(serializers.Serializer):
    """保管措施登记序列化器"""
    hold_type = serializers.ChoiceField(choices=RetentionHold.TYPE_CHOICES, error_messages={
        'required': '请选择措施类型',
        'invalid_choice': '无效的措施类型',
    })
    reason = serializers.CharField(min_length=1, max_length=500, error_messages={
        'required': '请填写事由',
        'blank': '事由不能为空',
        'max_length': '事由最多500个字',
    })
    start_date = serializers.DateField(required=False, error_messages={
        'invalid': '开始日期格式无效',
    })
    extended_due_date = serializers.DateField(required=False, error_messages={
        'invalid': '延期后到期日格式无效',
    })


class RetentionRecordListSerializer(serializers.ModelSerializer):
    """保管期限记录列表序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    case_type_display = serializers.CharField(source='get_case_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    current_due_date = serializers.SerializerMethodField()
    is_suspended = serializers.SerializerMethodField()
    is_eligible = serializers.SerializerMethodField()

    class Meta:
        model = RetentionRecord
        fields = [
            'id', 'goods', 'goods_name', 'goods_code', 'case_type', 'case_type_display',
            'anchor_date', 'original_due_date', 'current_due_date',
            'status', 'status_display', 'is_suspended', 'is_eligible',
            'disposed_at', 'created_by', 'created_by_name', 'created_at'
        ]

    def get_current_due_date(self, obj):
        return services.compute_current_due_date(obj)

    def get_is_suspended(self, obj):
        return services.active_pause_holds(obj).exists()

    def get_is_eligible(self, obj):
        return services.is_eligible(obj)


class RetentionRecordDetailSerializer(RetentionRecordListSerializer):
    """保管期限记录详情序列化器（含保留原因说明与措施列表）"""
    rule_effective_from = serializers.DateField(source='rule.effective_from', read_only=True)
    retention_months = serializers.IntegerField(source='rule.retention_months', read_only=True)
    explanation = serializers.SerializerMethodField()
    holds = RetentionHoldSerializer(many=True, read_only=True)

    class Meta(RetentionRecordListSerializer.Meta):
        fields = RetentionRecordListSerializer.Meta.fields + [
            'rule', 'rule_effective_from', 'retention_months',
            'explanation', 'holds', 'updated_at'
        ]

    def get_explanation(self, obj):
        return services.explain(obj)


class RetentionRecordCreateSerializer(serializers.Serializer):
    """保管期限记录建档序列化器"""
    goods = serializers.IntegerField(error_messages={
        'required': '请选择货物',
    })
    case_type = serializers.ChoiceField(choices=CASE_TYPE_CHOICES, error_messages={
        'required': '请选择案件类型',
        'invalid_choice': '无效的案件类型',
    })

    def validate_goods(self, value):
        goods = Goods.objects.filter(pk=value).select_related('variety__category').first()
        if not goods:
            raise serializers.ValidationError('货物不存在')
        if hasattr(goods, 'retention_record'):
            raise serializers.ValidationError('该货物已建立保管期限记录')
        return value


class DisposalPlanSerializer(serializers.ModelSerializer):
    """处置计划序列化器"""
    goods_name = serializers.CharField(source='record.goods.name', read_only=True)
    goods_code = serializers.CharField(source='record.goods.code', read_only=True)
    method_display = serializers.CharField(source='get_method_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    approved_by_name = serializers.CharField(source='approved_by.username', read_only=True)
    executed_by_name = serializers.CharField(source='executed_by.username', read_only=True)

    class Meta:
        model = DisposalPlan
        fields = [
            'id', 'record', 'goods_name', 'goods_code', 'planned_date',
            'method', 'method_display', 'status', 'status_display',
            'remark', 'cancel_reason',
            'created_by', 'created_by_name',
            'approved_by', 'approved_by_name', 'approved_at',
            'executed_by', 'executed_by_name', 'executed_at',
            'created_at', 'updated_at'
        ]


class DisposalPlanCreateSerializer(serializers.Serializer):
    """处置计划创建序列化器"""
    record = serializers.IntegerField(error_messages={
        'required': '请选择保管记录',
    })
    planned_date = serializers.DateField(required=False, error_messages={
        'invalid': '计划处置日期格式无效',
    })
    method = serializers.ChoiceField(
        choices=DisposalPlan.METHOD_CHOICES, default='destruction',
        error_messages={'invalid_choice': '无效的处置方式'}
    )
    remark = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_record(self, value):
        if not RetentionRecord.objects.filter(pk=value).exists():
            raise serializers.ValidationError('保管记录不存在')
        return value


class RetentionEventSerializer(serializers.ModelSerializer):
    """保管期限事件序列化器"""
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    actor_name = serializers.CharField(source='actor.username', read_only=True)

    class Meta:
        model = RetentionEvent
        fields = [
            'id', 'event_type', 'event_type_display', 'detail',
            'actor', 'actor_name', 'created_at'
        ]
