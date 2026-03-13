"""
database.py – SQLAlchemy + SQLite persistence layer
====================================================
Tables:
  staff          – master roster
  staff_stats    – cumulative counters per person
  schedules      – one row per published daily schedule (JSON blob)
  schedule_assignments – normalised assignments per schedule
"""

import json
from datetime import date
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean,
    Date, Float, Text, ForeignKey, UniqueConstraint
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

import os
DATABASE_URL = f"sqlite:///{os.path.join(os.path.expanduser('~'), 'anaesot.db')}"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# ── Models ───────────────────────────────────────────────────────────────────

class Staff(Base):
    __tablename__ = "staff"
    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(120), unique=True, nullable=False)
    role          = Column(String(40), nullable=False)   # Consultant / Specialist / Senior Trainee / Trainee
    pregnant      = Column(Boolean, default=False)
    specialties   = Column(Text, default="")             # comma-separated
    ot_mon        = Column(Boolean, default=False)
    ot_tue        = Column(Boolean, default=False)
    ot_wed        = Column(Boolean, default=False)
    ot_thu        = Column(Boolean, default=False)
    ot_fri        = Column(Boolean, default=False)
    ot_sat        = Column(Boolean, default=False)
    ot_sun        = Column(Boolean, default=False)
    active        = Column(Boolean, default=True)

    def ot_days(self):
        return {
            "Monday":    self.ot_mon,
            "Tuesday":   self.ot_tue,
            "Wednesday": self.ot_wed,
            "Thursday":  self.ot_thu,
            "Friday":    self.ot_fri,
            "Saturday":  self.ot_sat,
            "Sunday":    self.ot_sun,
        }

    def specialties_list(self):
        return [s.strip().lower() for s in self.specialties.split(",") if s.strip()]


class StaffStats(Base):
    __tablename__ = "staff_stats"
    id                   = Column(Integer, primary_key=True, index=True)
    staff_id             = Column(Integer, ForeignKey("staff.id"), unique=True)
    total_consultations  = Column(Integer, default=0)
    total_rooms          = Column(Integer, default=0)
    last_assigned_date   = Column(Date, nullable=True)
    surgery_type_counts  = Column(Text, default="{}")   # JSON {type: count}
    last_consultation    = Column(Date, nullable=True)
    consult_dates_json   = Column(Text, default="[]")   # last N dates as JSON list


class Schedule(Base):
    """One published schedule per day."""
    __tablename__ = "schedules"
    id             = Column(Integer, primary_key=True, index=True)
    schedule_date  = Column(Date, unique=True, nullable=False)
    published_at   = Column(Text, nullable=True)        # ISO timestamp string
    raw_json       = Column(Text, default="{}")         # full schedule blob


class ScheduleAssignment(Base):
    """Normalised per-assignment row for easy reporting."""
    __tablename__ = "schedule_assignments"
    id             = Column(Integer, primary_key=True, index=True)
    schedule_id    = Column(Integer, ForeignKey("schedules.id"))
    schedule_date  = Column(Date, nullable=False)
    room           = Column(String(20))
    surgery_type   = Column(String(40))
    is_xray        = Column(Boolean, default=False)
    role_type      = Column(String(20))   # "lead", "assistant", "consultation"
    staff_id       = Column(Integer, ForeignKey("staff.id"))
    staff_name     = Column(String(120))

    __table_args__ = (UniqueConstraint("schedule_id", "staff_id", "role_type", name="_uq_assign"),)


class AppSettings(Base):
    __tablename__ = "app_settings"
    key   = Column(String(80), primary_key=True)
    value = Column(Text, default="")


# ── Helpers ───────────────────────────────────────────────────────────────────

def init_db():
    Base.metadata.create_all(bind=engine)
    # Seed default settings if missing
    with SessionLocal() as s:
        defaults = {
            "surgery_types":        json.dumps([
                "Neuro", "Colorectal", "General", "Paediatric",
                "Obstetric", "Orthopaedic", "Urology", "Vascular",
                "ENT", "Plastics", "Gynaecology", "Ophthalmology"
            ]),
            "consultant_max_rooms_week": "3",
            "rooms_config": json.dumps({
                "C11": ["C11-1", "C11-2", "C11-3"],
                "C10": ["C10-1", "C10-2", "C10-3"],
                "C9":  ["C9-1",  "C9-2",  "C9-3"],
                "C8":  ["C8-1",  "C8-2",  "C8-3"],
                "C6":  ["C6-1",  "C6-2"],
                "C7":  ["C7-OBS"],
            }),
            "specialty_preferences": json.dumps({
                "Neuro":       ["neuro"],
                "Paediatric":  ["paeds", "paediatric"],
                "Obstetric":   ["obstetric", "obs"],
                "Colorectal":  ["colorectal"],
                "Vascular":    ["vascular"],
                "Orthopaedic": ["orthopaedic", "ortho"],
                "Urology":     ["urology"],
            }),
            "consult_criteria": "lowest_count",   # or "not_last_3_days"
            "solver":           "ortools",         # or "pulp"
        }
        for k, v in defaults.items():
            if not s.get(AppSettings, k):
                s.add(AppSettings(key=k, value=v))
        s.commit()


def get_setting(key: str, default=None):
    with SessionLocal() as s:
        row = s.get(AppSettings, key)
        return row.value if row else default


def set_setting(key: str, value: str):
    with SessionLocal() as s:
        row = s.get(AppSettings, key)
        if row:
            row.value = value
        else:
            s.add(AppSettings(key=key, value=value))
        s.commit()


def get_all_staff(active_only=True):
    with SessionLocal() as s:
        q = s.query(Staff)
        if active_only:
            q = q.filter(Staff.active == True)
        return q.all()


def get_staff_stats(staff_id: int):
    with SessionLocal() as s:
        row = s.query(StaffStats).filter(StaffStats.staff_id == staff_id).first()
        if not row:
            row = StaffStats(staff_id=staff_id)
            s.add(row)
            s.commit()
            s.refresh(row)
        return row


def upsert_stats_after_publish(assignments: list, schedule_date: date):
    """Update cumulative stats after a schedule is published."""
    from collections import defaultdict
    with SessionLocal() as s:
        for a in assignments:
            sid = a.get("staff_id")
            if not sid:
                continue
            stat = s.query(StaffStats).filter(StaffStats.staff_id == sid).first()
            if not stat:
                stat = StaffStats(staff_id=sid)
                s.add(stat)
                s.flush()

            if a["role_type"] == "consultation":
                stat.total_consultations = (stat.total_consultations or 0) + 1
                stat.last_consultation = schedule_date
                dates = json.loads(stat.consult_dates_json or "[]")
                dates.append(str(schedule_date))
                stat.consult_dates_json = json.dumps(dates[-30:])  # keep last 30
            else:
                stat.total_rooms = (stat.total_rooms or 0) + 1
                sc = json.loads(stat.surgery_type_counts or "{}")
                stype = a.get("surgery_type", "Unknown")
                sc[stype] = sc.get(stype, 0) + 1
                stat.surgery_type_counts = json.dumps(sc)

            stat.last_assigned_date = schedule_date
        s.commit()
