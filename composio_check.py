"""
composio_check.py -- Composio toolkit registry cross-check
Spec reference: composio-final-spec.md lines 17-20, 82, 120

Uses the `composio` SDK (not deprecated composio-core).
API: Composio(api_key=...).toolkits.list() -> ToolkitListResponse
     resp.items -> list of toolkit objects
     each toolkit: .slug, .name, .auth_schemes, .no_auth, .meta

Fetches ALL pages and builds a lookup dict:
  {app_name_lower -> {toolkit_id, auth_schemes, description, no_auth}}

Purpose: Pass B uses this as "ground truth" for apps in the Composio registry.
"""
import json
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


def get_composio_key() -> str | None:
    """Read Composio API key from config.json or env."""
    import os
    env = os.environ.get("COMPOSIO_API_KEY")
    if env:
        return env
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text())
        key = cfg.get("composio_api_key", "")
        if key:
            return key
    return None


def _build_registry(api_key: str) -> dict[str, dict]:
    """
    Fetch all toolkits from Composio registry via v3 HTTP API and build a normalised lookup.
    Uses direct HTTP because the installed SDK (0.17.x) talks to the deprecated v1 API (410 Gone).
    Returns: {normalised_name -> {toolkit_id, auth_schemes, description, no_auth}}
    """
    import httpx

    base_url = "https://backend.composio.dev/api/v3/toolkits"
    headers = {"X-API-Key": api_key}
    registry: dict[str, dict] = {}

    cursor = None
    page = 0
    while True:
        page += 1
        params: dict = {"limit": 100}
        if cursor:
            params["cursor"] = cursor

        resp = httpx.get(base_url, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            print(f"\n[CRITICAL WARNING] Composio API fetch failed with status {resp.status_code}: {resp.text}", file=sys.stderr)
            print("[CRITICAL WARNING] Pass B registry cross-check will be skipped!\n", file=sys.stderr)
            break
            
        try:
            data = resp.json()
        except ValueError:
            print(f"\n[CRITICAL WARNING] Composio API returned non-JSON response: {resp.text}", file=sys.stderr)
            print("[CRITICAL WARNING] Pass B registry cross-check will be skipped!\n", file=sys.stderr)
            break

        if not isinstance(data, dict):
            print(f"\n[CRITICAL WARNING] Composio API returned unexpected shape (not a dict): {type(data)}", file=sys.stderr)
            print("[CRITICAL WARNING] Pass B registry cross-check will be skipped!\n", file=sys.stderr)
            break

        items = data.get("items", [])
        if not items:
            break

        for t in items:
            if not isinstance(t, dict):
                continue
            slug = t.get("slug", "") or ""
            name = t.get("name", "") or slug
            no_auth = t.get("no_auth", False) or False
            meta = t.get("meta", {}) or {}
            description = meta.get("description", "") or ""

            auth_schemes = t.get("auth_schemes", []) or []

            entry = {
                "toolkit_id": slug,
                "name": name,
                "auth_schemes": auth_schemes,
                "description": description,
                "no_auth": no_auth,
            }

            # Index by multiple keys for fuzzy matching
            for key in [name.lower(), slug.lower(), name.lower().replace(" ", ""), slug.lower().replace("_", "")]:
                if key:
                    registry[key] = entry

        # Pagination
        cursor = data.get("next_cursor")
        if not cursor:
            break

    return registry


def _fuzzy_match(app_name: str, registry: dict) -> dict | None:
    """Try multiple name normalizations to find an app in the registry."""
    candidates = [
        app_name.lower(),
        app_name.lower().replace(" ", ""),
        app_name.lower().replace(" ", "_"),
        app_name.lower().replace("-", ""),
    ]
    for key in candidates:
        if key in registry:
            return registry[key]
    return None


def check_all_apps(apps: list[dict], api_key: str | None = None) -> dict[str, dict]:
    """
    Cross-check all apps against Composio registry.
    Returns: {app_name -> toolkit_match_dict} for matched apps (missing = not in registry)
    """
    if api_key is None:
        api_key = get_composio_key()

    if not api_key:
        print("\n" + "="*80, file=sys.stderr)
        print("  [CRITICAL WARNING] No Composio API key found in environment or config.json!", file=sys.stderr)
        print("  [CRITICAL WARNING] The Pass B registry cross-check will be SKIPPED.", file=sys.stderr)
        print("  [CRITICAL WARNING] composio_toolkit_match will be null for all apps.", file=sys.stderr)
        print("="*80 + "\n", file=sys.stderr)
        return {}

    try:
        print(f"  Fetching Composio toolkit registry...", file=sys.stderr)
        registry = _build_registry(api_key)
        print(f"  Registry loaded: {len(registry)} entries", file=sys.stderr)
    except Exception as e:
        print("\n" + "="*80, file=sys.stderr)
        print(f"  [CRITICAL WARNING] Could not fetch Composio app list: {type(e).__name__}: {e}", file=sys.stderr)
        print("  [CRITICAL WARNING] The Pass B registry cross-check will be SKIPPED.", file=sys.stderr)
        print("="*80 + "\n", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        return {}

    matches: dict[str, dict] = {}
    found = 0

    col_w = max(len(a["app"]) for a in apps) + 2
    for app in apps:
        match = _fuzzy_match(app["app"], registry)
        icon = "[OK]" if match else " -- "
        print(f"  {app['app']:{col_w}}{icon}  {'found: ' + match['toolkit_id'] if match else 'not found'}")
        if match:
            matches[app["app"]] = match
            found += 1

    print(f"\n{found}/{len(apps)} apps found in Composio registry.")
    return matches


if __name__ == "__main__":
    import json
    apps = json.loads(Path("apps.json").read_text())
    matches = check_all_apps(apps)
    print(json.dumps(matches, indent=2))
