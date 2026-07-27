#!/usr/bin/env python3
"""
Pull Shopify, Meta Ads and Klaviyo directly from their APIs and write raw.json.

Runs in GitHub Actions on a schedule — no Claude, no MCP connectors. Claude's
scheduled sessions get a fixed 29-tool allowlist with none of these connectors
in it, which is why the original refresh silently did nothing.

Each source is independent. If one fails, its previous data is carried over from
the existing raw.json and the failure is recorded in `sources` so the dashboard
can show that panel as stale instead of quietly lying.

Environment:
    SHOPIFY_STORE          hm0yqq-33.myshopify.com
    SHOPIFY_CLIENT_ID      Dev Dashboard app Client ID
    SHOPIFY_CLIENT_SECRET  Dev Dashboard app Secret
    META_TOKEN        a long-lived / system-user token
    META_AD_ACCOUNT   1251795310364570   (no "act_" prefix)
    KLAVIYO_KEY       pk_...
"""
import json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

SHOPIFY_API = "2026-04"
META_VERSIONS = ["v23.0", "v22.0", "v21.0", "v20.0"]
KLAVIYO_REVISIONS = ["2025-07-15", "2025-01-15", "2024-10-15"]
OUT = "raw.json"

def env(name, required=True):
    """Read a credential, tolerating whitespace. A trailing newline pasted into
    GitHub's secret textarea otherwise corrupts the auth header and surfaces as
    a bogus 'invalid key' error."""
    v = (os.environ.get(name) or "").strip()
    if not v and required:
        raise KeyError(name)
    return v


# ---------------------------------------------------------------- http
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(url, headers=None, body=None, method=None, timeout=60):
    req = urllib.request.Request(url, method=method or ("POST" if body else "GET"))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    last = None
    for attempt in range(3):
        try:
            with OPENER.open(req, data, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")[:400]
            last = RuntimeError(f"HTTP {e.code}: {payload}")
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            raise last
        except Exception as e:                                   # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise last


def money(x):
    """'A$1,190.08 AUD' / '1190.08' / None -> float."""
    if x in (None, "", "Not available"):
        return 0.0
    s = str(x)
    keep = "".join(c for c in s if c.isdigit() or c in ".-")
    try:
        return round(float(keep), 2)
    except ValueError:
        return 0.0


def syd_today():
    now = datetime.now(timezone.utc)
    def first_sunday(y, m):
        d = datetime(y, m, 1, tzinfo=timezone.utc)
        return d + timedelta(days=(6 - d.weekday()) % 7)
    y = now.year
    dst = now >= first_sunday(y, 10) + timedelta(hours=6) or now < first_sunday(y, 4) + timedelta(hours=5)
    return (now + timedelta(hours=11 if dst else 10)).date()


# ---------------------------------------------------------------- shopify
ORDERS_Q = """
query($q: String!) {
  orders(first: 250, query: $q, sortKey: CREATED_AT, reverse: true) {
    nodes { name createdAt displayFinancialStatus displayFulfillmentStatus
      currentTotalPriceSet { shopMoney { amount } }
      customer { displayName }
      lineItems(first: 10) { nodes { title quantity } } } }
}"""

PENDING_Q = """
query {
  orders(first: 50, query: "fulfillment_status:unfulfilled AND financial_status:paid") {
    nodes { name createdAt currentTotalPriceSet { shopMoney { amount } } customer { displayName } } }
  customersCount { count }
}"""

ABANDONED_Q = """
query($q: String!) { abandonedCheckoutsCount(query: $q) { count } }"""

INVENTORY_Q = """
query {
  productVariants(first: 250) {
    nodes { sku inventoryQuantity availableForSale price
      inventoryItem { tracked } product { title status } } }
}"""


_TOKEN_CACHE = {}


def shopify_token():
    """Dev Dashboard apps don't issue a static shpat_ token — exchange the app's
    client credentials for a short-lived one (24h) on each run.
    SHOPIFY_TOKEN still works if a legacy admin-created token is supplied."""
    if env("SHOPIFY_TOKEN", required=False):
        return env("SHOPIFY_TOKEN")
    if "t" in _TOKEN_CACHE:
        return _TOKEN_CACHE["t"]
    store = env("SHOPIFY_STORE")
    res = http(f"https://{store}/admin/oauth/access_token", {}, {
        "client_id": env("SHOPIFY_CLIENT_ID"),
        "client_secret": env("SHOPIFY_CLIENT_SECRET"),
        "grant_type": "client_credentials",
    })
    tok = res.get("access_token")
    if not tok:
        raise RuntimeError(f"no access_token in grant response: {str(res)[:200]}")
    _TOKEN_CACHE["t"] = tok
    return tok


def shopify(query, variables=None):
    store = env("SHOPIFY_STORE")
    url = f"https://{store}/admin/api/{SHOPIFY_API}/graphql.json"
    res = http(url, {"X-Shopify-Access-Token": shopify_token()},
               {"query": query, "variables": variables or {}})
    if res.get("errors"):
        raise RuntimeError(f"Shopify GraphQL: {json.dumps(res['errors'])[:300]}")
    return res["data"]


def fetch_shopify(today):
    since = (today - timedelta(days=30)).isoformat()

    d = shopify(ORDERS_Q, {"q": f"created_at:>={since}"})
    orders = [{
        "name": o["name"],
        "createdAt": o["createdAt"],
        "financial": o["displayFinancialStatus"],
        "fulfillment": o["displayFulfillmentStatus"],
        "total": money(o["currentTotalPriceSet"]["shopMoney"]["amount"]),
        "customer": (o.get("customer") or {}).get("displayName") or "Guest",
        "items": [{"title": i["title"], "quantity": i["quantity"]}
                  for i in o["lineItems"]["nodes"]],
    } for o in d["orders"]["nodes"]]

    d = shopify(PENDING_Q)
    pending = [{
        "name": o["name"], "createdAt": o["createdAt"],
        "total": money(o["currentTotalPriceSet"]["shopMoney"]["amount"]),
        "customer": (o.get("customer") or {}).get("displayName") or "Guest",
    } for o in d["orders"]["nodes"]]
    customers = d["customersCount"]["count"]

    try:   # needs read_checkouts; not fatal if the scope is missing
        ab = shopify(ABANDONED_Q, {"q": f"created_at:>={(today - timedelta(days=7)).isoformat()}"})
        abandoned = ab["abandonedCheckoutsCount"]["count"]
    except Exception as e:                                        # noqa: BLE001
        print(f"  abandoned checkouts unavailable ({e})", file=sys.stderr)
        abandoned = 0

    d = shopify(INVENTORY_Q)
    inventory = [{
        "title": v["product"]["title"],
        "sku": v["sku"] or "—",
        "qty": v["inventoryQuantity"] or 0,
        "price": money(v["price"]),
        "status": v["product"]["status"],
        "tracked": bool((v.get("inventoryItem") or {}).get("tracked")),
        "available": bool(v["availableForSale"]),
    } for v in d["productVariants"]["nodes"]]

    return {"orders": orders, "unfulfilled_paid": pending,
            "customers_total": customers, "abandoned_checkouts_recent": abandoned,
            "inventory": inventory}


# ---------------------------------------------------------------- meta
def meta_get(path, params):
    token = env("META_TOKEN")
    last = None
    for ver in META_VERSIONS:
        q = urllib.parse.urlencode({**params, "access_token": token})
        try:
            return http(f"https://graph.facebook.com/{ver}/{path}?{q}")
        except Exception as e:                                    # noqa: BLE001
            last = e
            if "Unsupported get request" in str(e) or "unknown version" in str(e).lower():
                continue
            raise
    raise last


def action_val(row, key, field="actions"):
    for a in row.get(field) or []:
        if a.get("action_type") == key:
            return money(a.get("value"))
    return 0.0


def roas_val(row):
    for a in row.get("purchase_roas") or []:
        return round(float(a.get("value") or 0), 4)
    return 0.0


def insight_row(r):
    spend = money(r.get("spend"))
    imp = int(r.get("impressions") or 0)
    clicks = int(r.get("clicks") or 0)
    return {
        "date": r.get("date_start"),
        "spend": spend, "impressions": imp, "clicks": clicks,
        "ctr": round(float(r.get("ctr") or 0), 2),
        "cpc": round(float(r.get("cpc") or 0), 2),
        "purchases": int(action_val(r, "omni_purchase") or 0),
        "purchase_value": action_val(r, "omni_purchase", "action_values"),
    }


def fetch_meta(today):
    acct = f"act_{env('META_AD_ACCOUNT')}"
    base = ("spend,impressions,clicks,ctr,cpc,actions,action_values,purchase_roas")

    daily = meta_get(f"{acct}/insights", {
        "level": "account", "date_preset": "last_14d",
        "time_increment": "1", "fields": base, "limit": 100})
    series = [insight_row(r) for r in daily.get("data", [])]

    t = meta_get(f"{acct}/insights", {
        "level": "account", "date_preset": "today", "fields": base})
    rows = t.get("data", [])
    ads_today = insight_row(rows[0]) if rows else {
        "date": today.isoformat(), "spend": 0.0, "impressions": 0, "clicks": 0,
        "ctr": 0.0, "cpc": 0.0, "purchases": 0, "purchase_value": 0.0}
    ads_today["date"] = today.isoformat()
    series = [s for s in series if s["date"] != ads_today["date"]] + [ads_today]
    series.sort(key=lambda s: s["date"])

    ci = meta_get(f"{acct}/insights", {
        "level": "campaign", "date_preset": "last_30d", "limit": 100,
        "fields": "campaign_id,campaign_name," + base + ",cpm,reach"})
    by_id = {r["campaign_id"]: r for r in ci.get("data", [])}

    meta_c = meta_get(f"{acct}/campaigns", {
        "fields": "id,name,status,effective_status,daily_budget,objective", "limit": 100})

    campaigns = []
    for c in meta_c.get("data", []):
        r = by_id.get(c["id"], {})
        spend = money(r.get("spend"))
        campaigns.append({
            "name": c.get("name", ""),
            "status": c.get("effective_status") or c.get("status") or "UNKNOWN",
            "daily_budget": round(int(c["daily_budget"]) / 100, 2) if c.get("daily_budget") else None,
            "spend": spend,
            "impressions": int(r.get("impressions") or 0),
            "clicks": int(r.get("clicks") or 0),
            "ctr": round(float(r.get("ctr") or 0), 2),
            "cpc": round(float(r.get("cpc") or 0), 2),
            "roas": roas_val(r),
            "purchases": int(action_val(r, "omni_purchase") or 0),
            "purchase_value": action_val(r, "omni_purchase", "action_values"),
            "atc": int(action_val(r, "omni_add_to_cart") or 0),
            "reach": int(r.get("reach") or 0),
        })
    campaigns.sort(key=lambda c: -c["spend"])
    return {"ads_daily": series, "ads_today": ads_today, "ads_campaigns": campaigns}


# ---------------------------------------------------------------- klaviyo
def klaviyo_get(path, params=None):
    key = env("KLAVIYO_KEY")
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    last = None
    for rev in KLAVIYO_REVISIONS:
        try:
            return http(f"https://a.klaviyo.com/api/{path}{q}",
                        {"Authorization": f"Klaviyo-API-Key {key}",
                         "revision": rev, "accept": "application/vnd.api+json"})
        except Exception as e:                                    # noqa: BLE001
            last = e
            if "revision" in str(e).lower():
                continue
            raise
    raise last


def fetch_klaviyo():
    flows = klaviyo_get("flows/").get("data", [])
    live = [f for f in flows if f["attributes"].get("status") == "live"]
    draft = [f for f in flows if f["attributes"].get("status") == "draft"]
    lists = klaviyo_get("lists/").get("data", [])
    camps = klaviyo_get("campaigns/",
                        {"filter": "equals(messages.channel,'email')"}).get("data", [])
    return {"klaviyo": {
        "flows_live": len(live),
        "flows_draft": len(draft),
        "live_flow_names": [f["attributes"]["name"] for f in live],
        "campaigns_sent": len(camps),
        "lists": len(lists),
    }}


# ---------------------------------------------------------------- main
def main():
    today = syd_today()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT))
        except Exception:                                         # noqa: BLE001
            prev = {}

    raw = {
        "generated_at_utc": now,
        "shop": {"name": "Lumara Home", "domain": "lumarahome.com.au",
                 "currency": "AUD", "tz": "Australia/Sydney"},
        "sources": {},
    }

    plan = [("shopify", lambda: fetch_shopify(today),
             ["orders", "unfulfilled_paid", "customers_total",
              "abandoned_checkouts_recent", "inventory"]),
            ("meta", lambda: fetch_meta(today),
             ["ads_daily", "ads_today", "ads_campaigns"]),
            ("klaviyo", fetch_klaviyo, ["klaviyo"])]

    failed = []
    for name, fn, keys in plan:
        try:
            raw.update(fn())
            raw["sources"][name] = {"ok": True, "fetched_at": now}
            print(f"{name}: ok")
        except Exception as e:                                    # noqa: BLE001
            msg = str(e)[:200]
            failed.append(name)
            for k in keys:
                if k in prev:
                    raw[k] = prev[k]
            old = (prev.get("sources") or {}).get(name, {})
            raw["sources"][name] = {
                "ok": False, "error": msg,
                "fetched_at": old.get("fetched_at") or prev.get("generated_at_utc"),
            }
            print(f"{name}: FAILED — {msg}", file=sys.stderr)

    missing = [k for k in ("orders", "inventory", "ads_daily", "klaviyo") if k not in raw]
    if missing:
        sys.exit(f"fatal: no data at all for {missing} — refusing to write raw.json")

    json.dump(raw, open(OUT, "w"), indent=1)
    print(f"wrote {OUT} at {now}" + (f" (stale: {', '.join(failed)})" if failed else ""))


if __name__ == "__main__":
    main()
