"""pages/settings.py – Application settings."""
import json
import streamlit as st
from database import get_setting, set_setting


def show():
    st.markdown("## ⚙️ Settings")

    tab1, tab2, tab3 = st.tabs(["🔧 Scheduler Rules", "⚖️ Preference Weights", "🗄️ Data Management"])

    # ── Tab 1: Rules ──────────────────────────────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Constraint Settings</div>', unsafe_allow_html=True)

        max_rooms = int(get_setting("consultant_max_rooms_week", "3"))
        new_max = st.number_input(
            "Max rooms per Consultant per week",
            min_value=1, max_value=10, value=max_rooms,
            help="Consultants will be avoided as leads if they have exceeded this threshold."
        )
        if st.button("💾 Save Max Rooms"):
            set_setting("consultant_max_rooms_week", str(int(new_max)))
            st.success(f"Saved: {int(new_max)} rooms/week for Consultants.")

        st.divider()

        st.markdown('<div class="section-header">Consultation Slot Criteria</div>', unsafe_allow_html=True)
        criteria_options = {
            "lowest_count":    "Assign to the Specialist/Senior Trainee with the fewest total consultations",
            "not_last_3_days": "Prefer those who haven't been assigned a consultation in the last 3 days",
        }
        current_criteria = get_setting("consult_criteria", "lowest_count")
        chosen = st.radio(
            "Consultation assignment rule",
            list(criteria_options.keys()),
            format_func=lambda x: criteria_options[x],
            index=list(criteria_options.keys()).index(current_criteria),
        )
        if st.button("💾 Save Consultation Criteria"):
            set_setting("consult_criteria", chosen)
            st.success(f"Saved: {criteria_options[chosen]}")

        st.divider()

        st.markdown('<div class="section-header">Default Solver</div>', unsafe_allow_html=True)
        solver_options = {"ortools": "OR-Tools CP-SAT (recommended)", "greedy": "Greedy fallback only"}
        current_solver = get_setting("solver", "ortools")
        new_solver = st.radio(
            "Optimisation solver",
            list(solver_options.keys()),
            format_func=lambda x: solver_options[x],
            index=list(solver_options.keys()).index(current_solver),
        )
        if st.button("💾 Save Solver Setting"):
            set_setting("solver", new_solver)
            st.success(f"Saved: {solver_options[new_solver]}")

    # ── Tab 2: Preference weights ─────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Surgery Type → Specialty Preferences</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        These mappings determine the **preference score** for assigning staff to rooms.
        Staff whose specialties match these keywords receive a bonus in the optimiser.
        Edit them in **Rooms & Cases → Surgery Types**.
        """)

        prefs = json.loads(get_setting("specialty_preferences", "{}"))
        if prefs:
            import pandas as pd
            df = pd.DataFrame(
                [{"Surgery Type": k, "Preferred Specialty Keywords": ", ".join(v)} for k, v in prefs.items()]
            )
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No preferences configured yet.")

        st.markdown("""
        <div class="card card-accent">
        <strong>Optimiser scoring breakdown:</strong><br>
        • Specialty match for lead: <strong>+10 points</strong><br>
        • Specialty match for assistant: <strong>+5 points</strong><br>
        • Non-Consultant as lead bonus: <strong>+5 points</strong><br>
        • Load penalty per prior room: <strong>-1 point each</strong><br>
        • Consultation: <strong>inverse of current consultation count</strong>
        </div>
        """, unsafe_allow_html=True)

    # ── Tab 3: Data management ────────────────────────────────────────────────
    with tab3:
        st.markdown('<div class="section-header">Data Management</div>', unsafe_allow_html=True)
        st.warning("⚠ These actions are irreversible. Use with caution.")

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("**Reset all staff statistics**")
            st.caption("Zeroes out consultation and room counts for all staff. Staff roster is preserved.")
            if st.button("🔄 Reset All Stats", type="secondary"):
                from database import SessionLocal, StaffStats
                with SessionLocal() as s:
                    s.query(StaffStats).update({
                        "total_consultations": 0,
                        "total_rooms": 0,
                        "last_assigned_date": None,
                        "surgery_type_counts": "{}",
                        "last_consultation": None,
                        "consult_dates_json": "[]",
                    })
                    s.commit()
                st.success("All stats reset.")

        with col2:
            st.markdown("**Delete all published schedules**")
            st.caption("Removes all schedule records. Stats are not affected.")
            if st.button("🗑 Delete All Schedules", type="secondary"):
                from database import SessionLocal, Schedule, ScheduleAssignment
                with SessionLocal() as s:
                    s.query(ScheduleAssignment).delete()
                    s.query(Schedule).delete()
                    s.commit()
                st.success("All schedules deleted.")

        st.divider()

        # About
        st.markdown('<div class="section-header">About AnaesOT Scheduler</div>', unsafe_allow_html=True)
        st.markdown("""
        <div class="card">
        <strong>AnaesOT Scheduler v1.0 MVP</strong><br><br>
        Built for anaesthesia department OT coordination.<br>
        Uses <strong>Google OR-Tools CP-SAT</strong> for constraint-based optimisation.<br><br>
        <strong>Tech stack:</strong> Streamlit · SQLAlchemy/SQLite · OR-Tools · Plotly · Pandas
        </div>
        """, unsafe_allow_html=True)
