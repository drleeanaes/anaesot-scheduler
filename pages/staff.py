"""pages/staff.py – Staff roster management, custom Excel import, click-to-edit."""
import json
import io
from datetime import datetime, date
import streamlit as st
import pandas as pd
import openpyxl
from database import SessionLocal, Staff, StaffStats

ROLES = ["Consultant", "Specialist", "Senior Trainee", "Trainee"]
DAYS  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

ROLE_COLOURS = {
    "Consultant":     "#1f8ef1",
    "Specialist":     "#00d4aa",
    "Senior Trainee": "#f59e0b",
    "Trainee":        "#8b949e",
}

DEFAULT_CONSULTANT_ROWS = list(range(4,  17, 2))
DEFAULT_SPECIALIST_ROWS = list(range(18, 93, 2))


# ─────────────────────────────────────────────────────────────────────────────
# Custom roster parser
# ─────────────────────────────────────────────────────────────────────────────

def _cell_is_ot(pm_val, am_val) -> bool:
    """True if colleague is OT-available (cell contains 'OT', with or without *)."""
    pm = str(pm_val).strip().upper() if pm_val else ""
    am = str(am_val).strip().upper() if am_val else ""
    return "OT" in pm or "OT" in am


def _cell_is_coordinator(pm_val, am_val) -> bool:
    """
    True if a Consultant is marked as Day Coordinator for that date.
    OT* means: on OT duty AND designated coordinator for the day.
    Only Consultants get this marker in the roster.
    """
    pm = str(pm_val).strip() if pm_val else ""
    am = str(am_val).strip() if am_val else ""
    return "OT*" in pm or "OT*" in am


def parse_custom_roster(file_obj, consultant_rows, specialist_rows):
    """
    Parse the department's native Excel roster.

    Returns
    -------
    parsed : list of dicts
        name, role, ot_by_date {date: bool},
        coordinator_dates [date],   ← NEW
        pregnant (False — not in file)
    all_dates : sorted list of date objects
    """
    wb = openpyxl.load_workbook(file_obj, data_only=True)
    ws = wb.active

    date_to_col = {}
    for col in range(2, ws.max_column + 1):
        v = ws.cell(2, col).value
        if isinstance(v, datetime):
            date_to_col[v.date()] = col
        elif isinstance(v, date):
            date_to_col[v] = col

    if not date_to_col:
        raise ValueError("No dates found in row 2. Check file format.")

    FOOTER_KEYWORDS = {"1 call", "2 call", "3 call", "rh call", "remark"}

    role_map = {r: "Consultant" for r in consultant_rows}
    role_map.update({r: "Specialist" for r in specialist_rows})

    results = []
    for name_row, role in sorted(role_map.items()):
        raw = ws.cell(name_row, 1).value
        if not raw or not isinstance(raw, str):
            continue
        name = raw.strip()
        if name.lower() in FOOTER_KEYWORDS:
            continue

        am_row = name_row + 1
        ot_by_date       = {}
        coordinator_dates = []

        for d, col in date_to_col.items():
            pm_val = ws.cell(name_row, col).value
            am_val = ws.cell(am_row,   col).value
            ot_by_date[d] = _cell_is_ot(pm_val, am_val)
            if role == "Consultant" and _cell_is_coordinator(pm_val, am_val):
                coordinator_dates.append(d)

        results.append({
            "name":             name,
            "role":             role,
            "ot_by_date":       ot_by_date,
            "coordinator_dates": coordinator_dates,
            "pregnant":         False,
            "specialties":      "",
        })

    return results, sorted(date_to_col.keys())


def _upsert_parsed_staff(parsed: list):
    """Write parsed roster into Staff table. Store coordinator_dates in JSON field."""
    added = updated = 0
    with SessionLocal() as s:
        for p in parsed:
            name = p["name"]
            existing = s.query(Staff).filter(Staff.name == name).first()

            # Weekly OT flags — True if they appear on ANY day of that weekday
            weekday_ot = {i: False for i in range(7)}
            for d, is_on in p["ot_by_date"].items():
                if is_on:
                    weekday_ot[d.weekday()] = True

            ot_vals = {
                "ot_mon": weekday_ot[0], "ot_tue": weekday_ot[1],
                "ot_wed": weekday_ot[2], "ot_thu": weekday_ot[3],
                "ot_fri": weekday_ot[4], "ot_sat": weekday_ot[5],
                "ot_sun": weekday_ot[6],
            }

            # Store coordinator dates and full per-date OT map in specialties
            # We use a reserved JSON prefix in the specialties field for extra data
            # so we don't need a schema change for MVP.
            # Format: "__meta__:{json}" followed optionally by real specialties
            coord_dates_str = json.dumps([str(d) for d in p.get("coordinator_dates", [])])
            ot_dates_str    = json.dumps([str(d) for d, v in p["ot_by_date"].items() if v])

            if existing:
                existing.role     = p["role"]
                existing.pregnant = p["pregnant"]
                for k, v in ot_vals.items():
                    setattr(existing, k, v)
                existing.active = True
                # Preserve existing specialties text, update meta
                existing_specs = existing.specialties or ""
                if existing_specs.startswith("__meta__"):
                    # Strip old meta
                    parts = existing_specs.split("\n", 1)
                    existing_specs = parts[1] if len(parts) > 1 else ""
                existing.specialties = (
                    f"__meta__{json.dumps({'coord': coord_dates_str, 'ot_dates': ot_dates_str})}\n"
                    + existing_specs
                )
                updated += 1
            else:
                meta = f"__meta__{json.dumps({'coord': coord_dates_str, 'ot_dates': ot_dates_str})}\n"
                m = Staff(
                    name=name, role=p["role"],
                    pregnant=p["pregnant"],
                    specialties=meta,
                    **ot_vals,
                )
                s.add(m)
                s.flush()
                s.add(StaffStats(staff_id=m.id))
                added += 1

        s.commit()
    return added, updated


def get_coordinator_dates(staff_obj) -> list:
    """Extract coordinator dates from the staff specialties meta field."""
    specs = staff_obj.specialties or ""
    if not specs.startswith("__meta__"):
        return []
    try:
        meta_line = specs.split("\n", 1)[0].replace("__meta__", "")
        meta = json.loads(meta_line)
        dates_raw = json.loads(meta.get("coord", "[]"))
        return [date.fromisoformat(d) for d in dates_raw]
    except Exception:
        return []


def get_ot_dates(staff_obj) -> list:
    """Extract per-date OT availability from meta."""
    specs = staff_obj.specialties or ""
    if not specs.startswith("__meta__"):
        return []
    try:
        meta_line = specs.split("\n", 1)[0].replace("__meta__", "")
        meta = json.loads(meta_line)
        dates_raw = json.loads(meta.get("ot_dates", "[]"))
        return [date.fromisoformat(d) for d in dates_raw]
    except Exception:
        return []


def get_real_specialties(staff_obj) -> str:
    """Return just the human-readable specialties, stripping meta prefix."""
    specs = staff_obj.specialties or ""
    if specs.startswith("__meta__"):
        parts = specs.split("\n", 1)
        return parts[1].strip() if len(parts) > 1 else ""
    return specs.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Edit / delete helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_staff_edits(staff_id, role, specialties, pregnant, ot_vals):
    with SessionLocal() as s:
        m = s.get(Staff, staff_id)
        if m:
            m.role     = role
            m.pregnant = pregnant
            # Preserve meta prefix, update only human specialties
            existing_specs = m.specialties or ""
            if existing_specs.startswith("__meta__"):
                meta_line = existing_specs.split("\n", 1)[0]
                m.specialties = meta_line + "\n" + specialties
            else:
                m.specialties = specialties
            for k, v in ot_vals.items():
                setattr(m, k, v)
            s.commit()


def _render_edit_panel(staff_id, staff_list):
    m = next((x for x in staff_list if x.id == staff_id), None)
    if not m:
        return
    colour       = ROLE_COLOURS.get(m.role, "#8b949e")
    real_specs   = get_real_specialties(m)
    coord_dates  = get_coordinator_dates(m)

    st.markdown(
        f'<div class="card card-accent" style="border-left-color:{colour};">'
        f'<strong style="font-size:1.05rem;">{m.name}</strong>'
        f'&nbsp;<span style="background:{colour}22;color:{colour};border:1px solid {colour};'
        f'border-radius:4px;padding:2px 8px;font-size:.75rem;">{m.role}</span>'
        + (f'&nbsp;<span class="badge-warn">⚠ Pregnant</span>' if m.pregnant else "")
        + (f'&nbsp;<span class="badge-ok">⭐ Coordinator on {len(coord_dates)} day(s)</span>'
           if coord_dates else "")
        + '</div>',
        unsafe_allow_html=True,
    )

    if coord_dates:
        st.caption(f"Day Coordinator dates this period: "
                   f"{', '.join(d.strftime('%d %b') for d in sorted(coord_dates))}")

    with st.form(key=f"edit_form_{staff_id}"):
        col1, col2 = st.columns(2)
        new_role     = col1.selectbox("Role", ROLES,
                                      index=ROLES.index(m.role) if m.role in ROLES else 0)
        new_pregnant = col2.checkbox("Pregnant", value=m.pregnant)
        new_specs    = st.text_input("Specialties (comma-separated)",
                                     value=real_specs,
                                     placeholder="e.g. neuro, paeds, colorectal")
        st.markdown("**OT Duty Days:**")
        day_cols_ui = st.columns(7)
        ot_sel = {d: day_cols_ui[i].checkbox(
            d, value=getattr(m, f"ot_{d.lower()}", False),
            key=f"edit_ot_{staff_id}_{d}"
        ) for i, d in enumerate(DAYS)}

        col_save, col_cancel = st.columns(2)
        saved    = col_save.form_submit_button("💾 Save", type="primary", use_container_width=True)
        cancelled = col_cancel.form_submit_button("✕ Cancel", use_container_width=True)
        if saved:
            _save_staff_edits(staff_id, new_role, new_specs.strip(), new_pregnant,
                              {f"ot_{d.lower()}": v for d, v in ot_sel.items()})
            st.success(f"✓ {m.name} updated.")
            st.session_state.editing_staff_id = None
            st.rerun()
        if cancelled:
            st.session_state.editing_staff_id = None
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────────────────────

def show():
    st.markdown("## 👥 Staff Management")

    if "editing_staff_id"  not in st.session_state: st.session_state.editing_staff_id  = None
    if "confirm_delete_id" not in st.session_state: st.session_state.confirm_delete_id = None

    tab1, tab2, tab3 = st.tabs(["📋 View Roster", "📤 Upload Roster", "➕ Add Staff"])

    # ── Tab 1: Roster table ───────────────────────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Active Roster</div>', unsafe_allow_html=True)
        st.caption("✏️ = edit details · 🗑 = remove from roster")

        col_filter, col_role = st.columns(2)
        search = col_filter.text_input("🔍 Search by name", placeholder="e.g. Chan")
        role_f = col_role.selectbox("Filter by role", ["All"] + ROLES)

        with SessionLocal() as s:
            q = s.query(Staff).filter(Staff.active == True)
            if search:        q = q.filter(Staff.name.ilike(f"%{search}%"))
            if role_f != "All": q = q.filter(Staff.role == role_f)
            staff_list = q.order_by(Staff.name).all()

        if not staff_list:
            st.info("No staff found. Upload a roster or add staff manually.")
            return

        # Header row
        h = st.columns([2.2, 1.4, 2.0, 0.8, 1.8, 0.5, 0.5])
        for lbl, col in zip(["Name","Role","Specialties","Pregnant","OT Days","",""], h):
            col.markdown(f"<small><b>{lbl}</b></small>", unsafe_allow_html=True)
        st.divider()

        for m in staff_list:
            ot_days    = [d for d in DAYS if getattr(m, f"ot_{d.lower()}", False)]
            colour     = ROLE_COLOURS.get(m.role, "#8b949e")
            is_editing = (st.session_state.editing_staff_id == m.id)
            real_specs = get_real_specialties(m)
            coord_dates= get_coordinator_dates(m)

            row = st.columns([2.2, 1.4, 2.0, 0.8, 1.8, 0.5, 0.5])

            # Name + coordinator badge
            name_display = m.name
            if coord_dates:
                name_display += " ⭐"
            row[0].markdown(name_display)
            row[1].markdown(f'<span style="color:{colour};font-weight:600;">{m.role}</span>',
                            unsafe_allow_html=True)
            row[2].markdown(f'<small>{real_specs or "—"}</small>', unsafe_allow_html=True)
            row[3].markdown("⚠ Yes" if m.pregnant else "No")
            row[4].markdown(f'<small>{", ".join(ot_days) if ot_days else "—"}</small>',
                            unsafe_allow_html=True)

            # ✏️ edit toggle
            if row[5].button("✕" if is_editing else "✏️",
                             key=f"editbtn_{m.id}", use_container_width=True):
                st.session_state.editing_staff_id  = None if is_editing else m.id
                st.session_state.confirm_delete_id = None
                st.rerun()

            # 🗑 delete
            if row[6].button("🗑", key=f"delbtn_{m.id}", use_container_width=True):
                st.session_state.confirm_delete_id = m.id
                st.session_state.editing_staff_id  = None
                st.rerun()

            # Delete confirmation
            if st.session_state.confirm_delete_id == m.id:
                cc = st.columns([3, 1, 1])
                cc[0].warning(f"⚠ Permanently remove **{m.name}**?")
                if cc[1].button("✓ Yes", key=f"confirm_{m.id}",
                                type="primary", use_container_width=True):
                    with SessionLocal() as s:
                        mem = s.get(Staff, m.id)
                        if mem:
                            mem.active = False
                            s.commit()
                    st.session_state.confirm_delete_id = None
                    st.success(f"✓ {m.name} removed.")
                    st.rerun()
                if cc[2].button("✕ No", key=f"canceldelete_{m.id}", use_container_width=True):
                    st.session_state.confirm_delete_id = None
                    st.rerun()

            # Edit panel
            if is_editing:
                with st.container():
                    st.markdown("")
                    _render_edit_panel(m.id, staff_list)
                st.divider()

        # Legend
        st.caption("⭐ = designated Day Coordinator on one or more dates this period")

        # Role tiles
        st.markdown("---")
        st.markdown('<div class="section-header">Role Distribution</div>', unsafe_allow_html=True)
        role_counts = {r: sum(1 for m in staff_list if m.role == r) for r in ROLES}
        tcols = st.columns(4)
        for i, (role, count) in enumerate(role_counts.items()):
            c = ROLE_COLOURS[role]
            tcols[i].markdown(
                f'<div class="metric-tile">'
                f'<div class="val" style="color:{c};">{count}</div>'
                f'<div class="lbl">{role}</div></div>',
                unsafe_allow_html=True,
            )

        # Individual stats
        with st.expander("📊 View individual stats"):
            chosen = st.selectbox("Select colleague", [m.name for m in staff_list])
            cm = next((m for m in staff_list if m.name == chosen), None)
            if cm:
                with SessionLocal() as s:
                    stat = s.query(StaffStats).filter(StaffStats.staff_id == cm.id).first()
                if stat:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Consultations", stat.total_consultations or 0)
                    c2.metric("Rooms",         stat.total_rooms or 0)
                    c3.metric("Last Assigned",
                              str(stat.last_assigned_date) if stat.last_assigned_date else "Never")
                    if stat.surgery_type_counts:
                        sc = json.loads(stat.surgery_type_counts)
                        if sc:
                            st.bar_chart(pd.DataFrame(list(sc.items()), columns=["Type","Count"])
                                         .set_index("Type"))

    # ── Tab 2: Upload ─────────────────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Upload Department Roster (Excel)</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        <strong>Your native roster format is fully supported:</strong><br><br>
        • <b>Row 1</b> – Day names &nbsp;|&nbsp; <b>Row 2</b> – Dates<br>
        • <b>Column A</b> – Colleague name (every other row; row below = AM entries)<br>
        • Cell contains <code>OT</code> → <b>available for OT</b> that day<br>
        • Cell contains <code>OT*</code> → available <b>AND designated Day Coordinator ⭐</b><br>
        • Rows 4–16 → <b>Consultant</b> &nbsp;|&nbsp; Rows 18–92 → <b>Specialist</b><br>
        • Day Coordinators are flagged in the scheduler and <b>will not be assigned room duty</b>
        </div>
        """, unsafe_allow_html=True)

        with st.expander("⚙️ Adjust role row boundaries (only if your file differs)"):
            col_a, col_b = st.columns(2)
            cons_start = col_a.number_input("Consultant — first name row", value=4,  min_value=1)
            cons_end   = col_a.number_input("Consultant — last name row",  value=16, min_value=1)
            spec_start = col_b.number_input("Specialist — first name row", value=18, min_value=1)
            spec_end   = col_b.number_input("Specialist — last name row",  value=92, min_value=1)

        cons_rows = list(range(int(cons_start), int(cons_end) + 1, 2))
        spec_rows = list(range(int(spec_start), int(spec_end) + 1, 2))

        uploaded = st.file_uploader("Choose your department roster (.xlsx)", type=["xlsx","xls"])

        if uploaded:
            try:
                parsed, all_dates = parse_custom_roster(uploaded, cons_rows, spec_rows)
            except Exception as e:
                st.error(f"Could not parse file: {e}")
                return

            # Summary
            coordinators = [(p["name"], p["coordinator_dates"])
                            for p in parsed if p["coordinator_dates"]]
            st.success(f"✓ Parsed **{len(parsed)}** colleagues · "
                       f"**{len(all_dates)}** days "
                       f"({min(all_dates)} → {max(all_dates)}) · "
                       f"**{len(coordinators)}** Day Coordinator designations found")

            # Day coordinator callout
            if coordinators:
                with st.expander(f"⭐ Day Coordinators detected ({len(coordinators)} consultants)"):
                    for name, dates in coordinators:
                        st.markdown(
                            f"**{name}** — coordinator on: "
                            + ", ".join(d.strftime("%d %b") for d in sorted(dates))
                        )

            # Preview table
            preview_rows = []
            for p in parsed[:12]:
                ot_days = [str(d) for d, v in p["ot_by_date"].items() if v]
                preview_rows.append({
                    "Name":      p["name"],
                    "Role":      p["role"],
                    "Coordinator days": len(p["coordinator_dates"]),
                    "OT days":   f"{len(ot_days)} "
                                 f"({', '.join(d[-5:] for d in ot_days[:4])}"
                                 f"{'…' if len(ot_days) > 4 else ''})",
                })
            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

            st.info("💡 Specialties (neuro, paeds, etc.) are not in this file — "
                    "add them per person using ✏️ in the View Roster tab after import.")

            if st.button("✅ Import into Database", type="primary"):
                added, updated = _upsert_parsed_staff(parsed)
                st.success(f"Import complete: **{added}** added · **{updated}** updated.")
                st.rerun()

    # ── Tab 3: Add manually ───────────────────────────────────────────────────
    with tab3:
        st.markdown('<div class="section-header">Add New Colleague Manually</div>',
                    unsafe_allow_html=True)
        with st.form("add_staff_form"):
            col1, col2 = st.columns(2)
            name     = col1.text_input("Full Name *")
            role     = col2.selectbox("Role *", ROLES)
            specs    = col1.text_input("Specialties", placeholder="neuro, paeds")
            pregnant = col2.checkbox("Pregnant")
            st.markdown("**OT Days:**")
            day_cols_ui = st.columns(7)
            ot_sel = {d: day_cols_ui[i].checkbox(d, key=f"add_ot_{d}")
                      for i, d in enumerate(DAYS)}
            if st.form_submit_button("➕ Add Colleague", type="primary"):
                if not name.strip():
                    st.error("Name is required.")
                else:
                    with SessionLocal() as s:
                        if s.query(Staff).filter(Staff.name == name.strip()).first():
                            st.warning(f"'{name}' already exists.")
                        else:
                            m = Staff(
                                name=name.strip(), role=role,
                                pregnant=pregnant, specialties=specs.strip(),
                                ot_mon=ot_sel["Mon"], ot_tue=ot_sel["Tue"],
                                ot_wed=ot_sel["Wed"], ot_thu=ot_sel["Thu"],
                                ot_fri=ot_sel["Fri"], ot_sat=ot_sel["Sat"],
                                ot_sun=ot_sel["Sun"],
                            )
                            s.add(m)
                            s.flush()
                            s.add(StaffStats(staff_id=m.id))
                            s.commit()
                            st.success(f"✓ Added {name}")
                            st.rerun()
