"""Tests for the important rules. Run with:  python -m unittest -v"""
import threading
import unittest
from datetime import datetime
from decimal import Decimal

from snack_counter import Employee, Office, RefillError, SnackService, Status
from snack_counter.fakes import FakeChat, FakeClock, FakeDirectory, FakeHR

RECEIPT = b"fake-receipt-image"


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(datetime(2026, 11, 2, 9, 0))  # a Monday morning
        self.chat = FakeChat()
        self.hr = FakeHR()
        self.dir = FakeDirectory()
        for emp_id in ("kari", "nils", "ola"):
            self.dir.add(Employee(emp_id, emp_id.title(), "Oslo", "NOK"))
        for emp_id in ("asha", "ravi"):
            self.dir.add(Employee(emp_id, emp_id.title(), "Bengaluru", "INR"))
        self.dir.add(Employee("oslo-admin", "Oslo Admin", "Oslo", "NOK"), volunteer=False)
        self.dir.add(Employee("blr-admin", "Blr Admin", "Bengaluru", "INR"), volunteer=False)
        self.svc = SnackService(
            offices=[
                Office("Oslo", "NOK", Decimal(6000), Decimal(300), Decimal(1200), "oslo-admin"),
                Office("Bengaluru", "INR", Decimal(15000), Decimal(500), Decimal(3000), "blr-admin"),
            ],
            directory=self.dir, chat=self.chat, hr=self.hr, clock=self.clock,
            fx_to_report={"NOK": "1", "INR": "0.12"}, report_currency="NOK")

    def today(self):
        return self.clock.now().date()

    def buy(self, office, buyer, amount, currency=None, items=()):
        """Shortcut: report -> commit -> upload receipt."""
        currency = currency or self.svc.offices[office].currency
        refill = self.svc.report_empty(office, buyer)
        self.svc.commit(refill.id, buyer)
        return self.svc.record_purchase(refill.id, buyer, amount, currency, RECEIPT, items)

    def reimburse(self, refill):
        self.hr.approve(refill.hr_claim_id)
        self.svc.tick()


class LifecycleTests(Base):
    def test_refill_goes_from_empty_to_reimbursed(self):
        refill = self.svc.report_empty("Oslo", "oslo-admin")
        self.assertEqual(refill.status, Status.OFFERED)
        self.assertEqual(refill.offered_to, "kari")
        self.assertEqual(len(self.chat.dms_to("kari")), 1)

        self.svc.commit(refill.id, "kari")
        self.assertEqual(refill.status, Status.COMMITTED)

        self.svc.record_purchase(refill.id, "kari", "450.50", "NOK", RECEIPT, ["crisps", "fruit"])
        self.assertEqual(refill.status, Status.CLAIM_SUBMITTED)

        # HR received exactly the five fields its API asks for.
        claim = self.hr.claims[refill.hr_claim_id]
        self.assertEqual(claim["amount"], Decimal("450.50"))
        self.assertEqual(claim["currency"], "NOK")
        self.assertEqual(claim["employee_id"], "kari")
        self.assertEqual(claim["receipt"], RECEIPT)
        self.assertIn(refill.id, claim["description"])

        self.clock.advance(days=3)
        self.hr.approve(refill.hr_claim_id)
        self.svc.tick()
        self.assertEqual(refill.status, Status.REIMBURSED)
        self.assertIn("approved", self.chat.dms_to("kari")[-1])

    def test_second_report_does_not_start_a_second_refill(self):
        first = self.svc.report_empty("Oslo", "ola")
        second = self.svc.report_empty("Oslo", "nils")
        self.assertIs(first, second)
        # but the other office is independent
        other = self.svc.report_empty("Bengaluru", "ravi")
        self.assertIsNot(first, other)

    def test_new_refill_can_start_once_the_box_was_refilled(self):
        first = self.buy("Oslo", "kari", 500)
        second = self.svc.report_empty("Oslo", "ola")
        self.assertIsNot(first, second)

    def test_receipt_is_required(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        with self.assertRaises(RefillError):
            self.svc.record_purchase(refill.id, "kari", 500, "NOK", b"")


class OnlyOneBuyerTests(Base):
    def test_two_people_cannot_both_commit(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        with self.assertRaises(RefillError):
            self.svc.commit(refill.id, "nils")
        self.assertEqual(refill.buyer, "kari")

    def test_exactly_one_wins_when_many_commit_at_the_same_moment(self):
        for i in range(30):
            self.dir.add(Employee(f"v{i}", f"V{i}", "Oslo", "NOK"))
        refill = self.svc.report_empty("Oslo", "ola")
        barrier = threading.Barrier(30)
        winners, losers = [], []

        def try_commit(emp_id):
            barrier.wait()
            try:
                self.svc.commit(refill.id, emp_id)
                winners.append(emp_id)
            except RefillError:
                losers.append(emp_id)

        threads = [threading.Thread(target=try_commit, args=(f"v{i}",)) for i in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), 29)
        self.assertEqual(refill.buyer, winners[0])

    def test_another_volunteer_may_step_in_before_the_asked_person(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.assertEqual(refill.offered_to, "kari")
        self.svc.commit(refill.id, "ola")
        with self.assertRaises(RefillError):
            self.svc.commit(refill.id, "kari")

    def test_non_volunteer_cannot_commit(self):
        refill = self.svc.report_empty("Oslo", "ola")
        with self.assertRaises(RefillError):
            self.svc.commit(refill.id, "oslo-admin")

    def test_only_the_committed_buyer_can_upload_the_receipt(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        with self.assertRaises(RefillError):
            self.svc.record_purchase(refill.id, "nils", 500, "NOK", RECEIPT)


class HandOverTests(Base):
    def test_unanswered_offer_moves_to_the_next_person(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.assertEqual(refill.offered_to, "kari")
        self.clock.advance(hours=1)
        self.svc.tick()
        self.assertEqual(refill.offered_to, "kari")  # not yet
        self.clock.advance(hours=1)
        self.svc.tick()
        self.assertEqual(refill.offered_to, "nils")

    def test_decline_moves_to_the_next_person_immediately(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.decline(refill.id, "kari")
        self.assertEqual(refill.offered_to, "nils")

    def test_refill_nobody_completes_is_handed_to_someone_else(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        self.clock.advance(hours=25)
        self.svc.tick()
        self.assertEqual(refill.status, Status.OFFERED)
        self.assertEqual(refill.offered_to, "nils")
        self.assertIsNone(refill.buyer)
        self.assertIn("don't buy", self.chat.dms_to("kari")[-1])
        # the late buyer can no longer claim it
        with self.assertRaises(RefillError):
            self.svc.record_purchase(refill.id, "kari", 500, "NOK", RECEIPT)
        # and the new person can complete it
        self.svc.commit(refill.id, "nils")
        self.svc.record_purchase(refill.id, "nils", 500, "NOK", RECEIPT)
        self.assertEqual(refill.status, Status.CLAIM_SUBMITTED)

    def test_buyer_is_reminded_once_before_the_deadline(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        self.clock.advance(hours=21)
        self.svc.tick()
        self.svc.tick()
        reminders = [m for m in self.chat.dms_to("kari") if m.startswith("Reminder")]
        self.assertEqual(len(reminders), 1)

    def test_buyer_can_back_out_and_it_is_handed_on(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        self.svc.release(refill.id, "kari")
        self.assertEqual(refill.offered_to, "nils")

    def test_when_everyone_passes_it_is_opened_to_all_and_admin_is_told(self):
        refill = self.svc.report_empty("Oslo", "ola")
        for emp_id in ("kari", "nils", "ola"):
            self.svc.decline(refill.id, emp_id)
        self.assertEqual(refill.status, Status.OFFERED)
        self.assertIsNone(refill.offered_to)
        self.assertTrue(self.chat.dms_to("oslo-admin"))
        self.assertTrue(any(office == "Oslo" for office, _ in self.chat.channel))
        # next day the round starts again
        self.clock.advance(days=1)
        self.svc.tick()
        self.assertEqual(refill.offered_to, "kari")


class ChoosingWhoBuysTests(Base):
    def test_people_not_in_the_office_today_are_skipped(self):
        self.dir.set_location("kari", self.today(), None)  # working from home / holiday
        refill = self.svc.report_empty("Oslo", "ola")
        self.assertEqual(refill.offered_to, "nils")

    def test_visitor_is_asked_in_the_office_they_are_visiting(self):
        for emp_id in ("kari", "nils", "ola"):
            self.dir.set_location(emp_id, self.today(), None)
        self.dir.set_location("asha", self.today(), "Oslo")  # Bengaluru volunteer visiting Oslo
        oslo = self.svc.report_empty("Oslo", "oslo-admin")
        self.assertEqual(oslo.offered_to, "asha")
        blr = self.svc.report_empty("Bengaluru", "blr-admin")
        self.assertEqual(blr.offered_to, "ravi")  # asha is not in Bengaluru today

    def test_person_who_bought_recently_goes_to_the_back(self):
        self.reimburse(self.buy("Oslo", "kari", 500))
        refill = self.svc.report_empty("Oslo", "ola")
        self.assertEqual(refill.offered_to, "nils")

    def test_load_rotates_across_everyone(self):
        buyers = []
        for _ in range(6):
            refill = self.svc.report_empty("Oslo", "oslo-admin")
            buyers.append(refill.offered_to)
            self.svc.commit(refill.id, refill.offered_to)
            self.svc.record_purchase(refill.id, refill.buyer, 300, "NOK", RECEIPT)
            self.reimburse(refill)
            self.clock.advance(days=1)
        self.assertEqual(buyers, ["kari", "nils", "ola", "kari", "nils", "ola"])

    def test_person_still_waiting_for_money_is_not_asked_first(self):
        self.dir.set_location("ola", self.today(), None)
        self.buy("Oslo", "kari", 500)                    # kari: 1 refill, still unpaid
        self.reimburse(self.buy("Oslo", "nils", 500))    # nils: 2 refills, both paid
        self.reimburse(self.buy("Oslo", "nils", 500))
        refill = self.svc.report_empty("Oslo", "oslo-admin")
        self.assertEqual(refill.offered_to, "nils")


class BudgetTests(Base):
    def test_buyer_is_told_the_normal_limit_when_budget_is_fine(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        self.assertEqual(refill.cap, Decimal(1200))

    def test_limit_shrinks_to_what_is_left_in_the_budget(self):
        for _ in range(5):
            self.buy("Oslo", "kari", 1000)
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(1000))
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "nils")
        self.assertEqual(refill.cap, Decimal(1000))

    def test_no_refill_is_started_when_the_budget_is_used_up(self):
        for _ in range(5):
            self.buy("Oslo", "kari", 1160)               # 5800 spent, 200 left (< 300 minimum)
        dms_before = len(self.chat.dms_to("nils")) + len(self.chat.dms_to("ola"))
        refill = self.svc.report_empty("Oslo", "ola")
        self.assertEqual(refill.status, Status.BUDGET_BLOCKED)
        self.assertEqual(len(self.chat.dms_to("nils")) + len(self.chat.dms_to("ola")), dms_before)
        with self.assertRaises(RefillError):
            self.svc.commit(refill.id, "nils")
        # Bengaluru has its own budget and is not affected
        self.assertEqual(self.svc.report_empty("Bengaluru", "ravi").status, Status.OFFERED)

    def test_blocked_refill_starts_when_the_new_month_begins(self):
        for _ in range(5):
            self.buy("Oslo", "kari", 1160)
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.tick()
        self.assertEqual(refill.status, Status.BUDGET_BLOCKED)
        self.clock.set(datetime(2026, 12, 1, 9, 0))
        self.svc.tick()
        self.assertEqual(refill.status, Status.OFFERED)
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(6000))

    def test_overspend_needs_admin_approval_before_it_goes_to_hr(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        self.svc.record_purchase(refill.id, "kari", 1500, "NOK", RECEIPT)
        self.assertEqual(refill.status, Status.PURCHASED)
        self.assertTrue(refill.needs_approval)
        self.svc.tick()
        self.assertEqual(self.hr.claims, {})
        with self.assertRaises(RefillError):
            self.svc.approve_overage(refill.id, "nils")
        self.svc.approve_overage(refill.id, "oslo-admin")
        self.assertEqual(refill.status, Status.CLAIM_SUBMITTED)
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(4500))

    def test_claim_rejected_by_hr_does_not_count_as_spend(self):
        refill = self.buy("Oslo", "kari", 1000)
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(5000))
        self.hr.reject(refill.hr_claim_id)
        self.svc.tick()
        self.assertEqual(refill.status, Status.REJECTED)
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(6000))
        self.assertTrue(self.chat.dms_to("oslo-admin"))


class CurrencyAndReportTests(Base):
    def test_purchase_must_be_in_the_office_currency(self):
        refill = self.svc.report_empty("Oslo", "ola")
        self.svc.commit(refill.id, "kari")
        with self.assertRaises(RefillError):
            self.svc.record_purchase(refill.id, "kari", 500, "INR", RECEIPT)

    def test_visitor_purchase_hits_the_visited_office_budget_in_its_currency(self):
        self.dir.set_location("asha", self.today(), "Oslo")
        refill = self.buy("Oslo", "asha", 500)           # INR-payroll employee buys in NOK
        claim = self.hr.claims[refill.hr_claim_id]
        self.assertEqual((claim["amount"], claim["currency"], claim["employee_id"]),
                         (Decimal(500), "NOK", "asha"))
        self.assertEqual(self.svc.remaining_budget("Oslo"), Decimal(5500))
        self.assertEqual(self.svc.remaining_budget("Bengaluru"), Decimal(15000))

    def test_monthly_report_totals_across_both_currencies(self):
        self.buy("Oslo", "kari", "450.50", items=["crisps", "fruit"])
        self.reimburse(self.buy("Oslo", "nils", "1000", items=["fruit"]))
        self.buy("Bengaluru", "asha", "2500.75", items=["namkeen"])
        self.clock.set(datetime(2026, 12, 3, 9, 0))
        self.buy("Oslo", "ola", "999")                   # December: must not appear

        report = self.svc.monthly_report(2026, 11)
        oslo, blr = report["offices"]["Oslo"], report["offices"]["Bengaluru"]
        self.assertEqual(oslo["spent"], Decimal("1450.50"))
        self.assertEqual(oslo["remaining"], Decimal("4549.50"))
        self.assertEqual(oslo["awaiting_reimbursement"], Decimal("450.50"))
        self.assertEqual(oslo["refills_by_volunteer"], {"kari": 1, "nils": 1})
        self.assertEqual(oslo["items_bought"], {"crisps": 1, "fruit": 2})
        self.assertEqual(blr["spent"], Decimal("2500.75"))
        self.assertEqual(blr["remaining"], Decimal("12499.25"))
        self.assertEqual(blr["spent_in_NOK"], Decimal("300.09"))   # 2500.75 * 0.12
        self.assertEqual(report["total"], Decimal("1750.59"))      # 1450.50 + 300.09
        self.assertEqual(report["total_currency"], "NOK")

    def test_money_is_exact_not_floating_point(self):
        for _ in range(3):
            self.buy("Oslo", "kari", "0.10")
        self.assertEqual(self.svc.spent("Oslo", 2026, 11), Decimal("0.30"))


class HRTests(Base):
    def test_hr_outage_is_retried_and_creates_exactly_one_claim(self):
        self.hr.down = True
        refill = self.buy("Oslo", "kari", 500)
        self.assertEqual(refill.status, Status.PURCHASED)
        self.svc.tick()
        self.assertEqual(self.hr.claims, {})
        self.hr.down = False
        self.svc.tick()
        self.svc.tick()
        self.assertEqual(refill.status, Status.CLAIM_SUBMITTED)
        self.assertEqual(len(self.hr.claims), 1)

    def test_slow_hr_approval_is_escalated_once(self):
        self.buy("Oslo", "kari", 500)
        self.clock.advance(days=9)
        self.svc.tick()
        self.assertEqual(self.chat.dms_to("oslo-admin"), [])
        self.clock.advance(days=2)
        self.svc.tick()
        self.svc.tick()
        self.assertEqual(len(self.chat.dms_to("oslo-admin")), 1)


if __name__ == "__main__":
    unittest.main()
