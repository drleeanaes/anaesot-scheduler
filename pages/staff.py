"""pages/staff.py – Staff roster management, custom Excel import, click-to-edit/delete."""
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

# ─────────────────────────────────────────────────────────────────────────────
# Meta helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_meta(staff_obj) -> dict:
    specs = staff_obj.specialties or ""
    if not specs.startswith("__meta__"):
        return {}
    try:
        return json.loads(specs.split("\n", 1)[0].replace("__meta__", ""))
    except Exception:
        return {}

def get_coordinator_dates(staff_obj) -> list:
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("coord", "[]"))]
    except Exception:
        return []

def get_ot_dates(staff_obj) -> list:
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("ot_dates", "[]"))]
    except Exception:
        return []

def get_am_call_dates(staff_obj) -> list:
    """Dates where colleague is on am 1/2/3 call (AM Emergency Team)."""
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("am_call_dates", "[]"))]
    except Exception:
        return []

def get_am_ot_dates(staff_obj) -> list:
    """Dates where colleague has explicit AM OT availability."""
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("am_ot_dates", "[]"))]
    except Exception:
        return []

def get_pm_paac_dates(staff_obj) -> list:
    """Dates where colleague has pm PAAC duty."""
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("pm_paac_dates", "[]"))]
    except Exception:
        return []

def get_am_blocked_dates(staff_obj) -> list:
    """Dates where AM is blocked (am meeting, am POMC, am sick, etc.)."""
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("am_blocked", "[]"))]
    except Exception:
        return []

def get_pm_blocked_dates(staff_obj) -> list:
    """Dates where PM is blocked (pm meeting, pm PAAC, pm sick, etc.)."""
    try:
        return [date.fromisoformat(d) for d in json.loads(_parse_meta(staff_obj).get("pm_blocked", "[]"))]
    except Exception:
        return []

def get_real_specialties(staff_obj) -> str:
    specs = staff_obj.specialties or ""
    if specs.startswith("__meta__"):
        parts = specs.split("\n", 1)
        return parts[1].strip() if len(parts) > 1 else ""
    return specs.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Cell classifiers
# ─────────────────────────────────────────────────────────────────────────────

def _classify_cell(pm_val, am_val):
    """
    Returns dict of booleans describing a colleague's availability for a date.
    
    Keys:
      ot_available   – appears in OT pool for this date
      am_call        – on am 1/2/3 call (AM Emergency Team; available but NOT assigned rooms)
      pm_paac        – has pm PAAC (listed separately; NOT assigned to OT rooms)
      am_blocked     – AM session unavailable (am meeting, am POMC, am sick etc.)
      pm_blocked     – PM session unavailable (pm meeting, pm PAAC, pm sick etc.)
      am_ot          – explicitly available for AM OT
      pm_ot          – explicitly available for PM OT
    """
    pm = str(pm_val).strip() if pm_val else ""
    am = str(am_val).strip() if am_val else ""
    pm_l = pm.lower()
    am_l = am.lower()
    combined_l = pm_l + " " + am_l

    # ── AM call ───────────────────────────────────────────────────────────────
    am_call = any(p in combined_l for p in ["am 1 call", "am 2 call", "am 3 call"])

    # ── PM PAAC ───────────────────────────────────────────────────────────────
    pm_paac = "pm paac" in pm_l or "pm paac" in am_l

    # ── AM blocked ────────────────────────────────────────────────────────────
    AM_BLOCK_PATTERNS = ["am meeting", "am pomc", "am sick", "am paac", "am rh meeting"]
    am_blocked = any(p in combined_l for p in AM_BLOCK_PATTERNS)

    # ── PM blocked ────────────────────────────────────────────────────────────
    PM_BLOCK_PATTERNS = ["pm meeting", "pm paac", "pm sick", "pm off"]
    pm_blocked = any(p in combined_l for p in PM_BLOCK_PATTERNS)

    # ── Explicit AM/PM OT ──────────────────────────────────────────────────────
    am_ot = "am ot" in am_l or "am ot" in pm_l
    pm_ot = "pm ot" in pm_l or "pm ot" in am_l

    # ── Whole-day OT ──────────────────────────────────────────────────────────
    # "OT" token in PM cell (e.g. OT, OT*, OT-, OT/Pain) = whole day OT
    ot_whole = (
        "OT" in pm.upper() and
        "AM OT" not in pm.upper() and
        "PM OT" not in pm.upper()
    ) or (
        "OT" in am.upper() and
        "AM OT" not in am.upper() and
        "PM OT" not in am.upper()
    )

    # Only expand to whole-day availability if OT is confirmed in the cell
    # Non-OT values (AL, off, Pain, RH, Sick, Study, ML etc.) do NOT make
    # someone available — they must have explicit OT, am OT, pm OT, or am call
    if ot_whole and not pm_paac:
        am_ot = am_ot or (not am_blocked)
        pm_ot = pm_ot or (not pm_blocked)
    
    # am call colleagues are available AM (emergency team)
    if am_call:
        am_ot = True

    # pm PAAC blocks PM
    if pm_paac:
        pm_ot = False
        pm_blocked = True

    # ot_available = appears in the pool at all
    ot_available = am_ot or pm_ot or am_call or pm_paac  # pm_paac always enter pool to show in PAAC panel

    return {
        "ot_available": ot_available,
        "am_call":      am_call,
        "pm_paac":      pm_paac,
        "am_blocked":   am_blocked,
        "pm_blocked":   pm_blocked,
        "am_ot":        am_ot,
        "pm_ot":        pm_ot,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Custom roster parser
# ─────────────────────────────────────────────────────────────────────────────

def _cell_is_coordinator(pm_val, am_val) -> bool:
    pm = str(pm_val).strip() if pm_val else ""
    am = str(am_val).strip() if am_val else ""
    return "OT*" in pm or "OT*" in am


def parse_custom_roster(file_obj, consultant_rows, specialist_rows, trainee_rows=None):
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
        raise ValueError("No dates found in row 2. Check the file format.")

    FOOTER_KEYWORDS = {"1 call", "2 call", "3 call", "rh call", "remark"}
    role_map = {r: "Consultant" for r in consultant_rows}
    role_map.update({r: "Specialist" for r in specialist_rows})
    if trainee_rows:
        role_map.update({r: "Trainee" for r in trainee_rows})

    results = []
    for name_row, role in sorted(role_map.items()):
        raw = ws.cell(name_row, 1).value
        if not raw or not isinstance(raw, str):
            continue
        name = raw.strip()
        if name.lower() in FOOTER_KEYWORDS:
            continue

        am_row = name_row - 1  # AM sub-row is the row ABOVE the name, not below
        ot_by_date         = {}
        am_ot_dates        = []
        pm_ot_dates        = []
        coordinator_dates  = []
        am_call_dates      = []
        pm_paac_dates      = []
        am_blocked_dates   = []
        pm_blocked_dates   = []

        for d, col in date_to_col.items():
            pm_val = ws.cell(name_row, col).value
            am_val = ws.cell(am_row,   col).value

            flags = _classify_cell(pm_val, am_val)
            ot_by_date[d] = flags["ot_available"]

            if flags["ot_available"]:
                if flags["am_ot"]:  am_ot_dates.append(d)
                if flags["pm_ot"]:  pm_ot_dates.append(d)
            if flags["am_call"]:    am_call_dates.append(d)
            if flags["pm_paac"]:    pm_paac_dates.append(d)
            if flags["am_blocked"]: am_blocked_dates.append(d)
            if flags["pm_blocked"]: pm_blocked_dates.append(d)

            if role == "Consultant" and _cell_is_coordinator(pm_val, am_val):
                coordinator_dates.append(d)

        results.append({
            "name":             name,
            "role":             role,
            "ot_by_date":       ot_by_date,
            "am_ot_dates":      am_ot_dates,
            "pm_ot_dates":      pm_ot_dates,
            "coordinator_dates": coordinator_dates,
            "am_call_dates":    am_call_dates,
            "pm_paac_dates":    pm_paac_dates,
            "am_blocked_dates": am_blocked_dates,
            "pm_blocked_dates": pm_blocked_dates,
            "pregnant":         False,
            "specialties":      "",
        })

    return results, sorted(date_to_col.keys())


def _upsert_parsed_staff(parsed: list):
    added = updated = 0
    with SessionLocal() as s:
        for p in parsed:
            name = p["name"]
            weekday_ot = {i: False for i in range(7)}
            for d, is_on in p["ot_by_date"].items():
                if is_on:
                    weekday_ot[d.weekday()] = True
            # Also mark weekdays where colleague has pm_paac (so they enter the pool)
            for d in p.get("pm_paac_dates", []):
                weekday_ot[d.weekday()] = True
            ot_vals = {
                "ot_mon": weekday_ot[0], "ot_tue": weekday_ot[1],
                "ot_wed": weekday_ot[2], "ot_thu": weekday_ot[3],
                "ot_fri": weekday_ot[4], "ot_sat": weekday_ot[5],
                "ot_sun": weekday_ot[6],
            }

            def _ds(lst): return json.dumps([str(d) for d in lst])

            # ot_dates includes pm_paac dates so those colleagues enter the scheduler pool
            all_active_dates = [
                d for d, v in p["ot_by_date"].items() if v
            ] + [d for d in p.get("pm_paac_dates", [])
                 if d not in [x for x, v in p["ot_by_date"].items() if v]]

            meta = json.dumps({
                "coord":         _ds(p.get("coordinator_dates", [])),
                "ot_dates":      _ds(all_active_dates),
                "am_ot_dates":   _ds(p.get("am_ot_dates", [])),
                "pm_ot_dates":   _ds(p.get("pm_ot_dates", [])),
                "am_call_dates": _ds(p.get("am_call_dates", [])),
                "pm_paac_dates": _ds(p.get("pm_paac_dates", [])),
                "am_blocked":    _ds(p.get("am_blocked_dates", [])),
                "pm_blocked":    _ds(p.get("pm_blocked_dates", [])),
            })
            meta_prefix = f"__meta__{meta}\n"

            existing = s.query(Staff).filter(Staff.name == name).first()
            if existing:
                existing.role = p["role"]
                existing.pregnant = p["pregnant"]
                for k, v in ot_vals.items():
                    setattr(existing, k, v)
                existing.active = True
                old = existing.specialties or ""
                human = old.split("\n", 1)[1].strip() if old.startswith("__meta__") else old.strip()
                existing.specialties = meta_prefix + human
                updated += 1
            else:
                m = Staff(name=name, role=p["role"], pregnant=False,
                          specialties=meta_prefix, **ot_vals)
                s.add(m)
                s.flush()
                s.add(StaffStats(staff_id=m.id))
                added += 1
        s.commit()
    return added, updated


# ─────────────────────────────────────────────────────────────────────────────
# Edit / delete helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_staff_edits(staff_id, role, specialties, pregnant, ot_vals):
    with SessionLocal() as s:
        m = s.get(Staff, staff_id)
        if m:
            m.role = role
            m.pregnant = pregnant
            old = m.specialties or ""
            meta_line = old.split("\n", 1)[0] if old.startswith("__meta__") else ""
            m.specialties = (meta_line + "\n" + specialties) if meta_line else specialties
            for k, v in ot_vals.items():
                setattr(m, k, v)
            s.commit()


def _render_edit_panel(staff_id, staff_list):
    m = next((x for x in staff_list if x.id == staff_id), None)
    if not m:
        return
    colour      = ROLE_COLOURS.get(m.role, "#8b949e")
    real_specs  = get_real_specialties(m)
    coord_dates = get_coordinator_dates(m)

    badges = (f'<span style="background:{colour}22;color:{colour};border:1px solid {colour};'
              f'border-radius:4px;padding:2px 8px;font-size:.75rem;">{m.role}</span>')
    if m.pregnant:
        badges += ' <span class="badge-warn">⚠ Pregnant</span>'
    if coord_dates:
        badges += f' <span class="badge-ok">⭐ Coordinator {len(coord_dates)} day(s)</span>'
    st.markdown(
        f'<div class="card card-accent" style="border-left-color:{colour};">'
        f'<strong style="font-size:1.05rem;">{m.name}</strong> {badges}</div>',
        unsafe_allow_html=True,
    )
    if coord_dates:
        st.caption("Coordinator on: " + ", ".join(d.strftime("%d %b") for d in sorted(coord_dates)))

    with st.form(key=f"edit_form_{staff_id}"):
        col1, col2 = st.columns(2)
        new_role     = col1.selectbox("Role", ROLES, index=ROLES.index(m.role) if m.role in ROLES else 0)
        new_pregnant = col2.checkbox("Pregnant", value=m.pregnant)
        new_specs    = st.text_input("Specialties (comma-separated)", value=real_specs,
                                     placeholder="e.g. neuro, paeds, colorectal")
        st.markdown("**OT Duty Days:**")
        day_cols_ui = st.columns(7)
        ot_sel = {d: day_cols_ui[i].checkbox(d, value=getattr(m, f"ot_{d.lower()}", False),
                                              key=f"eot_{staff_id}_{d}")
                  for i, d in enumerate(DAYS)}
        cs, cc = st.columns(2)
        if cs.form_submit_button("💾 Save", type="primary", use_container_width=True):
            _save_staff_edits(staff_id, new_role, new_specs.strip(), new_pregnant,
                              {f"ot_{d.lower()}": v for d, v in ot_sel.items()})
            st.success(f"✓ {m.name} updated.")
            st.session_state.editing_staff_id = None
            st.rerun()
        if cc.form_submit_button("✕ Cancel", use_container_width=True):
            st.session_state.editing_staff_id = None
            st.rerun()


# ─────────────────────────────────────────────────────────────────────────────
# Main show()
# ─────────────────────────────────────────────────────────────────────────────

def show():
    st.markdown("## 👥 Staff Management")

    if "editing_staff_id"  not in st.session_state: st.session_state.editing_staff_id  = None
    if "confirm_delete_id" not in st.session_state: st.session_state.confirm_delete_id = None

    tab1, tab2, tab3 = st.tabs(["📋 View Roster", "📤 Upload Roster", "➕ Add Staff"])

    with tab1:
        st.markdown('<div class="section-header">Active Roster</div>', unsafe_allow_html=True)
        st.caption("✏️ = edit  ·  🗑 = remove from roster")
        col_filter, col_role = st.columns(2)
        search = col_filter.text_input("🔍 Search by name", placeholder="e.g. Chan")
        role_f = col_role.selectbox("Filter by role", ["All"] + ROLES)
        with SessionLocal() as s:
            q = s.query(Staff).filter(Staff.active == True)
            if search:          q = q.filter(Staff.name.ilike(f"%{search}%"))
            if role_f != "All": q = q.filter(Staff.role == role_f)
            staff_list = q.order_by(Staff.name).all()

        if not staff_list:
            st.info("No staff found. Use the **Upload Roster** tab to import your Excel file.")
        else:
            h = st.columns([2.2, 1.4, 2.0, 0.8, 1.8, 0.5, 0.5])
            for lbl, col in zip(["Name","Role","Specialties","Pregnant","OT Days","",""], h):
                col.markdown(f"<small><b>{lbl}</b></small>", unsafe_allow_html=True)
            st.divider()

            for m in staff_list:
                ot_days    = [d for d in DAYS if getattr(m, f"ot_{d.lower()}", False)]
                colour     = ROLE_COLOURS.get(m.role, "#8b949e")
                is_editing = (st.session_state.editing_staff_id == m.id)
                real_specs = get_real_specialties(m)
                coord_dates = get_coordinator_dates(m)

                row = st.columns([2.2, 1.4, 2.0, 0.8, 1.8, 0.5, 0.5])
                row[0].markdown(("⭐ " if coord_dates else "") + m.name)
                row[1].markdown(f'<span style="color:{colour};font-weight:600;">{m.role}</span>',
                                unsafe_allow_html=True)
                row[2].markdown(f'<small>{real_specs or "—"}</small>', unsafe_allow_html=True)
                row[3].markdown("⚠ Yes" if m.pregnant else "No")
                row[4].markdown(f'<small>{", ".join(ot_days) if ot_days else "—"}</small>',
                                unsafe_allow_html=True)

                if row[5].button("✕" if is_editing else "✏️",
                                 key=f"editbtn_{m.id}", use_container_width=True):
                    st.session_state.editing_staff_id  = None if is_editing else m.id
                    st.session_state.confirm_delete_id = None
                    st.rerun()
                if row[6].button("🗑", key=f"delbtn_{m.id}", use_container_width=True):
                    st.session_state.confirm_delete_id = m.id
                    st.session_state.editing_staff_id  = None
                    st.rerun()

                if st.session_state.confirm_delete_id == m.id:
                    cc = st.columns([3, 1, 1])
                    cc[0].warning(f"⚠ Permanently remove **{m.name}**?")
                    if cc[1].button("✓ Yes", key=f"conf_{m.id}", type="primary", use_container_width=True):
                        with SessionLocal() as s:
                            mem = s.get(Staff, m.id)
                            if mem: mem.active = False; s.commit()
                        st.session_state.confirm_delete_id = None
                        st.success(f"✓ {m.name} removed.")
                        st.rerun()
                    if cc[2].button("✕ No", key=f"cxdel_{m.id}", use_container_width=True):
                        st.session_state.confirm_delete_id = None
                        st.rerun()

                if is_editing:
                    with st.container():
                        st.markdown("")
                        _render_edit_panel(m.id, staff_list)
                    st.divider()

            st.caption("⭐ = designated Day Coordinator on one or more dates")
            st.markdown("---")
            st.markdown('<div class="section-header">Role Distribution</div>', unsafe_allow_html=True)
            tcols = st.columns(4)
            for i, role in enumerate(ROLES):
                count = sum(1 for m in staff_list if m.role == role)
                c = ROLE_COLOURS[role]
                tcols[i].markdown(
                    f'<div class="metric-tile"><div class="val" style="color:{c};">{count}</div>'
                    f'<div class="lbl">{role}</div></div>', unsafe_allow_html=True)

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
                                st.bar_chart(pd.DataFrame(list(sc.items()),
                                             columns=["Type","Count"]).set_index("Type"))

    with tab2:
        st.markdown('<div class="section-header">Upload Department Roster (Excel)</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        <strong>Fully supported patterns:</strong><br><br>
        • <code>OT</code>, <code>OT*</code>, <code>am OT</code>, <code>pm OT</code> → OT available (full/half day)<br>
        • <code>am 1/2/3 call</code> → AM Emergency Team (not assigned to rooms)<br>
        • <code>pm PAAC</code> → listed separately, not assigned to OT rooms<br>
        • <code>am meeting</code>, <code>am POMC</code>, <code>am sick</code> → AM blocked<br>
        • <code>pm meeting</code>, <code>pm PAAC</code>, <code>pm sick</code> → PM blocked<br>
        • <b>Rows 4–16</b> → Consultant · <b>Rows 18–42</b> → Specialist · <b>Rows 44–92</b> → Trainee
        </div>
        """, unsafe_allow_html=True)

        with st.expander("⚙️ Adjust row boundaries"):
            col_a, col_b, col_c = st.columns(3)
            cons_start  = col_a.number_input("Consultant — first row", value=4,  min_value=1)
            cons_end    = col_a.number_input("Consultant — last row",  value=16, min_value=1)
            spec_start  = col_b.number_input("Specialist — first row", value=18, min_value=1)
            spec_end    = col_b.number_input("Specialist — last row",  value=42, min_value=1)
            train_start = col_c.number_input("Trainee — first row",    value=44, min_value=1)
            train_end   = col_c.number_input("Trainee — last row",     value=92, min_value=1)

        cons_rows  = list(range(int(cons_start),  int(cons_end)  + 1, 2))
        spec_rows  = list(range(int(spec_start),  int(spec_end)  + 1, 2))
        train_rows = list(range(int(train_start), int(train_end) + 1, 2))

        uploaded = st.file_uploader("Choose department roster (.xlsx/.xls)", type=["xlsx","xls"])
        if uploaded:
            try:
                parsed, all_dates = parse_custom_roster(uploaded, cons_rows, spec_rows, train_rows)
            except Exception as e:
                st.error(f"Could not parse: {e}")
            else:
                coordinators = [(p["name"], p["coordinator_dates"]) for p in parsed if p["coordinator_dates"]]
                am_call_total = sum(len(p["am_call_dates"]) for p in parsed)
                pm_paac_total = sum(len(p["pm_paac_dates"]) for p in parsed)
                st.success(
                    f"✓ **{len(parsed)}** colleagues · **{len(all_dates)}** days "
                    f"({min(all_dates).strftime('%d %b')} → {max(all_dates).strftime('%d %b %Y')}) · "
                    f"**{len(coordinators)}** coordinator(s) · "
                    f"**{am_call_total}** am-call entries · **{pm_paac_total}** pm-PAAC entries"
                )
                if coordinators:
                    with st.expander(f"⭐ Day Coordinators ({len(coordinators)})"):
                        for name, dates in coordinators:
                            st.markdown(f"**{name}** — " + ", ".join(d.strftime("%d %b") for d in sorted(dates)))

                preview_rows = []
                for p in parsed[:12]:
                    ot_days = [str(d) for d, v in p["ot_by_date"].items() if v]
                    preview_rows.append({
                        "Name": p["name"], "Role": p["role"],
                        "OT days": len(ot_days),
                        "AM call days": len(p["am_call_dates"]),
                        "PM PAAC days": len(p["pm_paac_dates"]),
                        "Coordinator days": len(p["coordinator_dates"]),
                    })
                st.markdown("**Preview (first 12):**")
                st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

                st.info("💡 Add specialties per person using ✏️ in View Roster after import.")
                if st.button("✅ Import into Database", type="primary"):
                    added, updated = _upsert_parsed_staff(parsed)
                    st.success(f"✓ {added} added · {updated} updated.")
                    st.rerun()

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
            ot_sel = {d: day_cols_ui[i].checkbox(d, key=f"add_ot_{d}") for i, d in enumerate(DAYS)}
            if st.form_submit_button("➕ Add Colleague", type="primary"):
                if not name.strip():
                    st.error("Name is required.")
                else:
                    with SessionLocal() as s:
                        if s.query(Staff).filter(Staff.name == name.strip()).first():
                            st.warning(f"'{name}' already exists.")
                        else:
                            m = Staff(name=name.strip(), role=role, pregnant=pregnant,
                                      specialties=specs.strip(),
                                      ot_mon=ot_sel["Mon"], ot_tue=ot_sel["Tue"],
                                      ot_wed=ot_sel["Wed"], ot_thu=ot_sel["Thu"],
                                      ot_fri=ot_sel["Fri"], ot_sat=ot_sel["Sat"],
                                      ot_sun=ot_sel["Sun"])
                            s.add(m)
                            s.flush()
                            s.add(StaffStats(staff_id=m.id))
                            s.commit()
                            st.success(f"✓ Added {name}")
                            st.rerun()
