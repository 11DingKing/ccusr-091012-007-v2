from datetime import date, datetime, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, StockIn, Unit, Variety
from . import services
from .models import DisposalPlan, RetentionEvent, RetentionHold, RetentionRecord, RetentionRule


def aware(y, m, d, hour=10):
    return timezone.make_aware(datetime(y, m, d, hour, 0, 0))


class RetentionFixture(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("retention-admin", "testpass123", role="admin")
        self.staff = User.objects.create_user("retention-staff", "testpass123", role="user")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.admin)}")
        self.staff_client = APIClient()
        self.staff_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.staff)}")

        self.unit = Unit.objects.create(name="件", created_by=self.admin)
        self.category = Category.objects.create(name="受控器材", unit=self.unit, created_by=self.admin)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.admin)
        self.goods = Goods.objects.create(
            variety=self.variety, name="执法记录终端", code="DEV-001", quantity=Decimal("12")
        )
        self.rule = RetentionRule.objects.create(
            case_type="criminal", category=self.category,
            retention_months=6, effective_from=date(2020, 1, 1),
            created_by=self.admin,
        )

    def make_goods(self, code="DEV-002", name="备用终端"):
        return Goods.objects.create(
            variety=self.variety, name=name, code=code, quantity=Decimal("3")
        )

    def stock_in(self, goods=None, when=None, quantity=Decimal("1")):
        goods = goods or self.goods
        record = StockIn.objects.create(goods=goods, operator=self.admin, quantity=quantity)
        if when:
            StockIn.objects.filter(pk=record.pk).update(stock_in_time=when)
            record.refresh_from_db()
        return record

    def create_record(self, goods=None, case_type="criminal"):
        goods = goods or self.goods
        return self.client.post(
            "/api/retention/records/", {"goods": goods.id, "case_type": case_type}, format="json"
        )


class AddMonthsTest(TestCase):
    def test_cross_year(self):
        self.assertEqual(services.add_months(date(2025, 12, 15), 6), date(2026, 6, 15))
        self.assertEqual(services.add_months(date(2025, 12, 31), 1), date(2026, 1, 31))
        self.assertEqual(services.add_months(date(2025, 11, 30), 13), date(2026, 12, 30))

    def test_month_end_clamp(self):
        self.assertEqual(services.add_months(date(2025, 1, 31), 1), date(2025, 2, 28))
        self.assertEqual(services.add_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(services.add_months(date(2025, 3, 31), 1), date(2025, 4, 30))


class RetentionRuleAPITest(RetentionFixture):
    def test_create_rule(self):
        response = self.client.post("/api/retention/rules/", {
            "case_type": "civil", "category": self.category.id,
            "retention_months": 12, "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["retention_months"], 12)

    def test_create_rule_requires_admin(self):
        response = self.staff_client.post("/api/retention/rules/", {
            "case_type": "civil", "category": self.category.id,
            "retention_months": 12, "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_reject_duplicate_version_and_invalid_months(self):
        duplicate = self.client.post("/api/retention/rules/", {
            "case_type": "criminal", "category": self.category.id,
            "retention_months": 9, "effective_from": "2020-01-01",
        }, format="json")
        zero_months = self.client.post("/api/retention/rules/", {
            "case_type": "civil", "category": self.category.id,
            "retention_months": 0, "effective_from": "2026-01-01",
        }, format="json")
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(zero_months.status_code, 400)

    def test_list_rules_filtered_by_case_type(self):
        self.client.post("/api/retention/rules/", {
            "case_type": "civil", "category": self.category.id,
            "retention_months": 12, "effective_from": "2026-01-01",
        }, format="json")
        response = self.client.get("/api/retention/rules/?case_type=civil")
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["list"][0]["case_type"], "civil")

    def test_referenced_rule_is_immutable_and_undeletable(self):
        self.stock_in(when=aware(2026, 1, 10))
        self.create_record()

        updated = self.client.put(f"/api/retention/rules/{self.rule.id}/", {
            "case_type": "criminal", "category": self.category.id,
            "retention_months": 24, "effective_from": "2020-01-01",
        }, format="json")
        deleted = self.client.delete(f"/api/retention/rules/{self.rule.id}/")
        self.assertEqual(updated.status_code, 400)
        self.assertEqual(deleted.status_code, 400)

        # 非关键字段（启用状态、备注）仍可修改
        toggled = self.client.put(f"/api/retention/rules/{self.rule.id}/", {
            "case_type": "criminal", "category": self.category.id,
            "retention_months": 6, "effective_from": "2020-01-01",
            "is_active": False, "remark": "已被新版本替代",
        }, format="json")
        self.assertEqual(toggled.status_code, 200)
        self.assertFalse(toggled.json()["data"]["is_active"])

    def test_delete_unreferenced_rule(self):
        response = self.client.delete(f"/api/retention/rules/{self.rule.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(RetentionRule.objects.filter(pk=self.rule.id).exists())


class RetentionRecordAPITest(RetentionFixture):
    def test_create_record_uses_earliest_stock_in(self):
        self.stock_in(when=aware(2026, 2, 20))
        self.stock_in(when=aware(2026, 1, 10))  # 更早的入库事实
        response = self.create_record()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["anchor_date"], "2026-01-10")
        self.assertEqual(data["original_due_date"], "2026-07-10")
        self.assertEqual(data["current_due_date"], "2026-07-10")
        self.assertEqual(data["status"], "in_custody")

    def test_create_record_cross_year_due_date(self):
        self.stock_in(when=aware(2025, 12, 15))
        response = self.create_record()
        self.assertEqual(response.json()["data"]["original_due_date"], "2026-06-15")

    def test_create_record_requires_stock_in(self):
        response = self.create_record()
        self.assertEqual(response.status_code, 400)
        self.assertIn("入库", response.json()["message"])

    def test_create_record_requires_matching_rule(self):
        self.stock_in(when=aware(2026, 1, 10))
        response = self.create_record(case_type="civil")
        self.assertEqual(response.status_code, 400)
        self.assertIn("规则", response.json()["message"])

    def test_create_record_rejects_rule_not_yet_effective(self):
        future_rule = RetentionRule.objects.create(
            case_type="civil", category=self.category,
            retention_months=12, effective_from=date(2026, 6, 1),
            created_by=self.admin,
        )
        self.stock_in(when=aware(2026, 1, 10))  # 入库时该规则尚未生效
        response = self.create_record(case_type="civil")
        self.assertEqual(response.status_code, 400)

    def test_create_record_rejects_duplicate(self):
        self.stock_in(when=aware(2026, 1, 10))
        first = self.create_record()
        second = self.create_record()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)

    def test_rule_version_selected_by_anchor_date(self):
        new_version = RetentionRule.objects.create(
            case_type="criminal", category=self.category,
            retention_months=24, effective_from=date(2026, 1, 1),
            created_by=self.admin,
        )
        # 旧版生效期间入库 → 6个月
        self.stock_in(when=aware(2025, 12, 15))
        old_resp = self.create_record()
        # 新版生效后入库 → 24个月
        goods2 = self.make_goods()
        self.stock_in(goods=goods2, when=aware(2026, 2, 1))
        new_resp = self.create_record(goods=goods2)

        old_data = old_resp.json()["data"]
        new_data = new_resp.json()["data"]
        self.assertEqual(old_data["rule"], self.rule.id)
        self.assertEqual(old_data["original_due_date"], "2026-06-15")
        self.assertEqual(new_data["rule"], new_version.id)
        self.assertEqual(new_data["original_due_date"], "2028-02-01")

    def test_new_rule_version_does_not_rewrite_existing_record(self):
        self.stock_in(when=aware(2026, 1, 10))
        self.create_record()
        record = RetentionRecord.objects.get(goods=self.goods)

        # 规则换版：新建更长期限的版本
        RetentionRule.objects.create(
            case_type="criminal", category=self.category,
            retention_months=36, effective_from=date(2026, 2, 1),
            created_by=self.admin,
        )
        record.refresh_from_db()
        self.assertEqual(record.rule_id, self.rule.id)
        self.assertEqual(record.original_due_date, date(2026, 7, 10))

    def test_list_records_and_eligible_filter(self):
        self.stock_in(when=aware(2020, 6, 1))  # 早已到期
        self.create_record()
        goods2 = self.make_goods()
        self.stock_in(goods=goods2)  # 今天入库，未到期
        self.create_record(goods=goods2)

        all_resp = self.client.get("/api/retention/records/")
        self.assertEqual(all_resp.json()["data"]["total"], 2)

        eligible_resp = self.client.get("/api/retention/records/?eligible=true")
        eligible_data = eligible_resp.json()["data"]
        self.assertEqual(eligible_data["total"], 1)
        self.assertEqual(eligible_data["list"][0]["goods"], self.goods.id)
        self.assertTrue(eligible_data["list"][0]["is_eligible"])

        not_eligible_resp = self.client.get("/api/retention/records/?eligible=false")
        self.assertEqual(not_eligible_resp.json()["data"]["total"], 1)

    def test_detail_explains_why_retained(self):
        # 昨天入库 → 到期日必然在未来
        yesterday = timezone.localdate() - timedelta(days=1)
        self.stock_in(when=timezone.make_aware(datetime.combine(yesterday, datetime.min.time())))
        record_id = self.create_record().json()["data"]["id"]
        response = self.client.get(f"/api/retention/records/{record_id}/")
        explanation = response.json()["data"]["explanation"]
        self.assertEqual(explanation["state"], "in_custody")
        self.assertIn("尚未到期", explanation["reason"])
        self.assertEqual(
            explanation["original_due_date"],
            services.add_months(yesterday, 6).isoformat(),
        )

    def test_requires_authentication(self):
        anonymous = APIClient().get("/api/retention/records/")
        self.assertEqual(anonymous.status_code, 401)


class RetentionHoldTest(RetentionFixture):
    def setUp(self):
        super().setUp()
        self.stock_in(when=aware(2026, 1, 10))
        self.record_id = self.create_record().json()["data"]["id"]
        self.record = RetentionRecord.objects.get(pk=self.record_id)

    def test_freeze_suspends_and_release_extends(self):
        today = timezone.localdate()
        start = today - timedelta(days=30)
        end = today - timedelta(days=10)

        placed = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
            "start_date": start.isoformat(),
        }, format="json")
        self.assertEqual(placed.status_code, 200)
        hold_id = placed.json()["data"]["id"]

        # 冻结进行中：暂停计时，当前到期日随冻结天数顺延
        detail = self.client.get(f"/api/retention/records/{self.record_id}/").json()["data"]
        self.assertTrue(detail["is_suspended"])
        self.assertFalse(detail["is_eligible"])
        self.assertEqual(detail["explanation"]["state"], "suspended")
        self.assertIn("法律冻结", detail["explanation"]["reason"])

        released = self.client.post(f"/api/retention/holds/{hold_id}/release/", {
            "end_date": end.isoformat(), "release_reason": "冻结解除",
        }, format="json")
        self.assertEqual(released.status_code, 200)

        self.record.refresh_from_db()
        # 暂停20天 → 当前到期日顺延20天，原到期日不变
        self.assertEqual(self.record.current_due_date, date(2026, 7, 10) + timedelta(days=20))
        self.assertEqual(self.record.original_due_date, date(2026, 7, 10))

    def test_investigation_hold_pauses_countdown(self):
        today = timezone.localdate()
        placed = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "investigation", "reason": "关联案件未结",
            "start_date": (today - timedelta(days=5)).isoformat(),
        }, format="json")
        self.assertEqual(placed.status_code, 200)

        detail = self.client.get(f"/api/retention/records/{self.record_id}/").json()["data"]
        self.assertEqual(detail["explanation"]["state"], "suspended")
        self.assertEqual(detail["explanation"]["paused_days"], 5)
        self.assertEqual(
            detail["current_due_date"],
            (date(2026, 7, 10) + timedelta(days=5)).isoformat(),
        )

    def test_reject_duplicate_active_pause_hold(self):
        self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
        }, format="json")
        duplicate = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "第二份冻结令",
        }, format="json")
        self.assertEqual(duplicate.status_code, 400)

    def test_extension_requires_admin(self):
        response = self.staff_client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "extension", "reason": "案情复杂",
            "extended_due_date": "2027-01-10",
        }, format="json")
        self.assertEqual(response.status_code, 403)

    def test_staff_can_register_freeze_but_not_release(self):
        placed = self.staff_client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
        }, format="json")
        self.assertEqual(placed.status_code, 200)
        hold_id = placed.json()["data"]["id"]

        released = self.staff_client.post(f"/api/retention/holds/{hold_id}/release/", {}, format="json")
        self.assertEqual(released.status_code, 403)

    def test_multiple_extensions_stack_and_original_due_kept(self):
        first = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "extension", "reason": "第一次延期",
            "extended_due_date": "2027-01-10",
        }, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["data"]["approved_by"], self.admin.id)

        # 第二次延期必须晚于当前到期日
        earlier = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "extension", "reason": "试图提前",
            "extended_due_date": "2026-12-01",
        }, format="json")
        self.assertEqual(earlier.status_code, 400)

        second = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "extension", "reason": "第二次延期",
            "extended_due_date": "2027-06-10",
        }, format="json")
        self.assertEqual(second.status_code, 200)

        self.record.refresh_from_db()
        self.assertEqual(self.record.current_due_date, date(2027, 6, 10))
        self.assertEqual(self.record.original_due_date, date(2026, 7, 10))
        self.assertEqual(
            RetentionHold.objects.filter(record=self.record, hold_type="extension").count(), 2
        )

    def test_extension_cannot_be_released(self):
        placed = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "extension", "reason": "延期",
            "extended_due_date": "2027-01-10",
        }, format="json")
        hold_id = placed.json()["data"]["id"]
        released = self.client.post(f"/api/retention/holds/{hold_id}/release/", {}, format="json")
        self.assertEqual(released.status_code, 400)

    def test_release_validates_dates(self):
        placed = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
        }, format="json")
        hold_id = placed.json()["data"]["id"]

        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        before_start = self.client.post(f"/api/retention/holds/{hold_id}/release/", {
            "end_date": yesterday,
        }, format="json")
        self.assertEqual(before_start.status_code, 400)

        ok = self.client.post(f"/api/retention/holds/{hold_id}/release/", {}, format="json")
        self.assertEqual(ok.status_code, 200)
        again = self.client.post(f"/api/retention/holds/{hold_id}/release/", {}, format="json")
        self.assertEqual(again.status_code, 400)

    def test_hold_rejected_after_disposed(self):
        self.record.status = "disposed"
        self.record.save(update_fields=["status"])
        response = self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
        }, format="json")
        self.assertEqual(response.status_code, 400)


class DisposalPlanTest(RetentionFixture):
    def setUp(self):
        super().setUp()
        self.stock_in(when=aware(2020, 6, 1))  # 早已到期
        self.record_id = self.create_record().json()["data"]["id"]
        self.record = RetentionRecord.objects.get(pk=self.record_id)

    def test_create_plan_when_eligible(self):
        response = self.client.post("/api/retention/plans/", {
            "record": self.record_id, "method": "destruction",
        }, format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["planned_date"], timezone.localdate().isoformat())

    def test_create_plan_rejected_when_not_eligible(self):
        goods2 = self.make_goods()
        self.stock_in(goods=goods2)  # 今天入库，未到期
        record_id = self.create_record(goods=goods2).json()["data"]["id"]
        response = self.client.post("/api/retention/plans/", {"record": record_id}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("处置资格", response.json()["message"])

    def test_create_plan_rejected_when_suspended(self):
        self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
        }, format="json")
        response = self.client.post("/api/retention/plans/", {"record": self.record_id}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_reject_duplicate_open_plan(self):
        self.client.post("/api/retention/plans/", {"record": self.record_id}, format="json")
        duplicate = self.client.post("/api/retention/plans/", {"record": self.record_id}, format="json")
        self.assertEqual(duplicate.status_code, 400)

    def test_full_plan_flow(self):
        plan_id = self.client.post("/api/retention/plans/", {
            "record": self.record_id,
        }, format="json").json()["data"]["id"]

        # 未批准不能执行
        early_execute = self.client.post(f"/api/retention/plans/{plan_id}/execute/")
        self.assertEqual(early_execute.status_code, 400)

        # 普通用户不能批准
        staff_approve = self.staff_client.post(f"/api/retention/plans/{plan_id}/approve/")
        self.assertEqual(staff_approve.status_code, 403)

        approved = self.client.post(f"/api/retention/plans/{plan_id}/approve/")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["data"]["status"], "approved")

        executed = self.client.post(f"/api/retention/plans/{plan_id}/execute/")
        self.assertEqual(executed.status_code, 200)

        self.record.refresh_from_db()
        self.assertEqual(self.record.status, "disposed")
        self.assertIsNotNone(self.record.disposed_at)

        detail = self.client.get(f"/api/retention/records/{self.record_id}/").json()["data"]
        self.assertEqual(detail["explanation"]["state"], "disposed")

    def test_execute_blocked_by_active_hold(self):
        plan_id = self.client.post("/api/retention/plans/", {
            "record": self.record_id,
        }, format="json").json()["data"]["id"]
        self.client.post(f"/api/retention/plans/{plan_id}/approve/")
        self.client.post(f"/api/retention/records/{self.record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "执行前收到冻结令",
        }, format="json")
        executed = self.client.post(f"/api/retention/plans/{plan_id}/execute/")
        self.assertEqual(executed.status_code, 400)

    def test_cancel_plan(self):
        plan_id = self.client.post("/api/retention/plans/", {
            "record": self.record_id,
        }, format="json").json()["data"]["id"]
        cancelled = self.client.post(f"/api/retention/plans/{plan_id}/cancel/", {
            "cancel_reason": "计划调整",
        }, format="json")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["data"]["status"], "cancelled")

        # 取消后可重新生成计划
        again = self.client.post("/api/retention/plans/", {"record": self.record_id}, format="json")
        self.assertEqual(again.status_code, 200)

    def test_generate_plans_for_eligible_records(self):
        goods2 = self.make_goods()
        self.stock_in(goods=goods2)  # 未到期
        self.create_record(goods=goods2)

        response = self.client.post("/api/retention/plans/generate/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["created_count"], 1)

        # 再次生成：已有未完成的计划，不再重复
        second = self.client.post("/api/retention/plans/generate/")
        self.assertEqual(second.json()["data"]["created_count"], 0)

    def test_generate_plans_requires_admin(self):
        response = self.staff_client.post("/api/retention/plans/generate/")
        self.assertEqual(response.status_code, 403)


class RetentionEventTraceTest(RetentionFixture):
    def test_full_audit_trail(self):
        self.stock_in(when=aware(2026, 1, 10))
        record_id = self.create_record().json()["data"]["id"]

        today = timezone.localdate()
        hold = self.client.post(f"/api/retention/records/{record_id}/holds/", {
            "hold_type": "legal_freeze", "reason": "法院冻结令",
            "start_date": (today - timedelta(days=10)).isoformat(),
        }, format="json").json()["data"]
        self.client.post(f"/api/retention/holds/{hold['id']}/release/", {
            "end_date": (today - timedelta(days=4)).isoformat(),
        }, format="json")
        self.client.post(f"/api/retention/records/{record_id}/holds/", {
            "hold_type": "extension", "reason": "主管批准延期",
            "extended_due_date": "2027-06-30",
        }, format="json")

        response = self.client.get(f"/api/retention/records/{record_id}/events/")
        events = response.json()["data"]
        self.assertEqual([e["event_type"] for e in events],
                         ["created", "hold_placed", "hold_released", "hold_placed"])

        created = events[0]["detail"]
        self.assertEqual(created["rule_id"], self.rule.id)
        self.assertEqual(created["retention_months"], 6)
        self.assertEqual(created["original_due_date"], "2026-07-10")

        released = events[2]["detail"]
        self.assertEqual(released["paused_days"], 6)
        self.assertEqual(released["current_due_date"], "2026-07-16")

        extension = events[3]["detail"]
        self.assertEqual(extension["extended_due_date"], "2027-06-30")
        # 当前到期日 = 延期日 2027-06-30 + 暂停6天
        self.assertEqual(extension["current_due_date"], "2027-07-06")

        # 当前到期日 = 延期日 2027-06-30 + 暂停6天
        detail = self.client.get(f"/api/retention/records/{record_id}/").json()["data"]
        self.assertEqual(detail["current_due_date"], "2027-07-06")
        self.assertEqual(detail["original_due_date"], "2026-07-10")
        self.assertEqual(detail["explanation"]["paused_days"], 6)
