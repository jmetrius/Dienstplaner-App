"""
CP-SAT automatic monthly schedule solver.

Design intent:
- Keep all scheduling logic in one place so constraint interactions are explicit.
- Separate *hard* feasibility constraints from *soft* objective preferences.
- Return rich diagnostics (logs + objective breakdowns) to make tuning easier.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable

from ortools.sat.python import cp_model

from database import (
    SHIFT_SLOT_CODES,
    SHIFT_SLOTS_HAUSDienst,
    SHIFT_SLOTS_ZNA,
    qualification_names_for_shift_slot,
)


@dataclass(frozen=True)
class SolverConfig:
    """Tunable solver knobs used by `solve_month_schedule`."""

    max_solutions: int = 3
    time_limit_seconds: float = 20.0
    prefer_off_penalty: int = 8
    prefer_work_reward: int = 3
    max_fairness_spread: int = 1
    mix_balance_weight: int = 4
    one_day_gap_penalty: int = 5
    clinic_uniqueness_soft: bool = False
    clinic_duplicate_penalty: int = 20


@dataclass(frozen=True)
class SolverSolution:
    """One feasible solver output plus reporting metadata."""

    assignments: dict[tuple[str, str], int]
    objective_value: int
    shift_counts: dict[int, int]
    assigned_shift_counts: dict[int, int]
    objective_breakdown: dict[str, int]
    preference_summary: dict[str, int]
    employee_preference_stats: dict[int, dict[str, int]]


@dataclass(frozen=True)
class SolverResult:
    """Top-level API return object for solver calls."""

    solutions: list[SolverSolution]
    status: str
    message: str
    logs: list[str] = field(default_factory=list)


def _weekday(iso_date: str) -> int:
    """Return weekday as Monday=0 .. Sunday=6."""

    return date.fromisoformat(iso_date).weekday()


def _weekend_group_key(iso_date: str) -> tuple[int, int]:
    """Return `(iso_year, iso_week)` used to group weekend days."""

    dt = date.fromisoformat(iso_date)
    return dt.isocalendar()[:2]


def solve_month_schedule(
    *,
    year: int,
    month: int,
    clinic_id: int,
    solver_input: dict[str, object],
    config: SolverConfig | None = None,
    logger: Callable[[str], None] | None = None,
    _skip_spread_diagnostic: bool = False,
) -> SolverResult:
    """
    Build and solve a monthly schedule model.

    High-level phases:
    1) Normalize `solver_input` and pre-validate fixed assignments.
    2) Build decision variables for open slots.
    3) Add hard constraints (coverage, rest, incompatibilities, maxima, etc.).
    4) Add soft objective terms (preferences, mix balance, clinic duplication).
    5) Solve up to `max_solutions` variants and extract detailed diagnostics.

    `solver_input` is expected to come from `database.load_solver_month_input`.
    """

    cfg = config or SolverConfig()
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(message)
        if logger is not None:
            logger(message)

    # Normalize incoming payload into local, strongly-typed containers.
    dates = list(solver_input.get("dates", []))
    employees_raw = list(solver_input.get("employees", []))
    fixed_assignments = dict(solver_input.get("fixed_assignments", {}))
    external_assignments_by_date = dict(
        solver_input.get("external_assignments_by_date", {})
    )
    absences_by_employee = dict(solver_input.get("absences_by_employee", {}))
    preferences = dict(solver_input.get("preferences", {}))
    employee_clinic_by_id = dict(solver_input.get("employee_clinic_by_id", {}))
    same_day_incompatible_raw = list(
        solver_input.get("same_day_incompatible_pairs", [])
    )

    # Employee normalization:
    # - ZNA employees are tracked but excluded from automatic assignment.
    # - We precompute constraints-relevant metadata for quick lookups later.
    active_employee_ids: list[int] = []
    solver_employee_ids: list[int] = []
    employee_max: dict[int, int] = {}
    employee_quals: dict[int, set[str]] = {}
    employee_blocked_weekdays: dict[int, set[int]] = {}
    zna_employee_ids: set[int] = set()
    zna_clinic_ids: set[int] = set()
    dual_qualified_employee_ids: list[int] = []
    for item in employees_raw:
        if not isinstance(item, dict):
            continue
        employee_id = int(item["id"])
        active_employee_ids.append(employee_id)
        is_zna_employee = str(item.get("clinic_code", "")) == "zna"
        if not is_zna_employee:
            solver_employee_ids.append(employee_id)
        else:
            zna_employee_ids.add(employee_id)
        employee_max[employee_id] = int(item["max_shifts_per_month"])
        clinic_value = int(item["clinic_id"])
        employee_clinic_by_id.setdefault(employee_id, clinic_value)
        if is_zna_employee:
            zna_clinic_ids.add(clinic_value)
        quals = item.get("qualifications", set())
        if isinstance(quals, set):
            employee_quals[employee_id] = {str(q) for q in quals}
        else:
            employee_quals[employee_id] = {str(q) for q in quals}
        blocked_raw = item.get("blocked_weekdays", set())
        if isinstance(blocked_raw, set):
            blocked_days = {int(day) for day in blocked_raw}
        else:
            blocked_days = {int(day) for day in blocked_raw}
        employee_blocked_weekdays[employee_id] = {
            day for day in blocked_days if day >= 0 and day <= 6
        }
    # Dual-qualified employees are eligible for the mix-balance objective.
    dual_qualified_employee_ids = [
        employee_id
        for employee_id in solver_employee_ids
        if "Hausdienst" in employee_quals.get(employee_id, set())
        and (
            "Notaufnahme" in employee_quals.get(employee_id, set())
            or "Notaufnahme-Facharztstandard"
            in employee_quals.get(employee_id, set())
        )
    ]

    if not dates:
        return SolverResult(
            solutions=[],
            status="invalid_input",
            message="No dates available for selected month.",
            logs=logs,
        )
    if not solver_employee_ids:
        return SolverResult(
            solutions=[],
            status="infeasible",
            message="No solver-eligible employees (clinic 'ZNA' is manual-only).",
            logs=logs,
        )

    # Canonicalize incompatibility pairs so downstream constraints can iterate once.
    same_day_incompatible_pairs: set[tuple[int, int]] = set()
    for entry in same_day_incompatible_raw:
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        left, right = int(entry[0]), int(entry[1])
        if left == right:
            continue
        pair = (left, right) if left < right else (right, left)
        same_day_incompatible_pairs.add(pair)

    # Core model data structures.
    model = cp_model.CpModel()
    variables: dict[tuple[str, str, int], cp_model.IntVar] = {}
    slot_candidates: dict[tuple[str, str], list[int]] = {}
    employee_day_vars: dict[tuple[int, str], list[cp_model.IntVar]] = {}
    employee_month_vars: dict[int, list[cp_model.IntVar]] = {
        employee_id: [] for employee_id in solver_employee_ids
    }

    employee_day_base: dict[tuple[int, str], int] = {}
    employee_month_base: dict[int, int] = {employee_id: 0 for employee_id in solver_employee_ids}
    clinic_day_base: dict[tuple[int, str], int] = {}

    # Base counters represent assignments that are already fixed externally.
    # Decision variables are added *on top* of these bases.
    for iso_date, rows in external_assignments_by_date.items():
        if not isinstance(rows, list):
            continue
        for entry in rows:
            if not isinstance(entry, tuple) or len(entry) != 2:
                continue
            employee_id, employee_clinic = int(entry[0]), int(entry[1])
            employee_day_base[(employee_id, iso_date)] = (
                employee_day_base.get((employee_id, iso_date), 0) + 1
            )
            clinic_day_base[(employee_clinic, iso_date)] = (
                clinic_day_base.get((employee_clinic, iso_date), 0) + 1
            )
            if employee_id in employee_month_base:
                employee_month_base[employee_id] += 1

    # Fixed assignments are also treated as base load and validated early.
    for (iso_date, slot), employee_id in fixed_assignments.items():
        eid = int(employee_id)
        if eid not in zna_employee_ids:
            weekday = _weekday(str(iso_date))
            if weekday in employee_blocked_weekdays.get(eid, set()):
                log(
                    f"Infeasible fixed assignment: employee {eid} is blocked on weekday {weekday} ({iso_date}) for {slot}."
                )
                return SolverResult(
                    solutions=[],
                    status="infeasible",
                    message="Fixed assignments conflict with blocked weekdays.",
                    logs=logs,
                )
        employee_day_base[(eid, iso_date)] = employee_day_base.get((eid, iso_date), 0) + 1
        if eid in employee_month_base:
            employee_month_base[eid] += 1
        clinic_for_employee = employee_clinic_by_id.get(eid)
        if clinic_for_employee is not None:
            key = (int(clinic_for_employee), iso_date)
            clinic_day_base[key] = clinic_day_base.get(key, 0) + 1
        if (
            eid not in zna_employee_ids
            and eid in absences_by_employee
            and iso_date in absences_by_employee[eid]
        ):
            log(
                f"Infeasible fixed assignment: employee {eid} is absent on {iso_date} for {slot}."
            )
            return SolverResult(
                solutions=[],
                status="infeasible",
                message="Fixed assignments conflict with absences.",
                logs=logs,
            )

    # Create one boolean variable per feasible (date, slot, employee) triple.
    for iso_date in dates:
        for slot in SHIFT_SLOT_CODES:
            fixed_employee = fixed_assignments.get((iso_date, slot))
            if fixed_employee is not None:
                continue
            required_quals = set(qualification_names_for_shift_slot(slot))
            candidates: list[int] = []
            weekday = _weekday(iso_date)
            for employee_id in solver_employee_ids:
                if required_quals and not (employee_quals.get(employee_id, set()) & required_quals):
                    continue
                if weekday in employee_blocked_weekdays.get(employee_id, set()):
                    continue
                if iso_date in absences_by_employee.get(employee_id, set()):
                    continue
                var = model.NewBoolVar(f"x_{iso_date}_{slot}_{employee_id}")
                variables[(iso_date, slot, employee_id)] = var
                candidates.append(employee_id)
                employee_day_vars.setdefault((employee_id, iso_date), []).append(var)
                employee_month_vars.setdefault(employee_id, []).append(var)
            if not candidates:
                log(f"Infeasible slot: no eligible employee for {iso_date} / {slot}.")
                return SolverResult(
                    solutions=[],
                    status="infeasible",
                    message="No eligible candidates for at least one shift.",
                    logs=logs,
                )
            slot_candidates[(iso_date, slot)] = candidates
            model.AddExactlyOne(
                [variables[(iso_date, slot, employee_id)] for employee_id in candidates]
            )

    # Hard rule: every day must include at least one ZNA doctor with
    # "Notaufnahme-Facharztstandard" across ZNA slots.
    facharzt_qual = "Notaufnahme-Facharztstandard"
    for iso_date in dates:
        fixed_has_facharzt = False
        facharzt_vars: list[cp_model.IntVar] = []
        for slot in SHIFT_SLOTS_ZNA:
            fixed_employee = fixed_assignments.get((iso_date, slot))
            if fixed_employee is not None:
                if facharzt_qual in employee_quals.get(int(fixed_employee), set()):
                    fixed_has_facharzt = True
                continue
            for employee_id in slot_candidates.get((iso_date, slot), []):
                if facharzt_qual in employee_quals.get(employee_id, set()):
                    facharzt_vars.append(variables[(iso_date, slot, employee_id)])
        if fixed_has_facharzt:
            continue
        if not facharzt_vars:
            log(
                "Infeasible day: no Notaufnahme-Facharztstandard coverage possible "
                f"for ZNA DR A/B on {iso_date}."
            )
            return SolverResult(
                solutions=[],
                status="infeasible",
                message=(
                    "At least one ZNA doctor per day must have "
                    "Notaufnahme-Facharztstandard."
                ),
                logs=logs,
            )
        model.Add(sum(facharzt_vars) >= 1)

    # Constraint scope:
    # include solver-managed employees plus anyone seen in base assignments.
    all_tracked_employees = set(solver_employee_ids)
    all_tracked_employees.update(employee_id for employee_id, _ in employee_day_base.keys())
    constrained_employees = {
        employee_id for employee_id in all_tracked_employees if employee_id not in zna_employee_ids
    }

    # Hard rule: at most one shift per employee per day.
    for employee_id in constrained_employees:
        for iso_date in dates:
            base = employee_day_base.get((employee_id, iso_date), 0)
            if base > 1:
                log(
                    f"Infeasible fixed data: employee {employee_id} already has more than one shift on {iso_date}."
                )
                return SolverResult(
                    solutions=[],
                    status="infeasible",
                    message="Fixed assignments violate one-shift-per-day rule.",
                    logs=logs,
                )
            day_vars = employee_day_vars.get((employee_id, iso_date), [])
            if day_vars:
                model.Add(sum(day_vars) + base <= 1)

    # Hard rule: no consecutive working days.
    for employee_id in constrained_employees:
        for idx in range(len(dates) - 1):
            d1 = dates[idx]
            d2 = dates[idx + 1]
            base1 = employee_day_base.get((employee_id, d1), 0)
            base2 = employee_day_base.get((employee_id, d2), 0)
            if base1 >= 1 and base2 >= 1:
                log(
                    f"Infeasible fixed data: employee {employee_id} works on consecutive days {d1}/{d2}."
                )
                return SolverResult(
                    solutions=[],
                    status="infeasible",
                    message="Fixed assignments violate no-consecutive-days rule.",
                    logs=logs,
                )
            expr1 = sum(employee_day_vars.get((employee_id, d1), [])) + base1
            expr2 = sum(employee_day_vars.get((employee_id, d2), [])) + base2
            model.Add(expr1 + expr2 <= 1)

    # Hard rule: configured incompatible employee pairs cannot work same day.
    for employee_a, employee_b in sorted(same_day_incompatible_pairs):
        for iso_date in dates:
            base_a = employee_day_base.get((employee_a, iso_date), 0)
            base_b = employee_day_base.get((employee_b, iso_date), 0)
            if base_a >= 1 and base_b >= 1:
                log(
                    "Infeasible fixed data: same-day incompatible employees "
                    f"{employee_a} and {employee_b} both work on {iso_date}."
                )
                return SolverResult(
                    solutions=[],
                    status="infeasible",
                    message="Fixed assignments violate same-day employee incompatibility.",
                    logs=logs,
                )
            expr_a = sum(employee_day_vars.get((employee_a, iso_date), [])) + base_a
            expr_b = sum(employee_day_vars.get((employee_b, iso_date), [])) + base_b
            model.Add(expr_a + expr_b <= 1)

    # Weekend load cap: no more than two worked weekends per employee.
    weekend_groups: dict[tuple[int, int], list[str]] = {}
    for iso_date in dates:
        if _weekday(iso_date) in (5, 6):
            weekend_groups.setdefault(_weekend_group_key(iso_date), []).append(iso_date)

    for employee_id in constrained_employees:
        worked_weekend_vars: list[cp_model.IntVar] = []
        for group_key, weekend_dates in weekend_groups.items():
            if not weekend_dates:
                continue
            weekend_var = model.NewBoolVar(
                f"weekend_work_{employee_id}_{group_key[0]}_{group_key[1]}"
            )
            for weekend_date in weekend_dates:
                day_expr = (
                    sum(employee_day_vars.get((employee_id, weekend_date), []))
                    + employee_day_base.get((employee_id, weekend_date), 0)
                )
                model.Add(day_expr <= weekend_var)
            worked_weekend_vars.append(weekend_var)
        if worked_weekend_vars:
            model.Add(sum(worked_weekend_vars) <= 2)

    # Soft rule helper: detect "work-rest-work" (single day gap) patterns.
    one_day_gap_vars: list[cp_model.IntVar] = []
    for employee_id in constrained_employees:
        for idx in range(len(dates) - 2):
            d1 = dates[idx]
            d3 = dates[idx + 2]
            day1_expr = sum(employee_day_vars.get((employee_id, d1), [])) + employee_day_base.get(
                (employee_id, d1), 0
            )
            day3_expr = sum(employee_day_vars.get((employee_id, d3), [])) + employee_day_base.get(
                (employee_id, d3), 0
            )
            gap_var = model.NewBoolVar(f"one_day_gap_{employee_id}_{d1}")
            model.Add(gap_var <= day1_expr)
            model.Add(gap_var <= day3_expr)
            model.Add(gap_var >= day1_expr + day3_expr - 1)
            one_day_gap_vars.append(gap_var)

    # Hard rule: per-employee monthly max shifts.
    for employee_id in solver_employee_ids:
        max_shifts = employee_max.get(employee_id, 0)
        base_count = employee_month_base.get(employee_id, 0)
        if base_count > max_shifts:
            log(
                f"Infeasible fixed data: employee {employee_id} exceeds max shifts ({base_count}>{max_shifts})."
            )
            return SolverResult(
                solutions=[],
                status="infeasible",
                message="Fixed assignments exceed max-shifts limits.",
                logs=logs,
            )
        model.Add(sum(employee_month_vars.get(employee_id, [])) + base_count <= max_shifts)

    # Clinic uniqueness: usually hard, optionally softened via excess variables.
    clinic_ids = {
        int(clinic_value)
        for clinic_value in employee_clinic_by_id.values()
        if clinic_value is not None
    }
    clinic_soft_terms: list[cp_model.IntVar] = []
    for iso_date in dates:
        weekday = _weekday(iso_date)
        if weekday in (4, 5):
            continue
        for clinic_value in clinic_ids:
            if clinic_value in zna_clinic_ids:
                continue
            base = clinic_day_base.get((clinic_value, iso_date), 0)
            vars_for_clinic: list[cp_model.IntVar] = []
            for slot in SHIFT_SLOT_CODES:
                for employee_id in slot_candidates.get((iso_date, slot), []):
                    if employee_clinic_by_id.get(employee_id) == clinic_value:
                        vars_for_clinic.append(variables[(iso_date, slot, employee_id)])
            if cfg.clinic_uniqueness_soft:
                upper = len(vars_for_clinic) + base
                if upper <= 1:
                    continue
                excess = model.NewIntVar(
                    0,
                    upper - 1,
                    f"clinic_excess_{clinic_value}_{iso_date}",
                )
                model.Add(excess >= sum(vars_for_clinic) + base - 1)
                clinic_soft_terms.append(excess)
            else:
                if base > 1:
                    log(
                        f"Infeasible fixed data: clinic {clinic_value} already duplicated on {iso_date}."
                    )
                    return SolverResult(
                        solutions=[],
                        status="infeasible",
                        message="Fixed assignments violate clinic uniqueness rule.",
                        logs=logs,
                    )
                if vars_for_clinic:
                    model.Add(sum(vars_for_clinic) + base <= 1)

    # Objective assembly starts with explicit preference handling.
    objective_terms: list[cp_model.LinearExpr] = []
    for (iso_date, _slot, employee_id), var in variables.items():
        pref = preferences.get((employee_id, iso_date))
        if pref == "prefer_off":
            objective_terms.append(cfg.prefer_off_penalty * var)
        elif pref == "prefer_work":
            objective_terms.append(-cfg.prefer_work_reward * var)

    # Fairness spread is modeled as max(total_shifts)-min(total_shifts).
    totals: list[cp_model.IntVar] = []
    spread_var: cp_model.IntVar | None = None
    max_total_bound = max((employee_max.get(eid, 0) for eid in solver_employee_ids), default=0)
    for employee_id in solver_employee_ids:
        base_count = employee_month_base.get(employee_id, 0)
        total_var = model.NewIntVar(0, max_total_bound, f"total_{employee_id}")
        model.Add(total_var == sum(employee_month_vars.get(employee_id, [])) + base_count)
        totals.append(total_var)

    if totals:
        max_total = model.NewIntVar(0, max_total_bound, "max_total")
        min_total = model.NewIntVar(0, max_total_bound, "min_total")
        model.AddMaxEquality(max_total, totals)
        model.AddMinEquality(min_total, totals)
        spread_var = model.NewIntVar(0, max_total_bound, "spread")
        model.Add(spread_var == max_total - min_total)
        max_spread_limit = max(0, int(cfg.max_fairness_spread))
        model.Add(spread_var <= max_spread_limit)
    if one_day_gap_vars:
        objective_terms.append(cfg.one_day_gap_penalty * sum(one_day_gap_vars))
    if cfg.clinic_uniqueness_soft and clinic_soft_terms:
        objective_terms.append(cfg.clinic_duplicate_penalty * sum(clinic_soft_terms))

    # Count fixed Haus/ZNA assignments so mix balancing reflects full month load.
    fixed_haus_count_by_employee: dict[int, int] = {
        employee_id: 0 for employee_id in solver_employee_ids
    }
    fixed_zna_count_by_employee: dict[int, int] = {
        employee_id: 0 for employee_id in solver_employee_ids
    }
    for (iso_date, slot), employee_id_raw in fixed_assignments.items():
        employee_id = int(employee_id_raw)
        if employee_id not in fixed_haus_count_by_employee:
            continue
        if slot in SHIFT_SLOTS_HAUSDienst:
            fixed_haus_count_by_employee[employee_id] += 1
        elif slot in SHIFT_SLOTS_ZNA:
            fixed_zna_count_by_employee[employee_id] += 1

    # Mix balancing penalizes dual-qualified employees who skew too much
    # into one slot family (Hausdienst vs ZNA).
    mix_deviation_vars: list[cp_model.IntVar] = []
    global_haus_slots = len(dates) * len(SHIFT_SLOTS_HAUSDienst)
    global_zna_slots = len(dates) * len(SHIFT_SLOTS_ZNA)
    mix_balance_weight = max(0, int(cfg.mix_balance_weight))
    if (
        mix_balance_weight > 0
        and dual_qualified_employee_ids
        and global_haus_slots > 0
        and global_zna_slots > 0
    ):
        max_mix_abs = max(1, max_total_bound)
        for employee_id in dual_qualified_employee_ids:
            haus_expr = (
                sum(
                    variables[(iso_date, slot, employee_id)]
                    for (iso_date, slot), candidates in slot_candidates.items()
                    if slot in SHIFT_SLOTS_HAUSDienst and employee_id in candidates
                )
                + fixed_haus_count_by_employee.get(employee_id, 0)
            )
            zna_expr = (
                sum(
                    variables[(iso_date, slot, employee_id)]
                    for (iso_date, slot), candidates in slot_candidates.items()
                    if slot in SHIFT_SLOTS_ZNA and employee_id in candidates
                )
                + fixed_zna_count_by_employee.get(employee_id, 0)
            )
            deviation = model.NewIntVar(
                0,
                max_mix_abs,
                f"mix_ratio_deviation_{employee_id}",
            )
            diff_expr = haus_expr - zna_expr
            model.Add(deviation >= diff_expr)
            model.Add(deviation >= -diff_expr)
            mix_deviation_vars.append(deviation)
        if mix_deviation_vars:
            objective_terms.append(mix_balance_weight * sum(mix_deviation_vars))
            log(
                "Mix balance objective enabled for "
                f"{len(mix_deviation_vars)} dual-qualified employees "
                "(proxy: minimize |haus_count-zna_count| per employee, "
                f"weight={mix_balance_weight})."
            )

    if objective_terms:
        model.Minimize(sum(objective_terms))

    log(
        f"Building model for {year:04d}-{month:02d} clinic {clinic_id}: "
        f"{len(slot_candidates)} open slots, {len(fixed_assignments)} fixed slots, "
        f"{len(active_employee_ids) - len(solver_employee_ids)} ZNA manual-only employees excluded."
    )

    # Solve repeatedly with different seeds and exclude previous solutions to
    # generate alternatives.
    solutions: list[SolverSolution] = []
    for attempt in range(cfg.max_solutions):
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = cfg.time_limit_seconds
        solver.parameters.num_search_workers = 8
        solver.parameters.random_seed = 100 + attempt
        status = solver.Solve(model)
        status_name = solver.StatusName(status)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            log(f"Solver finished on attempt {attempt + 1} with status {status_name}.")
            break

        # Reconstruct full assignment map: fixed rows + solver-selected rows.
        assignment_map: dict[tuple[str, str], int] = {}
        for key, employee_id in fixed_assignments.items():
            assignment_map[(str(key[0]), str(key[1]))] = int(employee_id)
        for (iso_date, slot), candidates in slot_candidates.items():
            for employee_id in candidates:
                if solver.Value(variables[(iso_date, slot, employee_id)]) == 1:
                    assignment_map[(iso_date, slot)] = employee_id
                    break

        # `shift_counts` includes base load, `assigned_shift_counts` counts only
        # assignments present in the final month map.
        shift_counts: dict[int, int] = {}
        for employee_id in solver_employee_ids:
            total = employee_month_base.get(employee_id, 0)
            for var in employee_month_vars.get(employee_id, []):
                total += solver.Value(var)
            shift_counts[employee_id] = total

        assigned_shift_counts: dict[int, int] = {}
        for employee_id in assignment_map.values():
            assigned_shift_counts[employee_id] = assigned_shift_counts.get(employee_id, 0) + 1
        assigned_employee_days: set[tuple[int, str]] = {
            (int(employee_id), str(iso_date))
            for (iso_date, _slot), employee_id in assignment_map.items()
        }

        prefer_work_total = 0
        prefer_work_honored = 0
        prefer_off_total = 0
        prefer_off_violated = 0
        employee_preference_stats: dict[int, dict[str, int]] = {}
        for key, pref in preferences.items():
            if not isinstance(key, tuple) or len(key) != 2:
                continue
            employee_id = int(key[0])
            iso_date = str(key[1])
            pref_code = str(pref)
            stats = employee_preference_stats.setdefault(
                employee_id,
                {
                    "prefer_work_total": 0,
                    "prefer_work_honored": 0,
                    "prefer_off_total": 0,
                    "prefer_off_violated": 0,
                },
            )
            assigned_to_employee = (employee_id, iso_date) in assigned_employee_days
            if pref_code == "prefer_work":
                prefer_work_total += 1
                stats["prefer_work_total"] += 1
                if assigned_to_employee:
                    prefer_work_honored += 1
                    stats["prefer_work_honored"] += 1
            elif pref_code == "prefer_off":
                prefer_off_total += 1
                stats["prefer_off_total"] += 1
                if assigned_to_employee:
                    prefer_off_violated += 1
                    stats["prefer_off_violated"] += 1

        for stats in employee_preference_stats.values():
            work_total = stats.get("prefer_work_total", 0)
            work_honored = stats.get("prefer_work_honored", 0)
            off_total = stats.get("prefer_off_total", 0)
            off_violated = stats.get("prefer_off_violated", 0)
            work_missed = work_total - work_honored
            off_honored = off_total - off_violated
            stats["prefer_work_missed"] = work_missed
            stats["prefer_off_honored"] = off_honored
            stats["preference_matches"] = work_honored + off_honored
            stats["preference_misses"] = work_missed + off_violated

        # Build a human-readable objective decomposition for UI/debugging.
        one_day_gap_count = sum(solver.Value(var) for var in one_day_gap_vars)
        clinic_duplicate_count = (
            sum(solver.Value(var) for var in clinic_soft_terms)
            if cfg.clinic_uniqueness_soft
            else 0
        )
        fairness_spread = solver.Value(spread_var) if spread_var is not None else 0
        prefer_off_penalty_total = prefer_off_violated * cfg.prefer_off_penalty
        prefer_work_reward_total = prefer_work_honored * cfg.prefer_work_reward
        mix_ratio_deviation = (
            sum(solver.Value(var) for var in mix_deviation_vars) if mix_deviation_vars else 0
        )
        mix_ratio_cost = mix_ratio_deviation * mix_balance_weight
        objective_breakdown = {
            "preference_penalty": prefer_off_penalty_total,
            "preference_reward": prefer_work_reward_total,
            "fairness_spread": fairness_spread,
            "fairness_cost": 0,
            "mix_ratio_target_haus_slots": global_haus_slots,
            "mix_ratio_target_zna_slots": global_zna_slots,
            "mix_ratio_deviation": mix_ratio_deviation,
            "mix_ratio_cost": mix_ratio_cost,
            "one_day_gap_count": one_day_gap_count,
            "one_day_gap_cost": one_day_gap_count * cfg.one_day_gap_penalty,
            "clinic_duplicate_count": clinic_duplicate_count,
            "clinic_duplicate_cost": clinic_duplicate_count
            * cfg.clinic_duplicate_penalty,
            "preference_net_cost": prefer_off_penalty_total - prefer_work_reward_total,
            "computed_objective": (
                prefer_off_penalty_total
                - prefer_work_reward_total
                + mix_ratio_cost
                + one_day_gap_count * cfg.one_day_gap_penalty
                + clinic_duplicate_count * cfg.clinic_duplicate_penalty
            ),
        }
        preference_summary = {
            "prefer_work_total": prefer_work_total,
            "prefer_work_honored": prefer_work_honored,
            "prefer_work_missed": prefer_work_total - prefer_work_honored,
            "prefer_off_total": prefer_off_total,
            "prefer_off_honored": prefer_off_total - prefer_off_violated,
            "prefer_off_violated": prefer_off_violated,
        }

        objective_value = int(round(solver.ObjectiveValue()))
        solutions.append(
            SolverSolution(
                assignments=assignment_map,
                objective_value=objective_value,
                shift_counts=shift_counts,
                assigned_shift_counts=assigned_shift_counts,
                objective_breakdown=objective_breakdown,
                preference_summary=preference_summary,
                employee_preference_stats=employee_preference_stats,
            )
        )
        log(
            f"Found solution {attempt + 1} with objective {objective_value} "
            f"({status_name})."
        )

        # Add a no-good cut to prevent returning the exact same assignment again.
        selected_literals: list[cp_model.IntVar] = []
        for (iso_date, slot), candidates in slot_candidates.items():
            for employee_id in candidates:
                var = variables[(iso_date, slot, employee_id)]
                if solver.Value(var) == 1:
                    selected_literals.append(var)
                    break
        if not selected_literals:
            break
        model.Add(sum(selected_literals) <= len(selected_literals) - 1)

    # If no solution is found, run an optional diagnostic pass with a relaxed
    # fairness spread to provide actionable feedback.
    if not solutions:
        if (
            not _skip_spread_diagnostic
            and cfg.max_fairness_spread >= 0
            and solver_employee_ids
        ):
            relaxed_spread_limit = max_total_bound
            if cfg.max_fairness_spread < relaxed_spread_limit:
                diagnostic_cfg = replace(
                    cfg,
                    max_fairness_spread=relaxed_spread_limit,
                    max_solutions=1,
                    time_limit_seconds=min(cfg.time_limit_seconds, 8.0),
                )
                diagnostic_result = solve_month_schedule(
                    year=year,
                    month=month,
                    clinic_id=clinic_id,
                    solver_input=solver_input,
                    config=diagnostic_cfg,
                    logger=None,
                    _skip_spread_diagnostic=True,
                )
                if diagnostic_result.solutions:
                    feasible_spread = int(
                        diagnostic_result.solutions[0].objective_breakdown.get(
                            "fairness_spread", relaxed_spread_limit
                        )
                    )
                    log(
                        "No solution under configured fairness spread limit; "
                        f"relaxed diagnostic found feasible spread {feasible_spread}."
                    )
                    return SolverResult(
                        solutions=[],
                        status="infeasible",
                        message=(
                            "No feasible solution with max fairness spread "
                            f"{cfg.max_fairness_spread}. "
                            f"A relaxed diagnostic run found feasibility at spread "
                            f"{feasible_spread}. Increase the max fairness spread."
                        ),
                        logs=logs,
                    )
        return SolverResult(
            solutions=[],
            status="infeasible",
            message="No feasible solution found for selected month and constraints.",
            logs=logs,
        )

    return SolverResult(
        solutions=solutions,
        status="ok",
        message=f"Generated {len(solutions)} solution(s).",
        logs=logs,
    )
