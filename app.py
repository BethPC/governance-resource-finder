import os
import re
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
                time.sleep(0.4)
                st.rerun()
            else:
                st.error("Incorrect passcode")

if not st.session_state.authed:
    st.stop()

# ----------------------------
# Gentle per-session rate limit (15 runs per rolling hour)
# ----------------------------
WINDOW_SECONDS = 3600     # 1 hour rolling window
MAX_RUNS_PER_WINDOW = 15  # per browser session

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
        "- Keep the objective broad (module-level)."
        "\n- Add constraints if helpful (e.g., region, since 2020, dataset + brief)."
    )
    temperature = st.slider("Creativity (lower = stricter)", 0.0, 1.0, 0.2, 0.1)
    model = st.selectbox("Model", ["gpt-4o", "gpt-4o-mini"], index=0)

# ----------------------------
# Inputs
# ----------------------------
mlo = st.text_area(
    "Module-level objective (MLO)",
    height=140,
    placeholder="e.g., Examine how digital living infrastructure enables 'cultural experiences from home' in future housing."
)
constraints = st.text_area(
    "Optional constraints",
    placeholder="Region: Southeast Asia; Recency: since 2020; Media: dataset + policy briefs; Exclusions: blogs"
)

# Guard against accidental huge inputs
MAX_CHARS = 2000
if len(mlo or "") > MAX_CHARS or len(constraints or "") > MAX_CHARS:
    st.warning("Input is too long. Please shorten the objective and/or constraints.")
    st.stop()

# ----------------------------
# System prompt (Resources-first + Student Reading; Streamlit-optimized table + URL rules)
# ----------------------------
SYSTEM_PROMPT = """
You are Governance Resource Finder, an AI research assistant for instructional designers building courses on government, sustainability, smart cities, and digital living infrastructure. Your purpose is to find, vet, and summarize open or freely accessible learning resources—NOT to teach or explain the topic yourself.

Terminology
- Module-Level Learning Objective (MLO): a broad goal for a module.
- Elemental Learning Objectives (ELOs): 2–4 specific, measurable outcomes that break the MLO into smaller parts; start with action verbs (Identify/Analyze/Evaluate…).
- Student Reading: a short (≤ 10 pages) open or freely accessible article/brief for educated non-specialists that introduces the core concept.

Mission (resources-first)
1) Search for open-licensed or freely accessible materials related to the MLO.
2) Summarize key themes that emerge across credible sources.
3) Derive 2–4 provisional ELOs grounded in those sources.
4) Present 6–8 curated resources mapped to the ELOs.
5) Recommend one Student Reading (≤ 10 pages).

Rules for Behavior
- You are a librarian, not an instructor; never fulfill/teach the objective.
- Every factual claim must come from cited materials.
- Preferred domains (in order): .gov, .edu, .org; IGOs/NGOs (UN, World Bank, OECD, WHO, UN-Habitat); university OER portals (OpenStax, MIT OCW, Harvard, Stanford); open datasets (Data.gov, Our World in Data, World Bank Data, OECD Stats); reputable legacy media only if freely viewable (NYT/WaPo/Guardian/TOI/China Daily).
- Prefer open-licensed (CC BY/CC BY-NC/open access); accept freely accessible (no paywall) if reputable. Exclude paywalled, login-restricted, unreliable sources.
- Include at least one dataset/visual and one applied case when possible.
- Default recency: 2019+ unless canonical.

URL Reliability Rules:
- Only include links that are likely valid and currently accessible (avoid 404s and generic homepages).
- Link to the specific resource page when possible (not just the domain root).
- Prefer direct links to PDFs or official report pages when available.
- If you cannot provide a reliable specific link, write: “(no stable open URL; available via [Organization Name] publications)”.

Output Format (Streamlit-optimized)
A. Acknowledgement & Search Plan — one short line (keywords + domains).
B. Resource Overview (Themes) — 2–4 bullets with short inline citations.
C. Provisional ELOs (derived from resources) — 2–4 measurable statements; after each, include 1–2 anchor citations.
D. Executive Summary (Top Resources per ELO) — bullets mapping 2–3 items per ELO with 1-line rationales.
E. Resource Table — render the main list as a Markdown table using embedded links for URLs (Markdown `[title](url)` format). Use exactly these columns and keep each resource to a single row:

| Title | Type | Year | Access Type | Why it aligns | Suggested Use | URL |
|-------|------|------|------------|---------------|---------------|-----|
| Example: Ofcom – Online Nation | Regulator report | 2023 | Freely accessible | Independent data on home device usage and streaming patterns | Pre-read | [Link](https://www.ofcom.org.uk/research-and-data/media-literacy-research/online-nation) |

F. Student Reading (≤ 10 pages) — citation, URL (as Markdown link), access type, approx. length/page count, and a 50–80 word rationale explaining why it’s a clear, accessible introduction for non-specialists.
G. Optional Leads (if applicable) — up to three restricted/paywalled items, each paired with a free/open substitute.

Start by asking only for the MLO and any optional constraints (region, media types, recency window, exclusions). Then proceed.
"""

# ----------------------------
# Run button
# ----------------------------
run = st.button("Find resources", type="primary")

if run:
    if not mlo.strip():
        st.warning("Please paste the module objective first.")
        st.stop()

    # Per-session throttle
    if not allow_session_run():
        st.warning("You’ve hit the session limit (15 runs/hour). Please try again later.")
        st.stop()

    api_key = os.environ.get("OPENAI_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        st.error("OPENAI_API_KEY not found. Add it in Streamlit Secrets.")
        st.stop()

    client = OpenAI(api_key=api_key)
    user_msg = f"Module-level objective (MLO): {mlo}\nOptional constraints: {constraints or 'None'}"

    with st.spinner("Searching open/freely accessible sources and drafting outputs…"):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg}
                ]
            )
            content = resp.choices[0].message.content
        except Exception as e:
            st.error(f"Error from OpenAI: {e}")
            st.stop()

    # Count only successful runs
    record_session_run()

    # Render the response
    st.markdown(content)

    # ----------------------------
    # Link Verification Report (lightweight)
    # ----------------------------
    # Extract URLs from raw content: handles plain URLs and Markdown [text](url)
    raw_urls = set()
    # Plain URLs
    raw_urls.update(re.findall(r"https?://[^\s)>\]]+", content))
    # Markdown link targets
    raw_urls.update(re.findall(r"\((https?://[^)]+)\)", content))

    # Sanitize and dedupe (limit to a reasonable number to keep the app snappy)
    cleaned_urls = []
    seen = set()
    for u in raw_urls:
        # Trim trailing punctuation
        u = u.strip().rstrip(".,);]")
        # Skip obviously bad URLs
        try:
            parsed = urlparse(u)
            if not parsed.scheme.startswith("http"):
                continue
        except Exception:
            continue
        if u not in seen:
            seen.add(u)
            cleaned_urls.append(u)
        if len(cleaned_urls) >= 20:  # cap verification to 20 links per run
            break

    if cleaned_urls:
        st.markdown("### 🔍 Link Verification Report")
        st.caption("Checks whether each URL responds (HEAD with redirects, then GET fallback if needed).")
        good, warn, bad = [], [], []

        for url in cleaned_urls:
            status = None
            try:
                # Some sites block HEAD; allow redirects
                r = requests.head(url, timeout=6, allow_redirects=True)
                status = r.status_code
                # Fallback to GET if HEAD is not helpful (e.g., 405/403)
                if status in (403, 405) or status >= 500:
                    r2 = requests.get(url, timeout=8, allow_redirects=True, stream=True)
                    status = r2.status_code
            except Exception as e:
                status = f"error: {e.__class__.__name__}"

            if isinstance(status, int) and 200 <= status < 300:
                good.append((url, status))
            elif isinstance(status, int) and 300 <= status < 400:
                warn.append((url, f"{status} (redirect)"))
            elif isinstance(status, int):
                bad.append((url, status))
            else:
                bad.append((url, status))

        if good:
            st.success("Working:")
            for url, s in good:
                st.write(f"✅ {s} — {url}")
        if warn:
            st.warning("Redirects (likely OK, verify content):")
            for url, s in warn:
                st.write(f"⚠️ {s} — {url}")
        if bad:
            st.error("Broken or blocked:")
            for url, s in bad:
                st.write(f"❌ {s} — {url}")

        st.caption("Tip: If a link is broken, try the organization’s publications page and search the exact report title.")
