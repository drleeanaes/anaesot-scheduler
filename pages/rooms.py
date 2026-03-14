"""pages/rooms.py – Room configuration, including special room types."""
import json
import pandas as pd
import streamlit as st
from database import get_setting, set_setting

# ── Special room type constants ───────────────────────────────────────────────
# These are stored in a separate setting: room_special_types
# Format: { "room_name": "eot" | "trauma" | "normal" }
# eot    → manual assignment only, optimiser skips it
# trauma → Senior Trainee works alone; fallback Specialist if none available
# normal → standard lead + optional assistant


def get_room_special_types() -> dict:
    return json.loads(get_setting("room_special_types", "{}"))


def set_room_special_type(room: str, stype: str):
    cfg = get_room_special_types()
    cfg[room] = stype
    set_setting("room_special_types", json.dumps(cfg))


SPECIAL_TYPE_LABELS = {
    "normal": ("⬜ Normal",  "#8b949e"),
    "eot":    ("🔴 EOT",     "#ef4444"),
    "trauma": ("🟡 Trauma",  "#f59e0b"),
}


def show():
    st.markdown("## 🏠 Rooms & Cases Configuration")

    rooms_cfg   = json.loads(get_setting("rooms_config", "{}"))
    surgery_types = json.loads(get_setting("surgery_types", "[]"))
    special_types = get_room_special_types()

    tab1, tab2, tab3 = st.tabs(["🏠 Operating Theatres", "🏷 Room Types", "🔧 Surgery Types"])

    # ── TAB 1: Theatre block config ───────────────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Theatre Block Configuration</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        Define your theatre blocks and rooms. Room types (Normal / EOT / Trauma)
        are configured in the <b>Room Types</b> tab.
        </div>
        """, unsafe_allow_html=True)

        # Current blocks
        all_room_list = []
        for block, rooms in rooms_cfg.items():
            st.markdown(f"**Block {block}** – {len(rooms)} room(s): {', '.join(rooms)}")
            for r in rooms:
                stype = special_types.get(r, "normal")
                label, colour = SPECIAL_TYPE_LABELS.get(stype, SPECIAL_TYPE_LABELS["normal"])
                all_room_list.append({
                    "Block": block,
                    "Room":  r,
                    "Type":  label,
                })

        st.divider()

        st.markdown("**Add a New Block:**")
        with st.form("add_block"):
            col1, col2 = st.columns(2)
            new_block = col1.text_input("Block Name (e.g. C5)")
            num_rooms = col2.number_input("Number of Rooms", min_value=1, max_value=10, value=2)
            prefix    = st.text_input("Room name prefix (optional, e.g. 'C5-')", value="")
            if st.form_submit_button("➕ Add Block"):
                if new_block.strip():
                    pref      = prefix.strip() or f"{new_block.strip()}-"
                    new_rooms = [f"{pref}{i+1}" for i in range(int(num_rooms))]
                    rooms_cfg[new_block.strip()] = new_rooms
                    set_setting("rooms_config", json.dumps(rooms_cfg))
                    st.success(f"Added block {new_block}: {', '.join(new_rooms)}")
                    st.rerun()

        st.markdown("**Remove a Block:**")
        if rooms_cfg:
            to_remove = st.selectbox("Select block to remove",
                                     ["— Select —"] + list(rooms_cfg.keys()))
            if to_remove != "— Select —":
                if st.button(f"🗑 Remove Block {to_remove}"):
                    del rooms_cfg[to_remove]
                    set_setting("rooms_config", json.dumps(rooms_cfg))
                    st.success(f"Removed block {to_remove}")
                    st.rerun()

        # Summary table with type column
        st.markdown('<div class="section-header">All Rooms</div>', unsafe_allow_html=True)
        if all_room_list:
            st.dataframe(pd.DataFrame(all_room_list), use_container_width=True, hide_index=True)

    # ── TAB 2: Room special types ─────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Room Type Assignment</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        Assign a special type to any room. This controls how the Daily Scheduler
        handles assignments for that room.<br><br>
        <span style="color:#ef4444;font-weight:600;">🔴 EOT</span>
        &nbsp;— Emergency OT. You assign this room <b>manually</b>;
        the optimiser will leave it blank for your override.<br><br>
        <span style="color:#f59e0b;font-weight:600;">🟡 Trauma</span>
        &nbsp;— Trauma room. Assigns a <b>Senior Trainee to work alone</b>.
        If no Senior Trainee is available, falls back to a Specialist.
        No assistant is assigned.<br><br>
        <span style="color:#8b949e;font-weight:600;">⬜ Normal</span>
        &nbsp;— Standard room. Lead (Specialist/Consultant) + optional Assistant.
        </div>
        """, unsafe_allow_html=True)

        # Build flat list of all rooms
        all_rooms_flat = [r for rooms in rooms_cfg.values() for r in rooms]

        if not all_rooms_flat:
            st.info("No rooms configured yet. Add blocks in the Operating Theatres tab.")
        else:
            st.markdown("**Set type for each room:**")

            # Group by block for readability
            for block, rooms in rooms_cfg.items():
                st.markdown(f"**Block {block}**")
                cols = st.columns(min(len(rooms), 4))
                for i, room in enumerate(rooms):
                    current = special_types.get(room, "normal")
                    options = list(SPECIAL_TYPE_LABELS.keys())
                    labels  = [SPECIAL_TYPE_LABELS[o][0] for o in options]
                    idx     = options.index(current) if current in options else 0
                    chosen  = cols[i % 4].selectbox(
                        room, labels, index=idx, key=f"rtype_{room}"
                    )
                    # Map label back to key
                    chosen_key = options[labels.index(chosen)]
                    if chosen_key != current:
                        set_room_special_type(room, chosen_key)
                        st.rerun()
                st.markdown("")

            # Summary table
            st.markdown('<div class="section-header">Current Room Type Summary</div>',
                        unsafe_allow_html=True)
            summary = []
            for block, rooms in rooms_cfg.items():
                for r in rooms:
                    stype = special_types.get(r, "normal")
                    label, _ = SPECIAL_TYPE_LABELS[stype]
                    summary.append({"Block": block, "Room": r, "Type": label,
                                    "Optimiser behaviour": {
                                        "normal": "Lead + optional assistant, preference-scored",
                                        "eot":    "Skipped — manual assignment only",
                                        "trauma": "Senior Trainee solo (Specialist if unavailable)",
                                    }.get(stype, "—")})
            st.dataframe(pd.DataFrame(summary), use_container_width=True, hide_index=True)

    # ── TAB 3: Surgery types ──────────────────────────────────────────────────
    with tab3:
        st.markdown('<div class="section-header">Surgery / Case Types</div>',
                    unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        These types appear in the Daily Scheduler room assignment dropdowns.
        </div>
        """, unsafe_allow_html=True)

        st.markdown("**Current types:**")
        cols = st.columns(4)
        for i, t in enumerate(surgery_types):
            cols[i % 4].markdown(f"• {t}")

        st.divider()
        col_add, col_del = st.columns(2)
        with col_add:
            new_type = st.text_input("Add new surgery type", placeholder="e.g. Hepatobiliary")
            if st.button("➕ Add Type"):
                if new_type.strip() and new_type.strip() not in surgery_types:
                    surgery_types.append(new_type.strip())
                    set_setting("surgery_types", json.dumps(surgery_types))
                    st.success(f"Added: {new_type.strip()}")
                    st.rerun()
        with col_del:
            if surgery_types:
                del_type = st.selectbox("Remove type", ["— Select —"] + surgery_types)
                if del_type != "— Select —":
                    if st.button(f"🗑 Remove {del_type}"):
                        surgery_types.remove(del_type)
                        set_setting("surgery_types", json.dumps(surgery_types))
                        st.success(f"Removed: {del_type}")
                        st.rerun()

        st.markdown('<div class="section-header">Specialty Preference Mapping</div>',
                    unsafe_allow_html=True)
        st.markdown("Staff whose specialties match these keywords get a preference bonus in the optimiser.")
        prefs = json.loads(get_setting("specialty_preferences", "{}"))
        pref_rows = [{"Surgery Type": k, "Preferred Keywords": ", ".join(v)}
                     for k, v in prefs.items()]
        if pref_rows:
            st.dataframe(pd.DataFrame(pref_rows), use_container_width=True, hide_index=True)

        with st.expander("Edit preference mapping"):
            with st.form("edit_prefs"):
                stype    = st.selectbox("Surgery type", surgery_types if surgery_types else ["—"])
                keywords = st.text_input("Preferred keywords (comma-separated)",
                                         placeholder="neuro, neurosurgery")
                if st.form_submit_button("💾 Save"):
                    prefs[stype] = [k.strip().lower() for k in keywords.split(",") if k.strip()]
                    set_setting("specialty_preferences", json.dumps(prefs))
                    st.success(f"Saved preference for {stype}")
                    st.rerun()
