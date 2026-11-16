# Snack Counter Automation — Design

## What "automate" means here

A chat bot owns the *coordination* and the *paperwork*; people still do the shopping. One refill is one record with exactly one owner at a time, and uploading the receipt in chat is the reimbursement claim. I did not automate ordering or stock sensing: the pain in the brief is about who, when and money, not about detecting emptiness.

## How a refill moves from "empty" to "reimbursed"

```
report_empty ─► OFFERED ─► COMMITTED ─► PURCHASED ─► CLAIM_SUBMITTED ─► REIMBURSED
     │            ▲  │no answer 2h    │not bought 24h      │                └► REJECTED (admin told)
     │            └──┴── next person ◄┘                    └ HR down: retry / over limit: admin approves
     └► BUDGET_BLOCKED (budget used up; reopens next month)
```

1. **Empty.** Anyone presses "box is empty" in chat. If the office already has an open refill, the report joins it; otherwise a new one is created.
2. **Budget check.** If the office's remaining monthly budget is below a minimum useful purchase, the refill is parked and the office is told why. Otherwise:
3. **Offered.** The bot DMs one volunteer (chosen below). No answer in 2 h, or "can't today" → next person. Nobody left → posted to the office channel, admin told, round restarts next day.
4. **Committed.** One person says yes. The bot tells them the spending limit (`min(normal max, budget left)`), and tells the channel "X is on it — nobody else buy". A reminder goes out before the 24 h deadline; if it passes, the refill is taken back and offered on.
5. **Purchased.** The buyer uploads receipt + amount in chat. The bot creates the HR claim itself (amount, currency, description, receipt, employee ID).
6. **Reimbursed.** The bot polls HR, tells the buyer when it is approved, and escalates to the admin if HR takes more than 10 days.

**Choosing who buys:** volunteers who are *in that office today* (location, not home office — so visitors are included and people at home or on holiday are skipped), ordered by: fewest unreimbursed refills → fewest refills in the last 30 days → longest since last refill. Presence data is only a hint; the "yes" is the real confirmation, and any volunteer may step in before the asked person.

## Main parts and the data each keeps

| Part | Responsibility | Data it keeps |
|---|---|---|
| **Refill service** (core, built) | State machine, selection, budget check, timers (`tick`), report | Refills: office, status, offered-to, buyer, deadlines, limit, amount, currency, items, receipt, HR claim id, audit trail. Office config: currency, budget, purchase range, admin |
| **Chat adapter** (simulated) | Buttons/DMs/channel posts in; method calls on the service | Nothing — stateless |
| **HR adapter** (simulated) | Create claim, read claim status | Nothing — HR is the source of truth for payment; we keep only the claim id |
| **Directory / presence** (simulated) | Who is a volunteer, where they are today | Owned by existing company systems |

## How each problem is prevented

- **Box empty for days** — every refill always has a named owner and a deadline; silence moves it on automatically and ends with a human (admin) rather than nowhere.
- **Two people buy** — one open refill per office, and commit is an atomic check-and-set: only one "yes" can win (tested with 30 simultaneous commits). In production this is a conditional DB update.
- **Same few people, slow money** — fairness ordering spreads the load and avoids asking people the company still owes. The claim is filed the moment the receipt is uploaded, and slow HR approvals are chased.
- **Nobody knows the spend** — every purchase is recorded with amount, currency and items, so the monthly report (per office + one combined total) and "what do we actually buy" fall out of the data.

## Trade-offs

- **Ask one person at a time, not broadcast.** Slower in the worst case, but it removes "someone else will do it". Others can still volunteer.
- **Overspend is flagged, not refused.** The volunteer has already paid; refusing would punish goodwill. Amounts over the limit wait for admin approval before reaching HR.
- **Money is `Decimal`, never float**, and the two currencies are never added together without an explicit rate.
- **Polling, not events** — a simple timer every few minutes is enough at this scale (35 people, a few refills a week).

## Assumptions, and why

| Assumption | Why |
|---|---|
| The budget is a hard stop for *starting* a refill, but a single overspend is only flagged | A budget nobody enforces is not a budget; but the volunteer's money is already spent, so a human decides instead of the bot refusing |
| Purchases are in the local office currency and count against the office where the box is, even for visitors | The snacks are eaten there and bought in a local shop. HR already pays people in their payroll currency, so conversion stays HR's job |
| Spend is counted on the purchase date | That is when the money leaves; approval can land 1–10 days later, in another month |
| HR lets us read a claim's status, but does not detect duplicates | Without status we cannot tell buyers or chase slow claims. Assuming no duplicate detection is the safe side: we never re-send once we hold a claim id, and the refill id is in the description so a duplicate is traceable |
| The combined report is in NOK at one fixed rate per month | Management wants one number; a fixed rate makes the report reproducible. Per-office totals stay in their own currency, so nothing is lost |
| "Who is in the office today" is available (chat status / calendar) but unreliable | Hybrid work changes daily, so it only narrows who is asked; the person's "yes" is the real confirmation |
| Each office has an admin to escalate to | Every automatic chain needs a human end point, otherwise the box is silently empty again |
| Anyone can report an empty box; only volunteers buy | Reporting should cost nothing; buying means fronting money, so it stays opt-in |

## Deliberately not built

Inventory or sensors; automatic ordering; snack voting; receipt OCR (the buyer types the amount); a web UI (chat only); live FX; working-hours/weekend/time-zone aware deadlines (the first thing I would add); splitting one refill between buyers; persistence (in-memory here — one `refills` table in production).
