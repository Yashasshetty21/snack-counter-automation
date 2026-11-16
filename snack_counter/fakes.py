"""Simulated stand-ins for everything outside our control:
the chat tool, the external HR system, the employee directory and the clock."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from .domain import Employee


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)

    def set(self, when: datetime) -> None:
        self._now = when


class FakeChat:
    """Records every outgoing message so tests can look at them."""

    def __init__(self):
        self.dms: list[tuple[str, str]] = []        # (employee_id, text)
        self.channel: list[tuple[str, str]] = []    # (office, text)

    def send_dm(self, employee_id: str, text: str) -> None:
        self.dms.append((employee_id, text))

    def post_channel(self, office: str, text: str) -> None:
        self.channel.append((office, text))

    def dms_to(self, employee_id: str) -> list[str]:
        return [text for emp, text in self.dms if emp == employee_id]


class HRUnavailable(Exception):
    pass


class FakeHR:
    """The external HR system. Its API takes exactly the five fields from the
    brief. Tests decide when a claim gets approved or rejected."""

    def __init__(self):
        self.claims: dict[str, dict] = {}
        self.down = False

    def create_claim(self, amount: Decimal, currency: str, description: str,
                     receipt: bytes, employee_id: str) -> str:
        if self.down:
            raise HRUnavailable("HR system did not answer")
        claim_id = f"HR-{len(self.claims) + 1}"
        self.claims[claim_id] = dict(
            amount=amount, currency=currency, description=description,
            receipt=receipt, employee_id=employee_id, status="PENDING")
        return claim_id

    def claim_status(self, claim_id: str) -> str:
        if self.down:
            raise HRUnavailable("HR system did not answer")
        return self.claims[claim_id]["status"]

    # test helpers (what the HR staff would do)
    def approve(self, claim_id: str) -> None:
        self.claims[claim_id]["status"] = "APPROVED"

    def reject(self, claim_id: str) -> None:
        self.claims[claim_id]["status"] = "REJECTED"


class FakeDirectory:
    """Who works here, who volunteers, and where each person is on a given day.
    In real life: company login + chat status / calendar."""

    def __init__(self):
        self._employees: dict[str, Employee] = {}
        self._volunteers: set[str] = set()
        self._where: dict[tuple[str, date], str | None] = {}

    def add(self, employee: Employee, volunteer: bool = True) -> Employee:
        self._employees[employee.id] = employee
        if volunteer:
            self._volunteers.add(employee.id)
        return employee

    def employee(self, employee_id: str) -> Employee:
        return self._employees[employee_id]

    def volunteers(self) -> list[str]:
        return sorted(self._volunteers)

    def set_location(self, employee_id: str, day: date, office: str | None) -> None:
        """office=None means not in any office that day (home or holiday)."""
        self._where[(employee_id, day)] = office

    def location(self, employee_id: str, day: date) -> str | None:
        """Defaults to the home office unless told otherwise."""
        key = (employee_id, day)
        if key in self._where:
            return self._where[key]
        return self._employees[employee_id].home_office
