"""
仓库管理URL配置
"""
from django.urls import path
from .views import (
    UnitListView, UnitDetailView, UnitBatchDeleteView, UnitAllView,
    CategoryListView, CategoryDetailView, CategoryBatchDeleteView, CategoryAllView,
    VarietyListView, VarietyDetailView, VarietyBatchDeleteView,
    VarietyTemplateView, VarietyImportView,
    DashboardView, GoodsListView, StockInListView, StockOutListView,
    WarningListView, ApprovalListView
)
from .retention_views import (
    RetentionRuleListCreateView, RetentionRuleDetailView, RetentionRuleResolveView,
    CustodyItemListCreateView, CustodyItemDetailView,
    CustodyScheduleView, CustodyTimelineView,
    CustodyHoldCreateView, CustodyHoldLiftView,
    ExtensionCreateView,
    DisposalPlanListGenerateView, DisposalPlanDetailView, CustodyGeneratePlanView,
    DisposalRequestListCreateView, DisposalRequestDecisionView,
)

urlpatterns = [
    # 仪表盘
    path('dashboard/', DashboardView.as_view(), name='dashboard'),
    
    # 单位管理
    path('units/', UnitListView.as_view(), name='unit-list'),
    path('units/all/', UnitAllView.as_view(), name='unit-all'),
    path('units/batch-delete/', UnitBatchDeleteView.as_view(), name='unit-batch-delete'),
    path('units/<int:pk>/', UnitDetailView.as_view(), name='unit-detail'),
    
    # 品类管理
    path('categories/', CategoryListView.as_view(), name='category-list'),
    path('categories/all/', CategoryAllView.as_view(), name='category-all'),
    path('categories/batch-delete/', CategoryBatchDeleteView.as_view(), name='category-batch-delete'),
    path('categories/<int:pk>/', CategoryDetailView.as_view(), name='category-detail'),
    
    # 品种管理
    path('varieties/', VarietyListView.as_view(), name='variety-list'),
    path('varieties/batch-delete/', VarietyBatchDeleteView.as_view(), name='variety-batch-delete'),
    path('varieties/template/', VarietyTemplateView.as_view(), name='variety-template'),
    path('varieties/import/', VarietyImportView.as_view(), name='variety-import'),
    path('varieties/<int:pk>/', VarietyDetailView.as_view(), name='variety-detail'),
    
    # 货物管理
    path('goods/', GoodsListView.as_view(), name='goods-list'),
    
    # 入库管理
    path('stock-in/', StockInListView.as_view(), name='stock-in-list'),
    
    # 出库管理
    path('stock-out/', StockOutListView.as_view(), name='stock-out-list'),
    
    # 预警管理
    path('warnings/', WarningListView.as_view(), name='warning-list'),
    
    # 审批管理
    path('approvals/', ApprovalListView.as_view(), name='approval-list'),

    # 保管期限规则（带生效日期、支持换版）
    path('retention/rules/', RetentionRuleListCreateView.as_view(), name='retention-rule-list'),
    path('retention/rules/resolve/', RetentionRuleResolveView.as_view(), name='retention-rule-resolve'),
    path('retention/rules/<int:pk>/', RetentionRuleDetailView.as_view(), name='retention-rule-detail'),

    # 保管物资
    path('custody-items/', CustodyItemListCreateView.as_view(), name='custody-item-list'),
    path('custody-items/<int:pk>/', CustodyItemDetailView.as_view(), name='custody-item-detail'),
    path('custody-items/<int:pk>/schedule/', CustodyScheduleView.as_view(), name='custody-item-schedule'),
    path('custody-items/<int:pk>/timeline/', CustodyTimelineView.as_view(), name='custody-item-timeline'),
    path('custody-items/<int:pk>/holds/', CustodyHoldCreateView.as_view(), name='custody-hold-create'),
    path('custody-items/<int:pk>/extensions/', ExtensionCreateView.as_view(), name='custody-extension-create'),
    path('custody-items/<int:pk>/generate-plan/', CustodyGeneratePlanView.as_view(), name='custody-generate-plan'),
    path('custody-items/<int:pk>/disposal-requests/', DisposalRequestListCreateView.as_view(),
         name='custody-disposal-requests'),

    # 暂停解除
    path('custody-holds/<int:pk>/lift/', CustodyHoldLiftView.as_view(), name='custody-hold-lift'),

    # 处置计划
    path('disposal-plans/', DisposalPlanListGenerateView.as_view(), name='disposal-plan-list'),
    path('disposal-plans/<int:pk>/', DisposalPlanDetailView.as_view(), name='disposal-plan-detail'),

    # 销毁申请审批
    path('disposal-requests/', DisposalRequestListCreateView.as_view(), name='disposal-request-list'),
    path('disposal-requests/<int:pk>/decision/', DisposalRequestDecisionView.as_view(),
         name='disposal-request-decision'),
]
