"""pages/dashboard.py – Overview dashboard."""
import json
from datetime import date, timedelta
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from database import SessionLocal, Staff, Schedule, ScheduleAssignment, StaffStats, get_setting


def show():
    st.markdown("## 📊 Dashboard")
    st.markdown("Real-time overview of your anaesthesia department's OT schedule.")

    with SessionLocal() as s:
        total_staff      = s.query(Staff).filter(Staff.active == True).count()
        consultants      = s.query(Staff).filter(Staff.active == True, Staff.role == "Consultant").count()
        specialists      = s.query(Staff).filter(Staff.active == True, Staff.role == "Specialist").count()
        trainees         = s.query(Staff).filter(Staff.active == True, Staff.role.in_(["Senior Trainee","Trainee"])).count()
        total_schedules  = s.query(Schedule).count()
        pregnant_staff   = s.query(Staff).filter(Staff.active == True, Staff.pregnant == True).count()

    # ── Metric tiles ──────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    tiles = [
        (c1, total_staff,     "Total Staff"),
        (c2, consultants,     "Consultants"),
        (c3, specialists,     "Specialists"),
        (c4, trainees,        "Trainees"),
        (c5, total_schedules, "Published Schedules"),
    ]
    for col, val, lbl in tiles:
        col.markdown(
            f'<div class="metric-tile"><div class="val">{val}</div><div class="lbl">{lbl}</div></div>',
            unsafe_allow_html=True,
        )

    if pregnant_staff:
        st.warning(f"⚠ {pregnant_staff} staff member(s) marked as pregnant – will be excluded from X-ray rooms automatically.")

    st.divider()

    # ── Recent schedules ──────────────────────────────────────────────────────
    col_a, col_b = st.columns([1.8, 1.2])

    with col_a:
        st.markdown('<div class="section-header">Recent Schedules</div>', unsafe_allow_html=True)
        with SessionLocal() as s:
            recent = s.query(Schedule).order_by(Schedule.schedule_date.desc()).limit(7).all()
        if recent:
            df = pd.DataFrame([{
                "Date":       str(r.schedule_date),
                "Published":  r.published_at[:16] if r.published_at else "—",
            } for r in recent])
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.info("No published schedules yet. Head to Daily Scheduler to create your first one.")

    with col_b:
        st.markdown('<div class="section-header">Staff by Role</div>', unsafe_allow_html=True)
        with SessionLocal() as s:
            rows = s.query(Staff.role, Staff.active).filter(Staff.active == True).all()
        if rows:
            role_counts = pd.DataFrame(rows, columns=["Role", "active"])
            role_counts = role_counts.groupby("Role").size().reset_index(name="Count")
            fig = px.pie(
                role_counts, names="Role", values="Count",
                color_discrete_sequence=["#1f8ef1","#00d4aa","#f59e0b","#ef4444"],
            )
            fig.update_layout(
                paper_bgcolor="#161b22",
                plot_bgcolor="#161b22",
                font_color="#e6edf3",
                margin=dict(t=10, b=10, l=10, r=10),
                height=260,
                showlegend=True,
            )
            fig.update_traces(textfont_color="#e6edf3")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("Upload a roster to see staff distribution.")

    # ── Consultation load over last 30 days ───────────────────────────────────
    st.markdown('<div class="section-header">Consultation Load (Top 10 Staff)</div>', unsafe_allow_html=True)
    with SessionLocal() as s:
        stats = (
            s.query(StaffStats, Staff.name, Staff.role)
             .join(Staff, Staff.id == StaffStats.staff_id)
             .filter(Staff.active == True)
             .order_by(StaffStats.total_consultations.desc())
             .limit(10)
             .all()
        )
    if stats:
        df_stats = pd.DataFrame([{
            "Name":         name,
            "Role":         role,
            "Consultations": st.total_consultations or 0,
            "Rooms":        st.total_rooms or 0,
        } for st, name, role in stats])
        fig2 = px.bar(
            df_stats, x="Name", y=["Consultations", "Rooms"],
            barmode="group",
            color_discrete_sequence=["#1f8ef1", "#00d4aa"],
        )
        fig2.update_layout(
            paper_bgcolor="#161b22",
            plot_bgcolor="#161b22",
            font_color="#e6edf3",
            xaxis=dict(gridcolor="#30363d"),
            yaxis=dict(gridcolor="#30363d"),
            margin=dict(t=10, b=10),
            height=320,
            legend=dict(bgcolor="#161b22"),
        )
        st.plotly_chart(fig2, use_container_width=True)
    else:
        st.info("No stats yet. Publish schedules to start tracking load.")

    # ── Today's availability quick-look ───────────────────────────────────────
    st.markdown('<div class="section-header">Today\'s OT Availability</div>', unsafe_allow_html=True)
    today = date.today()
    day_col = f"ot_{today.strftime('%a').lower()}"
    with SessionLocal() as s:
        all_staff = s.query(Staff).filter(Staff.active == True).all()
        available = [m for m in all_staff if getattr(m, day_col, False)]

    if available:
        df_avail = pd.DataFrame([{
            "Name":       m.name,
            "Role":       m.role,
            "Specialties": m.specialties,
            "Pregnant":   "⚠ Yes" if m.pregnant else "No",
        } for m in available])
        st.dataframe(df_avail, use_container_width=True, hide_index=True)
    else:
        st.info(f"No staff marked as on OT duty today ({today.strftime('%A, %d %b %Y')}).")
