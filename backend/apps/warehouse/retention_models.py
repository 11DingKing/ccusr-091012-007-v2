"""
保管期限规则领域模型。

设计要点：
- RetentionRule 按「案件类型 × 品类（NULL 表示该案件类型下的通用规则）」配置，
  带生效日期与版本链；旧版本换版后状态为 superseded，但仍然保留，
  用于解析历史日期与审计追溯。
- CustodyItem 在入库时快照当时适用的规则版本，规则换版不影响存量物资的基准期限。
- CustodyHold 表示法律冻结 / 未结调查造成的期限暂停；RetentionExtension 表示
  主管批准的延期（链式记录，保留每次批准时的到期日快照）。
- DisposalPlan 是某一时刻对物资处置资格的计算结果，每次生成均新增一行，
  原始到期日（original_due_date）一旦生成永不改变。
"""
from django.conf import settings
from django.db import models


class RetentionRule(models.Model):
    """保管期限规则（带生效日期的版本化规则）"""

    CASE_CRIMINAL = 'criminal'
    CASE_ADMINISTRATIVE = 'administrative'
    CASE_OTHER = 'other'
    CASE_TYPE_CHOICES = [
        (CASE_CRIMINAL, '刑事案件'),
        (CASE_ADMINISTRATIVE, '行政案件'),
        (CASE_OTHER, '其他案件'),
    ]

    STATUS_ACTIVE = 'active'
    STATUS_SUPERSEDED = 'superseded'
    STATUS_CHOICES = [
        (STATUS_ACTIVE, '现行有效'),
        (STATUS_SUPERSEDED, '已被新版替代'),
    ]

    case_type = models.CharField('案件类型', max_length=20, choices=CASE_TYPE_CHOICES)
    category = models.ForeignKey(
        'Category', on_delete=models.PROTECT, null=True, blank=True,
        related_name='retention_rules', verbose_name='适用品类（空为通用规则）'
    )
    retention_years = models.PositiveIntegerField('保管年限', null=True, blank=True)
    is_permanent = models.BooleanField('是否永久保管', default=False)
    effective_date = models.DateField('生效日期')
    version = models.PositiveIntegerField('版本号', default=1)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    replaces = models.ForeignKey(
        'self', on_delete=models.PROTECT, null=True, blank=True,
        related_name='successors', verbose_name='替代的上一版本'
    )
    superseded_by = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+', verbose_name='替代为本版本的新版'
    )
    remark = models.TextField('规则说明', blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_retention_rules', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_retention_rule'
        verbose_name = '保管期限规则'
        verbose_name_plural = verbose_name
        ordering = ['case_type', 'category_id', '-effective_date', '-version']
        unique_together = ['case_type', 'category', 'effective_date', 'version']

    def __str__(self):
        scope = self.category.name if self.category else '通用'
        term = '永久' if self.is_permanent else f'{self.retention_years}年'
        return f'{self.get_case_type_display()}/{scope} V{self.version}（{term}，{self.effective_date}生效）'

    @property
    def scope_name(self):
        return self.category.name if self.category else '通用规则'


class CustodyItem(models.Model):
    """保管物资（物证 / 受控物资），以入库事实作为期限起算点"""

    code = models.CharField('物资编号', max_length=50, unique=True)
    name = models.CharField('物资名称', max_length=200)
    goods = models.ForeignKey(
        'Goods', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='custody_items', verbose_name='关联库存货物'
    )
    category = models.ForeignKey(
        'Category', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='custody_items', verbose_name='物资品类'
    )
    case_type = models.CharField('案件类型', max_length=20, choices=RetentionRule.CASE_TYPE_CHOICES)
    case_no = models.CharField('案件编号', max_length=100, blank=True)
    intake_date = models.DateField('入库日期')
    rule_version = models.ForeignKey(
        RetentionRule, on_delete=models.PROTECT,
        related_name='custody_items', verbose_name='入库时适用规则版本'
    )
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否在管', default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_custody_items', verbose_name='登记人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_custody_item'
        verbose_name = '保管物资'
        verbose_name_plural = verbose_name
        ordering = ['-intake_date', '-created_at']

    def __str__(self):
        return f'{self.code} {self.name}'


class CustodyHold(models.Model):
    """期限暂停记录：法律冻结 / 未结调查"""

    TYPE_LEGAL_FREEZE = 'legal_freeze'
    TYPE_INVESTIGATION = 'investigation'
    TYPE_CHOICES = [
        (TYPE_LEGAL_FREEZE, '法律冻结'),
        (TYPE_INVESTIGATION, '未结调查'),
    ]

    item = models.ForeignKey(
        CustodyItem, on_delete=models.CASCADE,
        related_name='holds', verbose_name='保管物资'
    )
    hold_type = models.CharField('暂停类型', max_length=20, choices=TYPE_CHOICES)
    reference_no = models.CharField('文书/案号', max_length=100, blank=True)
    authority = models.CharField('决定机关/办案单位', max_length=200)
    reason = models.TextField('暂停事由', blank=True)
    start_date = models.DateField('开始日期')
    end_date = models.DateField('解除日期', null=True, blank=True)
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='approved_holds', verbose_name='批准人'
    )
    lifted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='lifted_holds', verbose_name='解除人'
    )
    lifted_at = models.DateTimeField('解除时间', null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_holds', verbose_name='登记人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_custody_hold'
        verbose_name = '期限暂停记录'
        verbose_name_plural = verbose_name
        ordering = ['-start_date', '-created_at']

    def __str__(self):
        state = '未解除' if self.end_date is None else f'至{self.end_date}'
        return f'{self.item.code} {self.get_hold_type_display()}（{self.authority}，{self.start_date}{state}）'

    @property
    def is_active(self):
        return self.end_date is None

    def is_active_on(self, day):
        """某一日期是否处于暂停状态（含首尾当日）"""
        return self.start_date <= day and (self.end_date is None or self.end_date >= day)


class RetentionExtension(models.Model):
    """主管批准的保管延期（链式记录，多次延期全部留痕）"""

    item = models.ForeignKey(
        CustodyItem, on_delete=models.CASCADE,
        related_name='extensions', verbose_name='保管物资'
    )
    seq = models.PositiveIntegerField('第几次延期')
    base_due_date = models.DateField('批准时认定的到期日')
    new_due_date = models.DateField('延期后到期日')
    extension_days = models.PositiveIntegerField('延期天数')
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='approved_extensions', verbose_name='批准主管'
    )
    reason = models.TextField('延期理由')
    approved_at = models.DateTimeField('批准时间')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_extensions', verbose_name='登记人'
    )

    class Meta:
        db_table = 'wh_retention_extension'
        verbose_name = '保管延期批准'
        verbose_name_plural = verbose_name
        ordering = ['item_id', 'seq']
        unique_together = ['item', 'seq']

    def __str__(self):
        return f'{self.item.code} 第{self.seq}次延期至{self.new_due_date}'


class DisposalPlan(models.Model):
    """处置资格计划：每次（重新）计算生成一行，历史行保留用于追溯"""

    STATE_PERMANENT = 'permanent'
    STATE_NORMAL = 'normal'
    STATE_SUSPENDED = 'suspended'
    STATE_TOLLED = 'tolled'
    STATE_EXTENDED = 'extended'
    STATE_ELIGIBLE = 'eligible'
    STATE_CHOICES = [
        (STATE_PERMANENT, '永久保管'),
        (STATE_NORMAL, '正常保管中'),
        (STATE_SUSPENDED, '冻结/调查暂停中'),
        (STATE_TOLLED, '暂停顺延中'),
        (STATE_EXTENDED, '批准延期中'),
        (STATE_ELIGIBLE, '可进入销毁流程'),
    ]

    item = models.ForeignKey(
        CustodyItem, on_delete=models.CASCADE,
        related_name='plans', verbose_name='保管物资'
    )
    generated_at = models.DateTimeField('生成时间', auto_now_add=True)
    as_of_date = models.DateField('计算基准日')
    rule_version = models.ForeignKey(
        RetentionRule, on_delete=models.PROTECT,
        related_name='plans', verbose_name='依据规则版本'
    )
    original_due_date = models.DateField('原始到期日', null=True, blank=True)
    target_due_date = models.DateField('延期后到期日（暂停计日前）', null=True, blank=True)
    projected_due_date = models.DateField('预计最终到期日', null=True, blank=True)
    suspension_days = models.PositiveIntegerField('暂停顺延天数', default=0)
    extension_days = models.PositiveIntegerField('累计延期天数', default=0)
    indefinite_hold = models.BooleanField('存在未解除冻结/调查', default=False)
    state = models.CharField('处置状态', max_length=20, choices=STATE_CHOICES)
    eligible_for_disposal = models.BooleanField('是否可进入销毁流程', default=False)
    retained_reasons = models.JSONField('仍需保管的原因', default=list)
    timeline = models.JSONField('期限计算时间线', default=list)

    class Meta:
        db_table = 'wh_disposal_plan'
        verbose_name = '处置计划'
        verbose_name_plural = verbose_name
        ordering = ['-generated_at']

    def __str__(self):
        return f'{self.item.code} 计划@{self.as_of_date}（{self.get_state_display()}）'


class DisposalRequest(models.Model):
    """销毁/处置申请：仅当物资具备处置资格时允许发起"""

    STATUS_PENDING = 'pending'
    STATUS_APPROVED = 'approved'
    STATUS_REJECTED = 'rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, '待审批'),
        (STATUS_APPROVED, '已批准'),
        (STATUS_REJECTED, '已拒绝'),
    ]

    item = models.ForeignKey(
        CustodyItem, on_delete=models.CASCADE,
        related_name='disposal_requests', verbose_name='保管物资'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    reason = models.TextField('申请理由', blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='disposal_requests', verbose_name='申请人'
    )
    created_at = models.DateTimeField('申请时间', auto_now_add=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='decided_disposal_requests', verbose_name='审批主管'
    )
    decided_at = models.DateTimeField('审批时间', null=True, blank=True)
    decision_remark = models.TextField('审批意见', blank=True)

    class Meta:
        db_table = 'wh_disposal_request'
        verbose_name = '销毁处置申请'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.item.code} 销毁申请（{self.get_status_display()}）'
