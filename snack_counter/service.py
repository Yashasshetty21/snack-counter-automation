"""The core logic: refill lifecycle, choosing who buys, and the budget check.

Everything that comes in from the chat tool (button clicks, receipt uploads) is
a method call here. Everything that goes out (messages, HR claims) goes through
the `chat` and `hr` objects, so they can be swapped for fakes in tests.
`tick()` is the timer: a scheduler would call it every few minutes.
"""
from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .domain import ACTIVE, Office, Refill, RefillError, Status

CENT = Decimal("0.01")


class SnackService:
    def __init__(self, offices, directory, chat, hr, clock, fx_to_report,
                 report_currency="NOK",
                 offer_timeout=timedelta(hours=2),
                 purchase_timeout=timedelta(hours=24),
                 reminder_lead=timedelta(hours=4),
                 fairness_window=timedelta(days=30),
                 hr_sla=timedelta(days=10)):
        self.offices: dict[str, Office] = {o.name: o for o in offices}
        self.directory = directory
        self.chat = chat
        self.hr = hr
        self.clock = clock
        self.fx_to_report = {k: Decimal(str(v)) for k, v in fx_to_report.items()}
        self.report_currency = report_currency
        self.offer_timeout = offer_timeout
        self.purchase_timeout = purchase_timeout
        self.reminder_lead = reminder_lead
        self.fairness_window = fairness_window
        self.hr_sla = hr_sla
        self._refills: dict[str, Refill] = {}
        # One lock around every state change. In production this is a database
        # transaction with a conditional update (UPDATE ... WHERE status='offered').
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lookups

    def get(self, refill_id: str) -> Refill:
        return self._refills[refill_id]

    def active_refill(self, office: str) -> Refill | None:
        for r in self._refills.values():
            if r.office == office and r.status in ACTIVE:
                return r
        return None

    # ------------------------------------------------------------------- budget

    def spent(self, office: str, year: int, month: int) -> Decimal:
        """Money spent by this office in this month, in the office currency.
        Counted from the purchase date. Claims HR rejected are not company spend."""
        return sum((r.amount for r in self._refills.values()
                    if r.office == office and r.purchased_at is not None
                    and (r.purchased_at.year, r.purchased_at.month) == (year, month)
                    and r.status is not Status.REJECTED), Decimal(0))

    def remaining_budget(self, office: str, when: datetime | None = None) -> Decimal:
        when = when or self.clock.now()
        return self.offices[office].monthly_budget - self.spent(office, when.year, when.month)

    # ------------------------------------------------------------ 1. box is empty

    def report_empty(self, office: str, reporter_id: str) -> Refill:
        """Anyone can report an empty box. One open refill per office, so a
        second report joins the existing one instead of starting a second buy."""
        with self._lock:
            if office not in self.offices:
                raise RefillError(f"unknown office {office}")
            existing = self.active_refill(office)
            if existing:
                who = self._name(existing.buyer) if existing.buyer else "someone is being asked"
                self.chat.send_dm(reporter_id, f"Thanks - already on it ({existing.id}: {who}).")
                return existing

            now = self.clock.now()
            refill = Refill(id=f"R-{len(self._refills) + 1:04d}", office=office,
                            reported_by=reporter_id, reported_at=now)
            self._refills[refill.id] = refill
            refill.log(now, f"reported empty by {reporter_id}")

            if self.remaining_budget(office) < self.offices[office].min_purchase:
                self._block_for_budget(refill)
            else:
                self._offer_next(refill)
            return refill

    # ------------------------------------------------------- 2. who should buy?

    def pick_candidate(self, refill: Refill) -> str | None:
        """Fair choice among volunteers who are physically in this office today.

        Location decides, not home office - so a Bengaluru volunteer visiting
        Oslo can be asked in Oslo. Order of preference:
          1. fewest refills still waiting for reimbursement (don't make people
             front more money while the company owes them)
          2. fewest refills in the last 30 days
          3. longest time since their last refill
        """
        now = self.clock.now()
        since = now - self.fairness_window
        best, best_key = None, None
        for emp_id in self.directory.volunteers():
            if emp_id in refill.tried:
                continue
            if self.directory.location(emp_id, now.date()) != refill.office:
                continue
            mine = [r for r in self._refills.values() if r.buyer == emp_id and r.purchased_at]
            outstanding = sum(1 for r in mine
                              if r.status in (Status.PURCHASED, Status.CLAIM_SUBMITTED))
            recent = sum(1 for r in mine if r.purchased_at >= since)
            last = max((r.purchased_at for r in mine), default=datetime.min)
            key = (outstanding, recent, last, emp_id)
            if best_key is None or key < best_key:
                best, best_key = emp_id, key
        return best

    def _offer_next(self, refill: Refill) -> None:
        now = self.clock.now()
        office = self.offices[refill.office]
        refill.status = Status.OFFERED
        refill.buyer = refill.committed_at = refill.purchase_due_at = refill.cap = None
        refill.reminded = False
        candidate = self.pick_candidate(refill)
        if candidate:
            refill.offered_to = candidate
            refill.offer_expires_at = now + self.offer_timeout
            refill.open_since = None
            refill.log(now, f"offered to {candidate}")
            self.chat.send_dm(candidate, f"The {office.name} snack box is empty. Can you refill it? "
                                         f"[Yes, I'll buy] [Can't today] ({refill.id})")
        else:
            # Nobody suitable: open it to everyone and tell a human.
            refill.offered_to = None
            refill.offer_expires_at = None
            refill.open_since = now
            refill.log(now, "no candidate - opened to the whole office")
            self.chat.post_channel(office.name, f"Snack box is empty and nobody is lined up. "
                                                f"Any volunteer can take {refill.id}.")
            self.chat.send_dm(office.admin_id, f"{refill.id}: no volunteer available in {office.name}.")

    def decline(self, refill_id: str, employee_id: str) -> None:
        with self._lock:
            refill = self.get(refill_id)
            if refill.status is not Status.OFFERED or refill.offered_to != employee_id:
                raise RefillError("this refill is not offered to you")
            refill.tried.add(employee_id)
            refill.log(self.clock.now(), f"{employee_id} declined")
            self._offer_next(refill)

    # ------------------------------------------------------------- 3. commitment

    def commit(self, refill_id: str, employee_id: str) -> Refill:
        """'Yes, I'll buy.' The person asked can accept, and any other volunteer
        may step in first - but only ONE commit ever succeeds per refill."""
        with self._lock:
            refill = self.get(refill_id)
            if employee_id not in self.directory.volunteers():
                raise RefillError("only volunteers can take a refill")
            if refill.status is not Status.OFFERED:
                taken_by = self._name(refill.buyer) if refill.buyer else "nobody"
                raise RefillError(f"{refill.id} is not available (status {refill.status.value}, "
                                  f"taken by {taken_by})")
            now = self.clock.now()
            office = self.offices[refill.office]
            refill.status = Status.COMMITTED
            refill.buyer = employee_id
            refill.offered_to = None
            refill.offer_expires_at = None
            refill.committed_at = now
            refill.purchase_due_at = now + self.purchase_timeout
            # Budget check: tell the buyer the most they may spend.
            refill.cap = min(office.max_purchase, self.remaining_budget(refill.office))
            refill.log(now, f"{employee_id} committed, cap {refill.cap} {office.currency}")
            self.chat.send_dm(employee_id, f"Thanks! Spend at most {refill.cap} {office.currency} and "
                                           f"upload the receipt here by {refill.purchase_due_at:%a %H:%M}.")
            self.chat.post_channel(office.name, f"{self._name(employee_id)} is refilling the snack box "
                                                f"- nobody else needs to buy.")
            return refill

    def release(self, refill_id: str, employee_id: str) -> None:
        """The buyer backs out ('sorry, can't make it')."""
        with self._lock:
            refill = self.get(refill_id)
            if refill.status is not Status.COMMITTED or refill.buyer != employee_id:
                raise RefillError("you are not the buyer of this refill")
            refill.tried.add(employee_id)
            refill.log(self.clock.now(), f"{employee_id} released")
            self._offer_next(refill)

    # --------------------------------------------------------------- 4. purchase

    def record_purchase(self, refill_id: str, employee_id: str, amount, currency: str,
                        receipt: bytes, items=()) -> Refill:
        """The buyer uploads the receipt. This both closes the 'box is empty'
        problem and starts the reimbursement - no separate HR form to fill in."""
        with self._lock:
            refill = self.get(refill_id)
            office = self.offices[refill.office]
            if refill.status is not Status.COMMITTED or refill.buyer != employee_id:
                raise RefillError("you are not the committed buyer of this refill")
            amount = Decimal(str(amount))
            if amount <= 0:
                raise RefillError("amount must be positive")
            if currency != office.currency:
                raise RefillError(f"{office.name} purchases must be in {office.currency}")
            if not receipt:
                raise RefillError("a receipt image is required")

            now = self.clock.now()
            allowed = min(refill.cap, self.remaining_budget(refill.office, now))
            refill.status = Status.PURCHASED
            refill.amount = amount
            refill.currency = currency
            refill.receipt = receipt
            refill.items = tuple(items)
            refill.purchased_at = now
            refill.log(now, f"purchased for {amount} {currency}")
            self.chat.post_channel(office.name, f"Snack box refilled by {self._name(employee_id)}. Thanks!")

            if amount > allowed:
                # The money is already spent, so we don't refuse it - but a human
                # must approve before it goes to HR.
                refill.needs_approval = True
                refill.log(now, f"over the limit of {allowed} - waiting for admin approval")
                self.chat.send_dm(office.admin_id, f"{refill.id}: {amount} {currency} is over the "
                                                   f"limit of {allowed}. [Approve]")
                self.chat.send_dm(employee_id, "That is over the limit, so the office admin has to "
                                               "approve it before it goes to HR.")
            else:
                self._submit_claim(refill)
            return refill

    def approve_overage(self, refill_id: str, admin_id: str) -> None:
        with self._lock:
            refill = self.get(refill_id)
            if admin_id != self.offices[refill.office].admin_id:
                raise RefillError("only the office admin can approve")
            if refill.status is not Status.PURCHASED or not refill.needs_approval:
                raise RefillError("nothing to approve")
            refill.needs_approval = False
            refill.log(self.clock.now(), f"overage approved by {admin_id}")
            self._submit_claim(refill)

    # ---------------------------------------------------------- 5. reimbursement

    def _submit_claim(self, refill: Refill) -> None:
        """Create the HR claim. If HR is down the refill stays PURCHASED and
        tick() tries again. We never create a second claim once we hold an id."""
        if refill.hr_claim_id is not None:
            return
        now = self.clock.now()
        description = (f"Snack refill {refill.id}, {refill.office} office, "
                       f"{refill.purchased_at:%Y-%m-%d}")
        try:
            # Amount is in the currency of the purchase. HR pays the employee in
            # their payroll currency - converting is HR's job, not ours.
            refill.hr_claim_id = self.hr.create_claim(
                amount=refill.amount, currency=refill.currency, description=description,
                receipt=refill.receipt, employee_id=refill.buyer)
        except Exception as exc:  # HR is someone else's system: anything can go wrong
            refill.log(now, f"HR submit failed ({exc}) - will retry")
            return
        refill.status = Status.CLAIM_SUBMITTED
        refill.claim_submitted_at = now
        refill.log(now, f"HR claim {refill.hr_claim_id} created")
        self.chat.send_dm(refill.buyer, f"Your claim for {refill.amount} {refill.currency} is with HR "
                                        f"({refill.hr_claim_id}). I'll tell you when it's approved.")

    # -------------------------------------------------------------------- timer

    def tick(self) -> None:
        """Called regularly by a scheduler. Handles everything that happens
        because time passed rather than because someone clicked."""
        with self._lock:
            now = self.clock.now()
            for refill in list(self._refills.values()):
                office = self.offices[refill.office]

                if refill.status is Status.BUDGET_BLOCKED:
                    if self.remaining_budget(refill.office) >= office.min_purchase:
                        refill.log(now, "budget available again")
                        self._offer_next(refill)

                elif refill.status is Status.OFFERED:
                    if refill.offered_to and now >= refill.offer_expires_at:
                        refill.tried.add(refill.offered_to)
                        refill.log(now, f"{refill.offered_to} did not answer")
                        self._offer_next(refill)
                    elif refill.offered_to is None and now.date() > refill.open_since.date():
                        # New day, different people in the office: start over.
                        refill.tried.clear()
                        self._offer_next(refill)

                elif refill.status is Status.COMMITTED:
                    if now >= refill.purchase_due_at:
                        late = refill.buyer
                        refill.tried.add(late)
                        refill.log(now, f"{late} did not complete in time")
                        self.chat.send_dm(late, f"{refill.id} has been handed to someone else - "
                                                f"please don't buy.")
                        self._offer_next(refill)
                    elif not refill.reminded and now >= refill.purchase_due_at - self.reminder_lead:
                        refill.reminded = True
                        self.chat.send_dm(refill.buyer, f"Reminder: {refill.id} is due "
                                                        f"{refill.purchase_due_at:%a %H:%M}. [Can't make it]")

                elif refill.status is Status.PURCHASED:
                    if not refill.needs_approval:
                        self._submit_claim(refill)

                elif refill.status is Status.CLAIM_SUBMITTED:
                    self._check_claim(refill, now)

    def _check_claim(self, refill: Refill, now: datetime) -> None:
        try:
            status = self.hr.claim_status(refill.hr_claim_id)
        except Exception:
            return  # try again next tick
        admin = self.offices[refill.office].admin_id
        if status == "APPROVED":
            refill.status = Status.REIMBURSED
            refill.reimbursed_at = now
            refill.log(now, "reimbursed")
            self.chat.send_dm(refill.buyer, f"HR approved your snack claim {refill.hr_claim_id}.")
        elif status == "REJECTED":
            refill.status = Status.REJECTED
            refill.log(now, "HR rejected the claim")
            self.chat.send_dm(refill.buyer, f"HR rejected claim {refill.hr_claim_id}. "
                                            f"The office admin has been told.")
            self.chat.send_dm(admin, f"{refill.id}: HR rejected claim {refill.hr_claim_id}.")
        elif not refill.sla_escalated and now - refill.claim_submitted_at > self.hr_sla:
            refill.sla_escalated = True
            refill.log(now, "HR is slower than 10 days - escalated")
            self.chat.send_dm(admin, f"{refill.id}: claim {refill.hr_claim_id} has waited more than "
                                     f"{self.hr_sla.days} days. Please chase HR.")

    def _block_for_budget(self, refill: Refill) -> None:
        office = self.offices[refill.office]
        refill.status = Status.BUDGET_BLOCKED
        refill.log(self.clock.now(), "blocked: monthly budget used up")
        self.chat.post_channel(office.name, "Snack box is empty, but this month's budget is used up. "
                                            "It will be refilled when the new month starts.")
        self.chat.send_dm(office.admin_id, f"{refill.id}: {office.name} snack budget is used up.")

    # ------------------------------------------------------------------- report

    def monthly_report(self, year: int, month: int) -> dict:
        """One report for management: each office in its own currency, plus one
        combined total in the report currency using a fixed rate for the month."""
        with self._lock:
            offices = {}
            total = Decimal(0)
            for office in self.offices.values():
                rows = [r for r in self._refills.values()
                        if r.office == office.name and r.purchased_at is not None
                        and (r.purchased_at.year, r.purchased_at.month) == (year, month)
                        and r.status is not Status.REJECTED]
                spent = sum((r.amount for r in rows), Decimal(0))
                pending = sum((r.amount for r in rows if r.status is not Status.REIMBURSED), Decimal(0))
                hours_empty = [(r.purchased_at - r.reported_at).total_seconds() / 3600 for r in rows]
                if office.currency not in self.fx_to_report:
                    raise RefillError(f"no exchange rate for {office.currency}")
                in_report_ccy = (spent * self.fx_to_report[office.currency]).quantize(CENT, ROUND_HALF_UP)
                total += in_report_ccy
                offices[office.name] = {
                    "currency": office.currency,
                    "budget": office.monthly_budget,
                    "spent": spent,
                    "remaining": office.monthly_budget - spent,
                    "refills": len(rows),
                    "awaiting_reimbursement": pending,
                    "avg_hours_empty": round(sum(hours_empty) / len(hours_empty), 1) if rows else None,
                    "refills_by_volunteer": dict(Counter(r.buyer for r in rows)),
                    "items_bought": dict(Counter(i for r in rows for i in r.items)),
                    f"spent_in_{self.report_currency}": in_report_ccy,
                }
            return {"month": f"{year}-{month:02d}", "offices": offices,
                    "total": total, "total_currency": self.report_currency}

    def _name(self, employee_id: str) -> str:
        return self.directory.employee(employee_id).name
