import json
import sys
from urllib.parse import urlparse

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

def disagree_in_kind(va, vb):
    a = va.lower()
    b = vb.lower()
    a_words = set(a.replace('-', ' ').replace('_', ' ').replace('(', ' ').replace(')', ' ').split())
    b_words = set(b.replace('-', ' ').replace('_', ' ').replace('(', ' ').replace(')', ' ').split())
    
    # 1. self-serve vs gated
    if ("self-serve" in a or "self serve" in a or "self_serve" in a or "self" in a_words) and ("gated" in b or "request access" in b or "contact" in b):
        return True
    if ("self-serve" in b or "self serve" in b or "self_serve" in b or "self" in b_words) and ("gated" in a or "request access" in a or "contact" in a):
        return True
        
    # 2. buildable vs not
    has_yes_a = "yes" in a_words or "buildable" in a_words
    has_no_b = "no" in b_words or "not" in b_words or "unbuildable" in b or "no public api" in b or "no api" in b
    if has_yes_a and has_no_b:
        return True
    has_yes_b = "yes" in b_words or "buildable" in b_words
    has_no_a = "no" in a_words or "not" in a_words or "unbuildable" in a or "no public api" in a or "no api" in a
    if has_yes_b and has_no_a:
        return True
            
    # 3. has-API vs no-API
    a_has_api = "oauth" in a or "api key" in a or "token" in a or "rest" in a or "graphql" in a or "soap" in a
    b_no_api = "no public api" in b or "no api" in b or "no hosted api" in b or "none" in b_words
    if a_has_api and b_no_api:
        return True
        
    b_has_api = "oauth" in b or "api key" in b or "token" in b or "rest" in b or "graphql" in b or "soap" in b
    a_no_api = "no public api" in a or "no api" in a or "no hosted api" in a or "none" in a_words
    if b_has_api and a_no_api:
        return True
        
    return False

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

def main():
    try:
        with open('results.json', 'r', encoding='utf-8') as f:
            results = json.load(f)
        with open('apps.json', 'r', encoding='utf-8') as f:
            apps = json.load(f)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    apps_lookup = {a['app']: a for a in apps}
    
    # Aggregator and blog denylist
    unreliable_domains = [
        'apitracker.io',
        'getpostman.com',
        'beingtechnicalwriter.com',
        'blogspot.com',
        'wordpress.com',
        'medium.com'
    ]

    high_risk_fields = []
    likely_wrong_fields = []
    low_confidence_fields = []

    for r in results:
        app_name = r['app']
        app_meta = apps_lookup.get(app_name, {})
        hint_val = app_meta.get('docs_hint') or app_meta.get('hint')
        hint_root = clean_hint(hint_val)
        
        for fname, fd in r['fields'].items():
            pa = fd.get('pass_a', {})
            pb = fd.get('pass_b', {})
            
            pa_src = pa.get('source', '')
            pb_src = pb.get('source', '')
            
            pa_root = get_root_domain(pa_src)
            pb_root = get_root_domain(pb_src)
            
            # 1. Check for LOW-CONFIDENCE SOURCE
            is_pa_unreliable = any(ud in pa_root for ud in unreliable_domains) or (pa_root and pa_root != hint_root and 'blog' in pa_src)
            is_pb_unreliable = any(ud in pb_root for ud in unreliable_domains) or (pb_root and pb_root != hint_root and 'blog' in pb_src)
            
            if is_pa_unreliable or is_pb_unreliable:
                unreliable_src = pa_src if is_pa_unreliable else pb_src
                low_confidence_fields.append({
                    'app': app_name,
                    'field': fname,
                    'unreliable_source': unreliable_src,
                    'final': fd.get('final')
                })

            if not pa_root or not pb_root or not hint_root:
                continue
                

            # 2. Check for HIGH RISK
            different_domains = pa_root != pb_root
            exactly_one_matches = (pa_root == hint_root) ^ (pb_root == hint_root)
            
            val_a = pa.get('value', '')
            val_b = pb.get('value', '')

            if different_domains and exactly_one_matches and disagree_in_kind(val_a, val_b):
                matching_pass = 'pass_a' if pa_root == hint_root else 'pass_b'
                non_matching_pass = 'pass_b' if matching_pass == 'pass_a' else 'pass_a'
                
                info = {
                    'app': app_name,
                    'field': fname,
                    'matching_pass': matching_pass,
                    'matching_val': pa.get('value') if matching_pass == 'pass_a' else pb.get('value'),
                    'matching_src': pa_src if matching_pass == 'pass_a' else pb_src,
                    'non_matching_val': pb.get('value') if matching_pass == 'pass_a' else pa.get('value'),
                    'non_matching_src': pb_src if matching_pass == 'pass_a' else pa_src,
                    'final': fd.get('final')
                }
                
                high_risk_fields.append(info)
                
                # Check if final matches the official matching source
                official_val = info['matching_val']
                if fd.get('final') != official_val and fd.get('final', '').lower() != official_val.lower():
                    likely_wrong_fields.append(info)

    # Output Reports
    print("=" * 80)
    print(" SOURCE RELIABILITY ANALYSIS REPORT")
    print("=" * 80)
    
    print(f"\n[+] Detected {len(low_confidence_fields)} LOW-CONFIDENCE SOURCE fields:")
    for f in low_confidence_fields:
        print(f"  - {f['app']}.{f['field']}: Source '{f['unreliable_source']}' is in the denylist (Final: '{f['final']}')")

    print(f"\n[+] Detected {len(high_risk_fields)} HIGH RISK fields:")
    for f in high_risk_fields:
        print(f"  - {f['app']}.{f['field']}: domain split between official ({f['matching_pass']}) and unofficial ({'pass_b' if f['matching_pass']=='pass_a' else 'pass_a'})")
        print(f"    * Official val: '{f['matching_val']}' (src: {f['matching_src']})")
        print(f"    * Unofficial val: '{f['non_matching_val']}' (src: {f['non_matching_src']})")
        print(f"    * Current Final: '{f['final']}'")

    print(f"\n[!] Detected {len(likely_wrong_fields)} LIKELY WRONG fields (Adjudication resolved to unofficial domain):")
    for f in likely_wrong_fields:
        print(f"  - {f['app']}.{f['field']}: Resolved to unofficial '{f['non_matching_val']}' over official '{f['matching_val']}'")

    print("=" * 80)
    
    if likely_wrong_fields:
        print(f"Exit Check: FAILED. {len(likely_wrong_fields)} fields are likely wrong.", file=sys.stderr)
        sys.exit(1)
    else:
        print("Exit Check: PASSED. 0 fields likely wrong.")
        sys.exit(0)

if __name__ == '__main__':
    main()
