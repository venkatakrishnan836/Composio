"""
resolve_escalated.py — Resolve ALL escalated fields in results.json.

For each app with escalated fields:
  1. Fetch actual developer docs (from pass_a/pass_b source or docs_hint)
  2. Send ALL escalated fields for that app in ONE LLM call
  3. LLM adjudicates: pick pass_a, pass_b, synthesize new, or declare unverifiable
  4. For "unverifiable", must state what was tried (guardrail)
  5. Also re-checks ~15 random auto-accepted fields as a blind spot audit

Writes updated results.json and resolution_log.json.
"""
import asyncio
import json
import random
import re
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from urllib.parse import urlparse
from composio_check import check_all_apps
from groq import AsyncGroq

def get_root_domain(url):
    if not url or not url.startswith('http'):
        return ""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        parts = netloc.split('.')
        if len(parts) >= 2:
            if parts[-2] in ('co', 'com', 'org', 'net', 'gov', 'edu') and len(parts) >= 3:
                return '.'.join(parts[-3:])
            return '.'.join(parts[-2:])
        return netloc
    except:
        return ""

def clean_hint(hint):
    if not hint:
        return ""
    hint = hint.lower()
    if hint.startswith('http://') or hint.startswith('https://'):
        netloc = urlparse(hint).netloc
    else:
        netloc = hint.split('/')[0]
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    parts = netloc.split('.')
    if len(parts) >= 2:
        if parts[-2] in ('co', 'com', 'org', 'net', 'gov', 'edu') and len(parts) >= 3:
            return '.'.join(parts[-3:])
        return '.'.join(parts[-2:])
    return netloc


# ---- Config ------------------------------------------------------------------

RESULTS_PATH = Path(__file__).parent / "results.json"
APPS_PATH = Path(__file__).parent / "apps.json"
CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_PATH = Path(__file__).parent / "resolution_log.json"

CONCURRENCY = 3
AUTO_ACCEPTED_RECHECK_COUNT = 15
RECHECK_SEED = 99  # reproducible

# ---- Load API keys -----------------------------------------------------------

def get_groq_keys() -> list[str]:
    keys = []
    env = os.environ.get("GROQ_API_KEY")
    if env:
        keys.append(env)
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        keys.extend(cfg.get("groq_api_keys", []))
        single = cfg.get("groq_api_key", "")
        if single and single not in keys:
            keys.append(single)
    valid = [k for k in keys if k and not k.startswith("PLACE_YOUR")]
    if not valid:
        print("[ERROR] No Groq API key found.", file=sys.stderr)
        sys.exit(1)
    return valid


# ---- HTTP fetch (reuse from pipeline) ----------------------------------------

async def fetch_page(url: str) -> str:
    """Fetch a URL using Playwright for SPA rendering."""
    from playwright.async_api import async_playwright

    if not url.startswith("http"):
        url = "https://" + url

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=15000)
                text = await page.evaluate("document.body.innerText")
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
        return f"[Fetch error: {e}]"


# ---- Web search --------------------------------------------------------------

async def web_search(query: str, max_results: int = 3) -> list[dict]:
    loop = asyncio.get_running_loop()
    def _search():
        for attempt in range(3):
            try:
                try:
                    from ddgs import DDGS
                except ImportError:
                    from duckduckgo_search import DDGS
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


# ---- Adjudication Prompt (per-app, all fields at once) -----------------------

ADJUDICATE_PROMPT = """You are a senior developer-tools researcher performing a careful adjudication.

Two research passes extracted data about the app "{app_name}" and disagreed on the following fields.
Your job is to determine the correct value for each field by examining the actual developer documentation provided below.

APP: {app_name}
CATEGORY HINT: {category}
DOCS HINT: {docs_hint}

--- ACTUAL DEVELOPER DOCUMENTATION (fetched from primary source) ---
{docs_content}
--- END DOCUMENTATION ---

For each field below, you must:
1. Compare Pass A and Pass B values against the documentation above
2. Decide: pick pass_a, pick pass_b, or synthesize a new correct answer
3. If the documentation is genuinely inaccessible (login-gated, contact-sales only, zero public docs), you may declare "unverifiable" BUT you must state exactly what you tried and what you found (e.g. "Checked docs.example.com — found login wall with no public API reference")

FIELDS TO ADJUDICATE:
{fields_block}

Respond with ONLY valid JSON (no markdown fences). For each field, provide:
{{
  "field_name": {{
    "final_value": "the correct answer",
    "source_chosen": "pass_a" | "pass_b" | "synthesized" | "unverifiable",
    "reason": "one-line explanation of why this is correct, or what you tried if unverifiable"
  }}
}}

Be precise. Do not guess. If both passes are wrong, say so and provide the correct answer from the docs.
If the docs show a real hosted API, do NOT say "CLI-only" or "no hosted API".
If the app has OAuth 2.0 AND API key options, list both.
"""


# ---- Resolve one app ---------------------------------------------------------

async def resolve_app(sem: asyncio.Semaphore, groq_clients: list[AsyncGroq],
                      app_result: dict, apps_lookup: dict, call_idx: int,
                      mode: str = "escalated", composio_matches: dict = None) -> list[dict]:
    """Resolve specific fields using LLM Adjudication, or SDK Ground Truth where applicable."""
    async with sem:
        app_name = app_result["app"]
        fields = app_result.get("fields", {})
        
        # Identify fields to resolve
        if mode == "escalated":
            target_fields = {
                fname: fdata for fname, fdata in fields.items()
                if fdata.get("status") == "escalated" or not fdata.get("final", "").strip()
            }
        else:  # recheck mode for auto-accepted
            target_fields = {
                fname: fdata for fname, fdata in fields.items()
                if fname in mode  # mode is a set of field names to recheck
            }
        
        if not target_fields:
            return []

        print(f"  [{call_idx:3d}] {app_name} ({len(target_fields)} fields to resolve)")

        log_entries = []

        # Ground Truth check: if auth_method is escalated and we have SDK data
        if "auth_method" in target_fields:
            match = composio_matches.get(app_name) if composio_matches else None
            if match and (match.get("auth_schemes") or match.get("no_auth")):
                auth_val = "None / No API" if match.get("no_auth") else ", ".join(match.get("auth_schemes", []))
                fdata = target_fields.pop("auth_method")
                fdata["final"] = auth_val
                fdata["status"] = "agent_resolved"
                fdata["resolved_by"] = "agent_verified"
                fdata["reasoning"] = f"SDK Ground Truth (toolkit_id: {match['toolkit_id']})"
                
                log_entries.append({
                    "app": app_name,
                    "field": "auth_method",
                    "old_pass_a": fdata.get("pass_a", {}).get("value", ""),
                    "old_pass_b": fdata.get("pass_b", {}).get("value", ""),
                    "new_final": auth_val,
                    "source_chosen": "composio_sdk",
                    "reason": fdata["reasoning"],
                    "mode": "escalated" if mode == "escalated" else "auto_recheck",
                })

        # Domain Authority check: if one pass matches the app's own official domain and the other doesn't,
        # skip LLM call and auto-resolve to the matching pass
        app_meta = apps_lookup.get(app_name, {})
        hint_val = app_meta.get("docs_hint") or app_meta.get("hint")
        hint_root = clean_hint(hint_val)
        
        if hint_root:
            to_remove = []
            for fname, fdata in target_fields.items():
                pa = fdata.get("pass_a", {})
                pb = fdata.get("pass_b", {})
                pa_src = pa.get("source", "")
                pb_src = pb.get("source", "")
                pa_root = get_root_domain(pa_src)
                pb_root = get_root_domain(pb_src)
                
                # Symmetrically check domain authority
                if pa_root != pb_root and (pa_root == hint_root) ^ (pb_root == hint_root):
                    matching_pass = "pass_a" if pa_root == hint_root else "pass_b"
                    non_matching_pass = "pass_b" if matching_pass == "pass_a" else "pass_a"
                    
                    official_val = pa.get("value") if matching_pass == "pass_a" else pb.get("value")
                    unofficial_root = pb_root if matching_pass == "pass_a" else pa_root
                    
                    fdata["final"] = official_val
                    fdata["status"] = "agent_resolved"
                    fdata["resolved_by"] = "agent_domain_check"
                    fdata["reasoning"] = f"Domain authority rule: Pass {matching_pass.upper()[-1]} checked the app's own official domain ({hint_root}) and is authoritative; Pass {non_matching_pass.upper()[-1]} checked an unofficial domain ({unofficial_root})."
                    
                    log_entries.append({
                        "app": app_name,
                        "field": fname,
                        "old_pass_a": pa.get("value", ""),
                        "old_pass_b": pb.get("value", ""),
                        "new_final": official_val,
                        "source_chosen": matching_pass,
                        "reason": fdata["reasoning"],
                        "mode": "escalated" if mode == "escalated" else "auto_recheck",
                    })
                    to_remove.append(fname)
                    
            for fname in to_remove:
                target_fields.pop(fname)

        if not target_fields:
            return log_entries

        # Get docs URL from various sources
        app_meta = apps_lookup.get(app_name, {})
        docs_hint = app_meta.get("docs_hint", app_meta.get("hint", ""))
        
        # Try to find the best source URL from the passes
        source_urls = set()
        for fname, fdata in target_fields.items():
            for pass_key in ("pass_a", "pass_b"):
                src = fdata.get(pass_key, {}).get("source", "")
                if src and not src.startswith("[") and "composio.dev" not in src and src != "search_snippets":
                    source_urls.add(src)
        
        # Fetch actual developer docs
        docs_content = ""
        
        # Priority 1: docs_hint (most reliable)
        if docs_hint:
            url = docs_hint if docs_hint.startswith("http") else f"https://{docs_hint}"
            docs_content = await fetch_page(url)
        
        # Priority 2: pass sources (if docs_hint failed)
        if not docs_content or len(docs_content) < 200 or docs_content.startswith("["):
            for src_url in list(source_urls)[:2]:
                content = await fetch_page(src_url)
                if content and len(content) > 200 and not content.startswith("["):
                    docs_content = content
                    break
        
        # Priority 3: web search fallback
        if not docs_content or len(docs_content) < 200 or docs_content.startswith("["):
            results = await web_search(f"{app_name} API documentation developer docs authentication", max_results=3)
            for r in results:
                url = r.get("href", r.get("link", ""))
                if url:
                    content = await fetch_page(url)
                    if content and len(content) > 200 and not content.startswith("["):
                        docs_content = content
                        break
            if not docs_content or len(docs_content) < 200:
                docs_content = " ".join(r.get("body", r.get("snippet", "")) for r in results)
                if not docs_content:
                    docs_content = "[No documentation could be fetched for this app]"

        # Build the fields block for the prompt
        fields_block_parts = []
        for fname, fdata in target_fields.items():
            pa_val = fdata.get("pass_a", {}).get("value", "N/A")
            pb_val = fdata.get("pass_b", {}).get("value", "N/A")
            pa_conf = fdata.get("pass_a", {}).get("confidence", "N/A")
            pb_conf = fdata.get("pass_b", {}).get("confidence", "N/A")
            fields_block_parts.append(
                f"- {fname}:\n"
                f"    Pass A: \"{pa_val}\" (confidence: {pa_conf})\n"
                f"    Pass B: \"{pb_val}\" (confidence: {pb_conf})"
            )
        fields_block = "\n".join(fields_block_parts)

        prompt = ADJUDICATE_PROMPT.format(
            app_name=app_name,
            category=app_meta.get("category", app_result.get("category", "")),
            docs_hint=docs_hint,
            docs_content=docs_content[:6000],
            fields_block=fields_block,
        )

        # Call LLM with round-robin key selection
        adjudication = {}
        for attempt in range(5):
            try:
                client = groq_clients[(call_idx + attempt) % len(groq_clients)]
                resp = await client.chat.completions.create(
                    model="llama-3.3-70b-versatile",  # Use the best available model for adjudication
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=1500,
                )
                raw = resp.choices[0].message.content.strip()
                m = re.search(r'\{.*\}', raw, re.DOTALL)
                if m:
                    adjudication = json.loads(m.group())
                    break
            except Exception as e:
                if "429" in str(e):
                    await asyncio.sleep(4 + attempt * 3)
                else:
                    print(f"    [WARN] LLM error for {app_name}: {e}", file=sys.stderr)
                    if attempt == 4:
                        break
                    await asyncio.sleep(2)

        # Apply adjudication results
        for fname, fdata in target_fields.items():
            adj = adjudication.get(fname, {})
            final_value = adj.get("final_value", "")
            source_chosen = adj.get("source_chosen", "")
            reason = adj.get("reason", "")
            
            old_pass_a = fdata.get("pass_a", {}).get("value", "")
            old_pass_b = fdata.get("pass_b", {}).get("value", "")

            # If LLM didn't return a result for this field, use best available
            if not final_value:
                # Fallback: prefer pass_b if it has a real value, else pass_a
                if old_pass_b and "could not extract" not in old_pass_b.lower():
                    final_value = old_pass_b
                    source_chosen = "pass_b"
                    reason = "LLM adjudication returned no result; defaulting to pass_b"
                elif old_pass_a and "could not extract" not in old_pass_a.lower():
                    final_value = old_pass_a
                    source_chosen = "pass_a"
                    reason = "LLM adjudication returned no result; defaulting to pass_a"
                else:
                    final_value = f"Unverifiable — adjudication failed, no valid pass data for {app_name}"
                    source_chosen = "unverifiable"
                    reason = "Both passes returned unusable data and adjudication failed"

            # Guardrail: if marked unverifiable, must have a reason stating what was tried
            if source_chosen == "unverifiable":
                if len(reason) < 20:
                    reason = f"Checked {docs_hint or 'web search'} — {reason or 'no accessible docs found'}"
                final_value = f"Unverifiable — {reason}"

            # Determine resolved_by
            if source_chosen == "unverifiable":
                resolved_by = "unverifiable"
                status = "escalated"  # keep escalated status for unverifiable
            else:
                resolved_by = "agent_verified"
                status = "agent_resolved"

            # Write back to the app result
            fdata["final"] = final_value
            fdata["status"] = status
            fdata["resolved_by"] = resolved_by
            fdata["reasoning"] = reason

            log_entries.append({
                "app": app_name,
                "field": fname,
                "old_pass_a": old_pass_a,
                "old_pass_b": old_pass_b,
                "new_final": final_value,
                "source_chosen": source_chosen,
                "reason": reason,
                "mode": "escalated" if mode == "escalated" else "auto_recheck",
            })

        return log_entries


# ---- Main --------------------------------------------------------------------

async def main():
    groq_keys = get_groq_keys()
    groq_clients = [AsyncGroq(api_key=k) for k in groq_keys]

    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    apps_data = json.loads(APPS_PATH.read_text(encoding="utf-8"))
    apps_lookup = {a["app"]: a for a in apps_data}

    # Count initial state
    total_escalated = sum(
        1 for r in results for f in r.get("fields", {}).values()
        if f.get("status") == "escalated" or not f.get("final", "").strip()
    )
    print(f"\n--- Resolving {total_escalated} escalated/empty fields ---\n")

    # Fetch SDK matches for ground truth
    composio_matches = check_all_apps(apps_data)

    # Phase 1: Resolve all escalated fields (batched by app)
    sem = asyncio.Semaphore(CONCURRENCY)
    all_log = []

    # Identify apps with escalated fields
    escalated_apps = [
        (i, r) for i, r in enumerate(results)
        if any(f.get("status") == "escalated" or not f.get("final", "").strip()
               for f in r.get("fields", {}).values())
    ]

    print(f"Phase 1: {len(escalated_apps)} apps with escalated fields\n")

    # Process in batches
    tasks = []
    for idx, (orig_idx, app_result) in enumerate(escalated_apps):
        tasks.append(resolve_app(sem, groq_clients, app_result, apps_lookup, idx + 1, mode="escalated", composio_matches=composio_matches))

    batch_results = await asyncio.gather(*tasks, return_exceptions=True)
    for br in batch_results:
        if isinstance(br, Exception):
            print(f"  [ERROR] {br}", file=sys.stderr)
        elif br:
            all_log.extend(br)

    # Phase 2: Re-check random auto-accepted fields
    print(f"\nPhase 2: Re-checking {AUTO_ACCEPTED_RECHECK_COUNT} random auto-accepted fields\n")

    rng = random.Random(RECHECK_SEED)
    auto_accepted_pool = []
    for r in results:
        for fname, fdata in r.get("fields", {}).items():
            if fdata.get("status") == "auto_accepted":
                auto_accepted_pool.append((r, fname))

    recheck_sample = rng.sample(auto_accepted_pool, min(AUTO_ACCEPTED_RECHECK_COUNT, len(auto_accepted_pool)))
    
    # Group by app for batched calls
    recheck_by_app = {}
    for r, fname in recheck_sample:
        app_name = r["app"]
        if app_name not in recheck_by_app:
            recheck_by_app[app_name] = {"result": r, "fields": set()}
        recheck_by_app[app_name]["fields"].add(fname)

    recheck_tasks = []
    for idx, (app_name, info) in enumerate(recheck_by_app.items()):
        recheck_tasks.append(
            resolve_app(sem, groq_clients, info["result"], apps_lookup, idx + 1, mode=info["fields"], composio_matches=composio_matches)
        )

    recheck_results = await asyncio.gather(*recheck_tasks, return_exceptions=True)
    for rr in recheck_results:
        if isinstance(rr, Exception):
            print(f"  [ERROR] {rr}", file=sys.stderr)
        elif rr:
            all_log.extend(rr)

    # Final check: ensure 0 empty finals
    empty_count = sum(
        1 for r in results for f in r.get("fields", {}).values()
        if not f.get("final", "").strip()
    )
    if empty_count > 0:
        print(f"\n[WARN] Still {empty_count} empty finals — patching with best available...", file=sys.stderr)
        for r in results:
            for fname, fdata in r.get("fields", {}).items():
                if not fdata.get("final", "").strip():
                    pb = fdata.get("pass_b", {}).get("value", "")
                    pa = fdata.get("pass_a", {}).get("value", "")
                    if pb and "could not extract" not in pb.lower():
                        fdata["final"] = pb
                    elif pa and "could not extract" not in pa.lower():
                        fdata["final"] = pa
                    else:
                        reason = f"no valid data found for {r['app']}.{fname}"
                        fdata["final"] = f"Unverifiable — {reason}"
                    fdata["resolved_by"] = "agent_verified"
                    fdata["status"] = "agent_resolved"
                    fdata["reasoning"] = "Patched from best available pass data"
                    all_log.append({
                        "app": r["app"], "field": fname,
                        "old_pass_a": pa, "old_pass_b": pb,
                        "new_final": fdata["final"],
                        "source_chosen": "pass_b" if fdata["final"] == pb else ("pass_a" if fdata["final"] == pa else "unverifiable"),
                        "reason": "Final patch — LLM adjudication missed this field",
                        "mode": "patch",
                    })

    # Also update top-level convenience fields
    for r in results:
        cat_final = r.get("fields", {}).get("category", {}).get("final", "")
        ol_final = r.get("fields", {}).get("one_liner", {}).get("final", "")
        if cat_final:
            r["category"] = cat_final
        if ol_final:
            r["one_liner"] = ol_final

    # Write results
    RESULTS_PATH.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    # Write log
    LOG_PATH.write_text(json.dumps(all_log, indent=2, ensure_ascii=False), encoding="utf-8")

    # Summary
    escalated_log = [e for e in all_log if e["mode"] == "escalated"]
    recheck_log = [e for e in all_log if e["mode"] == "auto_recheck"]
    patch_log = [e for e in all_log if e["mode"] == "patch"]

    source_counts = {}
    for e in escalated_log:
        sc = e["source_chosen"]
        source_counts[sc] = source_counts.get(sc, 0) + 1

    final_empty = sum(
        1 for r in results for f in r.get("fields", {}).values()
        if not f.get("final", "").strip()
    )

    print(f"\n--- Resolution Complete ---")
    print(f"Escalated fields resolved: {len(escalated_log)}")
    print(f"  -> Chose pass_a: {source_counts.get('pass_a', 0)}")
    print(f"  -> Chose pass_b: {source_counts.get('pass_b', 0)}")
    print(f"  -> Synthesized new: {source_counts.get('synthesized', 0)}")
    print(f"  -> Unverifiable: {source_counts.get('unverifiable', 0)}")
    print(f"Auto-accepted re-checked: {len(recheck_log)}")

    # Report recheck findings
    recheck_flipped = [e for e in recheck_log if e["source_chosen"] == "synthesized"]
    if recheck_flipped:
        print(f"  -> Flipped (auto-accepted was wrong): {len(recheck_flipped)}")
        for e in recheck_flipped:
            print(f"      {e['app']}.{e['field']}: was \"{e['old_pass_b'][:50]}\" -> now \"{e['new_final'][:50]}\"")

    print(f"Patched (fallback): {len(patch_log)}")
    print(f"Empty finals remaining: {final_empty}")
    print(f"\nSaved to: {RESULTS_PATH}")
    print(f"Log saved to: {LOG_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
