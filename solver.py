"""
CP-SAT automatic monthly schedule solver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from ortools.sat.python import cp_model

from database import SHIFT_SLOT_CODES, qualification_names_for_shift_slot


@dataclass(frozen=True)
class SolverConfig:
    max_solutions: int = 5
    time_limit_seconds: float = 20.0
    prefer_off_penalty: int = 8
    prefer_work_reward: int = 3
    imbalance_weight: int = 6
    clinic_uniqueness_soft: bool = False
    clinic_duplicate_penalty: int = 20


@dataclass(frozen=True)
class SolverSolution:
    assignments: dict[tuple[str, str], int]
    objective_value: int
    shift_counts: dict[int, int]


@dataclass(frozen=True)
class SolverResult:
    solutions: list[SolverSolution]
    status: str
    message: str
    logs: list[str] = field(default_factory=list)


def _weekday(iso_date: str) -> int:
    return date.fromisoformat(iso_date).weekday()


def solve_month_schedule(
    *,
    year: int,
    month: int,
    clinic_id: int,
    solver_input: dict[str, object],
    config: SolverConfig | None = None,
    logger: Callable[[str], None] | None = None,
) -> SolverResult:
    cfg = config or SolverConfig()
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(message)
        if logger is not None:
            logger(message)

    dates = list(solver_input.get("dates", []))
    employees_raw = list(solver_input.get("employees", []))
    fixed_assignments = dict(solver_input.get("fixed_assignments", {}))
    external_assignments_by_date = dict(
        solver_input.get("external_assignments_by_date", {})
    )
    absences_by_employee = dict(solver_input.get("absences_by_employee", {}))
    preferences = dict(solver_input.get("preferences", {}))
    employee_clinic_by_id = dict(solver_input.get("employee_clinic_by_id", {}))

    active_employee_ids: list[int] = []
    solver_employee_ids: list[int] = []
    employee_max: dict[int, int] = {}
    employee_quals: dict[int, set[str]] = {}
    for item in employees_raw:
        if not isinstance(item, dict):
            continue
        employee_id = int(item["id"])
        active_employee_ids.append(employee_id)
        if str(item.get("clinic_code", "")) != "zna":
            solver_employee_ids.append(employee_id)
        employee_max[employee_id] = int(item["max_shifts_per_month"])
        employee_clinic_by_id.setdefault(employee_id, int(item["clinic_id"]))
        quals = item.get("qualifications", set())
        if isinstance(quals, set):
            employee_quals[employee_id] = {str(q) for q in quals}
        else:
            employee_quals[employee_id] = {str(q) for q in quals}

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

    for (iso_date, slot), employee_id in fixed_assignments.items():
        eid = int(employee_id)
        employee_day_base[(eid, iso_date)] = employee_day_base.get((eid, iso_date), 0) + 1
        if eid in employee_month_base:
            employee_month_base[eid] += 1
        clinic_for_employee = employee_clinic_by_id.get(eid)
        if clinic_for_employee is not None:
            key = (int(clinic_for_employee), iso_date)
            clinic_day_base[key] = clinic_day_base.get(key, 0) + 1
        if eid in absences_by_employee and iso_date in absences_by_employee[eid]:
            log(
                f"Infeasible fixed assignment: employee {eid} is absent on {iso_date} for {slot}."
            )
            return SolverResult(
                solutions=[],
                status="infeasible",
                message="Fixed assignments conflict with absences.",
                logs=logs,
            )

    for iso_date in dates:
        for slot in SHIFT_SLOT_CODES:
            fixed_employee = fixed_assignments.get((iso_date, slot))
            if fixed_employee is not None:
                continue
            required_quals = set(qualification_names_for_shift_slot(slot))
            candidates: list[int] = []
            for employee_id in solver_employee_ids:
                if required_quals and not (employee_quals.get(employee_id, set()) & required_quals):
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

    all_tracked_employees = set(solver_employee_ids)
    all_tracked_employees.update(employee_id for employee_id, _ in employee_day_base.keys())

    for employee_id in all_tracked_employees:
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

    for employee_id in all_tracked_employees:
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

    objective_terms: list[cp_model.LinearExpr] = []
    for (iso_date, _slot, employee_id), var in variables.items():
        pref = preferences.get((employee_id, iso_date))
        if pref == "prefer_off":
            objective_terms.append(cfg.prefer_off_penalty * var)
        elif pref == "prefer_work":
            objective_terms.append(-cfg.prefer_work_reward * var)

    totals: list[cp_model.IntVar] = []
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
        spread = model.NewIntVar(0, max_total_bound, "spread")
        model.Add(spread == max_total - min_total)
        objective_terms.append(cfg.imbalance_weight * spread)
    if cfg.clinic_uniqueness_soft and clinic_soft_terms:
        objective_terms.append(cfg.clinic_duplicate_penalty * sum(clinic_soft_terms))

    if objective_terms:
        model.Minimize(sum(objective_terms))

    log(
        f"Building model for {year:04d}-{month:02d} clinic {clinic_id}: "
        f"{len(slot_candidates)} open slots, {len(fixed_assignments)} fixed slots, "
        f"{len(active_employee_ids) - len(solver_employee_ids)} ZNA manual-only employees excluded."
    )

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

        assignment_map: dict[tuple[str, str], int] = {}
        for key, employee_id in fixed_assignments.items():
            assignment_map[(str(key[0]), str(key[1]))] = int(employee_id)
        for (iso_date, slot), candidates in slot_candidates.items():
            for employee_id in candidates:
                if solver.Value(variables[(iso_date, slot, employee_id)]) == 1:
                    assignment_map[(iso_date, slot)] = employee_id
                    break

        shift_counts: dict[int, int] = {}
        for employee_id in solver_employee_ids:
            total = employee_month_base.get(employee_id, 0)
            for var in employee_month_vars.get(employee_id, []):
                total += solver.Value(var)
            shift_counts[employee_id] = total

        objective_value = int(round(solver.ObjectiveValue()))
        solutions.append(
            SolverSolution(
                assignments=assignment_map,
                objective_value=objective_value,
                shift_counts=shift_counts,
            )
        )
        log(
            f"Found solution {attempt + 1} with objective {objective_value} "
            f"({status_name})."
        )

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

    if not solutions:
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
