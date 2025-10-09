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
# System prompt (Resources-first + Student Reading; table + URL rules)
# ----------------------------
BASE_SYSTEM_PROMPT = """
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
- Provide specific resource URLs (not just homepages). Prefer official report pages or direct open PDFs.
- Do not fabricate paths. If you are not certain of a specific URL, write: “(no stable open URL; available via [Organization Name] publications)”.
- Aim for at least 6–8 sources whose URLs are likely to resolve (no 404s).

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
G. Optional Leads (paywalled or restricted) — provide 2–3 if available, each with a one-line note on value; if none exist, write a single line: “No suitable paywalled leads found; open sources cover the scope.”
"""

# A helper instruction used only on retries:
RETRY_USER_INSTRUCTION = """
Some URLs above appear invalid or generic. Replace any broken or generic links with valid, specific URLs to the cited resources.
Re-output ONLY sections:
E. Resource Table
G. Optional Leads (paywalled or restricted)
Keep the exact table columns and the same Markdown formatting as before.
Aim for a total of 6–8 working resource URLs in section E.
"""

# ----------------------------
# Helpers: model call & link verification
# ----------------------------
def call_model(mlo_text: str, constraints_text: str, prior_content: str | None = None) -> str:
    """Call the model. If prior_content is provided, this is a retry asking for replacements."""
    api_key = os.environ.get("OPENAI_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY")
    if not api_key:
        st.error("OPENAI_API_KEY not found. Add it in Streamlit Secrets.")
        st.stop()
    client = OpenAI(api_key=api_key)

    messages = [{"role": "system", "content": BASE_SYSTEM_PROMPT}]
    if prior_content:
        messages.append({"role": "assistant", "content": prior_content})
        messages.append({"role": "user", "content": RETRY_USER_INSTRUCTION})
    else:
        user_msg = f"Module-level objective (MLO): {mlo_text}\nOptional constraints: {constraints_text or 'None'}"
        messages.append({"role": "user", "content": user_msg})

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
    """Return (ok, note), where ok=True means a likely good link."""
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

    record_session_run()

    # First attempt
    with st.spinner("Generating draft and checking links…"):
        content = call_model(mlo, constraints)
        st.markdown(content)

        urls = extract_urls(content, cap=50)
        results = [(u, *check_url(u)) for u in urls]

        good = [u for (u, ok, note) in results if ok]
        bad = [(u, note) for (u, ok, note) in results if not ok]

        # Retry loop: ask model to replace broken links and re-output the table/leads
        MAX_RETRIES = 2
        attempt = 0
        while len(good) < 6 and attempt < MAX_RETRIES:
            attempt += 1
            st.info(f"Attempt {attempt}: replacing broken/generic links and retrying sections E & G…")
            retry_content = call_model(mlo, constraints, prior_content=content)
            # Show what changed
            st.markdown(retry_content)

            # Merge sections by simple concatenation (display-wise it’s fine)
            content += "\n\n" + retry_content

            # Re-verify with the combined text
            urls = extract_urls(retry_content, cap=50)
            results = [(u, *check_url(u)) for u in urls]
            good.extend([u for (u, ok, note) in results if ok and u not in good])

        # Final verification report
        all_urls = extract_urls(content, cap=100)
        final_results = [(u, *check_url(u)) for u in all_urls]
        good_final = [(u, note) for (u, ok, note) in final_results if ok]
        bad_final = [(u, note) for (u, ok, note) in final_results if not ok]

    # ----------------------------
    # Link Verification Report
    # ----------------------------
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

    # Explain if still short on valid links
    valid_count = len({u for u, _ in good_final})
    if valid_count < 6:
        st.warning(
            f"Only {valid_count} verified links were found after retries. "
            "Consider narrowing the topic, relaxing recency, or allowing reputable media sources."
        )
