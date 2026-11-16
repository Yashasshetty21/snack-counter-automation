# Snack Counter Automation — Design

## What "automate" means here

A chat bot owns the *coordination* and the *paperwork*; people still do the shopping. One refill is one record with exactly one owner at a time, and uploading the receipt in chat is the reimbursement claim. The pain in the brief is about who, when and money — not about detecting an empty box — so that is what I automated.

## How a refill moves from "empty" to "reimbursed"

```
report_empty ─► OFFERED ─► COMMITTED ─► PURCHASED ─► CLAIM_SUBMITTED ─► REIMBURSED
     │            ▲  │no answer 2h    │not bought 24h      │                └► REJECTED (admin told)
     │            └──┴── next person ◄┘                    └ HR down: retry / over limit: admin approves
     └► BUDGET_BLOCKED (budget used up; reopens next month)
```

1. **Empty.** Anyone presses "box is empty" in chat. If the office already has an open refill, the report joins it.
2. **Budget check.** If the remaining monthly budget is below a minimum useful purchase, the refill is parked and the office is told why.
3. **Offered.** The bot DMs one volunteer. No answer in 2 h, or "can't today" → next person. Nobody left → office channel + admin; the round restarts next day.
4. **Committed.** One person says yes and is told the spending limit: `min(normal max, budget left)`. The channel is told "X is on it". Not bought within 24 h (after one reminder) → taken back and offered on.
5. **Purchased.** The buyer uploads receipt + amount. The bot creates the HR claim itself.
6. **Reimbursed.** The bot polls HR, tells the buyer, and escalates to the admin after 10 days.

**Choosing who buys:** volunteers who are *in that office today* (location, not home office — visitors are included; people at home or on holiday are skipped), ordered by fewest unreimbursed refills → fewest refills in the last 30 days → longest since last refill. Any volunteer may step in before the asked person.

## Main parts and the data each keeps

| Part | Responsibility | Data it keeps |
|---|---|---|
| **Refill service** (built) | State machine, selection, budget check, timers, report | Refills: office, status, offered-to, buyer, deadlines, limit, amount, currency, items, receipt, HR claim id, audit trail. Office config: currency, budget, purchase range, admin |
| **Chat adapter** (simulated) | Buttons and messages in and out | Nothing |
| **HR adapter** (simulated) | Create claim, read status | Nothing — HR owns payment; we keep the claim id |
| **Directory** (simulated) | Who volunteers, who is where today | Owned by existing company systems |

## How each problem is prevented

- **Box empty for days** — a refill always has a named owner and a deadline; silence moves it on and ends with a human, not nowhere.
- **Two people buy** — one open refill per office, and commit is an atomic check-and-set: only one "yes" wins (tested with 30 simultaneous commits). In production, a conditional DB update.
- **Same few people, slow money** — fair ordering avoids people the company still owes; the claim is filed the moment the receipt is uploaded; slow approvals are chased.
- **Nobody knows the spend** — every purchase records amount, currency and items, so the monthly report falls out of the data.

## Assumptions, and why

| Assumption | Why |
|---|---|
| Budget is a hard stop for *starting* a refill; a single overspend is flagged for the admin | An unenforced budget is not a budget, but the volunteer's money is already spent, so a human decides |
| Purchases are in local currency and count against the office where the box is, even for visitors | Snacks are bought and eaten there; HR already converts to payroll currency |
| Spend is counted on the purchase date | That is when the money leaves; approval may land in another month |
| HR lets us read claim status, but does not detect duplicates | Needed to inform buyers and chase claims. So we never re-send once we hold a claim id, and the refill id is in the description |
| Combined report is in NOK at one fixed rate per month | One reproducible number for management; office totals stay in their own currency |
| "Who is in the office today" exists but is unreliable | It only narrows who is asked; the "yes" is the real confirmation |
| Each office has an admin; anyone can report, only volunteers buy | Every automatic chain needs a human end point; fronting money stays opt-in |

## Trade-offs

- **Ask one person at a time, not broadcast.** Slower in the worst case, but removes "someone else will do it".
- **Overspend is flagged, not refused** — refusing would punish goodwill.
- **Polling, not events** — a timer is enough for 35 people and a few refills a week.
- **Money is `Decimal`**, and currencies are never added without an explicit rate.

## Deliberately not built

Inventory or sensors; automatic ordering; snack voting; receipt OCR; a web UI; live FX; working-hours, weekend and time-zone aware deadlines (the first thing I would add); splitting a refill between buyers; persistence (in-memory here — one `refills` table in production).
