"""
pages/scheduler.py – Daily OT Scheduler (core workflow page)
=============================================================
Workflow:
  1. User picks a date.
  2. App shows available staff for that day.
  3. User assigns surgery types + X-ray flags per room.
  4. "Generate Suggested Draft" runs CP-SAT optimiser.
  5. User can override assignments manually.
  6. "Approve & Publish" saves to DB and updates stats.
"""
import json
import io
from datetime import date, datetime
import streamlit as st
import pandas as pd
from database import (
    SessionLocal, Staff, StaffStats, Schedule, ScheduleAssignment,
    get_setting, upsert_stats_after_publish
)
from optimizer import StaffMember, RoomCase, solve_ortools


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_available_staff(target_date: date):
    day_abbr  = target_date.strftime("%a").lower()   # mon, tue …
    day_col   = f"ot_{day_abbr}"
    with SessionLocal() as s:
        all_staff = s.query(Staff, StaffStats)\
            .outerjoin(StaffStats, Staff.id == StaffStats.staff_id)\
            .filter(Staff.active == True)\
            .all()
        result = []
        for m, stat in all_staff:
            if getattr(m, day_col, False):
                result.append(StaffMember(
                    id=m.id, name=m.name, role=m.role,
                    pregnant=m.pregnant,
                    specialties=m.specialties_list(),
                    total_rooms=stat.total_rooms if stat else 0,
                    total_consults=stat.total_consultations if stat else 0,
                    last_consult_dates=json.loads(stat.consult_dates_json or "[]") if stat else [],
                ))
    return result


def _all_rooms():
    cfg = json.loads(get_setting("rooms_config", "{}"))
    rooms = []
    for block, room_list in cfg.items():
        for r in room_list:
            rooms.append(r)
    return rooms


def _surgery_types():
    return json.loads(get_setting("surgery_types", '["General"]'))


def _staff_options(available: list, include_blank=True):
    names = [m.name for m in available]
    return (["— Unassigned —"] + names) if include_blank else names


def _staff_by_name(available: list, name: str):
    return next((m for m in available if m.name == name), None)


def _violations_html(violations: list) -> str:
    if not violations:
        return '<span class="badge-ok">✓ OK</span>'
    return " ".join(f'<span class="badge-danger">{v}</span>' for v in violations)


def _check_constraints(assignments_state: dict, available: list, is_xray: dict):
    """Return list of violation messages for the current manual state."""
    violations = []
    used_staff  = {}  # staff_id → room

    for room, a in assignments_state.items():
        lead_m   = _staff_by_name(available, a.get("lead", ""))
        assist_m = _staff_by_name(available, a.get("assistant", ""))

        # C1: lead must exist
        if not lead_m:
            violations.append(f"{room}: No lead assigned.")
        else:
            # C3: lead role
            if lead_m.role not in ("Specialist", "Senior Trainee", "Consultant"):
                violations.append(f"{room}: Lead '{lead_m.name}' is a Trainee – not permitted.")
            # C4: pregnant + x-ray
            if lead_m.pregnant and is_xray.get(room, False):
                violations.append(f"{room}: {lead_m.name} is pregnant and cannot be in an X-ray room.")
            # C5 duplicate
            if lead_m.id in used_staff:
                violations.append(f"{room}: {lead_m.name} already assigned to {used_staff[lead_m.id]}.")
            else:
                used_staff[lead_m.id] = room

        if assist_m:
            if assist_m.pregnant and is_xray.get(room, False):
                violations.append(f"{room}: {assist_m.name} is pregnant and cannot be in an X-ray room.")
            if assist_m.id in used_staff:
                violations.append(f"{room}: {assist_m.name} already assigned to {used_staff[assist_m.id]}.")
            else:
                used_staff[assist_m.id] = room

    return violations


# ── Main page ─────────────────────────────────────────────────────────────────

def show():
    st.markdown("## 📅 Daily Scheduler")

    # ── Step 1: Pick date ─────────────────────────────────────────────────────
    col_date, col_solver = st.columns([2, 1])
    target_date = col_date.date_input("Select OT Date", value=date.today())
    solver_choice = col_solver.selectbox("Solver", ["OR-Tools CP-SAT", "Greedy Fallback"])

    # ── Step 2: Available staff ────────────────────────────────────────────────
    available = _get_available_staff(target_date)
    all_rooms  = _all_rooms()
    surg_types = _surgery_types()
    specialty_prefs = json.loads(get_setting("specialty_preferences", "{}"))
    consult_criteria = get_setting("consult_criteria", "lowest_count")

    if not available:
        st.warning(f"No staff marked as on OT duty on {target_date.strftime('%A, %d %b %Y')}. "
                   "Check the roster or upload a new one in Staff Management.")
        return

    # ── Check if already published ────────────────────────────────────────────
    already_published = False
    with SessionLocal() as s:
        existing = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
        if existing:
            already_published = True
            st.markdown(f"""
            <div class="card card-ok">
            ✓ A schedule for <strong>{target_date.strftime('%A, %d %b %Y')}</strong>
            was already published at <strong>{existing.published_at}</strong>.
            You can re-generate and overwrite it below.
            </div>
            """, unsafe_allow_html=True)

    # ── Staff availability table ───────────────────────────────────────────────
    with st.expander(f"👥 Available Staff ({len(available)} on duty today)", expanded=False):
        df_avail = pd.DataFrame([{
            "Name":       m.name,
            "Role":       m.role,
            "Specialties": ", ".join(m.specialties) if m.specialties else "—",
            "Pregnant":   "⚠ Yes" if m.pregnant else "No",
            "Total Rooms": m.total_rooms,
            "Total Consults": m.total_consults,
        } for m in available])
        st.dataframe(df_avail, use_container_width=True, hide_index=True)

    st.divider()

    # ── Step 3: Room → Surgery type assignment + X-ray flags ──────────────────
    st.markdown('<div class="section-header">Step 1 – Assign Surgery Types & X-ray Flags</div>',
                unsafe_allow_html=True)

    # Session state keys
    if "room_stype"    not in st.session_state: st.session_state.room_stype    = {}
    if "room_xray"     not in st.session_state: st.session_state.room_xray     = {}
    if "draft"         not in st.session_state: st.session_state.draft         = None
    if "manual_assign" not in st.session_state: st.session_state.manual_assign = {}

    # Grid: room | surgery type | x-ray
    header_cols = st.columns([1.2, 2, 0.8])
    header_cols[0].markdown("**Room**")
    header_cols[1].markdown("**Surgery / Case Type**")
    header_cols[2].markdown("**X-ray?**")

    for room in all_rooms:
        cols = st.columns([1.2, 2, 0.8])
        cols[0].markdown(f"`{room}`")

        current_stype = st.session_state.room_stype.get(room, surg_types[0] if surg_types else "General")
        idx = surg_types.index(current_stype) if current_stype in surg_types else 0
        stype = cols[1].selectbox("", surg_types, index=idx, key=f"stype_{room}", label_visibility="collapsed")
        xray  = cols[2].checkbox("", value=st.session_state.room_xray.get(room, False),
                                  key=f"xray_{room}", label_visibility="collapsed")

        st.session_state.room_stype[room] = stype
        st.session_state.room_xray[room]  = xray

    st.divider()

    # ── Generate button ───────────────────────────────────────────────────────
    col_gen, col_clear = st.columns([2, 1])
    generate_clicked = col_gen.button("⚡ Generate Suggested Draft", type="primary", use_container_width=True)
    if col_clear.button("🗑 Clear Draft", use_container_width=True):
        st.session_state.draft = None
        st.session_state.manual_assign = {}
        st.rerun()

    if generate_clicked:
        with st.spinner("Running optimiser…"):
            room_cases = [
                RoomCase(
                    room=r,
                    surgery_type=st.session_state.room_stype.get(r, "General"),
                    is_xray=st.session_state.room_xray.get(r, False),
                )
                for r in all_rooms
            ]

            result = solve_ortools(
                staff=available,
                rooms=room_cases,
                specialty_prefs=specialty_prefs,
                consult_criteria=consult_criteria,
            )

        st.session_state.draft = result
        # Pre-populate manual assign from solver result
        ma = {}
        for a in result.assignments:
            ma[a.room] = {
                "lead":      a.lead_name      or "— Unassigned —",
                "assistant": a.assistant_name or "— Unassigned —",
            }
        st.session_state.manual_assign = ma
        st.session_state["consult_manual"] = result.consultation_staff_name or "— Unassigned —"

        if not result.feasible:
            st.error("⚠ No fully feasible solution found. Greedy fallback used. Check violations below.")
        else:
            st.success(f"✓ Draft generated using **{result.solver_used}**. Review and adjust below.")

    # ── Draft table ───────────────────────────────────────────────────────────
    if st.session_state.draft:
        result = st.session_state.draft
        staff_opts = _staff_options(available)

        st.markdown('<div class="section-header">Step 2 – Review & Override Draft Assignment</div>',
                    unsafe_allow_html=True)
        st.caption(f"Solver: {result.solver_used} · Click cells to override manually.")

        # Legend
        st.markdown("""
        <div style="display:flex;gap:1rem;margin-bottom:.5rem;">
            <span class="badge-ok">✓ Pref match</span>
            <span class="badge-warn">⚑ Sub-optimal</span>
            <span class="badge-danger">✗ Violation</span>
        </div>
        """, unsafe_allow_html=True)

        # Header
        h = st.columns([1, 1.2, 0.7, 1.5, 1.5, 1.2])
        for label, col in zip(["Room","Surgery Type","X-ray","Lead (Specialist)","Assistant","Status"], h):
            col.markdown(f"**{label}**")
        st.divider()

        # Rows
        for a in result.assignments:
            ma = st.session_state.manual_assign.get(a.room, {"lead":"— Unassigned —","assistant":"— Unassigned —"})
            c = st.columns([1, 1.2, 0.7, 1.5, 1.5, 1.2])

            c[0].markdown(f"`{a.room}`")
            c[1].markdown(a.surgery_type)
            c[2].markdown("☢ Yes" if a.is_xray else "No")

            # Manual override dropdowns
            lead_idx  = staff_opts.index(ma["lead"])      if ma["lead"]      in staff_opts else 0
            asst_idx  = staff_opts.index(ma["assistant"]) if ma["assistant"] in staff_opts else 0

            new_lead  = c[3].selectbox("", staff_opts, index=lead_idx,  key=f"lead_{a.room}",  label_visibility="collapsed")
            new_asst  = c[4].selectbox("", staff_opts, index=asst_idx,  key=f"asst_{a.room}",  label_visibility="collapsed")

            st.session_state.manual_assign[a.room] = {"lead": new_lead, "assistant": new_asst}

            # Status
            violations = _check_constraints(
                {a.room: {"lead": new_lead, "assistant": new_asst}},
                available,
                st.session_state.room_xray,
            )
            pref_lead = _staff_by_name(available, new_lead)
            pref_ok = pref_lead and any(
                k in pref_lead.specialties
                for k in specialty_prefs.get(a.surgery_type, [a.surgery_type.lower()])
            ) if pref_lead else False

            if violations:
                c[5].markdown('<span class="badge-danger">✗ Violation</span>', unsafe_allow_html=True)
            elif pref_ok:
                c[5].markdown('<span class="badge-ok">✓ Pref match</span>', unsafe_allow_html=True)
            else:
                c[5].markdown('<span class="badge-warn">⚑ No pref</span>', unsafe_allow_html=True)

        st.divider()

        # ── Consultation slot ─────────────────────────────────────────────────
        st.markdown('<div class="section-header">Consultation Slot</div>', unsafe_allow_html=True)
        consult_opts = _staff_options(
            [m for m in available if m.role in ("Specialist", "Senior Trainee")]
        )
        consult_default = st.session_state.get("consult_manual", result.consultation_staff_name or "— Unassigned —")
        consult_idx = consult_opts.index(consult_default) if consult_default in consult_opts else 0

        col_c1, col_c2 = st.columns([2, 2])
        chosen_consult = col_c1.selectbox(
            "Assign Consultation to", consult_opts, index=consult_idx, key="consult_select"
        )
        st.session_state["consult_manual"] = chosen_consult
        col_c2.markdown(f"Criteria: `{consult_criteria}` · {result.consultation_notes}")

        # ── Global violations check ───────────────────────────────────────────
        all_violations = _check_constraints(
            st.session_state.manual_assign, available, st.session_state.room_xray
        )
        if all_violations:
            st.markdown('<div class="card card-danger">', unsafe_allow_html=True)
            st.markdown("**Constraint Violations – must be resolved before publishing:**")
            for v in all_violations:
                st.markdown(f'<span class="badge-danger">{v}</span>', unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown('<div class="card card-ok">✓ No constraint violations detected.</div>',
                        unsafe_allow_html=True)

        st.divider()

        # ── Approve & Publish ─────────────────────────────────────────────────
        col_pub, col_exp = st.columns([1.5, 1])

        publish_disabled = bool(all_violations)
        if col_pub.button("✅ Approve & Publish Schedule", type="primary",
                          use_container_width=True, disabled=publish_disabled):
            _publish_schedule(target_date, available)

        if col_exp.button("📥 Export to Excel", use_container_width=True):
            _export_excel(target_date, available, result)


def _publish_schedule(target_date: date, available: list):
    """Save schedule to DB and update stats."""
    ma     = st.session_state.manual_assign
    xray   = st.session_state.room_xray
    stypes = st.session_state.room_stype
    consult_name = st.session_state.get("consult_manual", "")

    consult_m = next((m for m in available if m.name == consult_name), None)

    # Build raw JSON blob
    assignments_list = []
    for room, a in ma.items():
        lead_m  = next((m for m in available if m.name == a.get("lead", "")), None)
        asst_m  = next((m for m in available if m.name == a.get("assistant", "")), None)

        if lead_m:
            assignments_list.append({
                "staff_id": lead_m.id,
                "staff_name": lead_m.name,
                "role_type": "lead",
                "room": room,
                "surgery_type": stypes.get(room, "General"),
                "is_xray": xray.get(room, False),
            })
        if asst_m:
            assignments_list.append({
                "staff_id": asst_m.id,
                "staff_name": asst_m.name,
                "role_type": "assistant",
                "room": room,
                "surgery_type": stypes.get(room, "General"),
                "is_xray": xray.get(room, False),
            })

    if consult_m:
        assignments_list.append({
            "staff_id": consult_m.id,
            "staff_name": consult_m.name,
            "role_type": "consultation",
            "room": "CONSULT",
            "surgery_type": "Consultation",
            "is_xray": False,
        })

    raw_blob = json.dumps({
        "room_stypes": stypes,
        "room_xray":   {k: bool(v) for k, v in xray.items()},
        "assignments": assignments_list,
        "consultation_name": consult_name,
    })

    with SessionLocal() as s:
        # Upsert schedule
        sched = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
        if not sched:
            sched = Schedule(schedule_date=target_date)
            s.add(sched)
        sched.published_at = datetime.now().isoformat(timespec="seconds")
        sched.raw_json     = raw_blob
        s.flush()

        # Remove old assignment rows
        s.query(ScheduleAssignment).filter(ScheduleAssignment.schedule_id == sched.id).delete()

        # Insert new rows
        for a in assignments_list:
            s.add(ScheduleAssignment(
                schedule_id=sched.id,
                schedule_date=target_date,
                room=a["room"],
                surgery_type=a["surgery_type"],
                is_xray=a["is_xray"],
                role_type=a["role_type"],
                staff_id=a["staff_id"],
                staff_name=a["staff_name"],
            ))
        s.commit()
        sched_id = sched.id

    # Update stats
    upsert_stats_after_publish(assignments_list, target_date)

    st.success(f"✓ Schedule for {target_date.strftime('%A, %d %b %Y')} published successfully!")
    st.balloons()


def _export_excel(target_date: date, available: list, result):
    """Export current draft to Excel."""
    ma     = st.session_state.manual_assign
    xray   = st.session_state.room_xray
    stypes = st.session_state.room_stype
    consult_name = st.session_state.get("consult_manual", "")

    rows = []
    for room in _all_rooms():
        a = ma.get(room, {})
        rows.append({
            "Room":         room,
            "Surgery Type": stypes.get(room, "—"),
            "X-ray":        "Yes" if xray.get(room, False) else "No",
            "Lead":         a.get("lead", "— Unassigned —"),
            "Assistant":    a.get("assistant", "—"),
        })

    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=f"Schedule {target_date}")
        pd.DataFrame([{"Consultation": consult_name}]).to_excel(
            writer, index=False, sheet_name="Consultation"
        )
    buf.seek(0)
    st.download_button(
        "⬇️ Download Excel",
        data=buf,
        file_name=f"OT_Schedule_{target_date}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
