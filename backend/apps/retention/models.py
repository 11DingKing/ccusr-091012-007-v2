"""
保管期限模型
"""
from django.db import models
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods


CASE_TYPE_CHOICES = [
    ('criminal', '刑事案件'),
    ('civil', '民事案件'),
    ('administrative', '行政案件'),
    ('other', '其他案件'),
]


class RetentionRule(models.Model):
    """保管期限规则（按生效日期版本化，被引用后关键字段不可改）"""
    case_type = models.CharField('案件类型', max_length=20, choices=CASE_TYPE_CHOICES)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='retention_rules', verbose_name='物资品类'
    )
    retention_months = models.PositiveIntegerField('保管月数')
    effective_from = models.DateField('生效日期')
    is_active = models.BooleanField('是否启用', default=True)
    remark = models.TextField('备注', blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_retention_rules', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'rt_rule'
        verbose_name = '保管期限规则'
        verbose_name_plural = verbose_name
        ordering = ['case_type', 'category', '-effective_from']
        unique_together = ['case_type', 'category', 'effective_from']

    def __str__(self):
        return f"{self.get_case_type_display()} - {self.category.name} - {self.retention_months}个月"

    @property
    def is_referenced(self):
        """是否已被保管记录引用"""
        return self.records.exists()


class RetentionRecord(models.Model):
    """物资保管期限记录（按入库事实建档，原到期日永不可改）"""
    STATUS_CHOICES = [
        ('in_custody', '保管中'),
        ('disposed', '已处置'),
    ]

    goods = models.OneToOneField(
        Goods, on_delete=models.CASCADE,
        related_name='retention_record', verbose_name='货物'
    )
    case_type = models.CharField('案件类型', max_length=20, choices=CASE_TYPE_CHOICES)
    rule = models.ForeignKey(
        RetentionRule, on_delete=models.PROTECT,
        related_name='records', verbose_name='适用规则'
    )
    anchor_date = models.DateField('起算日期')
    original_due_date = models.DateField('原到期日')
    current_due_date = models.DateField('当前到期日')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='in_custody')
    disposed_at = models.DateTimeField('处置时间', null=True, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_retention_records', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'rt_record'
        verbose_name = '保管期限记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.get_status_display()}"


class RetentionHold(models.Model):
    """保管暂停/延期措施（法律冻结、未结调查暂停计时，批准延期重算到期日）"""
    TYPE_CHOICES = [
        ('legal_freeze', '法律冻结'),
        ('investigation', '未结调查'),
        ('extension', '批准延期'),
    ]

    record = models.ForeignKey(
        RetentionRecord, on_delete=models.CASCADE,
        related_name='holds', verbose_name='保管记录'
    )
    hold_type = models.CharField('措施类型', max_length=20, choices=TYPE_CHOICES)
    reason = models.TextField('事由')
    start_date = models.DateField('开始日期')
    end_date = models.DateField('解除日期', null=True, blank=True)
    extended_due_date = models.DateField('延期后到期日', null=True, blank=True)
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approved_retention_holds', verbose_name='批准人'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_retention_holds', verbose_name='登记人'
    )
    released_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='released_retention_holds', verbose_name='解除人'
    )
    release_reason = models.CharField('解除说明', max_length=200, blank=True)
    created_at = models.DateTimeField('登记时间', auto_now_add=True)

    class Meta:
        db_table = 'rt_hold'
        verbose_name = '保管措施'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.record.goods.name} - {self.get_hold_type_display()}"

    @property
    def is_active(self):
        """是否进行中"""
        return self.end_date is None


class DisposalPlan(models.Model):
    """处置计划"""
    METHOD_CHOICES = [
        ('destruction', '销毁'),
        ('transfer', '移交'),
    ]
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已批准'),
        ('executed', '已执行'),
        ('cancelled', '已取消'),
    ]

    record = models.ForeignKey(
        RetentionRecord, on_delete=models.CASCADE,
        related_name='disposal_plans', verbose_name='保管记录'
    )
    planned_date = models.DateField('计划处置日期')
    method = models.CharField('处置方式', max_length=20, choices=METHOD_CHOICES, default='destruction')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('备注', blank=True)
    cancel_reason = models.CharField('取消原因', max_length=200, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_disposal_plans', verbose_name='创建人'
    )
    approved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_disposal_plans', verbose_name='批准人'
    )
    approved_at = models.DateTimeField('批准时间', null=True, blank=True)
    executed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='executed_disposal_plans', verbose_name='执行人'
    )
    executed_at = models.DateTimeField('执行时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'rt_disposal_plan'
        verbose_name = '处置计划'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.record.goods.name} - {self.get_method_display()} - {self.get_status_display()}"


class RetentionEvent(models.Model):
    """保管期限事件（审计轨迹，记录规则版本、措施与到期日变化）"""
    TYPE_CHOICES = [
        ('created', '建档'),
        ('hold_placed', '措施登记'),
        ('hold_released', '措施解除'),
        ('plan_created', '计划生成'),
        ('plan_approved', '计划批准'),
        ('plan_executed', '计划执行'),
        ('plan_cancelled', '计划取消'),
    ]

    record = models.ForeignKey(
        RetentionRecord, on_delete=models.CASCADE,
        related_name='events', verbose_name='保管记录'
    )
    event_type = models.CharField('事件类型', max_length=20, choices=TYPE_CHOICES)
    detail = models.JSONField('事件详情', default=dict)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='retention_events', verbose_name='操作人'
    )
    created_at = models.DateTimeField('发生时间', auto_now_add=True)

    class Meta:
        db_table = 'rt_event'
        verbose_name = '保管期限事件'
        verbose_name_plural = verbose_name
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"{self.record.goods.name} - {self.get_event_type_display()}"
