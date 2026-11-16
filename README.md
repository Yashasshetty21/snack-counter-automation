# Snack Counter Automation

Python 3.10+, standard library only — nothing to install.

```
python -m unittest -v     # 32 tests
python demo.py            # short walk-through with chat messages and the monthly report
```

| File | What it is |
|---|---|
| `DESIGN.md` | The design document |
| `snack_counter/domain.py` | Data: `Office`, `Employee`, `Refill`, lifecycle `Status` |
| `snack_counter/service.py` | Core logic: lifecycle, choosing who buys, budget check, timers, monthly report |
| `snack_counter/fakes.py` | Simulated chat tool, HR system, employee directory, clock |
| `tests/test_snack_counter.py` | Tests, grouped by rule |

The assumptions I made, and why, are listed in `DESIGN.md`.
