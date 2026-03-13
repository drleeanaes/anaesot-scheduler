"""pages/rooms.py – Room configuration."""
import json
import streamlit as st
from database import get_setting, set_setting


def show():
    st.markdown("## 🏠 Rooms & Cases Configuration")

    rooms_cfg = json.loads(get_setting("rooms_config", "{}"))
    surgery_types = json.loads(get_setting("surgery_types", "[]"))

    tab1, tab2 = st.tabs(["🏠 Operating Theatres", "🔧 Surgery Types"])

    # ── Tab 1: Rooms ──────────────────────────────────────────────────────────
    with tab1:
        st.markdown('<div class="section-header">Theatre Block Configuration</div>', unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        Define your theatre blocks and the individual rooms within each block.
        These rooms appear in the Daily Scheduler for each assignment.
        </div>
        """, unsafe_allow_html=True)

        # Display current config
        for block, rooms in rooms_cfg.items():
            st.markdown(f"**Block {block}** – {len(rooms)} room(s): {', '.join(rooms)}")

        st.divider()
        st.markdown("**Add a New Block:**")
        with st.form("add_block"):
            col1, col2 = st.columns(2)
            new_block = col1.text_input("Block Name (e.g. C5)")
            num_rooms  = col2.number_input("Number of Rooms", min_value=1, max_value=10, value=2)
            prefix     = st.text_input("Room name prefix (optional, e.g. 'C5-')", value="")
            if st.form_submit_button("➕ Add Block"):
                if new_block.strip():
                    pref = prefix.strip() or f"{new_block.strip()}-"
                    new_rooms = [f"{pref}{i+1}" for i in range(int(num_rooms))]
                    rooms_cfg[new_block.strip()] = new_rooms
                    set_setting("rooms_config", json.dumps(rooms_cfg))
                    st.success(f"Added block {new_block} with rooms: {', '.join(new_rooms)}")
                    st.rerun()

        st.markdown("**Remove a Block:**")
        if rooms_cfg:
            block_to_remove = st.selectbox("Select block to remove", ["— Select —"] + list(rooms_cfg.keys()))
            if block_to_remove != "— Select —":
                if st.button(f"🗑 Remove Block {block_to_remove}"):
                    del rooms_cfg[block_to_remove]
                    set_setting("rooms_config", json.dumps(rooms_cfg))
                    st.success(f"Removed block {block_to_remove}")
                    st.rerun()

        # Summary table
        st.markdown('<div class="section-header">All Rooms</div>', unsafe_allow_html=True)
        all_rooms = []
        for block, rooms in rooms_cfg.items():
            for r in rooms:
                all_rooms.append({"Block": block, "Room": r})
        if all_rooms:
            import pandas as pd
            st.dataframe(pd.DataFrame(all_rooms), use_container_width=True, hide_index=True)

    # ── Tab 2: Surgery Types ──────────────────────────────────────────────────
    with tab2:
        st.markdown('<div class="section-header">Surgery / Case Types</div>', unsafe_allow_html=True)
        st.markdown("""
        <div class="card card-accent">
        These types appear in the Daily Scheduler room assignment dropdowns.
        Add specialty-specific types as needed.
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
                del_type = st.selectbox("Remove surgery type", ["— Select —"] + surgery_types)
                if del_type != "— Select —":
                    if st.button(f"🗑 Remove {del_type}"):
                        surgery_types.remove(del_type)
                        set_setting("surgery_types", json.dumps(surgery_types))
                        st.success(f"Removed: {del_type}")
                        st.rerun()

        # Specialty preferences
        st.markdown('<div class="section-header">Specialty Preference Mapping</div>', unsafe_allow_html=True)
        st.markdown("""
        Map surgery types to preferred specialty keywords.
        Staff with matching specialties will be preferred for those rooms.
        """)
        prefs = json.loads(get_setting("specialty_preferences", "{}"))

        import pandas as pd
        pref_rows = [{"Surgery Type": k, "Preferred Keywords": ", ".join(v)} for k, v in prefs.items()]
        st.dataframe(pd.DataFrame(pref_rows), use_container_width=True, hide_index=True)

        with st.expander("Edit preference mapping"):
            with st.form("edit_prefs"):
                stype = st.selectbox("Surgery type", surgery_types)
                keywords = st.text_input("Preferred specialty keywords (comma-separated)", placeholder="neuro, neurosurgery")
                if st.form_submit_button("💾 Save Preference"):
                    prefs[stype] = [k.strip().lower() for k in keywords.split(",") if k.strip()]
                    set_setting("specialty_preferences", json.dumps(prefs))
                    st.success(f"Saved preference for {stype}")
                    st.rerun()
