# Dienstplaner

Simple Python scheduling project with a small SQLite database and solver logic.

## Project files

- `main.py`: entrypoint script.
- `solver.py`: scheduling/solver logic.
- `database.py`: database access helpers.
- `dienstplaner.db`: local SQLite database file.

## Quick start

1. Install dependencies:
   - `pip install -r requirements.txt`
2. Run the application:
   - `python main.py`

## One-command launchers (auto-venv)

- Linux/macOS (bash):
  - `bash run.sh`
- Windows (Command Prompt):
  - `run.bat`

Both launchers:
- Create `.venv` in the project folder if it does not exist.
- Use `python -m pip install -r requirements.txt`; pip skips already satisfied packages.
- Launch `main.py`.

For CI/testing without opening the GUI:
- Linux/macOS: `DIENSTPLANER_SKIP_LAUNCH=1 bash run.sh`
- Windows: `set DIENSTPLANER_SKIP_LAUNCH=1 && run.bat`

## Solver constraints and objective

The monthly CP-SAT model in `solver.py` uses a mix of hard feasibility
constraints and soft objective terms.

### Hard constraints (must be satisfied)

- **Exactly one employee per open slot**
  - For each non-fixed `(date, shift_slot)`, the solver assigns exactly one
    eligible employee.
- **Eligibility filter for candidate generation**
  - Candidate variables are only created for employees who:
    - are solver-managed (employees in clinic `ZNA` are manual-only),
    - have at least one required qualification for the slot,
    - are not blocked on that weekday,
    - are not absent on that date.
- **Fixed assignment consistency checks**
  - Fixed/manual assignments are validated before solving:
    - no blocked-weekday violations,
    - no absence violations,
    - no >1 shift/day from fixed/base data,
    - no fixed consecutive-day violations,
    - no fixed incompatibility-pair violations,
    - no fixed monthly-max violations.
- **At most one shift per employee per day**
  - Across fixed + newly assigned shifts.
- **No consecutive working days**
  - For non-`ZNA` constrained employees, day `d` and `d+1` cannot both be worked.
- **Same-day incompatible employee pairs**
  - Configured incompatibility pairs cannot both work on the same date.
- **Weekend cap**
  - Per employee: at most 2 worked ISO-weekend groups (Fri/Sat/Sun grouped by ISO week).
- **Monthly max shifts per employee**
  - Total assignments (fixed/base + new) must stay within `max_shifts_per_month`.
- **ZNA Facharzt coverage rule**
  - For each date, across `ZNA DR A/B`, at least one assigned doctor must have
    qualification `Notaufnahme-Facharztstandard` (can be satisfied by fixed or new assignments).
- **Clinic/day uniqueness rule (default hard mode)**
  - On Monday-Thursday and Sunday (Friday/Saturday exempt), per clinic
    (except clinic `ZNA`), at most one assignment per day across all slots.
- **Fairness spread hard cap**
  - Let `total_e` be monthly assignments per solver-managed employee.
  - Enforced: `max(total_e) - min(total_e) <= max_fairness_spread` (default `1`).

### Soft objective terms (optimized, not mandatory)

The solver minimizes a weighted sum of soft terms.

- **Preference handling**
  - `prefer_off`: penalty `prefer_off_penalty * assignment`.
  - `prefer_work`: reward (negative cost) `-prefer_work_reward * assignment`.
- **One-day-gap penalty (`work-rest-work`)**
  - Penalizes patterns where an employee works day `d` and day `d+2`.
  - Exception: for a specific pattern, no penalty variable is created if the
    employee has explicit `prefer_work` on day `d` and day `d+2`.
- **Mix-balance penalty for dual-qualified employees**
  - For employees qualified for both Hausdienst and ZNA, minimize
    `abs(haus_count - zna_count)` per employee, weighted by `mix_balance_weight`.
- **Optional soft clinic uniqueness**
  - If `clinic_uniqueness_soft` is enabled, clinic/day duplicates on
    Monday-Thursday and Sunday are allowed but penalized by
    `clinic_duplicate_penalty` per excess assignment.

### Notes on fixed/manual assignments

- Fixed/manual assignments are treated as base load and included in:
  - daily constraints,
  - monthly max limits,
  - fairness totals,
  - clinic/day counting,
  - objective reporting.
- ZNA clinic employees are intentionally excluded from automatic assignment and
  remain manual-only in the current model.

### Fallback mode (partial fill on infeasible)

If `partial_fill_on_infeasible` is enabled in the Solver UI, the solver runs in
two stages:

1. **Normal mode first** (full-feasibility model above).
2. If no solution exists, **fallback mode** is started and returns status `partial`
   when at least one best-effort solution is found.

In fallback mode, constraints are handled explicitly as follows:

- **Relaxed**
  - Open-slot coverage changes from **exactly one** to **at most one**.
    - Result: open slots may remain unfilled.

- **Still enforced (hard)**
  - eligibility filtering (qualification/blocked weekday/absence),
  - fixed assignment consistency checks,
  - at most one shift per employee per day,
  - no consecutive working days,
  - same-day incompatibility pairs,
  - weekend cap,
  - monthly max shifts,
  - daily ZNA Facharzt coverage (`Notaufnahme-Facharztstandard`),
  - fairness spread hard cap,
  - clinic/day uniqueness hard rule (unless `clinic_uniqueness_soft` is enabled, as in normal mode).

- **Objective priority**
  - Primary driver: minimize number of unfilled open slots (large per-slot penalty).
  - Then standard soft terms apply (preferences, one-day-gap, mix balance, and
    optional clinic-duplicate penalty when clinic uniqueness is soft).
