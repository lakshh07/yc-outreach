#!/usr/bin/env python3
"""
Find founder emails for a YC batch.

Waterfall per founder:
  1. Public sources  - emails on the company website, GitHub commits (--github)
  2. Pattern guess   - first@, first.last@, ... verified via SMTP RCPT check
  3. Leftovers       - catch-all / unknown domains get a best guess, marked medium/low

Usage:
  python3 find_emails.py --batch "Summer 2025" --hiring-only --limit 20
  python3 find_emails.py --batch "Winter 2025" --batch "Spring 2025" --out w25_s25.csv

Stdlib only. SMTP verification needs outbound port 25.
"""
import argparse
import csv
import html
import json
import os
import random
import re
import smtplib
import socket
import ssl
import string
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ALGOLIA_APP = "45BWZJ1SGC"
ALGOLIA_URL = f"https://{ALGOLIA_APP.lower()}-dsn.algolia.net/1/indexes/YCCompany_production/query"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
GENERIC_LOCALS = {"hello", "hi", "team", "info", "contact", "support", "founders", "sales",
                  "careers", "jobs", "press", "help", "admin", "privacy", "legal", "security", "hey"}
SITE_PATHS = ["", "/about", "/team", "/contact", "/about-us", "/company"]

try:
    _SSL = ssl.create_default_context()
    import certifi  # noqa: optional
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass


# ---------------------------------------------------------------- http utils

def http_get(url, headers=None, timeout=15, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def cached(key, fn):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    val = fn()
    with open(path, "w") as f:
        json.dump(val, f)
    return val


# ---------------------------------------------------------------- YC data

def algolia_key():
    _, page = http_get("https://www.ycombinator.com/companies")
    m = re.search(r'AlgoliaOpts\s*=\s*\{"app":"[^"]+","key":"([^"]+)"', page)
    if not m:
        sys.exit("Could not find Algolia key on YC directory page")
    return m.group(1)


def fetch_companies(batches, hiring_only):
    key = algolia_key()
    out = []
    for batch in batches:
        page = 0
        while True:
            filters = [[f"batch:{batch}"]]
            if hiring_only:
                filters.append(["isHiring:true"])
            body = json.dumps({"query": "", "hitsPerPage": 100, "page": page,
                               "facetFilters": filters}).encode()
            _, txt = http_get(ALGOLIA_URL, data=body, headers={
                "X-Algolia-Application-Id": ALGOLIA_APP, "X-Algolia-API-Key": key,
                "Content-Type": "application/json"})
            d = json.loads(txt)
            out.extend(d["hits"])
            page += 1
            if page >= d["nbPages"]:
                break
    return out


def fetch_founders(slug):
    def load():
        _, page = http_get(f"https://www.ycombinator.com/companies/{slug}")
        m = re.search(r'data-page="([^"]+)"', page)
        if not m:
            return {"founders": [], "github_url": ""}
        c = json.loads(html.unescape(m.group(1)))["props"]["company"]
        return {
            "github_url": c.get("github_url") or "",
            "founders": [{
                "name": f.get("full_name", ""), "title": f.get("title", ""),
                "linkedin": f.get("linkedin_url", ""), "twitter": f.get("twitter_url", ""),
            } for f in c.get("founders", []) if f.get("is_active", True)],
        }
    return cached(f"yc_{slug}", load)


# ---------------------------------------------------------------- names & patterns

def ascii_lower(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def split_name(full):
    parts = ascii_lower(full).split()
    if not parts:
        return "", ""
    return parts[0], (parts[-1] if len(parts) > 1 else "")


PATTERNS = {  # ordered by how common they are at early-stage startups
    "first":      lambda f, l: f,
    "first.last": lambda f, l: f"{f}.{l}" if l else None,
    "firstlast":  lambda f, l: f"{f}{l}" if l else None,
    "flast":      lambda f, l: f"{f[0]}{l}" if l else None,
    "f.last":     lambda f, l: f"{f[0]}.{l}" if l else None,
    "first_last": lambda f, l: f"{f}_{l}" if l else None,
    "last":       lambda f, l: l or None,
    "firstl":     lambda f, l: f"{f}{l[0]}" if l else None,
}


def candidates(full_name, domain, preferred=None):
    f, l = split_name(full_name)
    if not f:
        return []
    order = list(PATTERNS)
    if preferred in PATTERNS:
        order.remove(preferred)
        order.insert(0, preferred)
    seen, out = set(), []
    for p in order:
        local = PATTERNS[p](f, l)
        if local and local not in seen:
            seen.add(local)
            out.append((p, f"{local}@{domain}"))
    return out


def detect_pattern(email, full_name):
    local = email.split("@")[0].lower()
    f, l = split_name(full_name)
    if not f:
        return None
    for p, fn in PATTERNS.items():
        if fn(f, l) == local:
            return p
    return None


def email_matches_name(email, full_name):
    return detect_pattern(email, full_name) is not None


# ---------------------------------------------------------------- domain / DNS

def domain_of(url):
    if not url:
        return ""
    if "://" not in url:
        url = "http://" + url
    host = urllib.parse.urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def mx_hosts(domain):
    def load():
        try:
            _, txt = http_get(f"https://dns.google/resolve?name={domain}&type=MX", timeout=10)
            ans = json.loads(txt).get("Answer", [])
            recs = []
            for a in ans:
                if a.get("type") == 15:
                    pri, host = a["data"].split()
                    recs.append((int(pri), host.rstrip(".")))
            return [h for _, h in sorted(recs)]
        except Exception:
            return []
    return cached(f"mx_{domain}", load)


# ---------------------------------------------------------------- step 1: public sources

def scrape_site_emails(website):
    def load():
        found, final_domain = set(), ""
        base = website if "://" in website else "http://" + website
        for path in SITE_PATHS:
            try:
                url, page = http_get(base.rstrip("/") + path, timeout=10)
                if not final_domain:
                    final_domain = domain_of(url)
                page = html.unescape(urllib.parse.unquote(page))
                for e in EMAIL_RE.findall(page):
                    e = e.lower().rstrip(".")
                    if not e.endswith((".png", ".jpg", ".svg", ".webp", ".gif", ".js", ".css")):
                        found.add(e)
            except Exception:
                continue
        return {"emails": sorted(found), "final_domain": final_domain}
    return cached(f"site_{domain_of(website)}", load)


_gh_last = [0.0]


def github_commit_emails(full_name):
    """Search public commits by author name. Unauthenticated: 10 req/min, token: 30 req/min."""
    def load():
        token = os.environ.get("GITHUB_TOKEN")
        gap = 2.2 if token else 6.5
        wait = _gh_last[0] + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        _gh_last[0] = time.time()
        q = urllib.parse.quote(f'author-name:"{full_name}"')
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            _, txt = http_get(f"https://api.github.com/search/commits?q={q}&per_page=50", headers=headers)
            items = json.loads(txt).get("items", [])
        except Exception:
            return []
        emails = set()
        for it in items:
            e = (it.get("commit", {}).get("author", {}).get("email") or "").lower()
            if e and "noreply" not in e:
                emails.add(e)
        return sorted(emails)
    return cached(f"gh_{ascii_lower(full_name).replace(' ', '_')}", load)


# ---------------------------------------------------------------- step 2: SMTP verification

def smtp_check(domain, addresses):
    """Returns (status, {address: code}). status in valid-check-ok | catch_all | no_mx | error."""
    mxs = mx_hosts(domain)
    if not mxs:
        return "no_mx", {}
    probe = "".join(random.choices(string.ascii_lowercase + string.digits, k=14)) + f"@{domain}"
    for mx in mxs[:2]:
        try:
            with smtplib.SMTP(mx, 25, timeout=15) as s:
                s.ehlo("mail.example.com")
                s.mail("")
                code, _ = s.rcpt(probe)
                if code == 250:
                    return "catch_all", {}
                results = {}
                for a in addresses:
                    code, _ = s.rcpt(a)
                    results[a] = code
                    if code == 250:
                        break  # first hit is enough; patterns are ordered
                if 250 not in results.values() and any(400 <= v < 500 for v in results.values()):
                    return "greylisted", results  # "try later", not "doesn't exist"
                return "ok", results
        except (socket.timeout, smtplib.SMTPException, OSError):
            continue
    return "error", {}


# ---------------------------------------------------------------- pipeline

def process_company(c, use_github, use_smtp):
    rows = []
    website = c.get("website") or ""
    info = fetch_founders(c["slug"])
    founders = info["founders"]
    if not founders:
        return rows

    domain = domain_of(website)
    site = scrape_site_emails(website) if website else {"emails": [], "final_domain": ""}
    if site["final_domain"] and site["final_domain"] != domain and not mx_hosts(domain):
        domain = site["final_domain"]

    site_emails = site["emails"]
    domain_emails = [e for e in site_emails if e.endswith("@" + domain)]
    generic = [e for e in domain_emails if e.split("@")[0] in GENERIC_LOCALS]

    # Collect public hits per founder, and learn the company's pattern from any of them
    public = {}
    pattern = None
    for f in founders:
        hits = [e for e in domain_emails if email_matches_name(e, f["name"])]
        alt = []
        if use_github:
            gh = github_commit_emails(f["name"])
            hits += [e for e in gh if e.endswith("@" + domain)]
            alt = [e for e in gh if not e.endswith("@" + domain)]
        hits = sorted(set(hits))
        public[f["name"]] = (hits, alt)
        for e in hits:
            pattern = pattern or detect_pattern(e, f["name"])

    for f in founders:
        hits, alt = public[f["name"]]
        row = {
            "batch": c.get("batch"), "company": c.get("name"), "one_liner": c.get("one_liner"),
            "website": website, "domain": domain, "hiring": c.get("isHiring"),
            "founder": f["name"], "first_name": split_name(f["name"])[0].title(),
            "title": f["title"], "linkedin": f["linkedin"], "twitter": f["twitter"],
            "email": "", "confidence": "none", "source": "", "alt_emails": "; ".join(alt),
            "generic_emails": "; ".join(generic), "notes": "",
        }
        if hits:
            row.update(email=hits[0], confidence="high", source="public")
            rows.append(row)
            continue
        if not domain:
            row["notes"] = "no website"
            rows.append(row)
            continue

        cands = candidates(f["name"], domain, preferred=pattern)
        if use_smtp and cands:
            status, res = smtp_check(domain, [a for _, a in cands])
            valid = [a for a, code in res.items() if code == 250]
            if valid:
                row.update(email=valid[0], confidence="high", source="smtp_verified")
            elif status == "catch_all":
                # first@ is by far the most common pattern at YC-stage startups
                best_pat, best = cands[0]
                row.update(email=best, confidence="medium" if pattern or best_pat == "first" else "low",
                           source="pattern_catch_all", notes="domain accepts all addresses")
            elif status == "ok":
                row.update(notes="all patterns rejected by mail server")
            else:
                row.update(email=cands[0][1], confidence="low", source="pattern_unverified", notes=status)
        elif cands:
            row.update(email=cands[0][1], confidence="low", source="pattern_unverified",
                       notes="smtp disabled")
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--batch", action="append", required=True, help='e.g. "Summer 2025" (repeatable)')
    ap.add_argument("--hiring-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="max companies (0 = all)")
    ap.add_argument("--github", action="store_true", help="search GitHub commits (set GITHUB_TOKEN for speed)")
    ap.add_argument("--no-smtp", action="store_true", help="skip SMTP verification")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="founders.csv")
    args = ap.parse_args()

    companies = fetch_companies(args.batch, args.hiring_only)
    print(f"{len(companies)} companies in {', '.join(args.batch)}"
          f"{' (hiring only)' if args.hiring_only else ''}", file=sys.stderr)
    if args.limit:
        random.seed(42)
        companies = random.sample(companies, min(args.limit, len(companies)))

    rows = []
    with ThreadPoolExecutor(max_workers=1 if args.github else args.workers) as ex:
        futs = [ex.submit(process_company, c, args.github, not args.no_smtp) for c in companies]
        for i, fut in enumerate(futs, 1):
            try:
                r = fut.result()
                rows.extend(r)
                best = ", ".join(f"{x['founder']}={x['confidence']}" for x in r)
                print(f"[{i}/{len(futs)}] {companies[i-1]['name']}: {best}", file=sys.stderr)
            except Exception as e:
                print(f"[{i}/{len(futs)}] {companies[i-1]['name']}: ERROR {e}", file=sys.stderr)

    if not rows:
        sys.exit("No rows")
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    total = len(rows)
    by_conf, by_src = {}, {}
    for r in rows:
        by_conf[r["confidence"]] = by_conf.get(r["confidence"], 0) + 1
        if r["source"]:
            by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    print(f"\nWrote {total} founders -> {args.out}", file=sys.stderr)
    for k in ["high", "medium", "low", "none"]:
        n = by_conf.get(k, 0)
        print(f"  {k:7s} {n:4d}  ({100*n/total:.0f}%)", file=sys.stderr)
    print("  sources:", by_src, file=sys.stderr)


if __name__ == "__main__":
    main()
