"""
optimizer.py – CP-SAT assignment engine
========================================
Produces a daily OT assignment draft that:
  • satisfies all hard constraints (X-ray / pregnancy, role rules, one-slot-per-person)
  • maximises a weighted objective: specialty match + fairness (min variance)

Fall-back: pure greedy if OR-Tools unavailable.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class StaffMember:
    id:         int
    name:       str
    role:       str           # Consultant | Specialist | Senior Trainee | Trainee
    pregnant:   bool
    specialties: List[str]    # lower-case list
    total_rooms: int = 0
    total_consults: int = 0
    last_consult_dates: List[str] = field(default_factory=list)


@dataclass
class RoomCase:
    room:        str
    surgery_type: str
    is_xray:     bool


@dataclass
class Assignment:
    room:         str
    surgery_type: str
    is_xray:      bool
    lead_id:      Optional[int]
    lead_name:    Optional[str]
    assistant_id: Optional[int]
    assistant_name: Optional[str]
    notes:        List[str] = field(default_factory=list)
    violations:   List[str] = field(default_factory=list)
    pref_match:   bool = False


@dataclass
class SolverResult:
    assignments:   List[Assignment]
    consultation_staff_id:   Optional[int]
    consultation_staff_name: Optional[str]
    consultation_notes:      str = ""
    feasible:      bool = True
    violations:    List[str] = field(default_factory=list)
    solver_used:   str = "ortools"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

ROLE_RANK = {
    "Consultant":     4,
    "Specialist":     3,
    "Senior Trainee": 2,
    "Trainee":        1,
}


def _specialty_match(staff: StaffMember, surgery_type: str, prefs: Dict) -> bool:
    stype_lower = surgery_type.lower()
    keywords = prefs.get(surgery_type, [stype_lower])
    return any(k in staff.specialties for k in keywords)


def _can_be_lead(staff: StaffMember) -> bool:
    """Lead must be Specialist, Senior Trainee (or Consultant as fallback)."""
    return staff.role in ("Specialist", "Senior Trainee", "Consultant")


def _eligible_for_consult(staff: StaffMember) -> bool:
    return staff.role in ("Specialist", "Senior Trainee")


# ─────────────────────────────────────────────────────────────────────────────
# OR-Tools CP-SAT solver
# ─────────────────────────────────────────────────────────────────────────────

def solve_ortools(
    staff: List[StaffMember],
    rooms: List[RoomCase],
    specialty_prefs: Dict,
    consult_criteria: str = "lowest_count",
) -> SolverResult:
    """
    Decision variables
    ------------------
    lead[r, s]      = 1  iff staff s is lead in room r
    assist[r, s]    = 1  iff staff s is assistant in room r
    consult[s]      = 1  iff staff s gets the consultation slot

    Hard constraints
    ----------------
    C1. Each room has exactly 1 lead.
    C2. Each room has 0 or 1 assistants (assistant optional but preferred).
    C3. Lead must be Specialist / Senior Trainee / Consultant (prefer non-Consultant).
    C4. Pregnant staff cannot be assigned to X-ray rooms.
    C5. Each staff member assigned to at most 1 slot total (lead + assist + consult ≤ 1).
    C6. Exactly 1 consultation slot assigned (to Specialist or Senior Trainee).

    Objective (maximise)
    --------------------
    +10 * specialty match for lead
    + 5 * specialty match for assistant
    -  1 * total_rooms for lead  (load spreading)
    -  2 * total_consults for consult  (prioritise those with fewer consults)
    """
    try:
        from ortools.sat.python import cp_model
    except ImportError:
        logger.warning("OR-Tools not available, falling back to greedy solver.")
        return solve_greedy(staff, rooms, specialty_prefs, consult_criteria)

    model = cp_model.CpModel()

    R = len(rooms)
    S = len(staff)
    si = list(range(S))
    ri = list(range(R))

    # ── Variable creation ────────────────────────────────────────────────────
    lead   = [[model.NewBoolVar(f"lead_r{r}_s{s}")   for s in si] for r in ri]
    assist = [[model.NewBoolVar(f"assist_r{r}_s{s}") for s in si] for r in ri]
    consult = [model.NewBoolVar(f"consult_s{s}") for s in si]

    # ── C1: exactly 1 lead per room ──────────────────────────────────────────
    for r in ri:
        model.Add(sum(lead[r][s] for s in si) == 1)

    # ── C2: at most 1 assistant per room ────────────────────────────────────
    for r in ri:
        model.Add(sum(assist[r][s] for s in si) <= 1)

    # ── C3: lead role constraint ─────────────────────────────────────────────
    for r in ri:
        for s in si:
            if not _can_be_lead(staff[s]):
                model.Add(lead[r][s] == 0)

    # ── C4: pregnant → no X-ray rooms ────────────────────────────────────────
    for r, room in enumerate(rooms):
        if room.is_xray:
            for s, m in enumerate(staff):
                if m.pregnant:
                    model.Add(lead[r][s]   == 0)
                    model.Add(assist[r][s] == 0)

    # ── C5: each person assigned at most once ─────────────────────────────────
    for s in si:
        model.Add(
            sum(lead[r][s]   for r in ri) +
            sum(assist[r][s] for r in ri) +
            consult[s]
            <= 1
        )

    # ── C6: exactly 1 consultation ───────────────────────────────────────────
    eligible_consult = [s for s, m in enumerate(staff) if _eligible_for_consult(m)]
    if eligible_consult:
        model.Add(sum(consult[s] for s in eligible_consult) == 1)
        for s in si:
            if s not in eligible_consult:
                model.Add(consult[s] == 0)
    else:
        for s in si:
            model.Add(consult[s] == 0)

    # ── Prevent lead + assistant being the same person in same room ──────────
    for r in ri:
        for s in si:
            model.Add(lead[r][s] + assist[r][s] <= 1)

    # ── Objective ─────────────────────────────────────────────────────────────
    objective_terms = []

    SCALE = 1000  # scale floats to ints for CP-SAT

    for r, room in enumerate(rooms):
        for s, m in enumerate(staff):
            # Specialty match bonus
            match = _specialty_match(m, room.surgery_type, specialty_prefs)
            match_score = 10 if match else 0

            # Prefer non-Consultant as lead (so Consultant load stays low)
            non_cons_bonus = 5 if m.role != "Consultant" else 0

            # Load-spreading penalty (favour those with fewer rooms)
            load_penalty = min(m.total_rooms, 50)   # cap at 50

            lead_score   = (match_score + non_cons_bonus - load_penalty) * SCALE
            assist_score = (5 if match else 0)       * SCALE

            objective_terms.append(lead_score   * lead[r][s])
            objective_terms.append(assist_score * assist[r][s])

    for s, m in enumerate(staff):
        # For consultation: prefer lowest consult count (or not assigned last 3 days)
        if consult_criteria == "lowest_count":
            consult_score = (100 - min(m.total_consults * 5, 95)) * SCALE
        else:
            # "not_last_3_days" – give high score if not recently consulted
            from datetime import date, timedelta
            recent = {str(date.today() - timedelta(days=i)) for i in range(1, 4)}
            recently_consulted = bool(set(m.last_consult_dates[-3:]) & recent)
            consult_score = (0 if recently_consulted else 100) * SCALE
        objective_terms.append(consult_score * consult[s])

    model.Maximize(sum(objective_terms))

    # ── Solve ─────────────────────────────────────────────────────────────────
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    solver.parameters.num_search_workers  = 4
    status = solver.Solve(model)

    feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)

    if not feasible:
        logger.warning("CP-SAT: no feasible solution found, falling back to greedy.")
        result = solve_greedy(staff, rooms, specialty_prefs, consult_criteria)
        result.feasible = False
        result.violations.append("⚠ CP-SAT could not find a fully feasible solution. Greedy fallback used.")
        return result

    # ── Extract solution ──────────────────────────────────────────────────────
    assignments = []
    used_staff_ids = set()

    for r, room in enumerate(rooms):
        lead_idx   = next((s for s in si if solver.Value(lead[r][s])   == 1), None)
        assist_idx = next((s for s in si if solver.Value(assist[r][s]) == 1), None)

        lead_m   = staff[lead_idx]   if lead_idx   is not None else None
        assist_m = staff[assist_idx] if assist_idx is not None else None

        notes = []
        violations = []
        pref_match = False

        if lead_m:
            used_staff_ids.add(lead_m.id)
            if _specialty_match(lead_m, room.surgery_type, specialty_prefs):
                pref_match = True
                notes.append(f"✓ Specialty match: {lead_m.name}")
            if lead_m.role == "Consultant":
                notes.append("⚑ Consultant as lead (sub-optimal)")
        else:
            violations.append("✗ No lead assigned")

        if assist_m:
            used_staff_ids.add(assist_m.id)

        assignments.append(Assignment(
            room=room.room,
            surgery_type=room.surgery_type,
            is_xray=room.is_xray,
            lead_id=lead_m.id if lead_m else None,
            lead_name=lead_m.name if lead_m else "— UNASSIGNED —",
            assistant_id=assist_m.id if assist_m else None,
            assistant_name=assist_m.name if assist_m else "",
            notes=notes,
            violations=violations,
            pref_match=pref_match,
        ))

    # ── Consultation ──────────────────────────────────────────────────────────
    consult_idx = next((s for s in si if solver.Value(consult[s]) == 1), None)
    consult_m   = staff[consult_idx] if consult_idx is not None else None

    return SolverResult(
        assignments=assignments,
        consultation_staff_id=consult_m.id if consult_m else None,
        consultation_staff_name=consult_m.name if consult_m else "— UNASSIGNED —",
        consultation_notes="Lowest consultation count" if consult_criteria == "lowest_count" else "Not assigned last 3 days",
        feasible=True,
        solver_used="CP-SAT (OR-Tools)",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Greedy fallback
# ─────────────────────────────────────────────────────────────────────────────

def solve_greedy(
    staff: List[StaffMember],
    rooms: List[RoomCase],
    specialty_prefs: Dict,
    consult_criteria: str = "lowest_count",
) -> SolverResult:
    """Simple greedy: pick best available staff for each room in order."""
    used = set()
    assignments = []

    def score_lead(m: StaffMember, room: RoomCase) -> int:
        s = 0
        if _specialty_match(m, room.surgery_type, specialty_prefs): s += 10
        if m.role != "Consultant": s += 5
        s -= min(m.total_rooms, 50)
        return s

    def score_assist(m: StaffMember, room: RoomCase) -> int:
        s = 0
        if _specialty_match(m, room.surgery_type, specialty_prefs): s += 5
        s -= min(m.total_rooms, 50)
        return s

    for room in rooms:
        # Lead candidates
        leads = [
            m for m in staff
            if m.id not in used
            and _can_be_lead(m)
            and not (m.pregnant and room.is_xray)
        ]
        leads.sort(key=lambda m: score_lead(m, room), reverse=True)
        lead_m = leads[0] if leads else None

        if lead_m:
            used.add(lead_m.id)

        # Assistant candidates
        assists = [
            m for m in staff
            if m.id not in used
            and not (m.pregnant and room.is_xray)
            and ROLE_RANK.get(m.role, 0) <= ROLE_RANK.get(lead_m.role if lead_m else "Trainee", 3)
        ]
        assists.sort(key=lambda m: score_assist(m, room), reverse=True)
        assist_m = assists[0] if assists else None
        if assist_m:
            used.add(assist_m.id)

        pref = _specialty_match(lead_m, room.surgery_type, specialty_prefs) if lead_m else False
        assignments.append(Assignment(
            room=room.room,
            surgery_type=room.surgery_type,
            is_xray=room.is_xray,
            lead_id=lead_m.id if lead_m else None,
            lead_name=lead_m.name if lead_m else "— UNASSIGNED —",
            assistant_id=assist_m.id if assist_m else None,
            assistant_name=assist_m.name if assist_m else "",
            violations=[] if lead_m else ["✗ No lead assigned"],
            pref_match=pref,
        ))

    # Consultation
    candidates = [m for m in staff if m.id not in used and _eligible_for_consult(m)]
    if consult_criteria == "lowest_count":
        candidates.sort(key=lambda m: m.total_consults)
    else:
        from datetime import date, timedelta
        recent = {str(date.today() - timedelta(days=i)) for i in range(1, 4)}
        candidates.sort(key=lambda m: (bool(set(m.last_consult_dates[-3:]) & recent), m.total_consults))

    consult_m = candidates[0] if candidates else None

    return SolverResult(
        assignments=assignments,
        consultation_staff_id=consult_m.id if consult_m else None,
        consultation_staff_name=consult_m.name if consult_m else "— UNASSIGNED —",
        consultation_notes="Greedy fallback",
        feasible=True,
        solver_used="Greedy",
    )
