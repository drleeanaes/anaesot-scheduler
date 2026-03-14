"""
pages/scheduler.py – Daily OT Scheduler
=========================================
Key features:
  • Detects Day Coordinator for selected date (OT* in roster) — excluded from room assignment
  • Manual override: every Lead and Assistant cell is a dropdown, always editable
  • Real-time constraint violation highlighting as you change dropdowns
  • Approve & Publish saves to DB and updates stats
"""
import json
import io
from datetime import date, datetime
import streamlit as st
import pandas as pd
from database import (
    SessionLocal, Staff, StaffStats, Schedule, ScheduleAssignment,
    get_setting, upsert_stats_after_publish,
)
from optimizer import StaffMember, RoomCase, solve_ortools


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_coordinator_for_date(target_date: date, staff_list: list):
    """
    Return the Staff object that is Day Coordinator for this specific date.
    Coordinator is a Consultant whose roster cell was 'OT*' on target_date.
    """
    from pages.staff import get_coordinator_dates
    for m in staff_list:
        if m.role != "Consultant":
            continue
        coord_dates = get_coordinator_dates(m)
        if target_date in coord_dates:
            return m
    return None


def _get_ot_available_for_date(target_date: date):
    """
    Return StaffMember list available for OT on target_date.
    Uses per-date OT map stored in meta (from custom roster import),
    falling back to weekly weekday flags if meta not present.
    """
    from pages.staff import get_ot_dates, get_real_specialties

    day_abbr = target_date.strftime("%a").lower()
    day_col  = f"ot_{day_abbr}"

    with SessionLocal() as s:
        rows = (
            s.query(Staff, StaffStats)
             .outerjoin(StaffStats, Staff.id == StaffStats.staff_id)
             .filter(Staff.active == True)
             .all()
        )
        result = []
        for m, stat in rows:
            # Check per-date availability first (precise), then fall back to weekly flag
            ot_dates = get_ot_dates(m)
            if ot_dates:
                available = target_date in ot_dates
            else:
                available = getattr(m, day_col, False)

            if available:
                specs = get_real_specialties(m)
                result.append(StaffMember(
                    id=m.id, name=m.name, role=m.role,
                    pregnant=m.pregnant,
                    specialties=[x.strip().lower() for x in specs.split(",") if x.strip()],
                    total_rooms=stat.total_rooms if stat else 0,
                    total_consults=stat.total_consultations if stat else 0,
                    last_consult_dates=json.loads(stat.consult_dates_json or "[]") if stat else [],
                ))
    return result


def _all_rooms():
    cfg = json.loads(get_setting("rooms_config", "{}"))
    return [r for rooms in cfg.values() for r in rooms]


def _surgery_types():
    return json.loads(get_setting("surgery_types", '["General"]'))


def _staff_options(available, include_blank=True):
    names = [m.name for m in available]
    return (["— Unassigned —"] + names) if include_blank else names


def _staff_by_name(available, name):
    return next((m for m in available if m.name == name), None)


ROLE_RANK = {"Consultant": 4, "Specialist": 3, "Senior Trainee": 2, "Trainee": 1}


def _check_constraints(manual_assign, available, room_xray, coordinator_name):
    """Return list of violation strings for current assignment state."""
    violations = []
    used = {}  # staff_id → room

    for room, a in manual_assign.items():
        lead_m  = _staff_by_name(available, a.get("lead", ""))
        asst_m  = _staff_by_name(available, a.get("assistant", ""))

        if not lead_m:
            violations.append(f"{room}: No lead assigned.")
        else:
            if lead_m.role == "Trainee":
                violations.append(f"{room}: {lead_m.name} is a Trainee — cannot be lead.")
            if lead_m.pregnant and room_xray.get(room, False):
                violations.append(f"{room}: {lead_m.name} is pregnant — cannot be in X-ray room.")
            if lead_m.name == coordinator_name:
                violations.append(f"{room}: {lead_m.name} is Day Coordinator — should not be assigned room duty.")
            if lead_m.id in used:
                violations.append(f"{room}: {lead_m.name} already assigned to {used[lead_m.id]}.")
            else:
                used[lead_m.id] = room

        if asst_m:
            if asst_m.pregnant and room_xray.get(room, False):
                violations.append(f"{room}: {asst_m.name} is pregnant — cannot be in X-ray room.")
            if asst_m.name == coordinator_name:
                violations.append(f"{room}: {asst_m.name} is Day Coordinator — should not be assigned room duty.")
            if asst_m.id in used:
                violations.append(f"{room}: {asst_m.name} already assigned to {used[asst_m.id]}.")
            else:
                used[asst_m.id] = room

    return violations


# ─────────────────────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────────────────────

def show():
    st.markdown("## 📅 Daily Scheduler")

    # ── Date picker ───────────────────────────────────────────────────────────
    col_date, col_info = st.columns([2, 2])
    target_date = col_date.date_input("Select OT Date", value=date.today())

    all_rooms        = _all_rooms()
    surg_types       = _surgery_types()
    specialty_prefs  = json.loads(get_setting("specialty_preferences", "{}"))
    consult_criteria = get_setting("consult_criteria", "lowest_count")

    # ── Load available staff for this date ────────────────────────────────────
    with SessionLocal() as s:
        all_staff_db = s.query(Staff).filter(Staff.active == True).all()

    available = _get_ot_available_for_date(target_date)
    coordinator_obj  = _get_coordinator_for_date(target_date, all_staff_db)
    coordinator_name = coordinator_obj.name if coordinator_obj else None

    # Show coordinator callout
    if coordinator_name:
        col_info.markdown(
            f'<div class="card card-warn" style="margin-top:1.6rem;">'
            f'⭐ <strong>Day Coordinator:</strong> {coordinator_name}<br>'
            f'<small>Will not be assigned room duty by the optimiser.</small>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        col_info.info(f"No Day Coordinator designated for {target_date.strftime('%d %b %Y')}.")

    if not available:
        st.warning(f"No staff on OT duty for {target_date.strftime('%A, %d %b %Y')}. "
                   "Check the roster.")
        return

    # ── Already published notice ──────────────────────────────────────────────
    with SessionLocal() as s:
        existing_sched = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
    if existing_sched:
        st.markdown(
            f'<div class="card card-ok">✓ Schedule for '
            f'<strong>{target_date.strftime("%A, %d %b %Y")}</strong> '
            f'was published at <strong>{existing_sched.published_at}</strong>. '
            f'You may re-generate and overwrite it.</div>',
            unsafe_allow_html=True,
        )

    # ── Available staff table ─────────────────────────────────────────────────
    with st.expander(f"👥 {len(available)} staff on OT duty today", expanded=False):
        from pages.staff import get_real_specialties
        with SessionLocal() as s:
            staff_db_map = {m.id: m for m in s.query(Staff).filter(Staff.active == True).all()}

        rows = []
        for m in available:
            db_m = staff_db_map.get(m.id)
            is_coord = (m.name == coordinator_name)
            rows.append({
                "Name":        ("⭐ " if is_coord else "") + m.name,
                "Role":        m.role,
                "Specialties": ", ".join(m.specialties) or "—",
                "Pregnant":    "⚠ Yes" if m.pregnant else "No",
                "Consults":    m.total_consults,
                "Rooms":       m.total_rooms,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.divider()

    # ── Session state init ────────────────────────────────────────────────────
    if "room_stype"    not in st.session_state: st.session_state.room_stype    = {}
    if "room_xray"     not in st.session_state: st.session_state.room_xray     = {}
    if "draft"         not in st.session_state: st.session_state.draft         = None
    if "manual_assign" not in st.session_state: st.session_state.manual_assign = {}

    # ── Step 1: Assign surgery types & X-ray flags ────────────────────────────
    st.markdown('<div class="section-header">Step 1 — Assign Surgery Types & X-ray Flags</div>',
                unsafe_allow_html=True)

    hdr = st.columns([1.2, 2, 0.8])
    hdr[0].markdown("**Room**"); hdr[1].markdown("**Surgery / Case Type**"); hdr[2].markdown("**X-ray?**")

    for room in all_rooms:
        cols = st.columns([1.2, 2, 0.8])
        cols[0].markdown(f"`{room}`")
        current_stype = st.session_state.room_stype.get(room, surg_types[0] if surg_types else "General")
        idx = surg_types.index(current_stype) if current_stype in surg_types else 0
        stype = cols[1].selectbox("", surg_types, index=idx,
                                  key=f"stype_{room}", label_visibility="collapsed")
        xray  = cols[2].checkbox("", value=st.session_state.room_xray.get(room, False),
                                 key=f"xray_{room}", label_visibility="collapsed")
        st.session_state.room_stype[room] = stype
        st.session_state.room_xray[room]  = xray

    st.divider()

    # ── Generate / Clear ──────────────────────────────────────────────────────
    col_gen, col_clear = st.columns([2, 1])
    generate_clicked = col_gen.button("⚡ Generate Suggested Draft",
                                      type="primary", use_container_width=True)
    if col_clear.button("🗑 Clear Draft", use_container_width=True):
        st.session_state.draft         = None
        st.session_state.manual_assign = {}
        st.rerun()

    if generate_clicked:
        # Exclude coordinator from solver pool
        solver_staff = [m for m in available if m.name != coordinator_name]

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
                staff=solver_staff,
                rooms=room_cases,
                specialty_prefs=specialty_prefs,
                consult_criteria=consult_criteria,
            )

        st.session_state.draft = result
        # Populate manual_assign from solver result
        ma = {}
        for a in result.assignments:
            ma[a.room] = {
                "lead":      a.lead_name      or "— Unassigned —",
                "assistant": a.assistant_name or "— Unassigned —",
            }
        st.session_state.manual_assign = ma
        st.session_state["consult_manual"] = (
            result.consultation_staff_name or "— Unassigned —"
        )

        if not result.feasible:
            st.error("⚠ No fully feasible solution. Greedy fallback used — check violations.")
        else:
            st.success(f"✓ Draft generated using **{result.solver_used}**.")

    # ── Draft table with full manual override ─────────────────────────────────
    # Always show override table if there's any assignment state
    if st.session_state.manual_assign or st.session_state.draft:

        st.markdown(
            '<div class="section-header">Step 2 — Review & Manually Override Assignments</div>',
            unsafe_allow_html=True,
        )
        st.markdown("""
        <div class="card card-accent">
        Every <b>Lead</b> and <b>Assistant</b> dropdown is fully editable — change any assignment
        at any time. Violations appear in red instantly. Green = specialty preference matched.
        </div>
        """, unsafe_allow_html=True)

        if coordinator_name:
            st.caption(f"⭐ {coordinator_name} (Day Coordinator) is listed in dropdowns "
                       "but assigning them will show a violation warning.")

        staff_opts = _staff_options(available)

        # Table header
        hdr2 = st.columns([0.9, 1.1, 0.6, 1.6, 1.6, 1.1])
        for lbl, c in zip(["Room","Surgery","X-ray","Lead ▾","Assistant ▾","Status"], hdr2):
            c.markdown(f"**{lbl}**")
        st.divider()

        for room in all_rooms:
            ma = st.session_state.manual_assign.get(
                room, {"lead": "— Unassigned —", "assistant": "— Unassigned —"}
            )
            stype = st.session_state.room_stype.get(room, "—")
            is_xray = st.session_state.room_xray.get(room, False)

            c = st.columns([0.9, 1.1, 0.6, 1.6, 1.6, 1.1])
            c[0].markdown(f"`{room}`")
            c[1].markdown(f"<small>{stype}</small>", unsafe_allow_html=True)
            c[2].markdown("☢ Yes" if is_xray else "No")

            # ── Lead dropdown (manual override) ───────────────────────────────
            lead_idx = staff_opts.index(ma["lead"]) if ma["lead"] in staff_opts else 0
            new_lead = c[3].selectbox(
                "", staff_opts, index=lead_idx,
                key=f"lead_{room}", label_visibility="collapsed"
            )

            # ── Assistant dropdown (manual override) ──────────────────────────
            asst_idx = staff_opts.index(ma["assistant"]) if ma["assistant"] in staff_opts else 0
            new_asst = c[4].selectbox(
                "", staff_opts, index=asst_idx,
                key=f"asst_{room}", label_visibility="collapsed"
            )

            # Write back overrides immediately
            st.session_state.manual_assign[room] = {
                "lead": new_lead, "assistant": new_asst
            }

            # ── Per-row status badge ───────────────────────────────────────────
            row_violations = _check_constraints(
                {room: {"lead": new_lead, "assistant": new_asst}},
                available,
                st.session_state.room_xray,
                coordinator_name or "",
            )
            lead_m   = _staff_by_name(available, new_lead)
            pref_ok  = False
            if lead_m:
                keywords = specialty_prefs.get(stype, [stype.lower()])
                pref_ok  = any(k in lead_m.specialties for k in keywords)

            if row_violations:
                c[5].markdown('<span class="badge-danger">✗ Violation</span>',
                              unsafe_allow_html=True)
                for v in row_violations:
                    st.caption(f"  ↳ {v}")
            elif pref_ok:
                c[5].markdown('<span class="badge-ok">✓ Pref match</span>',
                              unsafe_allow_html=True)
            else:
                c[5].markdown('<span class="badge-warn">⚑ No pref</span>',
                              unsafe_allow_html=True)

        st.divider()

        # ── Consultation slot ─────────────────────────────────────────────────
        st.markdown('<div class="section-header">Consultation Slot</div>',
                    unsafe_allow_html=True)
        consult_pool = [
            m for m in available
            if m.role in ("Specialist", "Senior Trainee") and m.name != coordinator_name
        ]
        consult_opts = _staff_options(consult_pool)
        consult_default = st.session_state.get(
            "consult_manual",
            (st.session_state.draft.consultation_staff_name
             if st.session_state.draft else "— Unassigned —")
        )
        consult_idx = (consult_opts.index(consult_default)
                       if consult_default in consult_opts else 0)

        col_c1, col_c2 = st.columns([2, 2])
        chosen_consult = col_c1.selectbox(
            "Assign Consultation to", consult_opts,
            index=consult_idx, key="consult_select"
        )
        st.session_state["consult_manual"] = chosen_consult
        col_c2.caption(f"Criteria: `{consult_criteria}`")

        # ── Global violation summary ──────────────────────────────────────────
        all_violations = _check_constraints(
            st.session_state.manual_assign,
            available,
            st.session_state.room_xray,
            coordinator_name or "",
        )
        st.divider()
        if all_violations:
            st.markdown('<div class="card card-danger">'
                        '<strong>⚠ Constraint Violations — resolve before publishing:</strong>',
                        unsafe_allow_html=True)
            for v in all_violations:
                st.markdown(f'<span class="badge-danger">{v}</span>',
                            unsafe_allow_html=True)
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            st.markdown(
                '<div class="card card-ok">✓ No constraint violations detected. '
                'Ready to publish.</div>',
                unsafe_allow_html=True,
            )

        # ── Action buttons ────────────────────────────────────────────────────
        col_pub, col_exp = st.columns([1.5, 1])
        if col_pub.button("✅ Approve & Publish Schedule",
                          type="primary", use_container_width=True,
                          disabled=bool(all_violations)):
            _publish_schedule(target_date, available, coordinator_name)

        if col_exp.button("📥 Export to Excel", use_container_width=True):
            _export_excel(target_date)


# ─────────────────────────────────────────────────────────────────────────────
# Publish
# ─────────────────────────────────────────────────────────────────────────────

def _publish_schedule(target_date, available, coordinator_name):
    ma           = st.session_state.manual_assign
    xray         = st.session_state.room_xray
    stypes       = st.session_state.room_stype
    consult_name = st.session_state.get("consult_manual", "")

    consult_m = next((m for m in available if m.name == consult_name), None)

    assignments_list = []
    for room, a in ma.items():
        lead_m = next((m for m in available if m.name == a.get("lead", "")), None)
        asst_m = next((m for m in available if m.name == a.get("assistant", "")), None)
        if lead_m:
            assignments_list.append({
                "staff_id": lead_m.id, "staff_name": lead_m.name,
                "role_type": "lead", "room": room,
                "surgery_type": stypes.get(room, "General"),
                "is_xray": xray.get(room, False),
            })
        if asst_m:
            assignments_list.append({
                "staff_id": asst_m.id, "staff_name": asst_m.name,
                "role_type": "assistant", "room": room,
                "surgery_type": stypes.get(room, "General"),
                "is_xray": xray.get(room, False),
            })

    if consult_m:
        assignments_list.append({
            "staff_id": consult_m.id, "staff_name": consult_m.name,
            "role_type": "consultation", "room": "CONSULT",
            "surgery_type": "Consultation", "is_xray": False,
        })

    if coordinator_name:
        # Record coordinator in schedule for reference (no stats impact)
        with SessionLocal() as s:
            coord_db = s.query(Staff).filter(Staff.name == coordinator_name).first()
            if coord_db:
                assignments_list.append({
                    "staff_id": coord_db.id, "staff_name": coordinator_name,
                    "role_type": "coordinator", "room": "COORD",
                    "surgery_type": "Day Coordinator", "is_xray": False,
                })

    raw_blob = json.dumps({
        "room_stypes": stypes,
        "room_xray":   {k: bool(v) for k, v in xray.items()},
        "assignments": assignments_list,
        "consultation_name": consult_name,
        "coordinator_name":  coordinator_name or "",
    })

    with SessionLocal() as s:
        sched = s.query(Schedule).filter(Schedule.schedule_date == target_date).first()
        if not sched:
            sched = Schedule(schedule_date=target_date)
            s.add(sched)
        sched.published_at = datetime.now().isoformat(timespec="seconds")
        sched.raw_json     = raw_blob
        s.flush()

        s.query(ScheduleAssignment).filter(
            ScheduleAssignment.schedule_id == sched.id
        ).delete()

        for a in assignments_list:
            if a["role_type"] == "coordinator":
                continue   # don't store coordinator as assignment row
            s.add(ScheduleAssignment(
                schedule_id=sched.id, schedule_date=target_date,
                room=a["room"], surgery_type=a["surgery_type"],
                is_xray=a["is_xray"], role_type=a["role_type"],
                staff_id=a["staff_id"], staff_name=a["staff_name"],
            ))
        s.commit()

    # Update stats (skip coordinator row)
    stat_assignments = [a for a in assignments_list if a["role_type"] != "coordinator"]
    upsert_stats_after_publish(stat_assignments, target_date)

    st.success(
        f"✓ Schedule for {target_date.strftime('%A, %d %b %Y')} published!"
    )
    st.balloons()


# ─────────────────────────────────────────────────────────────────────────────
# Excel export
# ─────────────────────────────────────────────────────────────────────────────

def _export_excel(target_date):
    ma     = st.session_state.manual_assign
    xray   = st.session_state.room_xray
    stypes = st.session_state.room_stype
    consult_name     = st.session_state.get("consult_manual", "")

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

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(rows).to_excel(writer, index=False, sheet_name=f"OT {target_date}")
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
