import os
import re
import json
import time
from collections import deque
from urllib.parse import urlparse

import requests
import streamlit as st
from openai import OpenAI

# ----------------------------
# Page setup
# ----------------------------
st.set_page_config(page_title="Governance Resource Finder", page_icon="📚", layout="wide")
st.title("📚 Governance Resource Finder (Open / Freely Accessible)")
st.caption("Paste a module-level learning objective (MLO). Optionally add constraints (region, media type, recency, exclusions).")

# Quick reset to clear all session state
with st.sidebar:
    if st.button("Reset app"):
        st.session_state.clear()
        st.success("App state cleared.")
        st.rerun()

# ----------------------------
# Passcode gate (passcode must be in Streamlit Secrets)
# ----------------------------
EXPECTED_PASSCODE = st.secrets.get("APP_PASSCODE", None)
if EXPECTED_PASSCODE is None:
    st.error("⚠️ APP_PASSCODE is not set in Streamlit Secrets. Add it in your app's Secrets.")
    st.stop()

if "authed" not in st.session_state:
    st.session_state.authed = False

with st.sidebar:
    st.markdown("**Access**")
    if not st.session_state.authed:
        code = st.text_input("Passcode", type="password")
        if st.button("Unlock"):
            if code == EXPECTED_PASSCODE:
                st.session_state.authed = True
                st.success("Unlocked")
                time.sleep(0.3)
                st.rerun()
            else:
                st.error("Incorrect passcode")

if not st.session_state.authed:
    st.stop()

# ----------------------------
# Gentle per-session rate limit (15 runs per rolling hour)
# ----------------------------
WINDOW_SECONDS = 3600
MAX_RUNS_PER_WINDOW = 15

if "run_stamps" not in st.session_state:
    st.session_state.run_stamps = deque()

def allow_session_run() -> bool:
    now = time.time()
    while st.session_state.run_stamps and now - st.session_state.run_stamps[0] > WINDOW_SECONDS:
        st.session_state.run_stamps.popleft()
    return len(st.session_state.run_stamps) < MAX_RUNS_PER_WINDOW

def record_session_run() -> None:
    st.session_state.run_stamps.append(time.time())

# ----------------------------
# Sidebar controls / tips
# ----------------------------
with st.sidebar:
    st.markdown("**Tips**")
    st.markdown(
        "- Keep the objective at module scope (broad goal).\n"
        "- Add constraints if helpful (region, since-year, media types, exclusions)."
    )
    temperature = st.slider("Creativity (lower = stricter)", 0.0, 1.0, 0.2, 0.1)
    model = st.selectbox("Model", ["gpt-4o", "gpt-4o-mini"], index=0)
    show_diag = st.checkbox("Show diagnostics (attempts & broken links)", value=False)

# ----------------------------
# Inputs (no topical examples)
# ----------------------------
mlo = st.text_area(
    "Module-level objective (MLO)",
    height=140,
    placeholder="e.g., Analyze how industrialization accelerated the growth of modern cities and identify resulting social and environmental problems."
)
constraints = st.text_area(
    "Optional constraints",
    placeholder="e.g., Region: global; Recency: since 2010; Media: datasets + policy briefs; Exclusions: blogs"
)

# Guard against giant inputs
MAX_CHARS = 2000
if len(mlo or "") > MAX_CHARS or len(constraints or "") > MAX_CHARS:
    st.warning("Input is too long. Please shorten the objective and/or constraints.")
    st.stop()

# ----------------------------
# System prompt (neutral; strict scope; no links outside E)
# ----------------------------
BASE_SYSTEM_PROMPT = """
You are Governance Resource Finder, an AI research assistant for instructional designers building courses on government and public policy, urbanization and city development, sustainability and environment, public administration, and related social-science topics. Your job is to find, vet, and summarize open or freely accessible learning resources—NOT to teach or fulfill the objective.

Terminology
- Module-Level Learning Objective (MLO): broad goal for a module.
- Elemental Learning Objectives (ELOs): 2–4 specific, measurable outcomes that break the MLO into parts; start with action verbs (Identify/Analyze/Evaluate…).
- Student Reading: a short (≤ 10 pages) open or freely accessible reading for educated non-specialists introducing the core concept.

Hard rule on scope
- All sections must strictly align to the user’s MLO. Do NOT switch to adjacent topics unless those exact terms appear in the MLO.

Mission (resources-first)
1) Search for open-licensed or freely accessible materials related to the MLO.
2) Summarize key themes across credible sources.
3) Derive 2–4 provisional ELOs grounded in those sources.
4) Present 6–8 curated resources mapped to the ELOs.
5) Recommend one Student Reading (≤ 10 pages).

Rules for Behavior
- You are a librarian, not an instructor; never fulfill/teach the objective.
- Every factual claim must come from cited materials.
- Preferred domains: .gov, .edu, .org; IGOs/NGOs (e.g., UN, World Bank, OECD, WHO); university OER; open datasets (e.g., Data.gov, Our World in Data, World Bank Data, OECD Stats); reputable legacy media only if freely viewable.
- Prefer open-licensed or open access; accept freely accessible (no paywall) if reputable.
- Include at least one dataset/visual and one applied case when possible.
- Default recency: 2019+ unless canonical.
- IMPORTANT: Include **hyperlinks only in Section E** (Resource Table). In all other sections (A–D, F, G), refer to resources by title/domain only—no links.

URL Reliability Rules
- Provide specific resource URLs (not homepages) in Section E.
- Do not fabricate paths; if unknown, write “(no stable open URL; available via [Organization])”.
- Aim for 6–8 sources that are likely to resolve (no 404s).

Output Format
A. Acknowledgement & Search Plan — one short line (keywords + domains).
B. Resource Overview (Themes) — 2–4 bullets with concise source attributions (no links).
C. Provisional ELOs (derived from resources) — 2–4 measurable statements; include short anchor attributions (no links).
D. Executive Summary (Top Resources per ELO) — bullets mapping 2–3 items per ELO with 1-line rationales (no links).
F. Student Reading (≤ 10 pages) — pick one item from Section E by title and justify in 50–80 words (no links).

(Section E is built separately by the app.)
G. Optional Leads (paywalled or restricted) — provide 2–3 titles with domain names and value notes; if none exist, output exactly: “No suitable paywalled leads found; open sources cover the scope.”
"""

RETRY_USER_INSTRUCTION = """
Some links look invalid or generic. Replace any broken or generic links with valid, specific URLs to the cited resources.
Re-output ONLY the Resource Table (you may show it as a Markdown table) and the Optional Leads section.
Aim for a total of 6–8 working resource URLs.
"""

def scope_lock(mlo_text: str) -> dict:
    return {
        "role": "system",
        "content": (
            f"SCOPE LOCK: Work ONLY on this exact topic — {mlo_text}. "
            f"Do not drift to adjacent topics unless those exact terms appear in the MLO."
        ),
    }

# ----------------------------
# Helpers: model call & link verification
# ----------------------------
def get_client() -> OpenAI:
    api_key = (
        os.environ.get("OPENAI_OPENAI_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or st.secrets.get("OPENAI_API_KEY")
    )
    if not api_key:
        st.error("OPENAI_API_KEY not found. Add it in Streamlit Secrets.")
        st.stop()
    return OpenAI(api_key=api_key)

def call_model(messages: list[dict], temperature: float) -> str:
    client = get_client()
    resp = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=messages
    )
    return resp.choices[0].message.content

def extract_urls(markdown_text: str, cap: int = 50) -> list[str]:
    raw_urls = set()
    raw_urls.update(re.findall(r"https?://[^\s)>\]]+", markdown_text))
    raw_urls.update(re.findall(r"\((https?://[^)]+)\)", markdown_text))
    cleaned, seen = [], set()
    for u in raw_urls:
        u = u.strip().rstrip(".,);]")
        try:
            parsed = urlparse(u)
            if not parsed.scheme.startswith("http"):
                continue
        except Exception:
            continue
        if u not in seen:
            seen.add(u)
            cleaned.append(u)
        if len(cleaned) >= cap:
            break
    return cleaned

def check_url(url: str, head_timeout=6, get_timeout=8) -> tuple[bool, str]:
    try:
        r = requests.head(url, timeout=head_timeout, allow_redirects=True)
        code = r.status_code
        if code in (403, 405) or code >= 500:
            r2 = requests.get(url, timeout=get_timeout, allow_redirects=True, stream=True)
            code = r2.status_code
        if 200 <= code < 300:
            return True, f"{code}"
        if 300 <= code < 400:
            return True, f"{code} (redirect)"
        return False, f"{code}"
    except Exception as e:
        return False, f"error: {e.__class__.__name__}"

def build_metadata_json(verified_urls: list[str]) -> list[dict]:
    """Ask the model for metadata for each verified URL and return a parsed JSON list."""
    if not verified_urls:
        return []
    verified_list = "\n".join(f"- {u}" for u in verified_urls)
    messages = [
        {"role": "system", "content": "You output strict JSON only. No prose. No markdown. UTF-8."},
        {"role": "user", "content": f"""
Given these verified URLs, return a JSON array where each item has exactly:
"title" (string),
"type" (one of ["Report","Dataset","Web page","Policy brief","Video","Overview","Academic article"]),
"year" (integer or null),
"access" (one of ["Open access","Open-licensed","Freely accessible"]),
"why_aligns" (string, <= 2 sentences),
"use" (one of ["Core reading","Supplementary reading","Pre-read","Dataset exercise","Case anchor","Video primer"]),
"url" (string, MUST EXACTLY match one of the provided URLs).

Use official titles if recognizable; otherwise concise accurate titles. Do NOT invent URLs. Keep the list order similar to input.

Verified URLs:
{verified_list}
"""}]
    content = call_model(messages, temperature=0.1)
    content = content.strip().removeprefix("```json").removesuffix("```").strip()
    try:
        data = json.loads(content)
        allowed = set(verified_urls)
        data = [row for row in data if isinstance(row, dict) and row.get("url") in allowed]
        order = {u: i for i, u in enumerate(verified_urls)}
        data.sort(key=lambda r: order.get(r["url"], 1e9))
        return data
    except Exception:
        return []

def render_resource_table(rows: list[dict]) -> str:
    if not rows:
        return "_No verified resources were available._"
    header = "| Title | Type | Year | Access Type | Why it aligns | Suggested Use | URL |\n"
    header += "|-------|------|------|------------|---------------|---------------|-----|\n"
    lines = []
    for r in rows:
        title = r.get("title", "").replace("|", "｜")
        typ = r.get("type", "")
        year = r.get("year", "") or ""
        acc = r.get("access", "")
        why = r.get("why_aligns", "").replace("|", "｜")
        use = r.get("use", "")
        url = r.get("url", "")
        lines.append(f"| {title} | {typ} | {year} | {acc} | {why} | {use} | [Link]({url}) |")
    return header + "\n".join(lines)

# ----------------------------
# Run button (clean output by default; diagnostics optional)
# ----------------------------
run = st.button("Find resources", type="primary")

if run:
    if not mlo.strip():
        st.warning("Please paste the module objective first.")
        st.stop()

    if not allow_session_run():
        st.warning("You’ve hit the session limit (15 runs/hour). Please try again later.")
        st.stop()

    record_session_run()

    # 1) Initial draft (A–D ideas + candidate sources)
    with st.spinner("Generating draft and checking links…"):
        messages = [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            scope_lock(mlo),
            {"role": "user", "content": f"Module-level objective (MLO): {mlo}\nOptional constraints: {constraints or 'None'}"}
        ]
        draft = call_model(messages, temperature)

    if show_diag:
        with st.expander("Diagnostics: raw draft"):
            st.markdown(draft)

    # 2) Verify URLs; retry to reach >=6 valid links
    urls = extract_urls(draft, cap=60)
    results = [(u, *check_url(u)) for u in urls]
    good = [u for (u, ok, note) in results if ok]

    MAX_RETRIES = 2
    attempts_text = []
    content_for_context = draft

    attempt = 0
    while len(good) < 6 and attempt < MAX_RETRIES:
        attempt += 1
        retry_messages = [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            scope_lock(mlo),
            {"role": "assistant", "content": content_for_context},
            {"role": "user", "content": RETRY_USER_INSTRUCTION}
        ]
        retry_chunk = call_model(retry_messages, temperature=0.2)
        attempts_text.append((attempt, retry_chunk))
        content_for_context += "\n\n" + retry_chunk

        urls_new = extract_urls(retry_chunk, cap=40)
        results_new = [(u, *check_url(u)) for u in urls_new]
        for (u, ok, _) in results_new:
            if ok and u not in good:
                good.append(u)

    if show_diag:
        with st.expander("Diagnostics: attempts & verification"):
            for i, chunk in attempts_text:
                st.info(f"Attempt {i}: replaced broken/generic links.")
                st.markdown(chunk)

            all_urls = extract_urls(content_for_context, cap=120)
            final_results = [(u, *check_url(u)) for u in all_urls]
            good_final = [(u, note) for (u, ok, note) in final_results if ok]
            bad_final = [(u, note) for (u, ok, note) in final_results if not ok]

            st.markdown("### 🔍 Link Verification Report")
            st.caption("Checks whether each URL responds (HEAD with redirects, then GET fallback).")
            if good_final:
                st.success("Working:")
                for url, note in good_final:
                    st.write(f"✅ {note} — {url}")
            if bad_final:
                st.error("Broken or blocked:")
                for url, note in bad_final:
                    st.write(f"❌ {note} — {url}")

    # 3) Build Section E from verified URLs only (programmatic table)
    good_unique = list(dict.fromkeys(good))
    metadata_rows = build_metadata_json(good_unique)
    section_e_table = render_resource_table(metadata_rows)

    # 4) Generate clean A–D and F (no links), and G (titles only)
    clean_adf = call_model(
        [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            scope_lock(mlo),
            {"role": "assistant", "content": "We will show a verified Resource Table (Section E) separately. Do not include any hyperlinks outside Section E."},
            {"role": "user", "content": "Re-output sections A–D (concise) and F (Student Reading) only. In F, reference one item from Section E by title and provide a 50–80 word rationale. No hyperlinks."}
        ],
        temperature=0.2
    )

    clean_g = call_model(
        [
            {"role": "system", "content": BASE_SYSTEM_PROMPT},
            scope_lock(mlo),
            {"role": "assistant", "content": "We will show Section E separately. Provide Optional Leads as titles plus domain names, no links."},
            {"role": "user", "content": "Re-output ONLY section G (Optional Leads). If none are suitable, output exactly: 'No suitable paywalled leads found; open sources cover the scope.'"}
        ],
        temperature=0.2
    )

    # 5) Present final clean output
    st.markdown("## Final Output")
    st.markdown(clean_adf)

    st.markdown("### E. Resource Table (verified URLs only)")
    st.markdown(section_e_table)

    st.markdown(clean_g)

    if len(good_unique) < 6:
        st.warning(
            f"Only {len(good_unique)} verified links were available. "
            "Consider narrowing the topic, relaxing recency, or allowing reputable media sources."
        )
