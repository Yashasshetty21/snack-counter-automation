"""Plain data: offices, employees, and the refill with its lifecycle states."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class Status(Enum):
    BUDGET_BLOCKED = "budget_blocked"    # box is empty but this month's budget is used up
    OFFERED = "offered"                  # waiting for someone to commit
    COMMITTED = "committed"              # exactly one person has promised to buy
    PURCHASED = "purchased"              # box refilled, HR claim not created yet
    CLAIM_SUBMITTED = "claim_submitted"  # HR has the claim, waiting for approval
    REIMBURSED = "reimbursed"            # done
    REJECTED = "rejected"                # HR said no; a human has to sort it out


# While a refill is in one of these states the box is still empty and the
# office may not open a second refill.
ACTIVE = {Status.BUDGET_BLOCKED, Status.OFFERED, Status.COMMITTED}


class RefillError(Exception):
    """A rule was broken (wrong state, wrong person, wrong currency...)."""


@dataclass(frozen=True)
class Office:
    name: str
    currency: str
    monthly_budget: Decimal
    min_purchase: Decimal   # below this much remaining budget, a refill is not worth starting
    max_purchase: Decimal   # normal ceiling for one refill
    admin_id: str           # who gets escalations (office manager)


@dataclass(frozen=True)
class Employee:
    id: str
    name: str
    home_office: str
    payroll_currency: str


@dataclass
class Refill:
    id: str
    office: str
    reported_by: str
    reported_at: datetime
    status: Status = Status.OFFERED

    # offering
    offered_to: Optional[str] = None            # None while OFFERED = open to every volunteer
    offer_expires_at: Optional[datetime] = None
    open_since: Optional[datetime] = None
    tried: set = field(default_factory=set)     # people who declined / timed out on this refill

    # commitment
    buyer: Optional[str] = None
    committed_at: Optional[datetime] = None
    purchase_due_at: Optional[datetime] = None
    cap: Optional[Decimal] = None               # spending limit told to the buyer
    reminded: bool = False

    # purchase
    amount: Optional[Decimal] = None
    currency: Optional[str] = None
    receipt: Optional[bytes] = None
    items: tuple = ()
    purchased_at: Optional[datetime] = None
    needs_approval: bool = False                # spent over the cap; admin must approve first

    # reimbursement
    hr_claim_id: Optional[str] = None
    claim_submitted_at: Optional[datetime] = None
    reimbursed_at: Optional[datetime] = None
    sla_escalated: bool = False

    history: list = field(default_factory=list)  # (time, text) audit trail

    def log(self, when: datetime, text: str) -> None:
        self.history.append((when, text))
