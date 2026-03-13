"""pages/staff.py – Staff roster management & Excel upload."""
import json
import io
from datetime import date
import streamlit as st
import pandas as pd
from database import SessionLocal, Staff, StaffStats, init_db

ROLES = ["Consultant", "Specialist", "Senior Trainee", "Trainee"]
DAYS  = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_COLS = [f"ot_{d.lower()}" for d in DAYS]

ROLE_COLOURS = {
    "Consultant":     "#1f8ef1",
    "Specialist":     "#00d4aa",
    "Senior Trainee": "#f59e0b",
    "Trainee":        "#8b949e",
}


def _import_df_to_db(df: pd.DataFrame, col_map: dict):
    """Import parsed DataFrame into the Staff table."""
    added = updated = skipped = 0
    with SessionLocal() as s:
        for _, row in df.iterrows():
            name = str(row.get(col_map["Name"], "")).strip()
            if not name:
                skipped += 1
                continue

            role = str(row.get(col_map["Role"], "Specialist")).strip()
            if role not in ROLES:
                role = "Specialist"

            pregnant_raw = str(row.get(col_map.get("Pregnant", "Pregnant"), "No")).strip().lower()
            pregnant = pregnant_raw in ("yes", "y", "true", "1")

            specs = str(row.get(col_map.get("Specialties", "Specialties"), "")).strip()

            ot_vals = {}
            for d in DAYS:
                mapped_col = col_map.get(f"OT_{d}", f"OT_{d}")
                raw = str(row.get(mapped_col, "No")).strip().lower()
                ot_vals[f"ot_{d.lower()}"] = raw in ("yes", "y", "true", "1")

            existing = s.query(Staff).filter(Staff.name == name).first()
            if existing:
                existing.role        = role
                existing.pregnant    = pregnant
                existing.specialties = specs
                for k, v in ot_vals.items():
                    setattr(existing, k, v)
                existing.active = True
                updated += 1
            else:
                m = Staff(name=name, role=role, pregnant=pregnant, specialties=specs, **ot_vals)
                s.add(m)
                s.flush()
                s.add(StaffStats(staff_id=m.id))
                added += 1

        s.commit()
    return added, updated, skipped


def _save_staff_edits(staff_id: int, role: str, specialties: str,
                      pregnant: bool, ot_vals: dict):
    """Persist edits for a single staff member."""
    with SessionLocal() as s:
        m = s.get(Staff, staff_id)
        if m:
            m.role        = role
            m.specialties = specialties
            m.pregnant    = pregnant
            for k, v in ot_vals.items():
                setattr(m, k, v)
            s.commit()


def _render_edit_panel(staff_id: int, staff_list: list):
    """Render the slide-in edit panel for the selected staff member."""
    m = next((x for x in staff_list if x.id == staff_id), None)
    if not m:
        return

    colour = ROLE_COLOURS.get(m.role, "#8b949e")
    st.markdown(f"""
    <div class="card card-accent" style="border-left-color:{colour};">
        <strong style="font-size:1.05rem;">{m.name}</strong>
        &nbsp;<span class="badge-ok" style="background:{colour}22;color:{colour};border-color:{colour};">
            {m.role}
        </span>
        {"&nbsp;<span class='badge-warn'>⚠ Pregnant</span>" if m.pregnant else ""}
    </div>
    """, unsafe_allow_html=True)

    with st.form(key=f"edit_form_{staff_id}"):
        col1, col2 = st.columns(2)

        # Role selector
        current_role_idx = ROLES.index(m.role) if m.role in ROLES else 0
        new_role = col1.selectbox("Role", ROLES, index=current_role_idx)

        # Pregnant toggle
        new_pregnant = col2.checkbox("Pregnant", value=m.pregnant)

        # Specialties
        new_specs = st.text_input(
            "Specialties (comma-separated)",
            value=m.specialties,
            placeholder="e.g. neuro, paeds, colorectal",
        )

        # OT days
        st.markdown("**OT Duty Days:**")
        day_cols_ui = st.columns(7)
        ot_selections = {}
        for i, d in enumerate(DAYS):
            current_val = getattr(m, f"ot_{d.lower()}", False)
            ot_selections[d] = day_cols_ui[i].checkbox(d, value=current_val,
                                                        key=f"edit_ot_{staff_id}_{d}")

        col_save, col_cancel = st.columns([1, 1])
        saved    = col_save.form_submit_button("💾 Save Changes", type="primary",
                                               use_container_width=True)
        cancelled = col_cancel.form_submit_button("✕ Cancel", use_container_width=True)

        if saved:
            ot_vals = {f"ot_{d.lower()}": v for d, v in ot_selections.items()}
            _save_staff_edits(staff_id, new_role, new_specs.strip(), new_pregnant, ot_vals)
            st.success(f"✓ {m.name} updated successfully.")
            st.session_state.editing_staff_id = None
            st.rerun()

        if cancelled:
            st.session_state.editing_staff_id = None
            st.rerun()


def show():
    st.markdown("## 👥 Staff Management")

    # Initialise edit state
    if "editing_staff_id" not in st.session_state:
        st.session_state.editing_staff_id = None

    tab1, tab2, tab3 = st.tabs(["📋 View Roster", "📤 Upload Excel", "➕ Add / Edit Staff"])

    # ── Tab 1: View roster with click-to-edit ────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Active Roster</div>', unsafe_allow_html=True)
        st.caption("Click ✏️ next to any staff member to edit their role, specialties, OT days or pregnant status.")

        col_filter, col_role = st.columns(2)
        search = col_filter.text_input("🔍 Search by name", placeholder="e.g. Smith")
        role_f = col_role.selectbox("Filter by role", ["All"] + ROLES)

        with SessionLocal() as s:
            q = s.query(Staff).filter(Staff.active == True)
            if search:
                q = q.filter(Staff.name.ilike(f"%{search}%"))
            if role_f != "All":
                q = q.filter(Staff.role == role_f)
            staff_list = q.order_by(Staff.name).all()

        if not staff_list:
            st.info("No staff found. Upload a roster or add staff manually.")
        else:
            # ── Roster table with edit buttons ────────────────────────────────
            # Header row
            h = st.columns([2.2, 1.4, 2.2, 1, 1.8, 0.6])
            for label, col in zip(["Name", "Role", "Specialties", "Pregnant", "OT Days", ""], h):
                col.markdown(f"<small><b>{label}</b></small>", unsafe_allow_html=True)
            st.divider()

            for m in staff_list:
                ot_days = [d for d in DAYS if getattr(m, f"ot_{d.lower()}", False)]
                colour  = ROLE_COLOURS.get(m.role, "#8b949e")
                is_editing = st.session_state.editing_staff_id == m.id

                row = st.columns([2.2, 1.4, 2.2, 1, 1.8, 0.6])
                row[0].markdown(m.name)
                row[1].markdown(
                    f'<span style="color:{colour};font-weight:600;">{m.role}</span>',
                    unsafe_allow_html=True,
                )
                row[2].markdown(
                    f'<small>{m.specialties if m.specialties else "—"}</small>',
                    unsafe_allow_html=True,
                )
                row[3].markdown("⚠ Yes" if m.pregnant else "No")
                row[4].markdown(
                    f'<small>{", ".join(ot_days) if ot_days else "—"}</small>',
                    unsafe_allow_html=True,
                )

                # Toggle edit button
                btn_label = "✕" if is_editing else "✏️"
                if row[5].button(btn_label, key=f"editbtn_{m.id}", use_container_width=True):
                    if is_editing:
                        st.session_state.editing_staff_id = None
                    else:
                        st.session_state.editing_staff_id = m.id
                    st.rerun()

                # Inline edit panel — appears directly below the row
                if is_editing:
                    with st.container():
                        st.markdown("")
                        _render_edit_panel(m.id, staff_list)
                    st.divider()

            st.markdown("---")

            # ── Quick role reassignment summary ───────────────────────────────
            st.markdown('<div class="section-header">Role Distribution</div>', unsafe_allow_html=True)
            role_counts = {r: sum(1 for m in staff_list if m.role == r) for r in ROLES}
            cols = st.columns(4)
            for i, (role, count) in enumerate(role_counts.items()):
                colour = ROLE_COLOURS[role]
                cols[i].markdown(
                    f'<div class="metric-tile">'
                    f'<div class="val" style="color:{colour};">{count}</div>'
                    f'<div class="lbl">{role}</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

            # ── Individual stats expander ──────────────────────────────────────
            with st.expander("📊 View individual stats"):
                names  = [m.name for m in staff_list]
                chosen = st.selectbox("Select staff member", names)
                chosen_staff = next((m for m in staff_list if m.name == chosen), None)
                if chosen_staff:
                    with SessionLocal() as s:
                        stat = s.query(StaffStats).filter(
                            StaffStats.staff_id == chosen_staff.id
                        ).first()
                    if stat:
                        c1, c2, c3 = st.columns(3)
                        c1.metric("Total Consultations", stat.total_consultations or 0)
                        c2.metric("Total Rooms",         stat.total_rooms or 0)
                        c3.metric("Last Assigned",
                                  str(stat.last_assigned_date) if stat.last_assigned_date else "Never")
                        if stat.surgery_type_counts:
                            sc = json.loads(stat.surgery_type_counts)
                            if sc:
                                sc_df = pd.DataFrame(
                                    list(sc.items()), columns=["Surgery Type", "Count"]
                                )
                                st.bar_chart(sc_df.set_index("Surgery Type"))

    # ── Tab 2: Upload Excel ───────────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Upload Weekly Roster (Excel)</div>', unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        Expected columns (exact names, or map below):<br>
        <code>Name</code>, <code>Role</code>, <code>Pregnant</code>, <code>Specialties</code>,
        <code>OT_Mon</code>, <code>OT_Tue</code>, <code>OT_Wed</code>, <code>OT_Thu</code>,
        <code>OT_Fri</code>, <code>OT_Sat</code>, <code>OT_Sun</code>
        </div>
        """, unsafe_allow_html=True)

        uploaded = st.file_uploader("Choose Excel file (.xlsx / .xls)", type=["xlsx", "xls"])

        if uploaded:
            try:
                df_raw = pd.read_excel(uploaded)
            except Exception as e:
                st.error(f"Could not read file: {e}")
                return

            st.markdown("**Preview (first 5 rows):**")
            st.dataframe(df_raw.head(), use_container_width=True)

            cols = list(df_raw.columns)
            expected = ["Name", "Role", "Pregnant", "Specialties",
                        "OT_Mon", "OT_Tue", "OT_Wed", "OT_Thu", "OT_Fri", "OT_Sat", "OT_Sun"]

            col_map     = {}
            auto_matched = all(e in cols for e in expected)

            if auto_matched:
                col_map = {e: e for e in expected}
                st.success("✓ All expected columns detected automatically.")
            else:
                st.warning("Could not auto-detect all columns. Please map them manually:")
                for e in expected:
                    default_idx = cols.index(e) if e in cols else 0
                    col_map[e] = st.selectbox(
                        f"Map → {e}", cols, index=default_idx, key=f"map_{e}"
                    )

            if st.button("✅ Import Roster", type="primary"):
                added, updated, skipped = _import_df_to_db(df_raw, col_map)
                st.success(
                    f"Import complete: **{added}** added, **{updated}** updated, **{skipped}** skipped."
                )
                st.rerun()

        st.markdown('<div class="section-header">Download Sample Roster Template</div>',
                    unsafe_allow_html=True)
        if st.button("📥 Generate Sample XLSX"):
            sample_data = {
                "Name":        ["Dr. Alice Chen",    "Dr. Bob Patel",    "Dr. Carol Mensah",
                                "Dr. David Kim",     "Dr. Eva Torres"],
                "Role":        ["Consultant",         "Specialist",       "Specialist",
                                "Senior Trainee",    "Trainee"],
                "Pregnant":    ["No","No","Yes","No","No"],
                "Specialties": ["neuro, paeds",       "neuro, colorectal","obstetric, general",
                                "vascular, urology", "general"],
                "OT_Mon":  ["Yes","Yes","Yes","Yes","No"],
                "OT_Tue":  ["Yes","No", "Yes","Yes","Yes"],
                "OT_Wed":  ["No", "Yes","No", "Yes","Yes"],
                "OT_Thu":  ["Yes","Yes","Yes","No", "Yes"],
                "OT_Fri":  ["Yes","Yes","No", "Yes","No"],
                "OT_Sat":  ["No", "No", "No", "No", "No"],
                "OT_Sun":  ["No", "No", "No", "No", "No"],
            }
            sample_df = pd.DataFrame(sample_data)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                sample_df.to_excel(writer, index=False, sheet_name="Roster")
            buf.seek(0)
            st.download_button(
                "⬇️ Download sample_roster.xlsx",
                data=buf,
                file_name="sample_roster.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    # ── Tab 3: Add / Edit staff ───────────────────────────────────────────────
    with tab3:
        st.markdown('<div class="section-header">Add New Staff Member</div>', unsafe_allow_html=True)

        with st.form("add_staff_form"):
            col1, col2 = st.columns(2)
            name     = col1.text_input("Full Name *")
            role     = col2.selectbox("Role *", ROLES)
            specs    = col1.text_input("Specialties (comma-separated)", placeholder="neuro, paeds")
            pregnant = col2.checkbox("Pregnant")

            st.markdown("**OT Days (on duty):**")
            day_cols_ui = st.columns(7)
            ot_selections = {}
            for i, d in enumerate(DAYS):
                ot_selections[d] = day_cols_ui[i].checkbox(d, key=f"add_ot_{d}")

            submitted = st.form_submit_button("➕ Add Staff Member", type="primary")
            if submitted:
                if not name.strip():
                    st.error("Name is required.")
                else:
                    with SessionLocal() as s:
                        existing = s.query(Staff).filter(Staff.name == name.strip()).first()
                        if existing:
                            st.warning(f"Staff member '{name}' already exists.")
                        else:
                            m = Staff(
                                name=name.strip(), role=role,
                                pregnant=pregnant, specialties=specs.strip(),
                                ot_mon=ot_selections["Mon"], ot_tue=ot_selections["Tue"],
                                ot_wed=ot_selections["Wed"], ot_thu=ot_selections["Thu"],
                                ot_fri=ot_selections["Fri"], ot_sat=ot_selections["Sat"],
                                ot_sun=ot_selections["Sun"],
                            )
                            s.add(m)
                            s.flush()
                            s.add(StaffStats(staff_id=m.id))
                            s.commit()
                            st.success(f"✓ Added {name}")
                            st.rerun()

        # Deactivate
        st.markdown('<div class="section-header">Deactivate Staff Member</div>', unsafe_allow_html=True)
        with SessionLocal() as s:
            active = s.query(Staff).filter(Staff.active == True).all()
        if active:
            names_active = [m.name for m in active]
            to_deactivate = st.selectbox("Select staff to deactivate", ["— Select —"] + names_active)
            if to_deactivate != "— Select —":
                if st.button(f"🗑 Deactivate {to_deactivate}", type="secondary"):
                    with SessionLocal() as s:
                        m = s.query(Staff).filter(Staff.name == to_deactivate).first()
                        if m:
                            m.active = False
                            s.commit()
                    st.success(f"Deactivated {to_deactivate}")
                    st.rerun()
