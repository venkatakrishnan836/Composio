"""
pipeline.py -- pass A + pass B + auto-diff, async
Spec reference: composio-final-spec.md lines 11-36, 65-93, 117-123

Architecture (locked):
  Pass A: Web search (1-2 queries) + fetch top doc/pricing page + LLM extract
  Pass B: Composio registry (ground truth) OR direct docs hint fetch + LLM extract
  Auto-diff per field: agree+High -> auto-accept, else -> escalate
  Random audit: 6 rows from auto-accepted (fixed seed)
  results.json written incrementally

Usage:
  python pipeline.py                          # run all 100 apps
  python pipeline.py --app "Stripe"          # single-app demo trigger
  python pipeline.py --prompt-version 2      # re-run with fixed prompt (V2)
  python pipeline.py --show-escalated        # show escalated rows for human review
  python pipeline.py --audit                 # show 6-row audit sample
"""
import asyncio
import json
import random
import re
import sys
import os
import argparse
from datetime import datetime, timezone
from pathlib import Path

import httpx
from groq import AsyncGroq

from composio_check import check_all_apps
from schema import FIELD_NAMES

# ---- Config ----------------------------------------------------------------

APPS_PATH = Path(__file__).parent / "apps.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
RESULTS_PATH = Path(__file__).parent / "results.json"

CONCURRENCY = 3        # Number of apps to research in parallel (reduced for rate limits)
AUDIT_SEED = 42          # fixed seed for reproducible audit
AUDIT_N = 6              # spec line 72: exactly 6

# ---- Load API keys ----------------------------------------------------------

def get_groq_keys() -> list[str]:
    """Read Groq keys from config.json (groq_api_keys list or groq_api_key string) or GROQ_API_KEY env var."""
    keys = []
    env = os.environ.get("GROQ_API_KEY")
    if env:
        keys.append(env)
    
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        if isinstance(cfg.get("groq_api_keys"), list):
            keys.extend(cfg["groq_api_keys"])
        if isinstance(cfg.get("groq_api_key"), str) and cfg["groq_api_key"] not in keys:
            keys.append(cfg["groq_api_key"])
            
    # Filter out placeholders
    valid_keys = [k for k in keys if k and not k.startswith("PLACE_YOUR_SECOND")]
    if not valid_keys:
        print("[ERROR] No valid Groq API key found in config.json or environment.", file=sys.stderr)
        sys.exit(1)
    return valid_keys


def get_composio_key() -> str | None:
    """Read Composio API key from config.json or COMPOSIO_API_KEY env var."""
    env = os.environ.get("COMPOSIO_API_KEY")
    if env:
        return env
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        key = cfg.get("composio_api_key", "")
        if key:
            return key
    return None


# ---- Extraction Prompts -----------------------------------------------------
# V1 = initial prompt.  V2 = after fixing systematic errors (spec line 33-36).

EXTRACT_PROMPT_V1 = """You are a developer-tools researcher. Given the web content below from an app's documentation or website, extract these 7 fields as JSON.

For each field, provide "value" (your answer) and "confidence" ("High", "Medium", or "Low").

Fields:
1. category: The app's primary category (e.g. CRM, Support, Messaging, etc.)
2. one_liner: What the app does, in one sentence.
3. auth_method: Authentication method(s) the API uses. Be specific: OAuth 2.0, API key, Basic auth, Bearer token, Bot token, HMAC, JWT, None, etc.
4. self_serve_or_gated: Can a developer get API credentials themselves for free or on a trial? Or does it need a paid plan, admin approval, partnership, or contact-sales? Answer "Self-serve (reason)" or "Gated (reason)".
5. api_surface: Documented public API type (REST, GraphQL, SOAP, none), rough breadth (narrow/medium/broad), and whether an MCP server exists.
6. buildability_verdict: Could this realistically be built into an AI agent toolkit today? "Yes - buildable" or "No - blocked by [specific reason]".
7. evidence_url: The single most relevant documentation URL.

App name: {app_name}
Category hint: {category_hint}
Website hint: {website_hint}

--- WEB CONTENT ---
{content}
--- END CONTENT ---

Respond with ONLY valid JSON, no markdown fences, no explanation:
{{"category": {{"value": "...", "confidence": "..."}}, "one_liner": {{"value": "...", "confidence": "..."}}, "auth_method": {{"value": "...", "confidence": "..."}}, "self_serve_or_gated": {{"value": "...", "confidence": "..."}}, "api_surface": {{"value": "...", "confidence": "..."}}, "buildability_verdict": {{"value": "...", "confidence": "..."}}, "evidence_url": {{"value": "...", "confidence": "..."}}}}"""


EXTRACT_PROMPT_V2 = """You are a developer-tools researcher. Given the web content below, extract 7 fields as JSON.

CRITICAL CORRECTION RULES (apply BEFORE extraction):
1. If the app is a GitHub repository / open-source CLI tool with NO hosted API endpoint:
   - auth_method = "None - local CLI/library, no hosted auth"
   - api_surface must say "No hosted API" not "REST API"
   - buildability_verdict = "No - CLI-only, no hosted API to call"
2. If the app is a consumer product with NO developer API documentation section:
   - auth_method = "None - no public developer API"
   - buildability_verdict = "No - no public API"
3. If the app offers BOTH OAuth 2.0 AND API key, list both.
4. If credentials require a paid subscription: self_serve_or_gated = "Gated (paid plan required)"
5. If credentials require partnership or sales contact: self_serve_or_gated = "Gated (enterprise/sales)"
6. Only say "Has MCP" if you see EXPLICIT MCP server documentation in the content.
7. For buildability: consider BOTH API availability AND credential obtainability.

For each field, provide "value" and "confidence" ("High", "Medium", or "Low").

Fields:
1. category: Primary category
2. one_liner: What it does, one sentence
3. auth_method: Exact auth type(s)
4. self_serve_or_gated: Developer access model with reason
5. api_surface: API type, breadth, MCP status
6. buildability_verdict: "Yes - buildable" or "No - blocked by [reason]"
7. evidence_url: Primary docs URL

App name: {app_name}
Category hint: {category_hint}
Website hint: {website_hint}

--- WEB CONTENT ---
{content}
--- END CONTENT ---

Respond with ONLY valid JSON, no markdown fences:
{{"category": {{"value": "...", "confidence": "..."}}, "one_liner": {{"value": "...", "confidence": "..."}}, "auth_method": {{"value": "...", "confidence": "..."}}, "self_serve_or_gated": {{"value": "...", "confidence": "..."}}, "api_surface": {{"value": "...", "confidence": "..."}}, "buildability_verdict": {{"value": "...", "confidence": "..."}}, "evidence_url": {{"value": "...", "confidence": "..."}}}}"""


PROMPTS = {1: EXTRACT_PROMPT_V1, 2: EXTRACT_PROMPT_V2}

# ---- Web search (Pass A) ----------------------------------------------------
# Spec line 14: "Web search (1-2 queries) + fetch the top doc/pricing page"

async def web_search(query: str, max_results: int = 3) -> list[dict]:
    """Search using DuckDuckGo (free, no API key needed)."""
    loop = asyncio.get_running_loop()
    def _search():
        for attempt in range(3):
            try:
                try:
                    from ddgs import DDGS  # new package name
                except ImportError:
                    from duckduckgo_search import DDGS  # old package name fallback
                with DDGS() as ddgs:
                    return list(ddgs.text(query, max_results=max_results))
            except Exception as e:
                if attempt == 2:
                    print(f"    [WARN] Search failed for '{query}': {e}", file=sys.stderr)
                    return []
                import time
                time.sleep(2 ** attempt)
        return []
    return await loop.run_in_executor(None, _search)


# ---- HTTP fetch --------------------------------------------------------------

async def fetch_page(url: str) -> str:
    """Fetch a URL and return cleaned text content using Playwright for SPAs."""
    from playwright.async_api import async_playwright
    
    if not url.startswith("http"):
        url = "https://" + url
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                # wait_until="networkidle" ensures client-side JS SPAs render
                await page.goto(url, wait_until="networkidle", timeout=15000)
                text = await page.evaluate("document.body.innerText")
                # Fallback to string processing if evaluate is empty
                if not text or len(text) < 50:
                    text = await page.content()
                    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL)
                    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL)
                    text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                return text[:8000]
            finally:
                await browser.close()
    except Exception as e:
        return f"[Browser Fetch error: {e}]"


# ---- LLM extraction ---------------------------------------------------------

async def llm_extract(groq_clients: list[AsyncGroq], content: str, app: dict,
                       prompt_template: str) -> dict:
    """Call Groq LLM to extract 7 structured fields from page content."""
    prompt = prompt_template.format(
        content=content[:6000],
        app_name=app["app"],
        category_hint=app["category"],
        website_hint=app.get("hint", ""),
    )
    for attempt in range(5):
        try:
            app_id = app.get("id", random.randint(0, 100))
            current_client = groq_clients[(app_id + attempt) % len(groq_clients)]
            resp = await current_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=800,
            )
            raw = resp.choices[0].message.content.strip()
            # Extract JSON from response
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                return json.loads(m.group())
        except Exception as e:
            if attempt == 4:
                print(f"    [WARN] LLM error for {app['app']} after 5 attempts: {e}", file=sys.stderr)
            elif "429" in str(e):
                # Rate limit — backoff
                await asyncio.sleep(4 + attempt * 2)
            else:
                print(f"    [WARN] LLM error for {app['app']}: {e}", file=sys.stderr)
                break
    return {}


# ---- Pass A: Generic path ---------------------------------------------------
# Spec line 13-15:
#   "Web search (1-2 queries) + fetch the top doc/pricing page"
#   "LLM extracts structured fields, each with a confidence label"

async def run_pass_a(sem: asyncio.Semaphore, client: httpx.AsyncClient, groq_clients: list[AsyncGroq],
                     app: dict, prompt_template: str) -> dict:
    """Pass A: web search -> fetch top result -> LLM extract 7 fields."""
    app_name = app["app"]
    query = f"{app_name} API documentation authentication developer docs"

    # 1. Web search
    results = await web_search(query, max_results=3)

    # 2. Fetch the top doc/pricing page
    content = ""
    source_url = ""
    for r in results:
        url = r.get("href", r.get("link", ""))
        if url:
            source_url = url
            content = await fetch_page(url)
            if len(content) > 200 and not content.startswith("["):
                break  # got good content

    if not content or len(content) < 100:
        # Fallback: search snippet text
        content = " ".join(r.get("body", r.get("snippet", "")) for r in results)
        source_url = "search_snippets"

    # 3. LLM extract
    extracted = await llm_extract(groq_clients, content, app, prompt_template)

    # Build per-field results
    result = {}
    for field in FIELD_NAMES:
        raw = extracted.get(field, {})
        if isinstance(raw, dict):
            val = raw.get("value", "Could not extract")
            conf = raw.get("confidence", "Low")
        else:
            val = str(raw) if raw else "Could not extract"
            conf = "Low"
        # Validate confidence
        if conf not in ("High", "Medium", "Low"):
            conf = "Medium"
        result[field] = {"value": val, "confidence": conf, "source": source_url}

    return result


# ---- Pass B: Structurally different path -------------------------------------
# Spec line 17-21:
#   "If the app is in Composio's own toolkit registry -> use that as ground truth"
#   "If not -> fetch the app's primary docs domain from the hint column, directly.
#    Skip search, go straight to source."

async def run_pass_b(sem: asyncio.Semaphore, client: httpx.AsyncClient, groq_clients: list[AsyncGroq],
                     app: dict, composio_matches: dict, prompt_template: str) -> dict:
    """Pass B: Composio registry ground truth OR direct docs fetch."""
    app_name = app["app"]
    composio_match = composio_matches.get(app_name)

    if composio_match:
        # Composio registry = ground truth. Use the actual SDK fields directly.
        toolkit_id = composio_match.get("toolkit_id", "")
        desc = composio_match.get("description", "")
        auth_schemes = composio_match.get("auth_schemes", [])
        no_auth = composio_match.get("no_auth", False)

        # Build the verified auth string from SDK data — this is ground truth, not a guess
        if no_auth:
            verified_auth = "None — no authentication required (confirmed by Composio registry no_auth=true)"
        elif auth_schemes:
            verified_auth = " + ".join(auth_schemes)
        else:
            verified_auth = "Unknown (not specified in registry)"

        # Also fetch the composio.dev app page for the other fields (category, one_liner, etc.)
        composio_url = f"https://composio.dev/apps/{toolkit_id}"
        fetched = await fetch_page(composio_url)

        # Inject VERIFIED auth as ground truth fact — LLM must not override it
        content = (
            f"[COMPOSIO REGISTRY — VERIFIED FACTS]\n"
            f"App: {app_name} (toolkit_id: {toolkit_id})\n"
            f"VERIFIED auth_method: {verified_auth}\n"
            f"VERIFIED buildability: Yes - buildable (confirmed in Composio registry)\n"
            f"Description: {desc}\n\n"
            f"[PAGE CONTENT FROM {composio_url} — use for category, one_liner, api_surface, evidence_url]\n"
            f"{fetched}"
        )
        source = composio_url
        # Do NOT force all fields to High — let the LLM's actual confidence stand,
        # except auth_method and buildability_verdict which are directly from registry
        composio_verified_fields = {"auth_method", "buildability_verdict"}
    else:
        # Direct docs fetch from hint column — skip search, go straight to source
        hint_url = app.get("docs_hint", app.get("hint", ""))
        content = await fetch_page(hint_url)
        source = f"https://{hint_url}" if not hint_url.startswith("http") else hint_url
        composio_verified_fields = set()

    # LLM extract
    extracted = await llm_extract(groq_clients, content, app, prompt_template)

    result = {}
    for field in FIELD_NAMES:
        raw = extracted.get(field, {})
        if isinstance(raw, dict):
            val = raw.get("value", "Could not extract")
            conf = raw.get("confidence", "Low")
        else:
            val = str(raw) if raw else "Could not extract"
            conf = "Low"
        if conf not in ("High", "Medium", "Low"):
            conf = "Medium"

        result[field] = {"value": val, "confidence": conf, "source": source}

    # Override auth_method with the verified SDK value directly (don't trust the LLM for this)
    if composio_match and "auth_method" in result:
        if no_auth:
            auth_val = "None — no authentication required"
        elif auth_schemes:
            auth_val = " + ".join(auth_schemes)
        else:
            auth_val = result["auth_method"]["value"]  # keep LLM value if no SDK data
        result["auth_method"] = {"value": auth_val, "confidence": "High", "source": f"composio_sdk:{toolkit_id}"}

    return result


# ---- Auto-diff ---------------------------------------------------------------
# Spec line 23-25:
#   "Agree + both High -> auto-accept, zero human time."
#   "Disagree, or either side Medium/Low -> escalate to human review."

def values_agree(va: str, vb: str) -> bool:
    """Check if two field values are in agreement (fuzzy)."""
    a = va.lower().strip()
    b = vb.lower().strip()
    if a == b:
        return True
    # Containment check (one is a more specific version of the other)
    if len(a) > 5 and len(b) > 5 and (a in b or b in a):
        return True
    # Key-phrase overlap
    a_words = set(a.split())
    b_words = set(b.split())
    if len(a_words) > 2 and len(b_words) > 2:
        overlap = a_words & b_words
        union = a_words | b_words
        if len(overlap) / len(union) > 0.4:
            return True
    return False


def auto_diff(pa_result: dict, pb_result: dict) -> dict:
    """Per-field diff. Returns dict of FieldResult-shaped dicts."""
    fields = {}
    for field in FIELD_NAMES:
        pa = pa_result.get(field, {"value": "", "confidence": "Low", "source": ""})
        pb = pb_result.get(field, {"value": "", "confidence": "Low", "source": ""})

        agree = values_agree(pa["value"], pb["value"])
        both_high = pa["confidence"] == "High" and pb["confidence"] == "High"

        if agree and both_high:
            status = "auto_accepted"
            resolved_by = "auto"
            final = pb["value"]  # prefer pass B (more specific)
        else:
            status = "escalated"
            resolved_by = "human"
            final = ""  # to be filled by human review

        fields[field] = {
            "pass_a": pa,
            "pass_b": pb,
            "agree": agree,
            "status": status,
            "final": final,
            "reasoning": "Auto-accepted (both High confidence + match)" if status == "auto_accepted" else "",
            "resolved_by": resolved_by,
        }
    return fields



# ---- Research one app --------------------------------------------------------

async def research_app(sem: asyncio.Semaphore, client: httpx.AsyncClient,
                        groq_clients: list[AsyncGroq], app: dict,
                        composio_matches: dict, prompt_version: int = 1) -> dict:
    """Full dual-pass pipeline for a single app."""
    async with sem:
        app_name = app["app"]
        print(f"  [{app['id']:3d}] {app_name}")

        composio_match = composio_matches.get(app_name)
        prompt_template = PROMPTS[prompt_version]

        # Run both passes concurrently (spec: asyncio.gather)
        pa_result, pb_result = await asyncio.gather(
            run_pass_a(sem, client, groq_clients, app, prompt_template),
            run_pass_b(sem, client, groq_clients, app, composio_matches, prompt_template),
        )

        # Auto-diff per field
        fields = auto_diff(pa_result, pb_result)



        # Derive convenience top-level fields from finals
        cat_field = fields.get("category", {})
        ol_field = fields.get("one_liner", {})

        from schema import AppResult
        result_dict = {
            "app": app_name,
            "category": cat_field.get("final") or app["category"],
            "one_liner": ol_field.get("final", ""),
            "composio_toolkit_match": composio_match.get("toolkit_id") if composio_match else None,
            "fields": fields,
        }
        # Validate against schema (this will raise ValidationError if we broke the spec)
        validated = AppResult.model_validate(result_dict)
        
        # Add metadata not in schema
        out = validated.model_dump()
        out["id"] = app["id"]
        out["researched_at"] = datetime.now(timezone.utc).isoformat()
        out["prompt_version"] = prompt_version
        return out


# ---- Write results incrementally --------------------------------------------
# Spec line 121: "Single JSON ... written incrementally"

def results_path_for_version(version: int) -> Path:
    """Return the version-specific results file path."""
    return Path(__file__).parent / f"results_v{version}.json"

def save_results(results: dict[str, dict], prompt_version: int):
    """Write results to version-specific file AND results.json (latest alias)."""
    data = list(results.values())
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    # Version-specific snapshot (never overwritten by other version)
    results_path_for_version(prompt_version).write_text(payload, encoding="utf-8")
    # Latest alias for backward compat (show-escalated, audit, build_page)
    RESULTS_PATH.write_text(payload, encoding="utf-8")


# ---- Main pipeline -----------------------------------------------------------

async def run_pipeline(app_filter: str | None = None, prompt_version: int = 1, no_cache: bool = False):
    """Run the dual-pass pipeline for all 100 apps (or one app for demo)."""
    groq_keys = get_groq_keys()
    apps = json.loads(APPS_PATH.read_text())

    if app_filter:
        apps = [a for a in apps if a["app"].lower() == app_filter.lower()]
        if not apps:
            print(f"App '{app_filter}' not found in apps.json.")
            return
        print(f"Single-app demo: {apps[0]['app']}")
    else:
        print(f"Running full pipeline: {len(apps)} apps, prompt V{prompt_version}")

    # Step 1: Composio registry cross-check (spec line 19-20, 82)
    print("\n--- Composio Registry Cross-Check ---")
    composio_key = get_composio_key()
    composio_matches = check_all_apps(apps, api_key=composio_key)

    # Step 2: Load existing results from VERSION-SPECIFIC file only
    # This prevents a V2 run from dropping V1 data (or vice versa)
    existing: dict[str, dict] = {}
    version_path = results_path_for_version(prompt_version)
    if version_path.exists():
        try:
            for r in json.loads(version_path.read_text(encoding="utf-8")):
                existing[r["app"]] = r
        except Exception:
            pass
    elif app_filter and RESULTS_PATH.exists():
        # Demo mode: fall back to results.json for caching single-app runs
        try:
            for r in json.loads(RESULTS_PATH.read_text(encoding="utf-8")):
                if r.get("prompt_version") == prompt_version:
                    existing[r["app"]] = r
        except Exception:
            pass

    # Step 3: Run dual-pass for each app
    print(f"\n--- Pass A + Pass B (concurrency={CONCURRENCY}) ---")
    sem = asyncio.Semaphore(CONCURRENCY)
    groq_clients = [AsyncGroq(api_key=k) for k in groq_keys]

    async with httpx.AsyncClient(timeout=20) as client:
        tasks = []
        for app in apps:
            if app["app"] in existing and not no_cache and not app_filter:
                print(f"  [{app['id']:3d}] {app['app']} (cached)")
            else:
                tasks.append(
                    research_app(sem, client, groq_clients, app, composio_matches, prompt_version)
                )

        if tasks:
            results_list = await asyncio.gather(*tasks, return_exceptions=True)

            for r in results_list:
                if isinstance(r, Exception):
                    print(f"  [ERROR] {r}", file=sys.stderr)
                elif isinstance(r, dict):
                    existing[r["app"]] = r
                    # Write incrementally (spec line 121)
                    save_results(existing, prompt_version)

    # Final save
    save_results(existing, prompt_version)

    # ---- Stats ----
    if app_filter:
        all_results = [r for r in list(existing.values()) if r["app"].lower() == app_filter.lower()]
    else:
        all_results = list(existing.values())
    total_fields = sum(len(r.get("fields", {})) for r in all_results)
    auto = sum(
        1 for r in all_results
        for f in r.get("fields", {}).values()
        if isinstance(f, dict) and f.get("status") == "auto_accepted"
    )
    esc = sum(
        1 for r in all_results
        for f in r.get("fields", {}).values()
        if isinstance(f, dict) and f.get("status") == "escalated"
    )

    print(f"\n--- Results ---")
    print(f"Apps researched: {len(all_results)}")
    print(f"Total field-checks: {total_fields}")
    print(f"Auto-accepted: {auto}")
    print(f"Escalated: {esc}")
    print(f"Prompt version: V{prompt_version}")
    print(f"Saved to: {results_path_for_version(prompt_version)} (and {RESULTS_PATH})")

    # ---- 6-row random audit (spec line 27-28) ----
    if not app_filter:
        draw_audit_sample(all_results)


def draw_audit_sample(results: list[dict]):
    """Draw exactly 6 rows from auto-accepted bucket (spec line 27-28, 72)."""
    # An app is "fully auto-accepted" if ALL its fields are auto_accepted
    full_auto = [
        r for r in results
        if all(
            isinstance(f, dict) and f.get("status") == "auto_accepted"
            for f in r.get("fields", {}).values()
        )
    ]
    n = min(AUDIT_N, len(full_auto))
    if n > 0:
        random.seed(AUDIT_SEED)
        sample = random.sample(full_auto, n)
        print(f"\n--- 6-Row Audit Sample (seed={AUDIT_SEED}) ---")
        for s in sample:
            print(f"  - {s['app']}")
        print("These apps need manual verification against real docs.")
    else:
        print("\n[INFO] No fully auto-accepted apps yet. Audit sample cannot be drawn.")


def show_escalated():
    """Show all escalated fields for human review (spec line 30-31)."""
    if not RESULTS_PATH.exists():
        print("No results.json found. Run the pipeline first.")
        return

    results = json.loads(RESULTS_PATH.read_text())
    print("\n--- Escalated Fields (need human review) ---\n")

    count = 0
    for r in results:
        for field_name, field_data in r.get("fields", {}).items():
            if isinstance(field_data, dict) and field_data.get("status") == "escalated":
                count += 1
                pa = field_data.get("pass_a", {})
                pb = field_data.get("pass_b", {})
                print(f"[{count}] {r['app']} / {field_name}")
                print(f"    Pass A: {pa.get('value', '?')} (conf: {pa.get('confidence', '?')}, src: {pa.get('source', '?')})")
                print(f"    Pass B: {pb.get('value', '?')} (conf: {pb.get('confidence', '?')}, src: {pb.get('source', '?')})")
                fin = field_data.get('final', '')
                rsn = field_data.get('reasoning', '')
                if fin or rsn:
                    print(f"    Final: {fin}")
                    print(f"    Reasoning: {rsn}")
                else:
                    print(f"    Status: NEEDS HUMAN REVIEW")
                print()

    print(f"Total escalated field-checks: {count}")
    print("\nTo resolve manually: edit results.json, set 'final', 'reasoning' (why you chose it), and 'resolved_by' for each.")
    print("Or run 'python resolve_escalated.py' to use Groq to resolve them automatically.")



# ---- CLI ---------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Composio App Research Pipeline (spec: composio-final-spec.md)"
    )
    parser.add_argument("--app", help="Run for a single app (live demo trigger)")
    parser.add_argument("--prompt-version", type=int, default=1, choices=[1, 2],
                        help="Extraction prompt version (1=initial, 2=fixed)")
    parser.add_argument("--show-escalated", action="store_true",
                        help="Show escalated rows for human review")

    parser.add_argument("--audit", action="store_true",
                        help="Show the 6-row audit sample")
    parser.add_argument("--no-cache", action="store_true",
                        help="Bypass caching and force research run")
    args = parser.parse_args()

    if args.show_escalated:
        show_escalated()
        return


    if args.audit:
        if RESULTS_PATH.exists():
            results = json.loads(RESULTS_PATH.read_text())
            draw_audit_sample(results)
        else:
            print("No results.json found.")
        return

    asyncio.run(run_pipeline(app_filter=args.app, prompt_version=args.prompt_version, no_cache=args.no_cache))


if __name__ == "__main__":
    main()
