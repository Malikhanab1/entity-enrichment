"""Cognivelle — Unified Lead Discovery, Research, Outreach & CRM Command Center.

Architecture:
  1. Agent Research Desk     – LangGraph Groq agent with MCP tool reasoning
  2. CRM & Pipeline          – Full lead CRUD, contacts, notes, Kanban pipeline
  3. Lead Discovery Hub      – pymapscraper (the ONLY Google Maps engine) + Social ingestion
  4. Outreach Approvals      – Human-in-the-loop Email & WhatsApp approvals
  5. Activity Log & Health   – Durable SQLite audit trail for all MCP executions
  6. 👥 Team Management       – Full team member CRUD, assignment stats, WhatsApp test

Data flow:
  - pymapscraper subprocess  → places table → auto-mirrored to businesses (unpopulated)
  - Reddit / FB scrapers     → ingest_scraper_lead (MCP) → score → assign → WhatsApp alert
  - Agent research           → upsert_business → score_lead (MCP) → assign

Running:
    cd agent
    uv run streamlit run streamlit_app.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import streamlit as st
from dotenv import dotenv_values, load_dotenv

# ---------------------------------------------------------------------------
# Path / env setup
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent
MCP_DIR = REPO_DIR / "mcp"
load_dotenv(APP_DIR / ".env")

sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(MCP_DIR / "src"))

# ---------------------------------------------------------------------------
# Optional imports — fail gracefully so the UI always boots
# ---------------------------------------------------------------------------
try:
    from enrichment_mcp.tools.contact_store import (
        build_crm_record,
        get_outreach_summary,
        log_followup,
        update_business_notes,
        update_draft_body,
        update_outreach_status,
        create_whatsapp_draft,
        update_whatsapp_draft,
        mark_whatsapp_draft_sent,
        discard_whatsapp_draft,
        list_whatsapp_drafts,
        upsert_business,
        delete_business,
        add_business_contact,
        delete_contact,
        ingest_maps_db_places,
        ingest_hot_leads_excel,
        assign_business_lead,
        get_team_members,
        add_team_member,
        remove_team_member,
        toggle_team_member,
        update_team_member,
        dispatch_whatsapp_message,
        sync_places_to_unpopulated_leads,
        ingest_scraper_lead,
    )
    _STORE_AVAILABLE = True
except ImportError as _e:
    _STORE_AVAILABLE = False

try:
    from enrichment_mcp.tools.lead_scoring import score_lead, score_all_unscored_leads, calculate_lead_score
    from enrichment_mcp.tools.populate_lead import populate_lead, populate_unpopulated_leads
    _PIPELINE_AVAILABLE = True
except Exception:
    _PIPELINE_AVAILABLE = False

try:
    from mvp.scrapers_manager import (
        start_service,
        stop_service,
        start_all_scrapers,
        stop_all_scrapers,
        get_services_status,
        launch_pymapscraper_subprocess,
        get_recent_log,
    )
    _SCRAPERS_AVAILABLE = True
except Exception:
    _SCRAPERS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Graceful shutdown: stop all background scrapers when Streamlit exits
# ---------------------------------------------------------------------------
import atexit
import signal as _signal

def _shutdown_scrapers() -> None:
    """Kill every managed background process tracked in .scrapers_pids.json."""
    if not _SCRAPERS_AVAILABLE:
        return
    try:
        stop_all_scrapers()
    except Exception:
        pass

# Register once at module level (Streamlit may re-import on hot-reload;
# atexit de-duplicates identical callables, so this is safe).
atexit.register(_shutdown_scrapers)

def _handle_exit_signal(signum, frame):  # noqa: ANN001
    _shutdown_scrapers()
    # Re-raise default behaviour so Streamlit's own SIGTERM handling still runs.
    _signal.signal(signum, _signal.SIG_DFL)
    _signal.raise_signal(signum)

for _sig in (_signal.SIGTERM, _signal.SIGINT):
    try:
        _signal.signal(_sig, _handle_exit_signal)
    except (OSError, ValueError):
        # Can't set signal handlers in non-main threads (e.g. Streamlit worker).
        pass


# ---------------------------------------------------------------------------
# Agent / MCP helpers
# ---------------------------------------------------------------------------
try:
    from langchain.agents import create_agent
    from langchain_groq import ChatGroq
    from langchain_mcp_adapters.client import MultiServerMCPClient
    _AGENT_AVAILABLE = True
except ImportError:
    _AGENT_AVAILABLE = False

SYSTEM_PROMPT = """You are a concise, evidence-led business researcher and outreach assistant.

WORKFLOW RULES:
1. For a named business: always call enrich_business first, then find_decision_makers only when asked.
2. For contact extraction from a website: call crawl_website_for_contacts with the URL.
3. For lead discovery: call find_leads or find_businesses.
4. For social/scraper leads: call ingest_scraper_lead with source, title, body, posted_by.
5. For scoring a lead: call score_lead with the business name.
6. For email: create a draft ONLY — the UI provides the approval button.
7. Never send email yourself.
8. Treat city/location as a primary entity key. If missing, ask before proceeding.

GOOGLE MAPS:
- The ONLY Google Maps discovery mechanism is the pymapscraper subprocess (launched from the Discovery tab).
- Do NOT attempt any HTTP-based maps API calls.

FOLLOWUP TRACKING:
- When the user asks to log outreach, confirm business + channel, then summarise.
"""

if "messages" not in st.session_state:
    st.session_state.messages = []
if "_groq_blacklisted_keys" not in st.session_state:
    st.session_state._groq_blacklisted_keys = set()


def _keys() -> list[str]:
    keys = [os.getenv(f"GROQ_API_KEY_{i}", "").strip() for i in (1, 2, 3)]
    keys = [k for k in keys if k]
    legacy = os.getenv("GROQ_API_KEY", "").strip()
    all_keys = keys + ([legacy] if legacy and legacy not in keys else [])
    return [k for k in all_keys if k not in st.session_state._groq_blacklisted_keys]


def _workspace() -> Path:
    config = dotenv_values(MCP_DIR / ".env")
    value = Path(config.get("MCP_FILE_WORKSPACE") or "../workspace")
    return (value if value.is_absolute() else MCP_DIR / value).resolve()


def _database() -> Path:
    return _workspace() / ".enrichment_contacts.sqlite3"


def _has_city_hint(text: str) -> bool:
    if not text:
        return False
    if re.search(r"\b(in|at|from|near|around|located in|city)\s+[A-Za-z][A-Za-z .'-]{2,60}\b", text, re.I):
        return True
    if re.search(r"\b(Lahore|Karachi|Islamabad|Rawalpindi|Dubai|Jakarta|London|New York|Paris|Berlin|Sialkot|Faisalabad|Baltimore|Manchester)\b", text, re.I):
        return True
    return False


async def _mcp_tools() -> list[Any]:
    server_path = os.getenv("MCP_SERVER_PATH")
    if not server_path:
        raise RuntimeError("MCP_SERVER_PATH is missing from agent/.env")
    client = MultiServerMCPClient(
        {"enrichment": {"transport": "stdio", "command": server_path, "args": []}}
    )
    return await client.get_tools()


_HISTORY_CHAR_LIMIT = 4_000
_MAX_HISTORY_MSGS   = 6
_MAX_TOKENS_OUT     = 256


def _trim_history(history: list[dict]) -> list[dict]:
    msgs = history[-_MAX_HISTORY_MSGS:]
    while len(msgs) > 1:
        total = sum(len(str(m.get("content", ""))) for m in msgs)
        if total <= _HISTORY_CHAR_LIMIT:
            break
        msgs = msgs[1:]
    return msgs


async def _ask(history: list[dict]) -> str:
    if not _AGENT_AVAILABLE:
        return "LangChain or Groq packages not installed — agent unavailable."
    tools = await _mcp_tools()
    failures: list[str] = []

    active_keys = _keys()
    if not active_keys:
        raise RuntimeError("No valid Groq API keys available.")

    current_history = _trim_history(list(history))
    max_tokens = max(150, int(os.getenv("AGENT_MAX_TOKENS", str(_MAX_TOKENS_OUT))))

    key_index = 0
    trim_attempts = 0
    max_trim_attempts = 3
    while key_index < len(active_keys):
        key = active_keys[key_index]
        masked = f"…{key[-4:]}" if len(key) > 4 else "key"
        agent = create_agent(
            model=ChatGroq(
                model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
                temperature=0,
                api_key=key,
                max_tokens=max_tokens,
            ),
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
        )
        try:
            result = await agent.ainvoke(
                {"messages": current_history}, config={"recursion_limit": 20}
            )
            for msg in reversed(result.get("messages", [])):
                content = getattr(msg, "content", "")
                if (
                    getattr(msg, "type", "") == "ai"
                    and not getattr(msg, "tool_calls", None)
                    and str(content).strip()
                ):
                    return str(content)
            return "Tools completed. Check the CRM or Approvals tab."

        except Exception as exc:
            err_str = str(exc)
            if "401" in err_str and "invalid_api_key" in err_str:
                st.session_state._groq_blacklisted_keys.add(key)
                failures.append(f"({masked}) 401 blacklisted")
                key_index += 1; trim_attempts = 0; continue
            if "413" in err_str or "too large" in err_str.lower() or (
                "tokens" in err_str.lower() and "limit" in err_str.lower()
            ):
                if trim_attempts < max_trim_attempts and len(current_history) > 1:
                    current_history = current_history[-max(1, len(current_history) // 2):]
                    trim_attempts += 1; continue
                failures.append(f"({masked}) 413 — trim budget exhausted, rotating key")
                key_index += 1; trim_attempts = 0; continue
            failures.append(f"({masked}) {type(exc).__name__}: {exc}")
            key_index += 1; trim_attempts = 0

    raise RuntimeError("All Groq keys failed: " + " | ".join(failures))


async def _call_tool(name: str, **arguments: Any) -> Any:
    for tool in await _mcp_tools():
        if tool.name == name:
            raw = await tool.ainvoke(arguments)
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
            if isinstance(raw, list):
                text = "".join(
                    (b.get("text") or b.get("content") or "")
                    if isinstance(b, dict)
                    else (getattr(b, "text", None) or str(b))
                    for b in raw
                )
                try:
                    return json.loads(text)
                except Exception:
                    return text
            return raw
    raise RuntimeError(f"MCP tool not found: {name}")


# ---------------------------------------------------------------------------
# Direct DB helpers
# ---------------------------------------------------------------------------
def _query(sql: str, parameters: tuple = ()) -> list[dict]:
    db = _database()
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, parameters).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _execute(sql: str, parameters: tuple = ()) -> None:
    db = _database()
    conn = sqlite3.connect(db)
    conn.execute(sql, parameters)
    conn.commit()
    conn.close()


def _get_outreach_summary_direct() -> dict[str, Any]:
    total = _query("SELECT COUNT(*) as c FROM businesses")
    contacted = _query("SELECT COUNT(*) as c FROM businesses WHERE outreach_status='contacted'")
    closed = _query("SELECT COUNT(*) as c FROM businesses WHERE outreach_status='closed'")
    demo = _query("SELECT COUNT(*) as c FROM businesses WHERE outreach_status='demo_scheduled'")
    not_interested = _query("SELECT COUNT(*) as c FROM businesses WHERE outreach_status='not_interested'")
    total_followups = _query("SELECT COUNT(*) as c FROM followups")
    tot = total[0]["c"] if total else 0
    cnt = contacted[0]["c"] if contacted else 0
    cls = closed[0]["c"] if closed else 0
    dm  = demo[0]["c"] if demo else 0
    ni  = not_interested[0]["c"] if not_interested else 0
    tf  = total_followups[0]["c"] if total_followups else 0
    return {
        "total_businesses": tot,
        "contacted": cnt,
        "not_contacted": max(0, tot - cnt - cls - ni - dm),
        "closed": cls,
        "demo_scheduled": dm,
        "not_interested": ni,
        "total_followups": tf,
    }


def _wa_link(phone: str, message: str) -> str:
    clean = re.sub(r"[^\d]", "", phone)
    encoded = urllib.parse.quote(message)
    return f"https://wa.me/{clean}?text={encoded}"


def _send_whatsapp_service(phone: str, message: str) -> dict[str, Any]:
    clean_phone = re.sub(r"[^\d+]", "", phone.strip())
    url = os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3000/sendMessage")
    api_key = os.getenv("WHATSAPP_INTERNAL_API_KEY", "dummy")
    payload = json.dumps({"phone": clean_phone, "text": message}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "X-Api-Key": api_key},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"status": "ok", "code": resp.status}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# Page configuration & Styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Cognivelle — Command Center",
    page_icon="◌",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""<style>
  div.block-container { padding-top: 1.2rem; }
  .badge-not_contacted { background: #e2e3e5; color: #383d41; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge-contacted     { background: #fff3cd; color: #856404; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge-demo_scheduled{ background: #d1ecf1; color: #0c5460; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge-closed        { background: #d4edda; color: #155724; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .badge-not_interested{ background: #f8d7da; color: #721c24; padding: 2px 8px; border-radius: 6px; font-weight: 600; }
  .score-badge         { background: #e8f4fd; color: #0066cc; padding: 2px 8px; border-radius: 6px; font-weight: bold; }
  .tier-hot  { background: #f8d7da; color: #721c24; padding: 1px 6px; border-radius: 4px; font-weight: 700; }
  .tier-warm { background: #fff3cd; color: #856404; padding: 1px 6px; border-radius: 4px; font-weight: 700; }
  .tier-cold { background: #e2e3e5; color: #383d41; padding: 1px 6px; border-radius: 4px; font-weight: 700; }
</style>""", unsafe_allow_html=True)

st.title("◌ Cognivelle")
st.caption("Unified Lead Discovery · Research · Outreach · Operational CRM")

# ---------------------------------------------------------------------------
# Session state defaults & auto-start scrapers on first load
# ---------------------------------------------------------------------------
if "biz_search_query" not in st.session_state:
    st.session_state.biz_search_query = ""
if "scrapers_auto_started" not in st.session_state:
    st.session_state.scrapers_auto_started = True
    if _SCRAPERS_AVAILABLE:
        try:
            start_all_scrapers()
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Navigation Tabs
# ---------------------------------------------------------------------------
(
    research_tab,
    crm_tab,
    discovery_tab,
    approvals_tab,
    team_tab,
    activity_tab,
) = st.tabs([
    "🤖 Agent Research",
    "💼 CRM & Pipeline",
    "🔎 Lead Discovery",
    "✉️ Outreach Approvals",
    "👥 Team Management",
    "📋 Activity Log",
])

# ============================================================
# TAB 1 — Agent Research
# ============================================================
with research_tab:
    st.markdown("### Autonomous Research Desk")
    st.caption("Ask your LangGraph agent to find businesses, extract contacts, score leads, or prepare outreach proposals.")

    col1, col2, col3, col4 = st.columns(4)
    quick_prompt = None
    with col1:
        if st.button("🔎 Find leads in my niche", use_container_width=True):
            quick_prompt = "Find 5 local businesses that could benefit from modern web development or workflow automation. Focus on medical clinics, real estate, or software houses."
    with col2:
        if st.button("🕷 Extract contacts from URL", use_container_width=True):
            quick_prompt = "Crawl the website https://example.com for emails, phone numbers, social profiles, and Google Maps links using crawl_website_for_contacts."
    with col3:
        if st.button("⚡ Score a lead", use_container_width=True):
            quick_prompt = "Score the first business in our CRM using score_lead."
    with col4:
        if st.button("📝 Draft outreach email", use_container_width=True):
            quick_prompt = "Draft a personalised outreach email for the last business I researched. Keep it short, reference their specific situation, and include a clear CTA."

    if quick_prompt:
        st.session_state.messages.append({"role": "user", "content": quick_prompt})

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    prompt = st.chat_input("Research a business, extract contacts, find leads, or draft outreach…")
    if prompt or quick_prompt:
        user_text = prompt or quick_prompt
        if prompt:
            st.session_state.messages.append({"role": "user", "content": user_text})
        if not _has_city_hint(user_text) and not user_text.lower().startswith("crawl") and not user_text.lower().startswith("find"):
            followup = "Which city or location should anchor this business entity?"
            with st.chat_message("user"):
                st.markdown(user_text)
            with st.chat_message("assistant"):
                st.markdown(followup)
            st.session_state.messages.append({"role": "assistant", "content": followup})
            st.rerun()
        else:
            with st.chat_message("user"):
                st.markdown(user_text)
            with st.chat_message("assistant"):
                with st.spinner("Researching…"):
                    try:
                        response = asyncio.run(_ask(st.session_state.messages))
                    except Exception as exc:
                        response = f"❌ Error: {type(exc).__name__}: {exc}"
                st.markdown(response)
            st.session_state.messages.append({"role": "assistant", "content": response})

    if st.session_state.messages:
        if st.button("🗑 Clear conversation", type="secondary"):
            st.session_state.messages = []
            st.rerun()

# ============================================================
# TAB 2 — CRM & Pipeline
# ============================================================
with crm_tab:
    st.markdown("### Operational CRM & Sales Pipeline")
    st.caption("Manage prospective and active businesses, contacts, notes, and deal stages.")

    summary = get_outreach_summary() if _STORE_AVAILABLE else _get_outreach_summary_direct()
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Leads", summary.get("total_businesses", 0))
    m2.metric("Not Contacted", summary.get("not_contacted", 0))
    m3.metric("Contacted", summary.get("contacted", 0))
    m4.metric("Demo Scheduled", summary.get("demo_scheduled", 0))
    m5.metric("Closed Won", summary.get("closed", 0))
    m6.metric("Follow-ups", summary.get("total_followups", 0))

    st.divider()

    crm_view_tab, kanban_view_tab = st.tabs(["📋 Business Directory", "📊 Kanban Board"])

    with crm_view_tab:
        # Action bar
        act_col1, act_col2, act_col3 = st.columns([2, 2, 2])
        with act_col1:
            if st.button("⚡ Score All Unscored Leads", type="secondary", use_container_width=True):
                if _PIPELINE_AVAILABLE:
                    with st.spinner("Scoring via MCP pipeline…"):
                        s_res = score_all_unscored_leads(limit=200)
                    st.success(f"Scored {s_res['scored_count']} leads — {s_res['tier_summary']}")
                    st.rerun()
                else:
                    st.warning("Lead scoring pipeline not available.")
        with act_col2:
            if st.button("⚡ Populate Unpopulated Leads", type="secondary", use_container_width=True):
                if _PIPELINE_AVAILABLE:
                    with st.spinner("Enriching unpopulated leads via MCP…"):
                        p_res = asyncio.run(populate_unpopulated_leads(limit=5))
                    st.success(f"Populated {p_res['successfully_populated']} leads with contacts & scores!")
                    st.rerun()
                else:
                    st.warning("Population engine not available.")
        with act_col3:
            with st.popover("➕ Add New Business", use_container_width=True):
                st.markdown("#### New Business Record")
                nb_name = st.text_input("Business Name *", key="nb_name")
                nb_website = st.text_input("Website URL", key="nb_web")
                nb_city = st.text_input("City / Location", key="nb_city")
                nb_phone = st.text_input("Phone Number", key="nb_phone")
                nb_email = st.text_input("Contact Email", key="nb_email")
                nb_notes = st.text_area("Notes & Opportunity Context", key="nb_notes")
                nb_stage = st.selectbox("Initial Stage", ["not_contacted", "unpopulated", "contacted", "demo_scheduled", "closed"], key="nb_stage")
                if st.button("💾 Save Business to CRM", type="primary", use_container_width=True):
                    if nb_name.strip():
                        try:
                            upsert_business(
                                name=nb_name.strip(), website=nb_website.strip() or None,
                                city=nb_city.strip() or None, phone=nb_phone.strip() or None,
                                email=nb_email.strip() or None, notes=nb_notes.strip() or None,
                                outreach_status=nb_stage,
                            )
                            st.success(f"Added {nb_name} to CRM!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")
                    else:
                        st.warning("Business name is required.")

        # Search / Filter
        s_col1, s_col2 = st.columns([3, 2])
        with s_col1:
            crm_query = st.text_input("Search businesses", value=st.session_state.biz_search_query,
                                      placeholder="Name, city, website, email…", label_visibility="collapsed")
            st.session_state.biz_search_query = crm_query
        with s_col2:
            stage_filter = st.selectbox(
                "Filter stage",
                ["All Stages", "unpopulated", "not_contacted", "contacted", "demo_scheduled", "closed", "not_interested"],
                label_visibility="collapsed"
            )

        like = f"%{crm_query.strip()}%"
        sql = (
            "SELECT id, name, website, city, phone, email, notes, outreach_status, "
            "followup_count, last_contacted_at, social_profiles, opportunity_score, detected_tech, source, "
            "assigned_member_id, assigned_member_name, lead_tier, proposal_path "
            "FROM businesses WHERE (name LIKE ? OR COALESCE(website,'') LIKE ? OR COALESCE(city,'') LIKE ? OR COALESCE(email,'') LIKE ?) "
        )
        params: list[Any] = [like, like, like, like]
        if stage_filter != "All Stages":
            sql += " AND outreach_status=? "
            params.append(stage_filter)
        sql += " ORDER BY updated_at DESC LIMIT 100"

        businesses = _query(sql, tuple(params))
        st.markdown(f"Found **{len(businesses)}** businesses:")

        if businesses:
            for biz in businesses:
                bid = biz["id"]
                bname = biz["name"]
                status = biz.get("outreach_status") or "not_contacted"
                score = biz.get("opportunity_score") or 0
                tier = biz.get("lead_tier") or "COLD"
                tier_badge = f"<span class='tier-{tier.lower()}'>{tier}</span>"
                city_str = f" · 📍 {biz['city']}" if biz.get("city") else ""
                web_str = f" · 🌐 {biz['website'][:28]}" if biz.get("website") else ""
                src_str = f" · 🔖 {biz['source']}" if biz.get("source") else ""

                with st.expander(
                    f"**{bname}**{city_str}{web_str}{src_str} — [{status.replace('_', ' ').title()}] Score: {score}/100 | Follow-ups: {biz.get('followup_count') or 0}"
                ):
                    col_info, col_actions = st.columns([3, 2])

                    with col_info:
                        st.markdown(f"##### Business Details  {tier_badge}", unsafe_allow_html=True)
                        e_web = st.text_input("Website", value=biz.get("website") or "", key=f"eweb_{bid}")
                        e_city = st.text_input("City / Region", value=biz.get("city") or "", key=f"ecity_{bid}")
                        c1, c2 = st.columns(2)
                        with c1:
                            e_phone = st.text_input("Phone", value=biz.get("phone") or "", key=f"ephone_{bid}")
                        with c2:
                            e_email = st.text_input("Email", value=biz.get("email") or "", key=f"eemail_{bid}")
                        e_notes = st.text_area("Notes", value=biz.get("notes") or "", key=f"enotes_{bid}", height=100)

                        try:
                            socials = json.loads(biz.get("social_profiles") or "{}") or {}
                        except Exception:
                            socials = {}
                        if socials:
                            parts = [f"[{p.capitalize()}]({u})" for p, u in socials.items() if u]
                            st.markdown("**Socials:** " + " · ".join(parts))

                        if st.button("💾 Save Changes", key=f"save_{bid}", type="primary"):
                            try:
                                upsert_business(
                                    name=bname, website=e_web.strip() or None,
                                    city=e_city.strip() or None, phone=e_phone.strip() or None,
                                    email=e_email.strip() or None, notes=e_notes.strip() or None,
                                )
                                st.success("Updated!")
                                st.rerun()
                            except Exception as ex:
                                st.error(str(ex))

                        st.markdown("---")
                        st.markdown("##### 👥 Contacts & Decision Makers")
                        biz_contacts = _query("SELECT id, name, title, email, phone, linkedin_url FROM contacts WHERE business_id=? ORDER BY id", (bid,))
                        if biz_contacts:
                            for ct in biz_contacts:
                                cid = ct["id"]
                                cname = ct.get("name") or "Unnamed"
                                ctitle = f" ({ct['title']})" if ct.get("title") else ""
                                cemail = f" · ✉️ {ct['email']}" if ct.get("email") else ""
                                cphone = f" · 📞 {ct['phone']}" if ct.get("phone") else ""
                                clink = f" · [LinkedIn]({ct['linkedin_url']})" if ct.get("linkedin_url") else ""
                                cc1, cc2 = st.columns([4, 2])
                                with cc1:
                                    st.markdown(f"**{cname}**{ctitle}{cemail}{cphone}{clink}")
                                with cc2:
                                    q_mail, q_wa, q_del = st.columns(3)
                                    with q_mail:
                                        if ct.get("email") and st.button("✉️", key=f"dm_{cid}", help="Draft Email"):
                                            draft_text = f"Hi {cname},\n\nI noticed {bname} and wanted to reach out regarding modern web development and automation."
                                            _execute(
                                                "INSERT INTO email_drafts(id, business_id, recipient, subject, body, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', datetime('now'))",
                                                (f"em-{cid}-{int(asyncio.get_event_loop().time())}", bid, ct["email"], f"Partnership with {bname}", draft_text)
                                            )
                                            st.toast("Email draft queued!")
                                    with q_wa:
                                        if (ct.get("phone") or biz.get("phone")) and st.button("💬", key=f"dwa_{cid}", help="Draft WhatsApp"):
                                            dest_phone = ct.get("phone") or biz.get("phone")
                                            create_whatsapp_draft(bname, dest_phone, f"Hi {cname}, reaching out from Cognivelle regarding automation for {bname}.")
                                            st.toast("WhatsApp draft queued!")
                                    with q_del:
                                        if st.button("🗑", key=f"delc_{cid}", help="Delete contact"):
                                            delete_contact(cid)
                                            st.rerun()

                        with st.popover(f"➕ Add Contact to {bname}"):
                            nc_name = st.text_input("Contact Name *", key=f"nc_name_{bid}")
                            nc_title = st.text_input("Role / Title", key=f"nc_title_{bid}")
                            nc_email = st.text_input("Direct Email", key=f"nc_email_{bid}")
                            nc_phone = st.text_input("Direct Phone", key=f"nc_phone_{bid}")
                            nc_linkedin = st.text_input("LinkedIn URL", key=f"nc_li_{bid}")
                            if st.button("Add Contact", key=f"btn_nc_{bid}", type="primary"):
                                if nc_name.strip():
                                    add_business_contact(bname, nc_name.strip(), nc_title.strip() or None,
                                                          nc_email.strip() or None, nc_phone.strip() or None, nc_linkedin.strip() or None)
                                    st.success("Contact added!")
                                    st.rerun()

                    with col_actions:
                        st.markdown("##### 🎯 Stage & Lead Actions")
                        avail_stages = ["unpopulated", "not_contacted", "contacted", "demo_scheduled", "closed", "not_interested"]
                        cur_idx = avail_stages.index(status) if status in avail_stages else 1
                        new_stage = st.selectbox("Pipeline Stage", avail_stages, index=cur_idx, key=f"stg_{bid}")
                        if new_stage != status:
                            if st.button("Update Stage", key=f"btn_stg_{bid}"):
                                update_outreach_status(bname, new_stage)
                                st.rerun()

                        p_col1, p_col2 = st.columns(2)
                        with p_col1:
                            if st.button("⚡ Score Lead", key=f"sc_{bid}", help="Run MCP scoring pipeline"):
                                if _PIPELINE_AVAILABLE:
                                    with st.spinner("Scoring…"):
                                        res_sc = asyncio.run(score_lead(bname))
                                    tier_icon = "🔴" if res_sc["lead_tier"] == "HOT" else ("🟡" if res_sc["lead_tier"] == "WARM" else "🔵")
                                    st.success(f"{tier_icon} {res_sc['opportunity_score']}/100 ({res_sc['lead_tier']})")
                                    st.rerun()
                        with p_col2:
                            if st.button("⚡ Populate MCP", key=f"pop_{bid}", help="Enrich website, contacts, DMs & score"):
                                if _PIPELINE_AVAILABLE:
                                    with st.spinner(f"Populating {bname}…"):
                                        res_pop = asyncio.run(populate_lead(bname))
                                    st.success(f"Populated! Assigned to: {res_pop.get('assigned_to', 'Team')}")
                                    st.rerun()

                        assigned_name = biz.get("assigned_member_name")
                        st.markdown(f"**Assigned:** `{assigned_name or 'Unassigned'}`")
                        with st.popover("👥 Assign & Notify via WhatsApp", use_container_width=True):
                            if _STORE_AVAILABLE:
                                active_tms = get_team_members(active_only=True)
                                tm_opts = {f"{m['name']} ({m['role']} - {m['contact_number']})": m for m in active_tms}
                                if tm_opts:
                                    sel_tm_label = st.selectbox("Select Team Member", list(tm_opts.keys()), key=f"sel_tm_{bid}")
                                    sel_tm = tm_opts[sel_tm_label]
                                    notify_wa = st.checkbox("Send WhatsApp alert now", value=True, key=f"notif_{bid}")
                                    if st.button("Assign Member", key=f"btn_assign_{bid}", type="primary"):
                                        asgn = assign_business_lead(bname, role=sel_tm["role"], notify=notify_wa)
                                        if asgn.get("notification", {}).get("sent"):
                                            st.success(f"Assigned to {sel_tm['name']} & WhatsApp sent!")
                                        else:
                                            st.info(f"Assigned to {sel_tm['name']} (WhatsApp queued in Approvals).")
                                        st.rerun()
                                else:
                                    st.warning("No active team members. Add them in the Team Management tab.")

                        st.markdown("---")
                        st.markdown("**Log Outreach Follow-up**")
                        fu_chan = st.selectbox("Channel", ["email", "phone", "whatsapp", "meeting", "linkedin"], key=f"chan_{bid}")
                        fu_note = st.text_input("Touchpoint Note", key=f"fnote_{bid}")
                        if st.button("➕ Log Follow-up", key=f"btn_fu_{bid}", type="secondary", use_container_width=True):
                            log_followup(bname, channel=fu_chan, notes=fu_note)
                            st.toast(f"Follow-up logged via {fu_chan}!")
                            st.rerun()

                        st.markdown("---")
                        if st.button("🗑 Delete Business Record", key=f"del_biz_{bid}", type="secondary"):
                            delete_business(bname)
                            st.warning(f"Deleted {bname}")
                            st.rerun()
        else:
            st.info("No businesses found. Use Lead Discovery to scrape Google Maps, or the Agent tab to research.")

    with kanban_view_tab:
        st.markdown("#### Sales Pipeline Kanban")
        k_cols = st.columns(6)
        stages = [
            ("⚪ Unpopulated", "unpopulated"),
            ("🔵 Not Contacted", "not_contacted"),
            ("🟡 Contacted", "contacted"),
            ("🟢 Demo Scheduled", "demo_scheduled"),
            ("✅ Closed Won", "closed"),
            ("🔴 Not Interested", "not_interested"),
        ]
        for col, (label, stg_val) in zip(k_cols, stages):
            with col:
                st.markdown(f"**{label}**")
                stage_biz = _query("SELECT name, website, city, followup_count, opportunity_score, lead_tier, assigned_member_name FROM businesses WHERE outreach_status=? ORDER BY updated_at DESC LIMIT 20", (stg_val,))
                st.caption(f"{len(stage_biz)} leads")
                for sb in stage_biz:
                    with st.container(border=True):
                        st.markdown(f"**{sb['name']}**")
                        if sb.get("city"):
                            st.caption(f"📍 {sb['city']}")
                        if sb.get("website"):
                            st.caption(f"🌐 {sb['website'][:22]}")
                        tier = (sb.get("lead_tier") or "COLD").upper()
                        tier_color = {"HOT": "🔴", "WARM": "🟡", "COLD": "🔵", "UNPOPULATED": "⚪"}.get(tier, "⚪")
                        st.markdown(f"{tier_color} Score: **{sb.get('opportunity_score') or 0}**")
                        if sb.get("assigned_member_name"):
                            st.caption(f"👤 {sb['assigned_member_name']}")

# ============================================================
# TAB 3 — Lead Discovery
# ============================================================
with discovery_tab:
    st.markdown("### Lead Discovery Hub")
    st.caption("Google Maps via pymapscraper subprocess · Social lead ingestion via MCP · Deep web crawler")

    # Background Scrapers Supervisor
    with st.container(border=True):
        sc_col1, sc_col2 = st.columns([3, 2])
        with sc_col1:
            st.markdown("#### ⚡ Background Services Supervisor")
            st.caption("All background scrapers feed the single central CRM database directly.")
        with sc_col2:
            b_start, b_stop, b_ref = st.columns(3)
            with b_start:
                if st.button("▶ Start All", type="primary", use_container_width=True, key="btn_start_all"):
                    if _SCRAPERS_AVAILABLE:
                        start_all_scrapers()
                        st.toast("Started background scrapers!")
                        st.rerun()
            with b_stop:
                if st.button("⏹ Stop All", type="secondary", use_container_width=True, key="btn_stop_all"):
                    if _SCRAPERS_AVAILABLE:
                        stop_all_scrapers()
                        st.toast("Stopped background scrapers.")
                        st.rerun()
            with b_ref:
                if st.button("🔄 Refresh", use_container_width=True, key="btn_ref"):
                    st.rerun()

        if _SCRAPERS_AVAILABLE:
            s_status = get_services_status()
            p1, p2, p3, p4, p5 = st.columns(5)
            for col, key_name in zip([p1, p2, p3, p4, p5],
                                     ["lead-classifier", "whatsappSenderService", "leadIngestion", "RedditScraper", "pymapscraper"]):
                svc = s_status.get(key_name, {})
                badge = "🟢" if svc.get("running") else "⚪"
                col.metric(svc.get("name", key_name).split()[0], f"{badge} {svc.get('status', 'Unknown')}")

        with st.expander("📋 Service Logs"):
            log_svc = st.selectbox("Service", ["lead-classifier", "whatsappSenderService", "leadIngestion", "RedditScraper", "pymapscraper"], key="sel_log_svc")
            if _SCRAPERS_AVAILABLE:
                st.code(get_recent_log(log_svc, max_lines=40), language="text")

    st.markdown("---")

    disc_maps_tab, disc_social_tab, disc_crawl_tab = st.tabs([
        "📍 Google Maps (pymapscraper)",
        "📱 Social Leads (Reddit / FB → MCP)",
        "🕷 Deep Web Crawler",
    ])

    # -------------------------------------------------------
    # Sub-tab: Google Maps — pymapscraper ONLY
    # -------------------------------------------------------
    with disc_maps_tab:
        st.markdown("#### Google Maps Subprocess Scraper")
        st.caption(
            "**pymapscraper** is the only Google Maps discovery engine. "
            "It runs as a background subprocess and saves places directly to the central database, "
            "which auto-mirrors them to the CRM as `unpopulated` leads."
        )

        m_col1, m_col2, m_col3, m_col4 = st.columns([3, 2, 1, 1])
        with m_col1:
            maps_query = st.text_input("Niche / Category", value="dental clinics", placeholder="e.g. dental clinics, real estate, hotels")
        with m_col2:
            maps_city = st.text_input("City / Region", value="Manchester", placeholder="e.g. London, Dubai, Karachi")
        with m_col3:
            maps_limit = st.number_input("Max Places", min_value=1, max_value=100, value=15)
        with m_col4:
            maps_delay = st.number_input("Delay (s)", min_value=1.0, max_value=10.0, value=2.0, step=0.5)

        btn_launch, btn_stop_sub, btn_sync = st.columns([2, 2, 2])
        with btn_launch:
            if st.button("🚀 Launch Maps Scraper Subprocess", type="primary", use_container_width=True):
                if _SCRAPERS_AVAILABLE:
                    res = launch_pymapscraper_subprocess(
                        query=maps_query, city=maps_city,
                        max_places=int(maps_limit), delay=float(maps_delay)
                    )
                    if res.get("status") == "started":
                        st.success(f"✅ Maps scraper launched (PID: {res.get('pid')})! Results stream directly into CRM as unpopulated leads.")
                    elif res.get("status") == "already_running":
                        st.warning("Maps scraper is already running.")
                    else:
                        st.error(f"Failed: {res.get('error')}")
                    st.rerun()
                else:
                    st.error("Scrapers manager not available.")

        with btn_stop_sub:
            if st.button("⏹ Stop Maps Subprocess", type="secondary", use_container_width=True):
                if _SCRAPERS_AVAILABLE:
                    stop_service("pymapscraper")
                    st.info("Maps scraper stopped.")
                    st.rerun()

        with btn_sync:
            if st.button("📥 Sync Places → CRM", type="secondary", use_container_width=True):
                if _STORE_AVAILABLE:
                    synced = sync_places_to_unpopulated_leads()
                    st.success(f"Synced {synced} new places to CRM as unpopulated leads!")
                    st.rerun()
                else:
                    st.error("Contact store not available.")

        # Live status indicator
        if _SCRAPERS_AVAILABLE:
            maps_svc = get_services_status().get("pymapscraper", {})
            if maps_svc.get("running"):
                st.info(f"🟢 Maps scraper subprocess is RUNNING (PID: {maps_svc.get('pid')}). Discovered places stream into the central database automatically.")
            else:
                st.caption("⚪ Maps scraper subprocess is idle.")

        # Quick stats from places table
        places_count = _query("SELECT COUNT(*) as c FROM places")
        unpop_count = _query("SELECT COUNT(*) as c FROM businesses WHERE outreach_status='unpopulated'")
        pc = places_count[0]["c"] if places_count else 0
        uc = unpop_count[0]["c"] if unpop_count else 0
        st.markdown(f"**Places in DB:** {pc} &nbsp;·&nbsp; **Unpopulated CRM leads:** {uc}")

    # -------------------------------------------------------
    # Sub-tab: Social Leads → MCP ingest
    # -------------------------------------------------------
    with disc_social_tab:
        st.markdown("#### Social Media Lead Ingestion via MCP")
        st.caption(
            "Reddit & Facebook scrapers run in the background and push leads here. "
            "Each lead flows through **`ingest_scraper_lead`** (MCP): normalise → score → assign → WhatsApp alert."
        )

        # Manual MCP ingest form (for testing / manual entry)
        with st.expander("➕ Manually Ingest a Social Lead via MCP", expanded=False):
            si_source = st.selectbox("Source Platform", ["reddit", "facebook", "linkedin", "other"], key="si_source")
            si_title = st.text_input("Post Title / Headline", key="si_title")
            si_posted_by = st.text_input("Posted By (username / company name)", key="si_posted_by")
            si_body = st.text_area("Post Body / Description", key="si_body", height=100)
            si_group = st.text_input("Group / Community Name", key="si_group")
            si_city = st.text_input("City (if known)", key="si_city")
            si_phone = st.text_input("Phone (if in post)", key="si_phone")
            si_email = st.text_input("Email (if in post)", key="si_email")
            si_auto_assign = st.checkbox("Auto-assign to team member & WhatsApp alert", value=True, key="si_auto")
            si_auto_score = st.checkbox("Auto-score via MCP scoring pipeline", value=True, key="si_score")

            if st.button("📥 Ingest Lead via MCP", type="primary", use_container_width=True, key="btn_si"):
                if si_title.strip() or si_posted_by.strip():
                    if _STORE_AVAILABLE:
                        with st.spinner("Ingesting via MCP pipeline…"):
                            res = ingest_scraper_lead(
                                title=si_title.strip(),
                                body=si_body.strip(),
                                source=si_source,
                                posted_by=si_posted_by.strip(),
                                contact_phone=si_phone.strip() or None,
                                contact_email=si_email.strip() or None,
                                group_name=si_group.strip() or None,
                                city=si_city.strip() or None,
                                auto_assign=si_auto_assign,
                                auto_score=si_auto_score,
                            )
                        score_str = f" | Score: {res['score']}/100 ({res['tier']})" if res.get("score") else ""
                        asgn_str = f" | Assigned to: {res.get('assignment', {}).get('assigned_name', 'N/A')}" if res.get("assignment") else ""
                        st.success(f"✅ Lead ingested: **{res['business_name']}**{score_str}{asgn_str}")
                        st.rerun()
                    else:
                        st.error("Contact store not available.")
                else:
                    st.warning("Provide at least a title or posted-by name.")

        # Bulk sync from background scrapers (reads from the `leads` table already saved by scrapers)
        st.markdown("---")
        st.markdown("##### Sync Leads from Background Social Scrapers")
        st.caption("The Reddit & Facebook scrapers write to the `leads` table in the central DB. Use this to push them through the MCP scoring & assignment pipeline.")

        sync_col1, sync_col2 = st.columns(2)
        with sync_col1:
            sync_limit = st.number_input("Max leads to process", min_value=1, max_value=200, value=20, key="sync_limit")
        with sync_col2:
            st.write("")
            st.write("")
            if st.button("🔄 Process Pending Social Leads → MCP", type="primary", use_container_width=True, key="btn_sync_social"):
                if _STORE_AVAILABLE:
                    # Read unprocessed leads from the `leads` table and push through ingest_scraper_lead
                    pending_leads = _query(
                        "SELECT id, title, text, source, posted_by, user, group_name, channel, contact_value "
                        "FROM leads WHERE status='assigned' "
                        "ORDER BY created_at DESC LIMIT ?",
                        (int(sync_limit),)
                    )
                    processed = 0
                    for lead in pending_leads:
                        try:
                            phone = lead["contact_value"] if lead.get("channel") == "call" else None
                            email = lead["contact_value"] if lead.get("channel") == "mail" else None
                            ingest_scraper_lead(
                                title=lead["title"] or "",
                                body=lead["text"] or "",
                                source=lead["source"] or "social",
                                posted_by=lead["posted_by"] or lead["user"] or "",
                                contact_phone=phone,
                                contact_email=email,
                                group_name=lead["group_name"],
                                auto_assign=True,
                                auto_score=True,
                            )
                            processed += 1
                        except Exception:
                            pass
                    st.success(f"Processed {processed}/{len(pending_leads)} social leads through MCP!")
                    st.rerun()
                else:
                    st.error("Contact store not available.")

        # Stats
        leads_count = _query("SELECT COUNT(*) as c FROM leads")
        lc = leads_count[0]["c"] if leads_count else 0
        reddit_count = _query("SELECT COUNT(*) as c FROM reddit_posts") if _query("SELECT name FROM sqlite_master WHERE type='table' AND name='reddit_posts'") else [{"c": 0}]
        rc = reddit_count[0]["c"] if reddit_count else 0
        st.markdown(f"**Leads in DB (scrapers):** {lc} &nbsp;·&nbsp; **Reddit posts:** {rc}")

    # -------------------------------------------------------
    # Sub-tab: Deep Web Crawler
    # -------------------------------------------------------
    with disc_crawl_tab:
        st.markdown("#### Deep Web Agency Crawler")
        st.caption("Powered by the Playwright/aiohttp `isolated_crawler` engine — detects CMS, booking systems, and lead scores.")

        cw_col1, cw_col2 = st.columns([4, 2])
        with cw_col1:
            crawl_url = st.text_input("Enter Website URL to Deep Crawl", placeholder="https://example.com")
        with cw_col2:
            st.write("")
            st.write("")
            if st.button("🕷 Run Web Crawler", type="primary", use_container_width=True):
                if crawl_url.strip():
                    with st.spinner(f"Crawling {crawl_url}…"):
                        try:
                            res = asyncio.run(_call_tool("crawl_website_for_contacts", website_url=crawl_url.strip(), max_pages=5))
                            st.success(f"Crawled {res.get('pages_crawled', 0)} pages!")
                            c1, c2, c3 = st.columns(3)
                            with c1:
                                st.markdown("**Emails:**")
                                for em in res.get("emails_found", []):
                                    st.code(em, language=None)
                            with c2:
                                st.markdown("**Phones:**")
                                for ph in res.get("phones_found", []):
                                    st.code(ph, language=None)
                            with c3:
                                st.markdown("**Socials:**")
                                for k, v in res.get("social_profiles", {}).items():
                                    st.markdown(f"[{k.capitalize()}]({v})")
                        except Exception as e:
                            st.error(f"Crawl failed: {e}")
                else:
                    st.warning("Please enter a website URL.")

        st.divider()
        st.markdown("##### Bulk Hot Leads Import")
        st.caption("Import pre-enriched leads from `isolated_crawler/hot_leads.xlsx`.")
        if st.button("📥 Import Hot Leads from hot_leads.xlsx", type="secondary"):
            with st.spinner("Importing enriched leads…"):
                if _STORE_AVAILABLE:
                    imported = ingest_hot_leads_excel()
                    st.success(f"✅ Imported {imported} hot leads with CMS tech detection & opportunity scores!")
                else:
                    st.error("Contact store not importable.")

# ============================================================
# TAB 4 — Outreach Approvals
# ============================================================
with approvals_tab:
    st.markdown("### Outreach Command & Approvals")
    st.caption("Human-in-the-loop review queue for Email and WhatsApp before dispatch.")

    outreach_email_tab, outreach_wa_tab = st.tabs(["📧 Email Approvals (SMTP)", "💬 WhatsApp Approvals"])

    with outreach_email_tab:
        drafts = _query(
            "SELECT d.id, d.recipient, d.subject, d.body, d.created_at, COALESCE(b.name, '') AS business_name "
            "FROM email_drafts d LEFT JOIN businesses b ON b.id = d.business_id "
            "WHERE d.status='pending' ORDER BY d.created_at DESC"
        )
        sent_count = _query("SELECT COUNT(*) as c FROM email_drafts WHERE status='sent'")
        ea1, ea2, ea3 = st.columns(3)
        ea1.metric("Pending Email Drafts", len(drafts))
        ea2.metric("Emails Sent", sent_count[0]["c"] if sent_count else 0)
        ea3.metric("Sender Account", os.getenv("SMTP_FROM_EMAIL", "Configured in mcp/.env"))

        with st.expander("➕ Create Manual Email Draft"):
            m_bname = st.text_input("Business Name", key="md_bname")
            m_recip = st.text_input("Recipient Email", key="md_recip")
            m_subj = st.text_input("Subject", key="md_subj")
            m_body = st.text_area("Body Text", key="md_body", height=150)
            if st.button("Queue Draft", key="btn_qmail"):
                if m_recip.strip() and m_body.strip():
                    _execute(
                        "INSERT INTO email_drafts(id, recipient, subject, body, status, created_at) VALUES (?, ?, ?, ?, 'pending', datetime('now'))",
                        (f"em-{int(asyncio.get_event_loop().time())}", m_recip.strip(), m_subj.strip(), m_body.strip())
                    )
                    st.success("Draft queued!")
                    st.rerun()

        if not drafts:
            st.info("No pending email drafts.")

        for draft in drafts:
            did = draft["id"]
            bname = draft.get("business_name") or ""
            with st.container(border=True):
                h1, h2 = st.columns([4, 1])
                with h1:
                    st.markdown(f"**To:** {draft['recipient']}" + (f" · **Business:** {bname}" if bname else "") + f" · *{draft['created_at'][:16]}*")
                with h2:
                    st.markdown("<span class='badge-contacted'>Pending</span>", unsafe_allow_html=True)
                new_subject = st.text_input("Subject", value=draft["subject"], key=f"subj-{did}")
                new_body = st.text_area("Email Body", value=draft["body"], height=160, key=f"body-{did}")
                col_save, col_prev, col_send, col_disc = st.columns([2, 2, 2, 1])
                with col_save:
                    if st.button("💾 Save Edits", key=f"save-{did}"):
                        update_draft_body(did, new_subject, new_body)
                        st.success("Draft saved.")
                        st.rerun()
                with col_prev:
                    with st.popover("👁 Preview HTML"):
                        try:
                            from enrichment_mcp.tools.outreach_email import _build_html
                            st.components.v1.html(_build_html(bname or "Business", new_body), height=500, scrolling=True)
                        except Exception as exc:
                            st.warning(f"Preview unavailable: {exc}")
                with col_send:
                    if st.button("✅ Approve & Send", key=f"send-{did}", type="primary"):
                        update_draft_body(did, new_subject, new_body)
                        with st.spinner("Sending via SMTP…"):
                            try:
                                res = asyncio.run(_call_tool("send_approved_email", draft_id=did, user_approval_phrase=f"APPROVE EMAIL {did}"))
                                st.success(f"Sent to {res.get('recipient', draft['recipient'])}!")
                                st.rerun()
                            except Exception as exc:
                                st.error(f"Send failed: {exc}")
                with col_disc:
                    if st.button("🗑", key=f"disc-{did}", help="Discard"):
                        _execute("UPDATE email_drafts SET status='discarded' WHERE id=?", (did,))
                        st.rerun()

        with st.expander("📤 Sent Emails Log"):
            sent_emails = _query("SELECT recipient, subject, sent_at FROM email_drafts WHERE status='sent' ORDER BY sent_at DESC LIMIT 30")
            if sent_emails:
                st.dataframe(sent_emails, use_container_width=True, hide_index=True)
            else:
                st.info("No emails sent yet.")

    with outreach_wa_tab:
        wa_drafts = list_whatsapp_drafts("pending") if _STORE_AVAILABLE else _query("SELECT * FROM whatsapp_drafts WHERE status='pending' ORDER BY created_at DESC")
        wa_sent = _query("SELECT COUNT(*) as c FROM whatsapp_drafts WHERE status='sent'")
        wa1, wa2, wa3 = st.columns(3)
        wa1.metric("Pending WhatsApp Drafts", len(wa_drafts))
        wa2.metric("WhatsApp Sent", wa_sent[0]["c"] if wa_sent else 0)
        wa3.metric("Gateway URL", os.getenv("WHATSAPP_SERVICE_URL", "http://localhost:3000/sendMessage"))

        with st.expander("➕ Create Manual WhatsApp Draft"):
            wa_bname = st.text_input("Business Name", key="wa_nbname")
            wa_phone = st.text_input("Recipient Phone (e.g. +923001234567)", key="wa_nphone")
            wa_msg = st.text_area("Message", key="wa_nmsg", height=100)
            if st.button("Queue WhatsApp Draft", key="btn_qwa"):
                if wa_phone.strip() and wa_msg.strip():
                    create_whatsapp_draft(wa_bname.strip(), wa_phone.strip(), wa_msg.strip())
                    st.success("WhatsApp draft queued!")
                    st.rerun()

        if not wa_drafts:
            st.info("No pending WhatsApp drafts.")

        for wd in wa_drafts:
            wid = wd["id"]
            wbname = wd.get("business_name") or ""
            wphone = wd["recipient_phone"]
            with st.container(border=True):
                wh1, wh2 = st.columns([4, 1])
                with wh1:
                    st.markdown(f"**To:** `{wphone}`" + (f" · **{wbname}**" if wbname else "") + f" · *{wd['created_at'][:16]}*")
                with wh2:
                    st.markdown("<span class='badge-contacted'>Pending</span>", unsafe_allow_html=True)
                edit_msg = st.text_area("Message", value=wd["message"], height=100, key=f"wmsg_{wid}")
                w_c1, w_c2, w_c3, w_c4 = st.columns([2, 2, 2, 1])
                with w_c1:
                    if st.button("💾 Save", key=f"wsave_{wid}"):
                        update_whatsapp_draft(wid, edit_msg)
                        st.success("Updated!"); st.rerun()
                with w_c2:
                    st.link_button("💬 Open WhatsApp Web", _wa_link(wphone, edit_msg), use_container_width=True)
                with w_c3:
                    if st.button("📲 Send via Gateway", key=f"wsend_{wid}", type="primary", use_container_width=True):
                        with st.spinner("Dispatching…"):
                            res = _send_whatsapp_service(wphone, edit_msg)
                            if res.get("status") == "ok":
                                mark_whatsapp_draft_sent(wid)
                                st.success("Sent!"); st.rerun()
                            else:
                                st.warning(f"Gateway error: {res.get('error')}. Use 'Open WhatsApp Web' directly.")
                with w_c4:
                    if st.button("🗑", key=f"wdisc_{wid}", help="Discard"):
                        discard_whatsapp_draft(wid); st.rerun()

        with st.expander("📤 Sent WhatsApp Log"):
            sent_wa = _query("SELECT recipient_phone, message, sent_at FROM whatsapp_drafts WHERE status='sent' ORDER BY sent_at DESC LIMIT 30")
            if sent_wa:
                st.dataframe(sent_wa, use_container_width=True, hide_index=True)
            else:
                st.info("No WhatsApp messages sent yet.")

# ============================================================
# TAB 5 — Team Management
# ============================================================
with team_tab:
    st.markdown("### 👥 Sales Team Management")
    st.caption("Manage your sales team members — their roles, WhatsApp numbers, and assignment statistics.")

    if not _STORE_AVAILABLE:
        st.error("Contact store not available — cannot load team members.")
    else:
        tms = get_team_members(active_only=False)

        # Summary metrics
        active_count = sum(1 for m in tms if m["is_active"])
        total_assigned = sum(m["assigned_count"] for m in tms)
        tm1, tm2, tm3 = st.columns(3)
        tm1.metric("Total Members", len(tms))
        tm2.metric("Active Members", active_count)
        tm3.metric("Total Leads Assigned", total_assigned)

        st.divider()

        # Add new member
        with st.expander("➕ Add New Team Member", expanded=len(tms) == 0):
            na_col1, na_col2, na_col3 = st.columns(3)
            with na_col1:
                atm_name = st.text_input("Full Name *", key="atm_name", placeholder="e.g. Ahmed Ali")
            with na_col2:
                atm_num = st.text_input("WhatsApp Number *", key="atm_num", placeholder="923001234567 (no spaces or +)")
            with na_col3:
                atm_role = st.selectbox("Role", ["caller", "DM", "email", "all"], key="atm_role",
                                         help="caller=phone leads, DM=WhatsApp/social, email=email leads, all=any")
            atm_email = st.text_input("Email (optional)", key="atm_email", placeholder="ahmed@example.com")
            if st.button("➕ Add Member to Team", key="btn_add_tm", type="primary", use_container_width=True):
                if atm_name.strip() and atm_num.strip():
                    add_team_member(atm_name.strip(), atm_num.strip(), role=atm_role, email=atm_email.strip() or None)
                    st.success(f"✅ Added **{atm_name}** to sales team!")
                    st.rerun()
                else:
                    st.warning("Name and WhatsApp number are required.")

        st.markdown("---")

        # Team member cards
        if not tms:
            st.info("No team members yet. Add one above to start assigning leads.")
        else:
            st.markdown("#### Team Members")
            for tm in tms:
                tid = tm["id"]
                is_active = bool(tm["is_active"])
                active_icon = "🟢" if is_active else "⚪"
                role_badge = {"caller": "📞 Caller", "DM": "💬 DM", "email": "📧 Email", "all": "🔀 All Roles"}.get(tm["role"], tm["role"])

                with st.container(border=True):
                    h1, h2 = st.columns([4, 2])
                    with h1:
                        st.markdown(f"### {active_icon} {tm['name']}")
                        st.markdown(f"**Role:** {role_badge} &nbsp;·&nbsp; **WhatsApp:** `{tm['contact_number']}`" +
                                    (f" &nbsp;·&nbsp; **Email:** {tm['email']}" if tm.get('email') else ""))
                        st.markdown(f"**Leads Assigned:** {tm['assigned_count']} &nbsp;·&nbsp; **Last Assigned:** {tm.get('last_assigned_at') or 'Never'}")
                    with h2:
                        bt1, bt2, bt3 = st.columns(3)
                        with bt1:
                            toggle_label = "⏸ Deactivate" if is_active else "▶ Activate"
                            if st.button(toggle_label, key=f"tog_{tid}", use_container_width=True):
                                toggle_team_member(tid, not is_active)
                                st.rerun()
                        with bt2:
                            if st.button("💬 Test WhatsApp", key=f"twa_{tid}", use_container_width=True):
                                test_msg = f"👋 Hi {tm['name']}, this is a test message from Cognivelle CRM. You are configured as a sales team member with role: {tm['role']}."
                                result = dispatch_whatsapp_message(tm["contact_number"], test_msg)
                                if result.get("sent"):
                                    st.success("WhatsApp test sent!")
                                else:
                                    st.info(f"Queued in Approvals tab (gateway not reachable). Error: {result.get('error', 'none')}")
                        with bt3:
                            if st.button("🗑 Remove", key=f"del_tm_{tid}", type="secondary", use_container_width=True):
                                remove_team_member(tid)
                                st.warning(f"Removed {tm['name']}")
                                st.rerun()

                    # Inline edit
                    with st.expander(f"✏️ Edit {tm['name']}'s Details"):
                        ed_col1, ed_col2, ed_col3 = st.columns(3)
                        with ed_col1:
                            ed_name = st.text_input("Name", value=tm["name"], key=f"ed_nm_{tid}")
                        with ed_col2:
                            ed_num = st.text_input("WhatsApp Number", value=tm["contact_number"], key=f"ed_num_{tid}")
                        with ed_col3:
                            role_opts = ["caller", "DM", "email", "all"]
                            cur_role_idx = role_opts.index(tm["role"]) if tm["role"] in role_opts else 3
                            ed_role = st.selectbox("Role", role_opts, index=cur_role_idx, key=f"ed_role_{tid}")
                        ed_email = st.text_input("Email", value=tm.get("email") or "", key=f"ed_email_{tid}")
                        if st.button("💾 Save Changes", key=f"ed_save_{tid}", type="primary"):
                            update_team_member(tid, name=ed_name.strip(), contact_number=ed_num.strip(),
                                               role=ed_role, email=ed_email.strip() or None)
                            st.success("Team member updated!")
                            st.rerun()

        st.divider()
        st.markdown("#### 📊 Assignment Distribution")
        if tms:
            assign_data = [{"Name": m["name"], "Role": m["role"], "Leads Assigned": m["assigned_count"],
                            "Active": "✅" if m["is_active"] else "❌", "WhatsApp": m["contact_number"]} for m in tms]
            st.dataframe(assign_data, use_container_width=True, hide_index=True)

            # Manual assignment: assign unassigned leads to round-robin members
            st.markdown("---")
            st.markdown("##### Bulk Auto-Assign Unassigned Leads")
            unassigned_count = _query("SELECT COUNT(*) as c FROM businesses WHERE assigned_member_name IS NULL OR assigned_member_name = ''")
            uc_val = unassigned_count[0]["c"] if unassigned_count else 0
            st.caption(f"Unassigned leads in CRM: **{uc_val}**")
            bulk_limit = st.number_input("Max leads to assign", min_value=1, max_value=500, value=50, key="bulk_assign_limit")
            if st.button("🔀 Auto-Assign Unassigned Leads to Team", type="primary", use_container_width=True, key="btn_bulk_assign"):
                unassigned = _query(
                    "SELECT name FROM businesses WHERE (assigned_member_name IS NULL OR assigned_member_name='') "
                    "AND outreach_status != 'closed' ORDER BY opportunity_score DESC LIMIT ?",
                    (int(bulk_limit),)
                )
                assigned_count = 0
                for b in unassigned:
                    try:
                        assign_business_lead(b["name"], notify=False)
                        assigned_count += 1
                    except Exception:
                        pass
                st.success(f"Auto-assigned {assigned_count} leads to team members!")
                st.rerun()

# ============================================================
# TAB 6 — Activity Log & Health
# ============================================================
with activity_tab:
    st.markdown("### Durable Activity Log & System Health")
    st.caption("Audit trail of all MCP tool executions stored in SQLite WAL mode.")

    filter_tool = st.selectbox(
        "Filter by tool",
        ["(all)"] + [r["tool_name"] for r in _query("SELECT DISTINCT tool_name FROM tool_runs ORDER BY tool_name")],
    )
    limit = st.slider("Max rows", 10, 200, 50)

    if filter_tool == "(all)":
        runs = _query("SELECT id, tool_name, status, error, created_at FROM tool_runs ORDER BY id DESC LIMIT ?", (limit,))
    else:
        runs = _query("SELECT id, tool_name, status, error, created_at FROM tool_runs WHERE tool_name=? ORDER BY id DESC LIMIT ?", (filter_tool, limit))

    ok_count = sum(1 for r in runs if r.get("status") == "ok")
    err_count = sum(1 for r in runs if r.get("status") == "error")
    st.markdown(f"Showing **{len(runs)}** runs — ✅ {ok_count} ok, ❌ {err_count} errors")
    st.dataframe(runs, use_container_width=True, hide_index=True)

    with st.expander("🔍 Inspect Full Run JSON"):
        run_id = st.number_input("Run ID", min_value=1, step=1, value=1)
        if st.button("Load Details"):
            detail = _query("SELECT * FROM tool_runs WHERE id=?", (int(run_id),))
            if detail:
                st.json(json.loads(detail[0].get("result_json") or "{}"))
            else:
                st.warning("Run ID not found.")

    st.divider()
    st.markdown("##### 🗄️ Database Summary")
    db_tables = ["businesses", "contacts", "places", "leads", "team_members", "email_drafts", "whatsapp_drafts", "followups", "tool_runs"]
    summary_rows = []
    for t in db_tables:
        r = _query(f"SELECT COUNT(*) as c FROM {t}")
        summary_rows.append({"Table": t, "Records": r[0]["c"] if r else 0})
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)
