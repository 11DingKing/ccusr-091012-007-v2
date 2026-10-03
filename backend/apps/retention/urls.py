"""
保管期限URL配置
"""
from django.urls import path
from .views import (
    DisposalPlanApproveView, DisposalPlanCancelView, DisposalPlanExecuteView,
    DisposalPlanGenerateView, DisposalPlanListView,
    RetentionHoldReleaseView, RetentionRecordDetailView, RetentionRecordEventListView,
    RetentionRecordHoldView, RetentionRecordListView,
    RetentionRuleDetailView, RetentionRuleListView,
)

urlpatterns = [
    # 保管期限规则（按生效日期版本化）
    path('rules/', RetentionRuleListView.as_view(), name='retention-rule-list'),
    path('rules/<int:pk>/', RetentionRuleDetailView.as_view(), name='retention-rule-detail'),

    # 保管期限记录
    path('records/', RetentionRecordListView.as_view(), name='retention-record-list'),
    path('records/<int:pk>/', RetentionRecordDetailView.as_view(), name='retention-record-detail'),
    path('records/<int:pk>/events/', RetentionRecordEventListView.as_view(), name='retention-record-events'),
    path('records/<int:pk>/holds/', RetentionRecordHoldView.as_view(), name='retention-record-holds'),

    # 保管措施
    path('holds/<int:pk>/release/', RetentionHoldReleaseView.as_view(), name='retention-hold-release'),

    # 处置计划
    path('plans/', DisposalPlanListView.as_view(), name='retention-plan-list'),
    path('plans/generate/', DisposalPlanGenerateView.as_view(), name='retention-plan-generate'),
    path('plans/<int:pk>/approve/', DisposalPlanApproveView.as_view(), name='retention-plan-approve'),
    path('plans/<int:pk>/execute/', DisposalPlanExecuteView.as_view(), name='retention-plan-execute'),
    path('plans/<int:pk>/cancel/', DisposalPlanCancelView.as_view(), name='retention-plan-cancel'),
]
