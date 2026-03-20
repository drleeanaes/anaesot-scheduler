"""
pages/scheduler.py – Daily OT Scheduler
=========================================
AM/PM split per room. AM Emergency Team displayed but not assigned.
PM PAAC colleagues listed, not assigned to OT. EOT left blank.
Reshuffle button re-runs optimiser with a new random seed.
"""
import json, io, random
from datetime import date, datetime
from dataclasses import dataclass, field
from typing import Optional
import streamlit as st
import pandas as pd
from database import (
    SessionLocal, Staff, StaffStats, Schedule, ScheduleAssignment,
    get_setting, upsert_stats_after_publish,
)
from optimizer import StaffMember, RoomCase, solve_ortools
from pages.rooms import get_room_special_types, SPECIAL_TYPE_LABELS

# ─────────────────────────────────────────────────────────────────────────────
# Weekly surgery schedule — Mon to Fri (from theatre table)
# Trauma and Emergency are always appended as fixed extra slots every weekday
# C7-OBS is always Obstetric AM only on weekdays
# ─────────────────────────────────────────────────────────────────────────────
WEEKLY_SCHEDULE = {
    # Each day: "am" and "pm" are ORDERED lists with one entry per room/list.
    # Repeated entries = multiple rooms running that surgery type.
    # Trauma and Emergency are always appended as fixed extra slots.
    # Obstetric (C7-OBS) runs whole day Mon / Wed / Fri only.
    "Monday": {
        "am": [
            "Orthopaedic",      # C11-OT1 (TJR)
            "Gynaecology",      # C11-OT2
            "Gynaecology",      # C11-OT3
            "ENT",              # C10-OT1
            "ENT",              # C10-OT2
            "Ophthalmology",    # C8-OT2
            "Upper GI",         # C8-OT3
            "Vascular",         # C6-OT1
            "General Surgery",  # C6-OT2
        ],
        "pm": [
            "Orthopaedic",
            "Gynaecology",
            "Gynaecology",
            "ENT",
            "ENT",
            "Ophthalmology",
            "Upper GI",
            "Vascular",
            "General Surgery",
        ],
        "obs": True,
    },
    "Tuesday": {
        "am": [
            "Neurosurgery",     # C11-OT1
            "Orthopaedic",      # C11-OT2
            "Orthopaedic",      # C11-OT3
            "General Surgery",  # C10-OT2 (mixed teams)
            "General Surgery",  # C10-OT3 (purple)
            "Upper GI",         # C10-OT2 sub-list
            "Colorectal",       # C8-OT3
            "Ophthalmology",    # C8-OT2
            "Vascular",         # C6-OT2
        ],
        "pm": [
            "Neurosurgery",
            "Orthopaedic",
            "Orthopaedic",
            "General Surgery",
            "General Surgery",
            "Hepatobiliary",    # C10-OT2 switches from Upper GI to Hepatobiliary PM
            "Colorectal",
            "Ophthalmology",
            "Vascular",
        ],
        "obs": False,
    },
    "Wednesday": {
        "am": [
            "Orthopaedic",      # C11-OT1 (TJR)
            "Orthopaedic",      # C11-OT2
            "Orthopaedic",      # C10-OT2
            "Gynaecology",      # C11-OT3
            "Urology",          # C10-OT1
            "Urology",          # C10-OT3
            "Ophthalmology",    # C8-OT2
            "Ophthalmology",    # C8-OT3 (am)
            "General Surgery",  # C8-OT1 (am trauma list)
            "OMFS",             # C6-OT2
            "Hepatobiliary",    # listed AM
        ],
        "pm": [
            "Orthopaedic",
            "Orthopaedic",
            "Orthopaedic",
            "Gynaecology",
            "Urology",
            "Urology",
            "Ophthalmology",    # C8-OT2
            "Hepatobiliary",    # C8-OT3 switches to Hepatobiliary PM
            "OMFS",
        ],
        "obs": True,
    },
    "Thursday": {
        "am": [
            "Neurosurgery",     # C11-OT1
            "Orthopaedic",      # C11-OT2
            "Orthopaedic",      # C11-OT3
            "ENT",              # C10-OT1
            "ENT",              # C10-OT2
            "Colorectal",       # C10-OT3
            "Hepatobiliary",    # C8-OT3
            "General Surgery",  # C6-OT2
        ],
        "pm": [
            "Neurosurgery",
            "Orthopaedic",
            "Orthopaedic",
            "ENT",
            "ENT",
            "Colorectal",
            "Hepatobiliary",
            "General Surgery",
        ],
        "obs": False,
    },
    "Friday": {
        "am": [
            "Orthopaedic",      # C11-OT1 (TJR)
            "Orthopaedic",      # C11-OT2
            "Orthopaedic",      # C10-OT2 (whole day)
            "Gynaecology",      # C11-OT3
            "General Surgery",  # C10-OT1
            "Urology",          # C10-OT3
            "Urology",          # C8-OT3
            "Ophthalmology",    # C8-OT2
        ],
        "pm": [
            "Orthopaedic",      # C11-OT1
            "Orthopaedic",      # C11-OT2
            "Orthopaedic",      # C10-OT2 (whole day — corrected)
            "Gynaecology",
            "General Surgery",
            "Urology",
            "Urology",
            "Ophthalmology",
        ],
        "obs": True,
    },
}
# Always added as fixed extra slots every weekday
FIXED_EXTRA_SLOTS = ["Trauma", "Emergency"]
# The OBS room name — only active Mon/Wed/Fri
OBS_ROOM = "C7-OBS"
# Days when OBS runs
OBS_DAYS = {"Monday", "Wednesday", "Friday"}


# ─────────────────────────────────────────────────────────────────────────────
# Per-colleague availability for a specific date
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ColleagueDay:
    staff:      StaffMember
    am_avail:   bool   # can work AM OT
    pm_avail:   bool   # can work PM OT
    am_call:    bool   # on AM emergency team
    pm_paac:    bool   # has pm PAAC
    am_blocked: bool
    pm_blocked: bool


def _get_day_availability(target_date: date) -> list:
    """Return ColleagueDay list for everyone available on target_date."""
    from pages.staff import (
        get_ot_dates, get_am_call_dates, get_am_ot_dates, get_pm_paac_dates,
        get_am_blocked_dates, get_pm_blocked_dates, get_real_specialties,
    )
    try:
        from pages.staff import _parse_meta
        def _get_dates(obj, key):
            meta = _parse_meta(obj)
            raw = json.loads(meta.get(key, "[]"))
            return [date.fromisoformat(d) for d in raw]
    except Exception:
        def _get_dates(obj, key): return []

    day_col = f"ot_{target_date.strftime('%a').lower()}"

    with SessionLocal() as s:
        rows = (s.query(Staff, StaffStats)
                 .outerjoin(StaffStats, Staff.id == StaffStats.staff_id)
                 .filter(Staff.active == True).all())
        result = []
        for m, stat in rows:
            ot_dates = get_ot_dates(m)
            # Check if available at all for this date
            if ot_dates:
                if target_date not in ot_dates:
                    continue
            else:
                if not getattr(m, day_col, False):
                    continue

            specs = get_real_specialties(m)
            sm = StaffMember(
                id=m.id, name=m.name, role=m.role, pregnant=m.pregnant,
                specialties=[x.strip().lower() for x in specs.split(",") if x.strip()],
                total_rooms=stat.total_rooms if stat else 0,
                total_consults=stat.total_consultations if stat else 0,
                last_consult_dates=json.loads(stat.consult_dates_json or "[]") if stat else [],
            )

            am_call    = target_date in get_am_call_dates(m)
            pm_paac    = target_date in get_pm_paac_dates(m)
            am_blocked = target_date in get_am_blocked_dates(m)
            pm_blocked = target_date in get_pm_blocked_dates(m) or pm_paac

            # Determine session availability
            # For pm_paac colleagues: am_avail requires explicit am_ot entry
            # (just having pm_paac with no AM entry does NOT make them AM available)
            if am_call:
                am_avail = True
                pm_avail = not pm_blocked
            elif pm_paac:
                # pm PAAC colleague: only AM available if they have an explicit am OT entry
                am_ot = target_date in get_am_ot_dates(m)
                am_avail = am_ot and not am_blocked
                pm_avail = False  # pm PAAC always blocks PM
            else:
                am_avail = not am_blocked
                pm_avail = not pm_blocked

            result.append(ColleagueDay(
                staff=sm,
                am_avail=am_avail,
                pm_avail=pm_avail,
                am_call=am_call,
                pm_paac=pm_paac,
                am_blocked=am_blocked,
                pm_blocked=pm_blocked,
            ))
    return result


def _get_coordinator_for_date(target_date, all_staff_db):
    from pages.staff import get_coordinator_dates
    for m in all_staff_db:
        if m.role != "Consultant": continue
        if target_date in get_coordinator_dates(m):
            return m
    return None


def _all_rooms():
    cfg = json.loads(get_setting("rooms_config", "{}"))
    return [r for rooms in cfg.values() for r in rooms]


def _surgery_types():
    return json.loads(get_setting("surgery_types", '["General"]'))


def _staff_opts(pool, include_blank=True):
    names = [cd.staff.name for cd in pool]
    return (["— Unassigned —"] + names) if include_blank else names


def _cd_by_name(pool, name) -> Optional[ColleagueDay]:
    return next((cd for cd in pool if cd.staff.name == name), None)


def _staff_by_name(pool, name) -> Optional[StaffMember]:
    cd = _cd_by_name(pool, name)
    return cd.staff if cd else None


def _all_assigned_ids(manual_assign, consult_name, all_day) -> set:
    used = set()
    for a in manual_assign.values():
        for slot in ("am_lead","am_asst","pm_lead","pm_asst"):
            cd = _cd_by_name(all_day, a.get(slot, ""))
            if cd: used.add(cd.staff.id)
    cd = _cd_by_name(all_day, consult_name)
    if cd: used.add(cd.staff.id)
    return used


def _check_constraints(manual_assign, all_day, room_xray, coordinator_name, special_types):
    """
    Constraint rules:
    - Lead must be Specialist or Consultant (never Trainee/Senior Trainee)
    - Assistant must NOT be Specialist or Consultant
    - Same person can work AM + PM in the SAME room (not a violation)
    - Same person cannot be in two DIFFERENT rooms in the same session
    - Pregnant staff cannot be in X-ray rooms
    - Day Coordinator cannot be assigned room duty
    - AM call team cannot be assigned rooms
    - PM PAAC colleagues cannot work PM slots
    - One specialist may lead two rooms — allowed (no violation)
    """
    violations = []

    # Track cross-room duplicates per session separately
    # Key: (staff_id, session) → "room/slot"  — same room AM+PM is fine
    am_used = {}  # staff_id → room (AM session)
    pm_used = {}  # staff_id → room (PM session)

    for room, a in manual_assign.items():
        stype = special_types.get(room, "normal")
        if stype == "eot":
            continue

        for slot_key, slot_label, session in [
            ("am_lead", "AM Lead", "AM"),
            ("am_asst", "AM Asst", "AM"),
            ("pm_lead", "PM Lead", "PM"),
            ("pm_asst", "PM Asst", "PM"),
        ]:
            name = a.get(slot_key, "")
            cd = _cd_by_name(all_day, name)
            if not cd:
                if slot_key in ("am_lead", "pm_lead"):
                    violations.append(f"{room} {slot_label}: No lead assigned.")
                continue

            m = cd.staff

            # Session availability
            avail = cd.am_avail if session == "AM" else cd.pm_avail
            if not avail:
                violations.append(f"{room} {slot_label}: {m.name} not available {session}.")

            # Role rules
            if slot_key in ("am_lead", "pm_lead") and stype != "trauma":
                if m.role not in ("Specialist", "Consultant"):
                    violations.append(
                        f"{room} {slot_label}: {m.name} is {m.role} — Lead must be Specialist or Consultant.")
            if slot_key in ("am_asst", "pm_asst"):
                if m.role in ("Specialist", "Consultant"):
                    violations.append(
                        f"{room} {slot_label}: {m.name} is {m.role} — Specialists/Consultants should be leads, not assistants.")

            # X-ray + pregnancy
            if m.pregnant and room_xray.get(room, False):
                violations.append(f"{room}: {m.name} is pregnant — cannot be in X-ray room.")

            # Coordinator
            if m.name == coordinator_name:
                violations.append(f"{room}: {m.name} is Day Coordinator — should not have room duty.")

            # AM call
            if cd.am_call:
                violations.append(f"{room} {slot_label}: {m.name} is AM Emergency Team — cannot be assigned.")

            # PM PAAC
            if cd.pm_paac and session == "PM":
                violations.append(f"{room}: {m.name} has pm PAAC — cannot work PM.")

            # Cross-room duplicate check (same person, same session, different room)
            used_dict = am_used if session == "AM" else pm_used
            if m.id in used_dict and used_dict[m.id] != room:
                # Allow one specialist/consultant to lead two rooms (by design)
                if slot_key in ("am_lead", "pm_lead") and m.role in ("Specialist", "Consultant"):
                    pass  # Intentional — leading two rooms is permitted
                else:
                    violations.append(
                        f"{room} {slot_label}: {m.name} already assigned to {used_dict[m.id]} ({session}).")
            else:
                used_dict[m.id] = room

            # Within same room: same person cannot be both lead and assistant in same session
            am_lead_name = a.get("am_lead", "")
            am_asst_name = a.get("am_asst", "")
            pm_lead_name = a.get("pm_lead", "")
            pm_asst_name = a.get("pm_asst", "")
            if session == "AM" and am_lead_name == am_asst_name and am_lead_name not in ("", "— Unassigned —"):
                if slot_key == "am_asst":
                    violations.append(f"{room}: Same person ({name}) cannot be both AM Lead and AM Assistant.")
            if session == "PM" and pm_lead_name == pm_asst_name and pm_lead_name not in ("", "— Unassigned —"):
                if slot_key == "pm_asst":
                    violations.append(f"{room}: Same person ({name}) cannot be both PM Lead and PM Assistant.")

    return violations


# ─────────────────────────────────────────────────────────────────────────────
# Draft generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_draft(all_day, rooms, coordinator_name, specialty_prefs, consult_criteria,
                    seed=0, target_date=None, xray_types=None):
    """
    Build draft assignments respecting AM/PM availability and per-day surgery types.
    xray_types: dict {surgery_type: bool} — pregnant staff excluded from True entries.
    Returns manual_assign dict and consult name.
    """
    if xray_types is None:
        xray_types = {}
    special_types = get_room_special_types()

    # Pools — exclude coordinator and am_call (emergency team)
    # pm_paac colleagues: allowed in AM pool only (their PM is blocked)
    base = [cd for cd in all_day
            if cd.staff.name != coordinator_name
            and not cd.am_call]

    am_pool = [cd for cd in base if cd.am_avail]
    pm_pool = [cd for cd in base if cd.pm_avail and not cd.pm_paac]

    # Shuffle for reshuffle button
    rng = random.Random(seed)
    rng.shuffle(am_pool)
    rng.shuffle(pm_pool)

    ma = {}
    am_used = set()
    pm_used = set()

    for room in rooms:
        stype_room = special_types.get(room, "normal")
        if stype_room == "eot":
            ma[room] = {"am_lead":"— Unassigned —","am_asst":"— Unassigned —",
                        "pm_lead":"— Unassigned —","pm_asst":"— Unassigned —",
                        "_note":"🔴 EOT — assign manually", "_xray": False}
            continue

        # C7-OBS: only scheduled Mon/Wed/Fri
        if room == OBS_ROOM:
            weekday = target_date.strftime("%A") if target_date else "Monday"
            if weekday not in OBS_DAYS:
                ma[room] = {"am_lead":"— Unassigned —","am_asst":"— Unassigned —",
                            "pm_lead":"— Unassigned —","pm_asst":"— Unassigned —",
                            "_note":"⬜ Obstetric not scheduled today", "_xray": False}
                continue

        if stype_room == "trauma":
            # AM: pick Senior Trainee solo
            am_lead = _pick_solo(am_pool, am_used, coordinator_name, prefer_role="Senior Trainee")
            pm_lead = _pick_solo(pm_pool, pm_used, coordinator_name, prefer_role="Senior Trainee")
            ma[room] = {
                "am_lead": am_lead, "am_asst": "— Unassigned —",
                "pm_lead": pm_lead, "pm_asst": "— Unassigned —",
                "_note": "🟡 Trauma — Senior Trainee solo", "_xray": False,
            }
            continue

        # Normal room: AM lead + optional asst, PM lead + optional asst
        weekday      = target_date.strftime("%A") if target_date else "Monday"
        day_sched    = WEEKLY_SCHEDULE.get(weekday, {"am":[], "pm":[]})
        stype_str    = st.session_state.room_stype.get(room, "General Surgery")
        pm_stype_str = stype_str

        # X-ray flag: check if this surgery type has ANY instance marked as X-ray
        # Keys are type_idx (e.g. "Orthopaedic_1", "Orthopaedic_2")
        # A room is X-ray if at least one instance of its type is ticked
        room_is_xray = any(
            v for k, v in xray_types.items()
            if k.rsplit("_", 1)[0] == stype_str
        )
        am_pool_use  = [cd for cd in am_pool if not (room_is_xray and cd.staff.pregnant)]
        pm_pool_use  = [cd for cd in pm_pool if not (room_is_xray and cd.staff.pregnant)]

        # Pick AM lead (Specialist/Consultant only)
        am_lead_cd = _pick_lead(am_pool_use, am_used, coordinator_name, stype_str, specialty_prefs)
        am_lead    = am_lead_cd.staff.name if am_lead_cd else "— Unassigned —"

        # Pick AM assistant (Senior Trainee/Trainee only) — optional
        am_asst_cd = _pick_asst(am_pool_use, am_used, coordinator_name)
        am_asst    = am_asst_cd.staff.name if am_asst_cd else "— Unassigned —"

        # PM Lead: prefer same person as AM lead for continuity in same room
        pm_lead_cd = None
        if am_lead_cd:
            same_in_pm = next((cd for cd in pm_pool_use
                               if cd.staff.id == am_lead_cd.staff.id
                               and cd.staff.id not in pm_used), None)
            if same_in_pm:
                pm_lead_cd = same_in_pm
                pm_used.add(same_in_pm.staff.id)
        if pm_lead_cd is None:
            pm_lead_cd = _pick_lead(pm_pool_use, pm_used, coordinator_name, pm_stype_str, specialty_prefs)
        pm_lead = pm_lead_cd.staff.name if pm_lead_cd else "— Unassigned —"

        # PM Assistant: reuse AM asst if PM available, else leave blank (second pass fills surplus)
        pm_asst_cd = None
        if am_asst_cd:
            same_asst_pm = next((cd for cd in pm_pool_use
                                 if cd.staff.id == am_asst_cd.staff.id
                                 and cd.staff.id not in pm_used), None)
            if same_asst_pm:
                pm_asst_cd = same_asst_pm
                pm_used.add(same_asst_pm.staff.id)

        pm_asst = pm_asst_cd.staff.name if pm_asst_cd else "— Unassigned —"

        ma[room] = {
            "am_lead": am_lead, "am_asst": am_asst,
            "pm_lead": pm_lead, "pm_asst": pm_asst,
            "_note": "☢ X-ray list" if room_is_xray else "",
            "_xray": room_is_xray,
        }

    # ── Second pass: fill PM assistants with surplus trainees ────────────────
    # Only assign a replacement PM assistant if there are spare Senior Trainee/Trainee
    # left after all rooms have their leads. This avoids forcing a replacement when
    # the AM assistant had pm PAAC (they were AM-only).
    spare_pm_trainees = [
        cd for cd in pm_pool
        if cd.staff.id not in pm_used
        and cd.staff.role in ("Senior Trainee", "Trainee")
        and cd.staff.name != coordinator_name
    ]
    spare_pm_trainees.sort(key=lambda cd: cd.staff.total_rooms)

    for room in rooms:
        stype_room = special_types.get(room, "normal")
        if stype_room in ("eot", "trauma"):
            continue
        a = ma.get(room, {})
        # Only fill blank PM assistants, and only if spare trainees exist
        if a.get("pm_asst", "— Unassigned —") == "— Unassigned —" and spare_pm_trainees:
            cd = spare_pm_trainees.pop(0)
            pm_used.add(cd.staff.id)
            a["pm_asst"] = cd.staff.name
            ma[room] = a

    # Consultation — Specialist or Senior Trainee not yet used
    all_used = am_used | pm_used
    consult_pool = [cd for cd in all_day
                    if cd.staff.id not in all_used
                    and cd.staff.role in ("Specialist","Senior Trainee")
                    and cd.staff.name != coordinator_name
                    and not cd.am_call
                    and not cd.pm_paac]
    if consult_criteria == "lowest_count":
        consult_pool.sort(key=lambda cd: cd.staff.total_consults)
    consult_name = consult_pool[0].staff.name if consult_pool else "— Unassigned —"

    return ma, consult_name


def _pick_lead(pool, used, coordinator_name, stype, prefs):
    """
    Lead must be Specialist or Consultant only.
    Prefer specialty match, then load balance.
    """
    candidates = [cd for cd in pool
                  if cd.staff.id not in used
                  and cd.staff.name != coordinator_name
                  and cd.staff.role in ("Specialist", "Consultant")]
    keywords = prefs.get(stype, [stype.lower()])
    matched  = [cd for cd in candidates if any(k in cd.staff.specialties for k in keywords)]
    chosen   = matched or candidates
    if not chosen: return None
    chosen.sort(key=lambda cd: cd.staff.total_rooms)
    cd = chosen[0]
    used.add(cd.staff.id)
    return cd


def _pick_asst(pool, used, coordinator_name):
    """
    Assistant must be Senior Trainee or Trainee only.
    Specialists and Consultants should always be leads, never assistants.
    Assistant is optional — returns None if no suitable person available.

    Preference order:
      1. Trainees/Senior Trainees with NO pm PAAC (full-day available)
      2. Trainees/Senior Trainees WITH pm PAAC — only used as last resort
    This avoids assigning a pm PAAC trainee to a room when a full-day
    trainee is available, since their PM slot would be blank anyway.
    """
    candidates = [cd for cd in pool
                  if cd.staff.id not in used
                  and cd.staff.name != coordinator_name
                  and cd.staff.role in ("Senior Trainee", "Trainee")]
    if not candidates: return None

    # Prefer non-PAAC first
    preferred = [cd for cd in candidates if not cd.pm_paac]
    fallback  = [cd for cd in candidates if cd.pm_paac]

    chosen = preferred or fallback
    chosen.sort(key=lambda cd: cd.staff.total_rooms)
    cd = chosen[0]
    used.add(cd.staff.id)
    return cd


def _pick_solo(pool, used, coordinator_name, prefer_role="Senior Trainee"):
    """Trauma room: Senior Trainee solo. Fallback to Specialist."""
    candidates = [cd for cd in pool
                  if cd.staff.id not in used
                  and cd.staff.name != coordinator_name]
    preferred = [cd for cd in candidates if cd.staff.role == prefer_role]
    fallback  = [cd for cd in candidates if cd.staff.role == "Specialist"]
    chosen = preferred or fallback
    if not chosen: return "— Unassigned —"
    chosen.sort(key=lambda cd: cd.staff.total_rooms)
    cd = chosen[0]
    used.add(cd.staff.id)
    return cd.staff.name


# ─────────────────────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────────────────────

def show():
    st.markdown("## 📅 Daily Scheduler")

    col_date, col_info = st.columns([2, 2])
    target_date = col_date.date_input("Select OT Date", value=date.today())

    all_rooms        = _all_rooms()
    surg_types       = _surgery_types()
    specialty_prefs  = json.loads(get_setting("specialty_preferences", "{}"))
    consult_criteria = get_setting("consult_criteria", "lowest_count")
    special_types    = get_room_special_types()

    with SessionLocal() as s:
        all_staff_db = s.query(Staff).filter(Staff.active == True).all()

    all_day          = _get_day_availability(target_date)
    coordinator_obj  = _get_coordinator_for_date(target_date, all_staff_db)
    coordinator_name = coordinator_obj.name if coordinator_obj else None

    # Coordinator callout
    if coordinator_name:
        col_info.markdown(
            f'<div class="card card-warn" style="margin-top:1.6rem;">'
            f'⭐ <strong>Day Coordinator:</strong> {coordinator_name}<br>'
            f'<small>Excluded from room assignments.</small></div>',
            unsafe_allow_html=True,
        )
    else:
        col_info.info(f"No Day Coordinator for {target_date.strftime('%d %b %Y')}.")

    if not all_day:
        st.warning(f"No staff available for {target_date.strftime('%A, %d %b %Y')}.")
        st.info("💡 If you expect staff to be available, go to **Staff Management → Upload Roster** "
                "and re-import your Excel file to refresh the database.")
        return

    # Warn if the pool looks unexpectedly small (possible stale data)
    pm_paac_shown = [cd for cd in all_day if cd.pm_paac]
    if not pm_paac_shown and target_date.weekday() < 5:  # weekday
        pass  # No warning — could be a valid day with no PAAC

    # ── AM Emergency Team ─────────────────────────────────────────────────────
    am_call_team = [cd for cd in all_day if cd.am_call]
    if am_call_team:
        st.markdown('<div class="section-header">🚨 AM Emergency Team</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-danger">
        These colleagues are on <b>am call</b> today. They are available for emergencies
        but <b>will NOT be scheduled to any OT room</b>.
        </div>
        """, unsafe_allow_html=True)
        ecols = st.columns(min(len(am_call_team), 4))
        for i, cd in enumerate(am_call_team):
            ecols[i % 4].markdown(
                f'<div class="metric-tile">'
                f'<div style="font-weight:600;color:#ef4444;">{cd.staff.name}</div>'
                f'<div style="font-size:.78rem;color:#8b949e;">{cd.staff.role}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # ── PM PAAC list ──────────────────────────────────────────────────────────
    pm_paac_team = [cd for cd in all_day if cd.pm_paac]
    if pm_paac_team:
        st.markdown('<div class="section-header">📋 PM PAAC Duty</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-warn">
        These colleagues have <b>pm PAAC</b> duty today. They <b>cannot be assigned to OT rooms</b>.
        They may be available for AM sessions only if their AM is free.
        </div>
        """, unsafe_allow_html=True)
        pcols = st.columns(min(len(pm_paac_team), 4))
        for i, cd in enumerate(pm_paac_team):
            am_note = "AM available" if cd.am_avail and not cd.am_blocked else "Not available AM"
            pcols[i % 4].markdown(
                f'<div class="metric-tile">'
                f'<div style="font-weight:600;color:#f59e0b;">{cd.staff.name}</div>'
                f'<div style="font-size:.78rem;color:#8b949e;">{cd.staff.role} · {am_note}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    # Already published notice
    with SessionLocal() as s:
        existing_sched = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
    if existing_sched:
        st.markdown(
            f'<div class="card card-ok">✓ Schedule for '
            f'<strong>{target_date.strftime("%A, %d %b %Y")}</strong> '
            f'published at <strong>{existing_sched.published_at}</strong>.</div>',
            unsafe_allow_html=True,
        )

    # Available staff expander
    with st.expander(f"👥 {len(all_day)} staff available today", expanded=False):
        rows = []
        for cd in all_day:
            tag = ""
            if cd.am_call:  tag = "🚨 AM Call"
            elif cd.pm_paac: tag = "📋 PM PAAC"
            elif cd.staff.name == coordinator_name: tag = "⭐ Coordinator"
            rows.append({
                "Name":      cd.staff.name,
                "Role":      cd.staff.role,
                "Status":    tag or "OT",
                "AM avail":  "✓" if cd.am_avail else "—",
                "PM avail":  "✓" if cd.pm_avail else "—",
                "Consults":  cd.staff.total_consults,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Session state ─────────────────────────────────────────────────────────
    for key in ("room_stype","room_xray","manual_assign"):
        if key not in st.session_state: st.session_state[key] = {}
    if "draft"        not in st.session_state: st.session_state.draft        = None
    if "reshuffle_seed" not in st.session_state: st.session_state.reshuffle_seed = 0

    # ── Derive today's surgery lists from weekly schedule ────────────────────
    weekday_name = target_date.strftime("%A")
    day_schedule = WEEKLY_SCHEDULE.get(weekday_name)

    if not day_schedule:
        day_schedule = {"am": [], "pm": [], "obs": False}

    am_types  = day_schedule["am"]
    pm_types  = day_schedule["pm"]
    obs_today = weekday_name in OBS_DAYS
    all_types_today = list(dict.fromkeys(
        am_types + pm_types + FIXED_EXTRA_SLOTS + (["Obstetric"] if obs_today else [])
    ))

    # Auto-populate room_stype on first load for this date (silently)
    date_key = f"sched_date_{target_date}"
    if date_key not in st.session_state:
        st.session_state.room_stype    = {}
        st.session_state.room_xray     = {}
        st.session_state.manual_assign = {}
        st.session_state.draft         = None
        st.session_state.reshuffle_seed = 0
        st.session_state[date_key]     = True
        am_queue = list(am_types)
        for room in all_rooms:
            stype_room = special_types.get(room, "normal")
            if room == OBS_ROOM:
                st.session_state.room_stype[room] = "Obstetric" if obs_today else "— Not scheduled —"
            elif stype_room == "trauma":
                st.session_state.room_stype[room] = "Trauma"
            elif stype_room == "eot":
                st.session_state.room_stype[room] = "Emergency"
            elif am_queue:
                st.session_state.room_stype[room] = am_queue.pop(0)
            else:
                st.session_state.room_stype[room] = "General Surgery"

    # ── Step 1: Surgery list summary ──────────────────────────────────────────
    st.markdown("<div class='section-header'>Step 1 — Today's Surgery Lists</div>",
                unsafe_allow_html=True)

    if not (am_types or pm_types):
        st.info(f"{weekday_name} is a weekend — no standard elective lists. "
                "Trauma and Emergency slots are always available.")

    # ── X-ray state: one toggle per unique surgery type ───────────────────────
    # Initialise on date change (already cleared in date_key block above)
    xray_state_key = f"xray_types_{target_date}"
    if xray_state_key not in st.session_state:
        # Initialise empty — individual rows will seed their own defaults on first render
        st.session_state[xray_state_key] = {}
    xray_types = st.session_state[xray_state_key]

    # ── Pregnant colleagues warning ───────────────────────────────────────────
    pregnant_staff = [cd for cd in all_day if cd.staff.pregnant]
    # Collect unique type names that have any X-ray instance ticked
    xray_list_names = list(dict.fromkeys(
        k.rsplit("_", 1)[0] for k, v in xray_types.items() if v
    ))
    if pregnant_staff and xray_list_names:
        preg_names = ", ".join(cd.staff.name for cd in pregnant_staff)
        st.warning(
            f"⚠ **{preg_names}** {'is' if len(pregnant_staff)==1 else 'are'} pregnant "
            f"and will be excluded from X-ray lists: **{', '.join(xray_list_names)}**"
        )

    # ── Build expanded row list: one row per list instance ──────────────────
    # e.g. 3x Orthopaedic → 3 separate rows, each with its own X-ray toggle
    # Expand am_types and pm_types (already repeated in WEEKLY_SCHEDULE)
    # Add fixed extras and OBS as single entries
    all_am = list(am_types) + (["Obstetric"] if obs_today else []) + FIXED_EXTRA_SLOTS
    all_pm = list(pm_types) + (["Obstetric"] if obs_today else []) + FIXED_EXTRA_SLOTS

    # Build ordered unique (type, index) rows covering both AM and PM
    # Each row = one specific list instance identified by (type, occurrence_index)
    # We pair up AM and PM entries of the same type by position
    from collections import Counter
    am_counter = {}   # type → count seen so far
    pm_counter = {}

    # Full list of row keys: (type, idx) in AM order, then any PM-only extras
    row_keys = []
    for t in all_am:
        am_counter[t] = am_counter.get(t, 0) + 1
        row_keys.append((t, am_counter[t]))

    # Add PM-only entries not in AM
    pm_seen = {}
    for t in all_pm:
        pm_seen[t] = pm_seen.get(t, 0) + 1
    for t, cnt in pm_seen.items():
        am_cnt = am_counter.get(t, 0)
        for i in range(am_cnt + 1, cnt + 1):
            row_keys.append((t, i))

    st.markdown("""
    <div class="card card-accent">
    <small>Each row is one surgery list running today. Repeated types (e.g. three Orthopaedic lists)
    appear as separate rows — tick <b>☢ X-ray</b> individually for each one.
    Pregnant colleagues are excluded from X-ray lists when generating the draft.</small>
    </div>
    """, unsafe_allow_html=True)

    # Header
    h = st.columns([0.25, 2.5, 0.6, 0.6, 0.9])
    h[1].markdown("<small><b>Surgery List</b></small>", unsafe_allow_html=True)
    h[2].markdown("<small><b>AM</b></small>", unsafe_allow_html=True)
    h[3].markdown("<small><b>PM</b></small>", unsafe_allow_html=True)
    h[4].markdown("<small><b>☢ X-ray?</b></small>", unsafe_allow_html=True)

    # Track how many of each type we have rendered to compare AM vs PM counts
    am_totals = {}
    for t in all_am: am_totals[t] = am_totals.get(t, 0) + 1
    pm_totals = {}
    for t in all_pm: pm_totals[t] = pm_totals.get(t, 0) + 1

    for (t, idx) in row_keys:
        in_am = idx <= am_totals.get(t, 0)
        in_pm = idx <= pm_totals.get(t, 0)

        type_colour = (
            "#ef4444" if t in FIXED_EXTRA_SLOTS else
            "#00d4aa" if t == "Obstetric" else
            "#e6edf3"
        )

        # Unique key per row instance
        row_key = f"{t}_{idx}"

        row = st.columns([0.25, 2.5, 0.6, 0.6, 0.9])
        row[0].markdown(
            f'<div style="width:10px;height:10px;border-radius:50%;'
            f'background:{type_colour};margin-top:10px;"></div>',
            unsafe_allow_html=True,
        )
        row[1].markdown(
            f'<div style="padding:5px 0;font-size:.95rem;">{t}</div>',
            unsafe_allow_html=True,
        )
        row[2].markdown(
            f'<div style="padding:5px 0;color:{"#1f8ef1" if in_am else "#30363d"};font-size:.9rem;">{"✓" if in_am else "—"}</div>',
            unsafe_allow_html=True,
        )
        row[3].markdown(
            f'<div style="padding:5px 0;color:{"#f59e0b" if in_pm else "#30363d"};font-size:.9rem;">{"✓" if in_pm else "—"}</div>',
            unsafe_allow_html=True,
        )

        # Individual X-ray checkbox per list instance
        xray_key_inst = f"{t}_{idx}"
        if xray_key_inst not in xray_types:
            xray_types[xray_key_inst] = (t == "Orthopaedic")
        new_xray = row[4].checkbox(
            "", value=xray_types[xray_key_inst],
            key=f"xray_inst_{t}_{idx}_{target_date}",
            label_visibility="collapsed"
        )
        xray_types[xray_key_inst] = new_xray

    st.session_state[xray_state_key] = xray_types

    # Brief note if AM and PM counts differ for any type
    diff_notes = []
    for t in set(list(am_totals.keys()) + list(pm_totals.keys())):
        if t in FIXED_EXTRA_SLOTS: continue
        a, p = am_totals.get(t, 0), pm_totals.get(t, 0)
        if a != p:
            diff_notes.append(f"{t}: AM {a} list{'s' if a>1 else ''} / PM {p} list{'s' if p>1 else ''}")
    if diff_notes:
        st.caption("Session differences: " + " · ".join(diff_notes))

    if not obs_today:
        st.caption("⬜ C7-OBS not scheduled today (runs Mon / Wed / Fri)")

    st.divider()

    # ── Generate / Reshuffle / Clear ──────────────────────────────────────────
    col_gen, col_reshuffle, col_clear = st.columns([2, 1.2, 0.8])
    generate_clicked  = col_gen.button("⚡ Generate Suggested Draft",
                                       type="primary", use_container_width=True)
    reshuffle_clicked = col_reshuffle.button("🔀 Reshuffle", use_container_width=True,
                                              help="Re-run with a different colleague combination")
    if col_clear.button("🗑 Clear", use_container_width=True):
        st.session_state.draft          = None
        st.session_state.manual_assign  = {}
        st.session_state.reshuffle_seed = 0
        st.rerun()

    if generate_clicked:
        st.session_state.reshuffle_seed = 0
        xray_types_now = st.session_state.get(f"xray_types_{target_date}", {})
        with st.spinner("Generating draft…"):
            ma, consult_name = _generate_draft(
                all_day, all_rooms, coordinator_name,
                specialty_prefs, consult_criteria, seed=0,
                target_date=target_date,
                xray_types=xray_types_now,
            )
        # Apply X-ray flags back to session state from draft result
        for room, a in ma.items():
            if a.get("_xray") is not None:
                st.session_state.room_xray[room] = a["_xray"]
        st.session_state.manual_assign    = ma
        st.session_state["consult_manual"] = consult_name
        st.session_state.draft            = True
        st.success("✓ Draft generated. Review and override below.")

    if reshuffle_clicked and st.session_state.draft:
        st.session_state.reshuffle_seed += 1
        seed = st.session_state.reshuffle_seed
        xray_types_now = st.session_state.get(f"xray_types_{target_date}", {})
        with st.spinner(f"Reshuffling (combination #{seed})…"):
            ma, consult_name = _generate_draft(
                all_day, all_rooms, coordinator_name,
                specialty_prefs, consult_criteria, seed=seed,
                target_date=target_date,
                xray_types=xray_types_now,
            )
        for room, a in ma.items():
            if a.get("_xray") is not None:
                st.session_state.room_xray[room] = a["_xray"]
        st.session_state.manual_assign    = ma
        st.session_state["consult_manual"] = consult_name
        st.success(f"✓ Reshuffled — combination #{seed}.")

    # ── Draft table ───────────────────────────────────────────────────────────
    if st.session_state.manual_assign:
        st.markdown(
            '<div class="section-header">Step 2 — Review & Override (AM / PM per Room)</div>',
            unsafe_allow_html=True,
        )
        st.markdown("""
        <div class="card card-accent">
        Each room has <b>AM</b> and <b>PM</b> Lead + Assistant separately.
        Colleagues with <code>am OT</code> only appear in AM dropdowns;
        <code>pm OT</code> only in PM. All dropdowns are manually overrideable.
        </div>
        """, unsafe_allow_html=True)

        # ── Staff pools ───────────────────────────────────────────────────────
        am_opts_pool = [cd for cd in all_day if cd.am_avail and not cd.am_call]
        pm_opts_pool = [cd for cd in all_day if cd.pm_avail and not cd.am_call and not cd.pm_paac]
        am_opts = ["— Unassigned —"] + [cd.staff.name for cd in am_opts_pool]
        pm_opts = ["— Unassigned —"] + [cd.staff.name for cd in pm_opts_pool]
        all_room_opts = ["— None —"] + all_rooms

        # ── Build surgery-type-centric view ───────────────────────────────────
        # Group rooms by their assigned surgery type from session state
        # Each surgery type gets its own card showing: staff, room assignment, X-ray toggle

        # Collect the full list of surgery types for today (AM + PM + fixed)
        # Use the draft's room_stype assignments as the source of truth
        # Build: stype → list of rooms assigned to it
        stype_to_rooms = {}
        for room, stype in st.session_state.room_stype.items():
            if stype and stype not in ("— Not scheduled —",):
                stype_to_rooms.setdefault(stype, []).append(room)

        # Build ordered list: AM types first, PM types, then fixed
        ordered_stypes = list(dict.fromkeys(
            am_types + pm_types + FIXED_EXTRA_SLOTS +
            (["Obstetric"] if obs_today else [])
        ))
        # Add any types in room assignments not already in the list
        for st_type in stype_to_rooms:
            if st_type not in ordered_stypes:
                ordered_stypes.append(st_type)

        # ── Render one card per surgery type ──────────────────────────────────
        for stype in ordered_stypes:
            # Determine AM/PM availability for this type
            in_am = stype in am_types or stype in ("Trauma","Emergency","Obstetric")
            in_pm = stype in pm_types or stype in ("Trauma","Emergency","Obstetric")
            session_tag = ""
            if in_am and in_pm:   session_tag = "AM + PM"
            elif in_am:           session_tag = "AM only"
            elif in_pm:           session_tag = "PM only"

            # Colour coding
            type_colour = {
                "Trauma":       "#f59e0b",
                "Emergency":    "#ef4444",
                "Obstetric":    "#00d4aa",
                "Neurosurgery": "#1f8ef1",
            }.get(stype, "#e6edf3")

            # Rooms currently assigned to this type
            assigned_rooms = stype_to_rooms.get(stype, [])

            st.markdown(
                f'<div class="card card-accent" style="border-left-color:{type_colour};">'                f'<strong style="color:{type_colour};font-size:1rem;">{stype}</strong>'                f' &nbsp;<small style="color:#8b949e;">{session_tag}</small></div>',
                unsafe_allow_html=True,
            )

            # ── X-ray toggle for this surgery type ────────────────────────────
            xray_key = f"xray_stype_{stype}"
            if xray_key not in st.session_state:
                # Default: Orthopaedic is usually X-ray
                st.session_state[xray_key] = stype in ("Orthopaedic",)
            col_xray, col_room_assign = st.columns([1, 3])
            xray_val = col_xray.checkbox(
                "☢ X-ray list", value=st.session_state[xray_key], key=f"xray_cb_{stype}"
            )
            st.session_state[xray_key] = xray_val
            # Propagate X-ray flag to all rooms of this type
            for room in assigned_rooms:
                st.session_state.room_xray[room] = xray_val

            # ── Room assignment for this surgery type ─────────────────────────
            with col_room_assign:
                cur_rooms_display = ", ".join(assigned_rooms) if assigned_rooms else "— None assigned —"
                st.caption(f"Currently assigned rooms: **{cur_rooms_display}**")

                # Multi-select to assign rooms to this surgery type
                new_rooms = st.multiselect(
                    "Assign rooms to this list",
                    options=all_rooms,
                    default=assigned_rooms,
                    key=f"rooms_for_{stype}",
                )
                # Update room_stype for selected/deselected rooms
                for room in new_rooms:
                    st.session_state.room_stype[room] = stype
                    st.session_state.room_xray[room]  = xray_val
                # Unassign rooms removed from this type (only if they still point here)
                for room in assigned_rooms:
                    if room not in new_rooms:
                        # Only clear if still pointing to this type
                        if st.session_state.room_stype.get(room) == stype:
                            st.session_state.room_stype[room] = "— Unassigned —"

            # ── Staff assignment per room for this surgery type ───────────────
            current_rooms = [r for r in all_rooms
                             if st.session_state.room_stype.get(r) == stype]

            if not current_rooms:
                st.caption("  No rooms assigned yet.")
            else:
                # Table header
                h = st.columns([1.0, 1.4, 1.4, 1.4, 1.4, 0.9])
                for lbl, col in zip(["Room","AM Lead","AM Asst","PM Lead","PM Asst","Status"], h):
                    col.markdown(f"<small><b>{lbl}</b></small>", unsafe_allow_html=True)

                for room in current_rooms:
                    ma = st.session_state.manual_assign.get(room, {
                        "am_lead":"— Unassigned —","am_asst":"— Unassigned —",
                        "pm_lead":"— Unassigned —","pm_asst":"— Unassigned —","_note":"",
                    })
                    stype_room = special_types.get(room, "normal")
                    row = st.columns([1.0, 1.4, 1.4, 1.4, 1.4, 0.9])
                    row[0].markdown(f"`{room}`")

                    if stype_room == "eot":
                        for i, (slot, opts) in enumerate([
                            ("am_lead",am_opts),("am_asst",am_opts),
                            ("pm_lead",pm_opts),("pm_asst",pm_opts),
                        ]):
                            cur = ma.get(slot,"— Unassigned —")
                            idx = opts.index(cur) if cur in opts else 0
                            ma[slot] = row[1+i].selectbox("",opts,index=idx,
                                key=f"{slot}_{room}",label_visibility="collapsed")
                        row[5].markdown('<span class="badge-danger">🔴</span>',unsafe_allow_html=True)

                    elif stype_room == "trauma":
                        for i, (slot, opts) in enumerate([("am_lead",am_opts),("pm_lead",pm_opts)]):
                            cur = ma.get(slot,"— Unassigned —")
                            idx = opts.index(cur) if cur in opts else 0
                            ma[slot] = row[1+i*2].selectbox("",opts,index=idx,
                                key=f"{slot}_{room}",label_visibility="collapsed")
                        row[2].markdown('<small style="color:#8b949e;">solo</small>',unsafe_allow_html=True)
                        row[4].markdown('<small style="color:#8b949e;">solo</small>',unsafe_allow_html=True)
                        ma["am_asst"] = "— Unassigned —"
                        ma["pm_asst"] = "— Unassigned —"
                        row[5].markdown('<span class="badge-warn">🟡</span>',unsafe_allow_html=True)

                    else:
                        for i, (slot, opts) in enumerate([
                            ("am_lead",am_opts),("am_asst",am_opts),
                            ("pm_lead",pm_opts),("pm_asst",pm_opts),
                        ]):
                            cur = ma.get(slot,"— Unassigned —")
                            idx = opts.index(cur) if cur in opts else 0
                            ma[slot] = row[1+i].selectbox("",opts,index=idx,
                                key=f"{slot}_{room}",label_visibility="collapsed")

                        row_viol = _check_constraints(
                            {room: ma}, all_day, st.session_state.room_xray,
                            coordinator_name or "", special_types
                        )
                        if row_viol:
                            row[5].markdown('<span class="badge-danger">✗</span>',unsafe_allow_html=True)
                            for v in row_viol: st.caption(f"  ↳ {v}")
                        else:
                            lead_cd = _cd_by_name(am_opts_pool, ma.get("am_lead",""))
                            pref_ok = lead_cd and any(
                                k in lead_cd.staff.specialties
                                for k in specialty_prefs.get(stype, [stype.lower()])
                            )
                            row[5].markdown(
                                '<span class="badge-ok">✓</span>' if pref_ok
                                else '<span class="badge-warn">⚑</span>',
                                unsafe_allow_html=True,
                            )

                    st.session_state.manual_assign[room] = ma

            st.markdown("---")

        st.divider()

        # ── Consultation ──────────────────────────────────────────────────────
        st.markdown('<div class="section-header">Consultation Slot</div>', unsafe_allow_html=True)

        # Consultation pool: Senior Trainee, Specialist, Consultant
        # Must be on OT (am or pm available), not AM call, not coordinator
        consult_pool = [cd for cd in all_day
                        if cd.staff.role in ("Consultant", "Specialist", "Senior Trainee")
                        and cd.staff.name != coordinator_name
                        and not cd.am_call
                        and (cd.am_avail or cd.pm_avail)]
        consult_pool.sort(key=lambda cd: cd.staff.total_consults)

        c_opts = ["— Unassigned —"] + [cd.staff.name for cd in consult_pool]
        c_default = st.session_state.get("consult_manual", "— Unassigned —")
        c_idx = c_opts.index(c_default) if c_default in c_opts else 0

        col_c1, col_c2 = st.columns([2, 2])
        chosen_consult = col_c1.selectbox("Assign Consultation to", c_opts,
                                          index=c_idx, key="consult_select")
        st.session_state["consult_manual"] = chosen_consult
        col_c2.caption(f"Criteria: `{consult_criteria}` · sorted by fewest consults first")

        # ── Consultation load table ───────────────────────────────────────────
        with st.expander("📊 Consultation load — Senior Trainee / Specialist / Consultant", expanded=True):
            from database import SessionLocal as _SL, Staff as _Staff, StaffStats as _SS
            with _SL() as _s:
                _stat_rows = (
                    _s.query(_Staff, _SS)
                     .outerjoin(_SS, _Staff.id == _SS.staff_id)
                     .filter(
                         _Staff.active == True,
                         _Staff.role.in_(["Consultant","Specialist","Senior Trainee"])
                     )
                     .order_by(_SS.total_consultations)
                     .all()
                )

            # Mark who is available today and who is selected
            avail_ids  = {cd.staff.id for cd in all_day if cd.am_avail or cd.pm_avail}
            chosen_cd  = next((cd for cd in consult_pool
                               if cd.staff.name == chosen_consult), None)
            chosen_id  = chosen_cd.staff.id if chosen_cd else None

            load_rows = []
            for m, stat in _stat_rows:
                today_avail = m.id in avail_ids
                is_chosen   = m.id == chosen_id
                load_rows.append({
                    "Name":        ("✅ " if is_chosen else "") + m.name,
                    "Role":        m.role,
                    "Total Consults": stat.total_consultations if stat else 0,
                    "Today":       "On duty" if today_avail else "—",
                    "Last Consult": str(stat.last_consultation) if (stat and stat.last_consultation) else "Never",
                })

            import pandas as _pd
            load_df = _pd.DataFrame(load_rows)
            # Colour-code: highlight selected row
            st.dataframe(load_df, use_container_width=True, hide_index=True)
            st.caption(f"✅ = currently selected for today's consultation · "
                       f"sorted by fewest consultations")

        st.divider()

        # ── Unassigned colleagues ─────────────────────────────────────────────
        st.markdown('<div class="section-header">👤 No Duty Assigned Today</div>',
                    unsafe_allow_html=True)
        assigned_ids = _all_assigned_ids(
            st.session_state.manual_assign,
            st.session_state.get("consult_manual",""),
            all_day,
        )
        if coordinator_obj:
            assigned_ids.add(coordinator_obj.id)
        for cd in all_day:
            if cd.am_call:
                assigned_ids.add(cd.staff.id)   # AM call shown in their own section
            # pm_paac colleagues NOT auto-excluded — they may still be unassigned

        unassigned = [cd for cd in all_day if cd.staff.id not in assigned_ids]
        if unassigned:
            ua_rows = [{
                "Name": cd.staff.name, "Role": cd.staff.role,
                "AM": "✓" if cd.am_avail else "—",
                "PM": "✓" if cd.pm_avail else "—",
                "Consults": cd.staff.total_consults,
            } for cd in unassigned]
            st.dataframe(pd.DataFrame(ua_rows), use_container_width=True, hide_index=True)
            st.caption(f"{len(unassigned)} colleague(s) on duty but not yet assigned a slot.")
        else:
            st.markdown('<div class="card card-ok">✓ All available colleagues assigned.</div>',
                        unsafe_allow_html=True)

        st.divider()

        # ── Global violations ─────────────────────────────────────────────────
        all_violations = _check_constraints(
            st.session_state.manual_assign, all_day,
            st.session_state.room_xray, coordinator_name or "", special_types,
        )
        if all_violations:
            st.markdown('<div class="card card-danger"><strong>⚠ Violations:</strong>',
                        unsafe_allow_html=True)
            for v in all_violations:
                st.markdown(f'<span class="badge-danger">{v}</span>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="card card-ok">✓ No violations. Ready to publish.</div>',
                        unsafe_allow_html=True)

        col_pub, col_exp = st.columns([1.5, 1])
        if col_pub.button("✅ Approve & Publish", type="primary",
                          use_container_width=True, disabled=bool(all_violations)):
            _publish_schedule(target_date, all_day, coordinator_name)
        if col_exp.button("📥 Export Excel", use_container_width=True):
            _export_excel(target_date)


# ─────────────────────────────────────────────────────────────────────────────
# Publish & Export
# ─────────────────────────────────────────────────────────────────────────────

def _publish_schedule(target_date, all_day, coordinator_name):
    ma           = st.session_state.manual_assign
    xray         = st.session_state.room_xray
    stypes       = st.session_state.room_stype
    consult_name = st.session_state.get("consult_manual","")

    consult_cd = _cd_by_name(all_day, consult_name)
    assignments_list = []

    for room, a in ma.items():
        for slot_key, role_type in [
            ("am_lead","am_lead"),("am_asst","am_asst"),
            ("pm_lead","pm_lead"),("pm_asst","pm_asst"),
        ]:
            cd = _cd_by_name(all_day, a.get(slot_key,""))
            if cd:
                assignments_list.append({
                    "staff_id": cd.staff.id, "staff_name": cd.staff.name,
                    "role_type": role_type, "room": room,
                    "surgery_type": stypes.get(room,"General"),
                    "is_xray": xray.get(room, False),
                })

    if consult_cd:
        assignments_list.append({
            "staff_id": consult_cd.staff.id, "staff_name": consult_cd.staff.name,
            "role_type": "consultation", "room": "CONSULT",
            "surgery_type": "Consultation", "is_xray": False,
        })
    if coordinator_name:
        with SessionLocal() as s:
            cd_db = s.query(Staff).filter(Staff.name == coordinator_name).first()
            if cd_db:
                assignments_list.append({
                    "staff_id": cd_db.id, "staff_name": coordinator_name,
                    "role_type": "coordinator", "room": "COORD",
                    "surgery_type": "Day Coordinator", "is_xray": False,
                })

    raw_blob = json.dumps({
        "room_stypes": stypes, "room_xray": {k: bool(v) for k, v in xray.items()},
        "assignments": assignments_list, "consultation_name": consult_name,
        "coordinator_name": coordinator_name or "",
    })

    with SessionLocal() as s:
        sched = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
        if not sched:
            sched = Schedule(schedule_date=target_date)
            s.add(sched)
        sched.published_at = datetime.now().isoformat(timespec="seconds")
        sched.raw_json     = raw_blob
        s.flush()
        s.query(ScheduleAssignment).filter(ScheduleAssignment.schedule_id == sched.id).delete()
        for a in assignments_list:
            if a["role_type"] in ("coordinator",): continue
            s.add(ScheduleAssignment(
                schedule_id=sched.id, schedule_date=target_date,
                room=a["room"], surgery_type=a["surgery_type"],
                is_xray=a["is_xray"], role_type=a["role_type"],
                staff_id=a["staff_id"], staff_name=a["staff_name"],
            ))
        s.commit()

    upsert_stats_after_publish(
        [a for a in assignments_list if a["role_type"] not in ("coordinator",)],
        target_date,
    )
    st.success(f"✓ Published for {target_date.strftime('%A, %d %b %Y')}!")
    st.balloons()


def _export_excel(target_date):
    ma     = st.session_state.manual_assign
    xray   = st.session_state.room_xray
    stypes = st.session_state.room_stype
    special_types = get_room_special_types()
    consult_name  = st.session_state.get("consult_manual","")
    rows = []
    for room in _all_rooms():
        a = ma.get(room, {})
        label, _ = SPECIAL_TYPE_LABELS.get(special_types.get(room,"normal"), SPECIAL_TYPE_LABELS["normal"])
        rows.append({
            "Room": room, "Type": label,
            "Surgery": stypes.get(room,"—"),
            "X-ray": "Yes" if xray.get(room,False) else "No",
            "AM Lead": a.get("am_lead","—"), "AM Asst": a.get("am_asst","—"),
            "PM Lead": a.get("pm_lead","—"), "PM Asst": a.get("pm_asst","—"),
        })
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name=f"OT {target_date}")
        pd.DataFrame([{"Consultation": consult_name}]).to_excel(
            writer, index=False, sheet_name="Consultation")
    buf.seek(0)
    st.download_button("⬇️ Download Excel", data=buf,
                       file_name=f"OT_Schedule_{target_date}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
