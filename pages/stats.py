"""pages/stats.py – Statistics & History."""
import json
import io
from datetime import date, timedelta
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from database import SessionLocal, Staff, StaffStats, Schedule, ScheduleAssignment


def show():
    st.markdown("## 📈 Stats & History")

    tab1, tab2, tab3 = st.tabs(["📊 Load Balance", "📅 Schedule History", "📤 Export"])

    # ── Tab 1: Load balance charts ────────────────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Consultation Load</div>', unsafe_allow_html=True)

        with SessionLocal() as s:
            stats_data = (
                s.query(StaffStats, Staff.name, Staff.role)
                 .join(Staff, Staff.id == StaffStats.staff_id)
                 .filter(Staff.active == True)
                 .all()
            )

        if not stats_data:
            st.info("No stats yet. Publish some schedules first.")
            return

        df = pd.DataFrame([{
            "Name":         name,
            "Role":         role,
            "Consultations": st.total_consultations or 0,
            "Rooms":        st.total_rooms or 0,
            "Last Consult":  str(st.last_consultation) if st.last_consultation else "Never",
            "Last Assigned": str(st.last_assigned_date) if st.last_assigned_date else "Never",
        } for st, name, role in stats_data])

        df_sorted = df.sort_values("Consultations", ascending=False)

        fig1 = px.bar(
            df_sorted, x="Name", y="Consultations", color="Role",
            title="Total Consultations per Staff Member",
            color_discrete_sequence=px.colors.qualitative.Plotly,
        )
        fig1.update_layout(
            paper_bgcolor="#161b22", plot_bgcolor="#161b22",
            font_color="#e6edf3", xaxis=dict(gridcolor="#30363d"),
            yaxis=dict(gridcolor="#30363d"), margin=dict(t=40, b=10),
            height=350,
        )
        st.plotly_chart(fig1, use_container_width=True)

        st.markdown('<div class="section-header">Room Assignment Load</div>', unsafe_allow_html=True)
        fig2 = px.bar(
            df_sorted.sort_values("Rooms", ascending=False),
            x="Name", y="Rooms", color="Role",
            title="Total Room Assignments per Staff Member",
        )
        fig2.update_layout(
            paper_bgcolor="#161b22", plot_bgcolor="#161b22",
            font_color="#e6edf3", xaxis=dict(gridcolor="#30363d"),
            yaxis=dict(gridcolor="#30363d"), margin=dict(t=40, b=10),
            height=350,
        )
        st.plotly_chart(fig2, use_container_width=True)

        # Surgery type exposure heatmap
        st.markdown('<div class="section-header">Surgery Type Exposure</div>', unsafe_allow_html=True)
        heatmap_data = {}
        with SessionLocal() as s:
            all_stats = s.query(StaffStats, Staff.name).join(Staff, Staff.id == StaffStats.staff_id).filter(Staff.active == True).all()
        all_types = set()
        for stat, name in all_stats:
            sc = json.loads(stat.surgery_type_counts or "{}")
            heatmap_data[name] = sc
            all_types.update(sc.keys())

        if all_types:
            all_types = sorted(all_types)
            heatmap_df = pd.DataFrame(
                {name: [heatmap_data[name].get(t, 0) for t in all_types] for name in heatmap_data},
                index=all_types
            )
            fig3 = px.imshow(
                heatmap_df,
                color_continuous_scale="Blues",
                title="Surgery Type Exposure Heatmap",
            )
            fig3.update_layout(
                paper_bgcolor="#161b22", font_color="#e6edf3",
                margin=dict(t=40, b=10), height=400,
            )
            st.plotly_chart(fig3, use_container_width=True)

        # Data table
        st.markdown('<div class="section-header">Full Stats Table</div>', unsafe_allow_html=True)
        st.dataframe(df_sorted, use_container_width=True, hide_index=True)

    # ── Tab 2: Schedule history ────────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Published Schedules</div>', unsafe_allow_html=True)
        with SessionLocal() as s:
            schedules = s.query(Schedule).order_by(Schedule.schedule_date.desc()).all()

        if not schedules:
            st.info("No published schedules yet.")
            return

        sched_dates = [str(sc.schedule_date) for sc in schedules]
        chosen_date = st.selectbox("Select a date to view", sched_dates)

        if chosen_date:
            with SessionLocal() as s:
                sched = s.query(Schedule).filter(Schedule.schedule_date == chosen_date).first()
                if sched:
                    assignments = s.query(ScheduleAssignment).filter(
                        ScheduleAssignment.schedule_id == sched.id
                    ).all()

            if sched:
                st.markdown(f"**Published at:** {sched.published_at}")
                rows = [{
                    "Room":         a.room,
                    "Surgery Type": a.surgery_type,
                    "X-ray":        "Yes" if a.is_xray else "No",
                    "Role":         a.role_type.title(),
                    "Staff":        a.staff_name,
                } for a in assignments]
                if rows:
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
                else:
                    st.info("No assignment rows for this schedule.")

    # ── Tab 3: Export ─────────────────────────────────────────────────────────
    with tab3:
        st.markdown('<div class="section-header">Export Stats to Excel</div>', unsafe_allow_html=True)
        if st.button("📥 Export All Stats as Excel"):
            with SessionLocal() as s:
                stats_data = (
                    s.query(StaffStats, Staff.name, Staff.role)
                     .join(Staff, Staff.id == StaffStats.staff_id)
                     .filter(Staff.active == True)
                     .all()
                )
                all_assign = s.query(ScheduleAssignment).all()

            df_stats = pd.DataFrame([{
                "Name":         name,
                "Role":         role,
                "Consultations": st.total_consultations or 0,
                "Rooms":        st.total_rooms or 0,
                "Last Consult":  str(st.last_consultation or ""),
                "Last Assigned": str(st.last_assigned_date or ""),
            } for st, name, role in stats_data])

            df_assign = pd.DataFrame([{
                "Date":         str(a.schedule_date),
                "Room":         a.room,
                "Surgery Type": a.surgery_type,
                "Role":         a.role_type,
                "Staff":        a.staff_name,
            } for a in all_assign])

            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df_stats.to_excel(writer, index=False, sheet_name="Staff Stats")
                df_assign.to_excel(writer, index=False, sheet_name="All Assignments")
            buf.seek(0)
            st.download_button(
                "⬇️ Download Stats Export",
                data=buf,
                file_name="anaesot_stats_export.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
