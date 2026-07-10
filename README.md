# Composio App Research Agent

Dual-pass async pipeline that researches 100 SaaS applications for AI agent toolkit buildability, executing registry cross-checks, web search extraction, and domain-authority-based automated adjudication.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Install headless browser binaries for scraping SPAs
playwright install chromium

# 3. Configure API keys (GROQ_API_KEY required, write to env or config.json)
$env:GROQ_API_KEY="your_groq_api_key"

# 4. Run the pipeline (all 100 apps, V2 prompt)
python pipeline.py --prompt-version 2

# 5. Run source conflicts audit
python check_source_conflicts.py

# 6. Generate the visual report
python build_page.py
```

### Single-App Demo (Live Run)
To run a single app live, bypassing cache and triggering live web scrapers and LLM extraction:
```bash
python pipeline.py --app "Stripe" --prompt-version 2
```

---

## Directory Map

```text
composio-research-agent/
├── pipeline.py                # Main async dual-pass research pipeline (Pass A + Pass B)
├── resolve_escalated.py       # LLM adjudication & domain authority resolution script
├── composio_check.py          # Registry match tool linking into the Composio SDK
├── schema.py                  # Pydantic schemas validating extracted fields & confidence
├── apps.json                  # Source dataset (100 apps with hints & categories)
├── results.json               # Final clean consolidated output (alias of latest version)
├── results_v1.json            # V1 Prompt run results snapshot
├── results_v2.json            # V2 Prompt run results snapshot
├── resolution_log.json        # Incremental audit trail logging every resolution step
├── verification.json          # Historical hand-verified stratified verification sample
├── check_source_conflicts.py   # Pre-submission source reliability audit script
├── build_page.py              # Visual dashboard generator (JSON → output.html)
├── requirements.txt           # Python dependency requirements
├── .gitignore                 # Configured to ignore credentials, caches, and tmp files
└── output.html                # High-fidelity dashboard report (deliverable)
```

---

## Core Architecture

- **Pass A (Web Discovery)**: Executes targeted DuckDuckGo searches, fetches primary text page content using Playwright, and extracts specifications via LLaMA-3.3-70B.
- **Pass B (Verification/Registry)**: Queries the Composio SDK Registry to establish a baseline. If missing, it fetches the official `docs_hint` directly.
- **Auto-Diff & Adjudication**: Automatically accepts values where both passes agree with high confidence. Disagreements trigger:
  - *Domain Authority Pre-check*: Symmetrically checks and auto-resolves fields if only one pass was sourced from the official domain (e.g. `docs.clay.com` vs `apitracker.io`).
  - *LLM Adjudication*: If unresolved, LLaMA-3.3-70B adjudicates using the fetched primary developer documentation.
- **Pre-Submission Conflict Audit**: `check_source_conflicts.py` verifies the resolved final database against root domains, exiting non-zero if any unofficial sources outvoted primary domains.



