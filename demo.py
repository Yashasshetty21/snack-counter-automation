"""A short walk-through you can run:  python demo.py"""
from datetime import datetime
from decimal import Decimal

from snack_counter import Employee, Office, SnackService
from snack_counter.fakes import FakeChat, FakeClock, FakeDirectory, FakeHR

clock, chat, hr, directory = FakeClock(datetime(2026, 11, 2, 9, 0)), FakeChat(), FakeHR(), FakeDirectory()
for emp_id in ("kari", "nils"):
    directory.add(Employee(emp_id, emp_id.title(), "Oslo", "NOK"))
directory.add(Employee("asha", "Asha", "Bengaluru", "INR"))
directory.add(Employee("oslo-admin", "Oslo Admin", "Oslo", "NOK"), volunteer=False)
directory.add(Employee("blr-admin", "Blr Admin", "Bengaluru", "INR"), volunteer=False)

svc = SnackService(
    offices=[Office("Oslo", "NOK", Decimal(6000), Decimal(300), Decimal(1200), "oslo-admin"),
             Office("Bengaluru", "INR", Decimal(15000), Decimal(500), Decimal(3000), "blr-admin")],
    directory=directory, chat=chat, hr=hr, clock=clock, fx_to_report={"NOK": "1", "INR": "0.12"})

# Oslo: Kari is asked, commits, then never buys -> handed to Nils, who completes it.
r = svc.report_empty("Oslo", "nils")
svc.commit(r.id, "kari")
clock.advance(hours=25)
svc.tick()
svc.commit(r.id, "nils")
svc.record_purchase(r.id, "nils", "640", "NOK", b"receipt", ["fruit", "crisps"])
clock.advance(days=4)
hr.approve(r.hr_claim_id)
svc.tick()

# Bengaluru: straightforward refill, HR has not approved yet.
b = svc.report_empty("Bengaluru", "blr-admin")
svc.commit(b.id, "asha")
svc.record_purchase(b.id, "asha", "2100", "INR", b"receipt", ["namkeen"])

print("--- audit trail of", r.id)
for when, text in r.history:
    print(f"  {when:%d %b %H:%M}  {text}")
print("\n--- chat messages")
for who, text in chat.dms:
    print(f"  DM to {who}: {text}")
for office, text in chat.channel:
    print(f"  #{office}: {text}")
print("\n--- monthly report")
report = svc.monthly_report(2026, 11)
for name, data in report["offices"].items():
    print(f"  {name}: spent {data['spent']} of {data['budget']} {data['currency']}, "
          f"{data['refills']} refill(s), awaiting reimbursement {data['awaiting_reimbursement']}")
print(f"  TOTAL: {report['total']} {report['total_currency']}")
