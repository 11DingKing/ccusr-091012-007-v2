"""
保管期限规则与处置资格测试。

覆盖：跨年/闰年到期日、法律冻结与未结调查暂停（含重叠去重）、解除后重算、
主管多次延期（原到期日不可变）、到期日后冻结只阻断不补天、规则换版不影响
存量、规则版本解析、处置计划生成、销毁准入与 API 追溯。
"""
from datetime import date, datetime

from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.core.exceptions import BusinessException
from .retention_engine import (
    add_years,
    assert_disposal_eligible,
    compute_schedule,
    generate_plan,
    grant_extension,
    resolve_rule,
)
from .retention_models import (
    CustodyHold,
    CustodyItem,
    DisposalPlan,
    DisposalRequest,
    RetentionExtension,
    RetentionRule,
)
from .tests import WarehouseFixture


def make_rule(case_type='administrative', category=None, years=2,
              effective=date(2020, 1, 1), version=1, is_permanent=False, **kw):
    return RetentionRule.objects.create(
        case_type=case_type, category=category,
        retention_years=None if is_permanent else years,
        is_permanent=is_permanent,
        effective_date=effective, version=version, **kw
    )


def make_item(rule, intake=date(2023, 3, 15), code='EV-001', case_type='administrative', **kw):
    return CustodyItem.objects.create(
        code=code, name=kw.pop('name', '涉案物资'),
        category=kw.pop('category', None),
        case_type=case_type, intake_date=intake, rule_version=rule, **kw
    )


def hold(item, start, end=None, htype=CustodyHold.TYPE_LEGAL_FREEZE, authority='法院', **kw):
    return CustodyHold.objects.create(
        item=item, hold_type=htype, authority=authority,
        start_date=start, end_date=end, **kw
    )


# ==================== 纯日期口径 ====================

class DateArithmeticTest(WarehouseFixture):
    def test_add_years_cross_year(self):
        self.assertEqual(add_years(date(2023, 3, 15), 2), date(2025, 3, 15))
        self.assertEqual(add_years(date(2023, 12, 31), 1), date(2024, 12, 31))

    def test_leap_day_falls_back_to_28(self):
        self.assertEqual(add_years(date(2024, 2, 29), 1), date(2025, 2, 28))
        self.assertEqual(add_years(date(2024, 2, 29), 4), date(2028, 2, 29))


# ==================== 规则与入库快照 ====================

class RuleVersioningTest(WarehouseFixture):
    def test_intake_snapshots_rule_version(self):
        v1 = make_rule(years=2, created_by=self.user)
        item = make_item(v1, category=self.category)
        schedule = compute_schedule(item, date(2024, 1, 1))
        self.assertEqual(schedule['rule'].id, v1.id)
        # 原始到期日 = 入库日 + N 年 − 1 天
        self.assertEqual(schedule['original_due_date'], date(2025, 3, 14))

    def test_new_rule_version_does_not_change_existing_items(self):
        v1 = make_rule(years=2, created_by=self.user)
        item = make_item(v1, category=self.category)
        make_rule(years=5, effective=date(2024, 1, 1), version=2,
                  replaces=v1, status=RetentionRule.STATUS_ACTIVE, created_by=self.user)
        schedule = compute_schedule(item, date(2024, 6, 1))
        self.assertEqual(schedule['rule'].version, 1)
        self.assertEqual(schedule['original_due_date'], date(2025, 3, 14))

    def test_specific_category_rule_overrides_general(self):
        general = make_rule(years=2, created_by=self.user)
        specific = make_rule(years=3, category=self.category, created_by=self.user)
        self.assertEqual(
            resolve_rule('administrative', self.category.id, date(2025, 1, 1)).id,
            specific.id,
        )
        self.assertEqual(
            resolve_rule('administrative', None, date(2025, 1, 1)).id,
            general.id,
        )

    def test_resolve_rule_ignores_future_versions(self):
        make_rule(years=2, created_by=self.user)
        make_rule(years=5, effective=date(2026, 1, 1), version=2, created_by=self.user)
        rule = resolve_rule('administrative', None, date(2025, 6, 1))
        self.assertEqual(rule.retention_years, 2)

    def test_resolve_rule_missing_raises(self):
        with self.assertRaises(BusinessException):
            resolve_rule('criminal', None, date(2020, 1, 1))

    def test_release_rule_version_marks_old_superseded(self):
        from apps.warehouse.retention_engine import release_rule_version
        v1 = make_rule(years=2, created_by=self.user)
        v2 = release_rule_version(v1, date(2025, 1, 1), self.user, retention_years=5)
        v1.refresh_from_db()
        self.assertEqual(v2.version, 2)
        self.assertEqual(v2.replaces_id, v1.id)
        self.assertEqual(v1.status, RetentionRule.STATUS_SUPERSEDED)
        self.assertEqual(v1.superseded_by_id, v2.id)
        with self.assertRaises(BusinessException):
            release_rule_version(v1, date(2019, 1, 1), self.user, retention_years=1)


# ==================== 暂停（冻结 / 调查） ====================

class SuspensionTest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        self.rule = make_rule(years=2, created_by=self.user)
        self.item = make_item(self.rule, category=self.category)

    def test_active_freeze_keeps_item_and_due_date_unprojectable(self):
        hold(self.item, date(2024, 1, 10), date(2024, 2, 8))
        schedule = compute_schedule(self.item, date(2024, 1, 20))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_SUSPENDED)
        self.assertIsNone(schedule['projected_due_date'])
        self.assertTrue(schedule['indefinite_hold'])
        self.assertFalse(schedule['eligible_for_disposal'])
        # 截至基准日已经过 1/10–1/20 共 11 天
        self.assertEqual(schedule['suspension_days'], 11)
        self.assertTrue(any(r['code'] == 'legal_freeze' for r in schedule['retained_reasons']))

    def test_lifted_freeze_extends_due_date_by_union_days(self):
        hold(self.item, date(2024, 1, 10), date(2024, 2, 8))
        schedule = compute_schedule(self.item, date(2024, 3, 1))
        # 1/10–2/8 共 30 天
        self.assertEqual(schedule['suspension_days'], 30)
        self.assertEqual(schedule['projected_due_date'], date(2025, 4, 13))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_NORMAL)

    def test_overlapping_holds_not_double_counted(self):
        hold(self.item, date(2024, 1, 1), date(2024, 1, 31))
        hold(self.item, date(2024, 1, 15), date(2024, 2, 14),
             htype=CustodyHold.TYPE_INVESTIGATION, authority='办案队')
        schedule = compute_schedule(self.item, date(2024, 3, 1))
        # 并集 1/1–2/14 共 45 天，不是 31+31
        self.assertEqual(schedule['suspension_days'], 45)
        self.assertEqual(schedule['projected_due_date'], date(2025, 4, 28))

    def test_freeze_starting_after_due_date_blocks_disposal_but_adds_no_days(self):
        hold(self.item, date(2025, 5, 1))  # 晚于到期日 2025-03-14
        schedule = compute_schedule(self.item, date(2025, 5, 5))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_SUSPENDED)
        self.assertFalse(schedule['eligible_for_disposal'])
        self.assertEqual(schedule['suspension_days'], 0)
        self.assertEqual(schedule['original_due_date'], date(2025, 3, 14))

    def test_historical_recompute_ignores_future_hold(self):
        # 以冻结开始前的日期复算：未来的冻结当时尚未发生，期限已届满
        hold(self.item, date(2025, 5, 1))
        schedule = compute_schedule(self.item, date(2025, 3, 20))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_ELIGIBLE)
        self.assertTrue(schedule['eligible_for_disposal'])

    def test_investigation_then_freeze_chained(self):
        hold(self.item, date(2024, 6, 1), date(2024, 6, 10),
             htype=CustodyHold.TYPE_INVESTIGATION, authority='办案队')
        hold(self.item, date(2024, 7, 1), date(2024, 7, 5))
        schedule = compute_schedule(self.item, date(2024, 8, 1))
        self.assertEqual(schedule['suspension_days'], 15)
        self.assertEqual(schedule['projected_due_date'], date(2025, 3, 29))

    def test_hold_starting_before_intake_is_clipped_to_intake(self):
        # 冻结自入库前 3/10 开始、3/20 解除：仅入库日 3/15 起的 6 天计入顺延
        hold(self.item, date(2023, 3, 10), date(2023, 3, 20))
        schedule = compute_schedule(self.item, date(2023, 4, 1))
        self.assertEqual(schedule['suspension_days'], 6)
        self.assertEqual(schedule['projected_due_date'], date(2025, 3, 20))


# ==================== 主管延期 ====================

class ExtensionTest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        self.rule = make_rule(years=2, created_by=self.user)
        self.item = make_item(self.rule, category=self.category)
        hold(self.item, date(2024, 1, 10), date(2024, 2, 8))

    def _approve(self, day, days):
        return grant_extension(
            self.item, days=days, reason='业务需要', approver=self.user,
            approved_at=timezone.make_aware(datetime.combine(day, datetime.min.time())),
            created_by=self.user,
        )

    def test_multiple_extensions_chain_and_keep_original_due(self):
        self._approve(date(2024, 3, 1), 30)
        self._approve(date(2024, 4, 1), 10)
        schedule = compute_schedule(self.item, date(2024, 5, 1))
        self.assertEqual(schedule['original_due_date'], date(2025, 3, 14))
        self.assertEqual(schedule['extension_days'], 40)
        # 30 天暂停已嵌入延期快照；最终到期日 = 原到期 + 30 暂停 + 40 延期
        self.assertEqual(schedule['target_due_date'], date(2025, 5, 23))
        self.assertEqual(schedule['projected_due_date'], date(2025, 5, 23))
        self.assertEqual(schedule['suspension_days'], 30)
        seqs = list(self.item.extensions.values_list('seq', flat=True))
        self.assertEqual(seqs, [1, 2])

    def test_hold_after_last_extension_adds_incrementally(self):
        self._approve(date(2024, 3, 1), 30)
        self._approve(date(2024, 4, 1), 10)
        hold(self.item, date(2025, 1, 1), date(2025, 1, 10),
             htype=CustodyHold.TYPE_INVESTIGATION, authority='办案队')
        schedule = compute_schedule(self.item, date(2025, 2, 1))
        self.assertEqual(schedule['projected_due_date'], date(2025, 6, 2))
        self.assertEqual(schedule['suspension_days'], 40)

    def test_extension_snapshots_survive_later_lift_edits(self):
        ext = self._approve(date(2024, 3, 1), 30)
        self.assertEqual(ext.base_due_date, date(2025, 4, 13))
        self.assertEqual(ext.new_due_date, date(2025, 5, 13))
        # 原始到期日在延期记录中完全不出现覆盖
        self.assertEqual(
            RetentionExtension.objects.get(pk=ext.pk).base_due_date,
            date(2025, 4, 13),
        )

    def test_extension_rejected_during_active_freeze(self):
        hold(self.item, date(2025, 1, 1))
        with self.assertRaises(BusinessException):
            self._approve(date(2025, 1, 5), 30)

    def test_extension_requires_positive_days(self):
        with self.assertRaises(BusinessException):
            grant_extension(self.item, 0, 'x', self.user, timezone.now())

    def test_extended_state_after_original_due(self):
        self._approve(date(2024, 3, 1), 30)
        schedule = compute_schedule(self.item, date(2025, 3, 20))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_EXTENDED)
        self.assertFalse(schedule['eligible_for_disposal'])


# ==================== 处置资格与计划 ====================

class DisposalEligibilityTest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        self.rule = make_rule(years=2, created_by=self.user)
        self.item = make_item(self.rule, category=self.category)

    def test_eligible_day_after_final_due(self):
        before = compute_schedule(self.item, date(2025, 3, 14))
        after = compute_schedule(self.item, date(2025, 3, 15))
        self.assertFalse(before['eligible_for_disposal'])
        self.assertTrue(after['eligible_for_disposal'])
        self.assertEqual(after['state'], DisposalPlan.STATE_ELIGIBLE)

    def test_assert_disposal_eligible_raises_when_retained(self):
        with self.assertRaises(BusinessException):
            assert_disposal_eligible(self.item, date(2025, 1, 1))
        schedule = assert_disposal_eligible(self.item, date(2025, 3, 15))
        self.assertTrue(schedule['eligible_for_disposal'])

    def test_generate_plan_persists_history_and_immutable_original_due(self):
        p1 = generate_plan(self.item, date(2025, 1, 1))
        hold(self.item, date(2025, 1, 10))
        p2 = generate_plan(self.item, date(2025, 1, 20))
        self.assertEqual(self.item.plans.count(), 2)
        self.assertEqual(p1.state, DisposalPlan.STATE_NORMAL)
        self.assertEqual(p2.state, DisposalPlan.STATE_SUSPENDED)
        # 两次计划的原始到期日一致、不可变
        self.assertEqual(p1.original_due_date, p2.original_due_date)
        self.assertEqual(p1.original_due_date, date(2025, 3, 14))
        self.assertIn('rule_applied', [e['event'] for e in p2.timeline])

    def test_permanent_rule_never_eligible(self):
        rule = make_rule(case_type='criminal', is_permanent=True,
                         created_by=self.user)
        item = make_item(rule, code='CR-001', case_type='criminal',
                         intake=date(2000, 1, 1), category=self.category)
        schedule = compute_schedule(item, date(2026, 10, 3))
        self.assertEqual(schedule['state'], DisposalPlan.STATE_PERMANENT)
        self.assertIsNone(schedule['original_due_date'])
        self.assertFalse(schedule['eligible_for_disposal'])


# ==================== API 层 ====================

class RetentionAPITest(WarehouseFixture):
    def setUp(self):
        super().setUp()
        self.rule = make_rule(years=2, created_by=self.user)
        self.item = make_item(self.rule, category=self.category)

    def test_create_rule_requires_admin(self):
        normal = User.objects.create_user('clerk', 'p', role='user')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(normal)}")
        resp = client.post('/api/retention/rules/', {
            'case_type': 'criminal', 'retention_years': 5,
            'effective_date': '2025-01-01',
        }, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_create_rule_and_resolve(self):
        resp = self.client.post('/api/retention/rules/', {
            'case_type': 'criminal', 'retention_years': 5,
            'effective_date': '2025-01-01',
        }, format='json')
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertEqual(resp.json()['data']['version'], 1)
        resolved = self.client.get('/api/retention/rules/resolve/', {
            'case_type': 'administrative', 'on_date': '2025-06-01',
        })
        self.assertEqual(resolved.json()['data']['rule']['retention_years'], 2)

    def test_rule_version_chain_returned(self):
        v2 = make_rule(years=5, effective=date(2025, 1, 1), version=2,
                       replaces=self.rule, status=RetentionRule.STATUS_ACTIVE,
                       created_by=self.user)
        resp = self.client.get(f'/api/retention/rules/{v2.id}/')
        chain = resp.json()['data']['version_chain']
        self.assertEqual([n['version'] for n in chain], [2, 1])

    def test_register_item_without_rule_returns_422(self):
        resp = self.client.post('/api/custody-items/', {
            'code': 'X-1', 'name': '无规则物资', 'case_type': 'other',
            'intake_date': '2025-01-01',
        }, format='json')
        self.assertEqual(resp.status_code, 422)

    def test_schedule_explains_retention(self):
        resp = self.client.get(f'/api/custody-items/{self.item.id}/schedule/',
                               {'as_of': '2025-01-01'})
        data = resp.json()['data']
        self.assertEqual(data['original_due_date'], '2025-03-14')
        self.assertFalse(data['eligible_for_disposal'])
        self.assertTrue(data['retained_reasons'])
        events = [e['event'] for e in data['timeline']]
        self.assertIn('intake', events)
        self.assertIn('original_due', events)

    def test_freeze_lift_and_reschedule_flow(self):
        created = self.client.post(f'/api/custody-items/{self.item.id}/holds/', {
            'hold_type': 'legal_freeze', 'authority': '某法院',
            'start_date': '2024-01-10',
        }, format='json')
        self.assertEqual(created.status_code, 200, created.json())
        hold_id = created.json()['data']['id']
        held = self.client.get(f'/api/custody-items/{self.item.id}/schedule/',
                               {'as_of': '2024-01-20'})
        self.assertEqual(held.json()['data']['state'], 'suspended')

        lifted = self.client.post(f'/api/custody-holds/{hold_id}/lift/',
                                  {'end_date': '2024-02-08'}, format='json')
        self.assertEqual(lifted.status_code, 200, lifted.json())
        self.assertEqual(lifted.json()['data']['projected_due_date'], '2025-04-13')
        # 重复解除被拒绝
        again = self.client.post(f'/api/custody-holds/{hold_id}/lift/',
                                 {'end_date': '2024-02-09'}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_extension_flow_requires_admin_role_approver(self):
        bad_approver = User.objects.create_user('nobody', 'p', role='user')
        resp = self.client.post(f'/api/custody-items/{self.item.id}/extensions/', {
            'days': 30, 'reason': '核查需要', 'approver': bad_approver.id,
        }, format='json')
        self.assertEqual(resp.status_code, 400)
        ok = self.client.post(f'/api/custody-items/{self.item.id}/extensions/', {
            'days': 30, 'reason': '核查需要', 'approver': self.user.id,
            'approved_at': '2025-01-01T09:00:00',
        }, format='json')
        self.assertEqual(ok.status_code, 200, ok.json())
        self.assertEqual(ok.json()['data']['original_due_date'], '2025-03-14')
        self.assertEqual(ok.json()['data']['projected_due_date'], '2025-04-13')

    def test_disposal_request_blocked_then_allowed(self):
        blocked = self.client.post(
            f'/api/custody-items/{self.item.id}/disposal-requests/',
            {'as_of': '2025-01-01'}, format='json')
        self.assertEqual(blocked.status_code, 409)

        allowed = self.client.post(
            f'/api/custody-items/{self.item.id}/disposal-requests/',
            {'as_of': '2025-03-15', 'reason': '期限届满'}, format='json')
        self.assertEqual(allowed.status_code, 200, allowed.json())
        req_id = allowed.json()['data']['request']['id']

        decided = self.client.post(f'/api/disposal-requests/{req_id}/decision/', {
            'status': 'approved', 'decision_remark': '同意销毁',
        }, format='json')
        self.assertEqual(decided.status_code, 200)
        self.item.refresh_from_db()
        self.assertFalse(self.item.is_active)

    def test_disposal_approval_rechecks_active_freeze(self):
        req = DisposalRequest.objects.create(
            item=self.item, reason='届满', requested_by=self.user)
        # 审批当日出现新冻结，审批必须拦截
        hold(self.item, date(2025, 3, 15))
        resp = self.client.post(f'/api/disposal-requests/{req.id}/decision/', {
            'status': 'approved',
        }, format='json')
        self.assertEqual(resp.status_code, 409)
        req.refresh_from_db()
        self.assertEqual(req.status, DisposalRequest.STATUS_PENDING)

    def test_generate_plan_endpoint(self):
        resp = self.client.post('/api/disposal-plans/', {'as_of': '2025-01-01'},
                                format='json')
        self.assertEqual(resp.status_code, 200, resp.json())
        self.assertGreaterEqual(resp.json()['data']['generated_count'], 1)

    def test_timeline_endpoint_is_complete(self):
        hold(self.item, date(2024, 1, 1), date(2024, 1, 5))
        grant_extension(
            self.item, 10, 'r', self.user,
            timezone.make_aware(datetime(2025, 1, 1, 9, 0)), created_by=self.user,
        )
        generate_plan(self.item, date(2025, 1, 1))
        resp = self.client.get(f'/api/custody-items/{self.item.id}/timeline/')
        data = resp.json()['data']
        self.assertEqual(len(data['holds']), 1)
        self.assertEqual(len(data['extensions']), 1)
        self.assertEqual(len(data['plans']), 1)
