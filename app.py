"""
AnaesOT Scheduler - Main Entry Point
=====================================
Run with: streamlit run app.py
"""

import streamlit as st
from database import init_db

# ── Page configuration ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="AnaesOT Scheduler",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Initialise database on first run ────────────────────────────────────────
init_db()

# ── Global CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* ── Fonts ── */
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap');

html, body, [class*="css"] {
    font-family: 'IBM Plex Sans', sans-serif;
}

/* ── Palette ── */
:root {
    --bg:        #0d1117;
    --surface:   #161b22;
    --surface2:  #1c2230;
    --border:    #30363d;
    --accent:    #1f8ef1;
    --accent2:   #00d4aa;
    --warn:      #f59e0b;
    --danger:    #ef4444;
    --text:      #e6edf3;
    --muted:     #8b949e;
}

/* ── Sidebar ── */
section[data-testid="stSidebar"] {
    background: var(--surface) !important;
    border-right: 1px solid var(--border);
}
section[data-testid="stSidebar"] * { color: var(--text) !important; }

/* ── Main BG ── */
.stApp { background: var(--bg) !important; }

/* ── Cards ── */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.25rem 1.5rem;
    margin-bottom: 1rem;
}
.card-accent { border-left: 3px solid var(--accent); }
.card-warn   { border-left: 3px solid var(--warn); }
.card-danger { border-left: 3px solid var(--danger); }
.card-ok     { border-left: 3px solid var(--accent2); }

/* ── Metric tiles ── */
.metric-tile {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 1rem;
    text-align: center;
}
.metric-tile .val { font-size: 2rem; font-weight: 700; color: var(--accent); }
.metric-tile .lbl { font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: .06em; }

/* ── Violation badge ── */
.badge-danger { background:#ef444422; color:#ef4444; border:1px solid #ef4444; border-radius:4px; padding:2px 8px; font-size:.75rem; font-family:'IBM Plex Mono'; }
.badge-ok     { background:#00d4aa22; color:#00d4aa; border:1px solid #00d4aa; border-radius:4px; padding:2px 8px; font-size:.75rem; font-family:'IBM Plex Mono'; }
.badge-warn   { background:#f59e0b22; color:#f59e0b; border:1px solid #f59e0b; border-radius:4px; padding:2px 8px; font-size:.75rem; font-family:'IBM Plex Mono'; }

/* ── Section header ── */
.section-header {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--accent);
    text-transform: uppercase;
    letter-spacing: .08em;
    border-bottom: 1px solid var(--border);
    padding-bottom: .4rem;
    margin: 1.2rem 0 .8rem;
}

/* ── Table tweaks ── */
thead tr th { background: var(--surface2) !important; color: var(--muted) !important; font-size:.78rem !important; text-transform: uppercase; letter-spacing:.06em; }
tbody tr:hover td { background: var(--surface2) !important; }

/* ── Buttons ── */
.stButton > button {
    background: var(--accent) !important;
    color: #fff !important;
    border: none !important;
    border-radius: 6px !important;
    font-weight: 600 !important;
    letter-spacing: .03em !important;
}
.stButton > button:hover { opacity: .88; }

/* ── Input fields ── */
.stTextInput input, .stSelectbox select, .stNumberInput input {
    background: var(--surface2) !important;
    border: 1px solid var(--border) !important;
    color: var(--text) !important;
    border-radius: 6px !important;
}

/* ── Scrollbar ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

/* ── Logo / title ── */
.app-title {
    font-size: 1.3rem;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: .04em;
    padding: .5rem 0 .25rem;
}
.app-subtitle { font-size: .78rem; color: var(--muted); margin-bottom: 1.2rem; }
</style>
""", unsafe_allow_html=True)

# ── Sidebar navigation ───────────────────────────────────────────────────────
st.sidebar.markdown('<div class="app-title">🏥 AnaesOT Scheduler</div>', unsafe_allow_html=True)
st.sidebar.markdown('<div class="app-subtitle">Anaesthesia Department · OT Management</div>', unsafe_allow_html=True)
st.sidebar.divider()

PAGES = {
    "📊 Dashboard":        "dashboard",
    "👥 Staff Management": "staff",
    "🏠 Rooms & Cases":    "rooms",
    "📅 Daily Scheduler":  "scheduler",
    "📈 Stats & History":  "stats",
    "⚙️ Settings":         "settings",
}

if "page" not in st.session_state:
    st.session_state.page = "dashboard"

for label, key in PAGES.items():
    if st.sidebar.button(label, use_container_width=True,
                         type="primary" if st.session_state.page == key else "secondary"):
        st.session_state.page = key
        st.rerun()

st.sidebar.divider()
st.sidebar.caption("v1.0 MVP · AnaesOT")

# ── Route to page ─────────────────────────────────────────────────────────────
page = st.session_state.page

if page == "dashboard":
    from pages.dashboard import show; show()
elif page == "staff":
    from pages.staff import show; show()
elif page == "rooms":
    from pages.rooms import show; show()
elif page == "scheduler":
    from pages.scheduler import show; show()
elif page == "stats":
    from pages.stats import show; show()
elif page == "settings":
    from pages.settings import show; show()
