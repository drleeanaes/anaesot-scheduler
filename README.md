# AnaesOT Scheduler – MVP v1.0

A production-ready Streamlit web application for managing anaesthesia department OT scheduling.

---

## Quick Start

```bash
# 1. Clone / unzip the project
cd anaesot

# 2. Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
streamlit run app.py
```

The app opens at **http://localhost:8501** and creates `anaesot.db` (SQLite) on first run.

---

## File Structure

```
anaesot/
├── app.py                  # Entry point + sidebar navigation
├── database.py             # SQLAlchemy models, helpers, init_db()
├── optimizer.py            # OR-Tools CP-SAT solver + greedy fallback
├── requirements.txt
├── README.md
└── pages/
    ├── dashboard.py        # Overview metrics + today's availability
    ├── staff.py            # Roster upload, add/edit, individual stats
    ├── rooms.py            # Theatre block & surgery type configuration
    ├── scheduler.py        # Core daily workflow (generate → review → publish)
    ├── stats.py            # Charts, history, export
    └── settings.py         # Rules, preferences, data management
```

---

## Excel Roster Format

Upload an Excel file with these columns (exact names for auto-detection):

| Column | Values | Description |
|--------|--------|-------------|
| Name | Free text | Full name |
| Role | Consultant / Specialist / Senior Trainee / Trainee | Clinical grade |
| Pregnant | Yes / No | Excludes from X-ray rooms |
| Specialties | Comma-separated, e.g. "neuro, paeds" | For preference matching |
| OT_Mon | Yes / No | On OT duty Monday |
| OT_Tue | Yes / No | On OT duty Tuesday |
| OT_Wed | Yes / No | … |
| OT_Thu | Yes / No | … |
| OT_Fri | Yes / No | … |
| OT_Sat | Yes / No | … |
| OT_Sun | Yes / No | … |

You can download a sample template from **Staff Management → Upload Excel → Generate Sample XLSX**.

---

## Daily Workflow

1. **Staff Management** → upload the weekly Excel roster
2. **Rooms & Cases** → confirm theatre blocks and surgery types are configured
3. **Daily Scheduler**:
   - Pick the OT date
   - Assign a surgery/case type to each room
   - Tick the X-ray checkbox for rooms with fluoroscopy
   - Click **⚡ Generate Suggested Draft**
   - Review the draft table (violations shown in red, specialty matches in green)
   - Override any assignment via the dropdown selectors
   - Assign the consultation slot
   - Click **✅ Approve & Publish Schedule**
4. **Stats & History** → review load balance charts, view past schedules, export

---

## Theatre Configuration (defaults)

| Block | Rooms |
|-------|-------|
| C11 | C11-1, C11-2, C11-3 |
| C10 | C10-1, C10-2, C10-3 |
| C9 | C9-1, C9-2, C9-3 |
| C8 | C8-1, C8-2, C8-3 |
| C6 | C6-1, C6-2 |
| C7 | C7-OBS (elective obstetric) |

Rooms and blocks are fully configurable in **Rooms & Cases**.

---

## Hard Constraints (always enforced)

- Every room must have at least one Specialist / Senior Trainee / Consultant as lead
- Pregnant staff **cannot** be assigned to any X-ray room
- Each staff member can be assigned to **at most one** slot per day (lead, assistant, or consultation)
- Exactly **one** consultation slot per day, assigned to a Specialist or Senior Trainee

## Soft Preferences (optimiser maximises)

- Neuro rooms → staff with "neuro" in specialties
- Paediatric rooms → staff with "paeds" / "paediatric"
- Obstetric room → staff with "obstetric" / "obs"
- Configurable per surgery type in **Rooms & Cases → Surgery Types**

---

## Optimiser Details

The app uses **Google OR-Tools CP-SAT** (constraint programming + SAT):

```
Objective: maximise Σ (specialty_match_score × assignment_variable)
                  + Σ (non_consultant_bonus × lead_variable)
                  - Σ (load_penalty × assignment_variable)
                  + Σ (consult_fairness_score × consult_variable)

Subject to:
  C1: Σ_s lead[r,s] = 1  ∀ rooms r
  C2: Σ_s assist[r,s] ≤ 1  ∀ r
  C3: lead[r,s] = 0 if role(s) = Trainee
  C4: lead[r,s] = assist[r,s] = 0 if pregnant(s) and xray(r)
  C5: Σ_r lead[r,s] + Σ_r assist[r,s] + consult[s] ≤ 1  ∀ s
  C6: Σ_s consult[s] = 1  (eligible = Specialist or Senior Trainee)
```

Solver timeout: 10 seconds. Falls back to greedy if infeasible.

---

## Next Steps for v2

1. **User authentication** – per-coordinator login, audit trail
2. **Real-time multi-user** – WebSocket sync or Supabase backend
3. **Email / WhatsApp export** – auto-send schedule to staff
4. **Leave management** – integrate with HR system for annual leave / sick days
5. **Weekly view** – generate entire week in one optimiser pass with consecutive-day equity
6. **Push notifications** – alert Consultants when consultation load is imbalanced
7. **PDF schedule export** – formatted printable A4 daily sheet
8. **Mobile-responsive PWA** – pin to home screen on ward tablets
9. **Audit log** – who changed what and when (full change history)
10. **API endpoint** – REST API so the schedule can be consumed by hospital PAS systems
