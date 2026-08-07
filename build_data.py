#!/usr/bin/env python3
"""
Lumara Home ops dashboard — derives data.json from raw.json.

raw.json is a dump of connector output (see the runbook). This script does all
date maths in Australia/Sydney and computes every derived metric the page shows,
so the hourly refresh only has to paste raw numbers in.

Usage:  python3 build_data.py [raw.json] [data.json]
"""
import json, sys
from datetime import datetime, timedelta, timezone

SYD = timezone(timedelta(hours=10))  # AEST; AEDT (+11) handled below

RAW = sys.argv[1] if len(sys.argv) > 1 else "raw.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "data.json"


def syd(dt_utc):
    """UTC datetime -> Sydney local. AEDT runs first Sun Oct -> first Sun Apr."""
    y = dt_utc.year
    def first_sunday(year, month):
        d = datetime(year, month, 1, tzinfo=timezone.utc)
        return d + timedelta(days=(6 - d.weekday()) % 7)
    dst_start = first_sunday(y, 10) + timedelta(hours=6)   # 2am AEST
    dst_end = first_sunday(y, 4) + timedelta(hours=5)      # 3am AEDT
    offset = 11 if (dt_utc >= dst_start or dt_utc < dst_end) else 10
    return dt_utc + timedelta(hours=offset)


def parse(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def money(x):
    return round(float(x or 0), 2)


raw = json.load(open(RAW))
now_utc = parse(raw["generated_at_utc"])
now_syd = syd(now_utc)
today = now_syd.date()

# ---------------------------------------------------------------- orders
orders = []
for o in raw["orders"]:
    d = syd(parse(o["createdAt"]))
    orders.append({**o, "date": d.date(), "local": d.strftime("%-d %b, %-I:%M%p").lower()})

live = [o for o in orders if o["financial"] not in ("REFUNDED", "VOIDED")]


def window(days):
    start = today - timedelta(days=days - 1)
    sel = [o for o in live if o["date"] >= start]
    rev = sum(o["total"] for o in sel)
    return {"orders": len(sel), "revenue": money(rev),
            "aov": money(rev / len(sel)) if sel else 0.0}


today_w, d7, d30 = window(1), window(7), window(30)
prev7_start, prev7_end = today - timedelta(days=13), today - timedelta(days=7)
prev7 = [o for o in live if prev7_start <= o["date"] <= prev7_end]
prev7_rev = money(sum(o["total"] for o in prev7))

# daily order/revenue series, 30 days
series_days = [today - timedelta(days=i) for i in range(29, -1, -1)]
by_day = {}
for o in live:
    b = by_day.setdefault(o["date"], {"orders": 0, "revenue": 0.0})
    b["orders"] += 1
    b["revenue"] += o["total"]
daily_sales = [{"date": d.isoformat(),
                "label": d.strftime("%-d %b"),
                "orders": by_day.get(d, {}).get("orders", 0),
                "revenue": money(by_day.get(d, {}).get("revenue", 0))}
               for d in series_days]

last_order = max((o for o in live), key=lambda o: o["date"], default=None)
days_since_order = (today - last_order["date"]).days if last_order else None

# ---------------------------------------------------------------- ads
ads = raw["ads_daily"]
ads_by_date = {a["date"]: a for a in ads}


def ads_window(days):
    ds = [(today - timedelta(days=i)).isoformat() for i in range(days)]
    sel = [ads_by_date[d] for d in ds if d in ads_by_date]
    spend = sum(a["spend"] for a in sel)
    val = sum(a["purchase_value"] for a in sel)
    clicks = sum(a["clicks"] for a in sel)
    imp = sum(a["impressions"] for a in sel)
    pur = sum(a["purchases"] for a in sel)
    return {"spend": money(spend), "value": money(val),
            "roas": round(val / spend, 2) if spend else 0.0,
            "clicks": clicks, "impressions": imp, "purchases": pur,
            "cpc": round(spend / clicks, 2) if clicks else 0.0,
            "ctr": round(clicks / imp * 100, 2) if imp else 0.0,
            "cpa": round(spend / pur, 2) if pur else None}


ads_today = ads_window(1)
ads_7 = ads_window(7)
ads_prev7 = None
_ds = [(today - timedelta(days=i)).isoformat() for i in range(7, 14)]
_sel = [ads_by_date[d] for d in _ds if d in ads_by_date]
if _sel:
    _sp = sum(a["spend"] for a in _sel)
    _vl = sum(a["purchase_value"] for a in _sel)
    ads_prev7 = {"spend": money(_sp), "value": money(_vl),
                 "roas": round(_vl / _sp, 2) if _sp else 0.0}

ads_series = [{"date": a["date"],
               "label": datetime.fromisoformat(a["date"]).strftime("%-d %b"),
               "spend": a["spend"], "revenue": a["purchase_value"],
               "purchases": a["purchases"], "clicks": a["clicks"],
               "ctr": a["ctr"], "cpc": a["cpc"]}
              for a in sorted(ads, key=lambda a: a["date"])][-14:]

# consecutive trailing days of spend with zero attributed purchases
dry_spend, dry_days, dry_start = 0.0, 0, None
for a in reversed(ads_series):
    if a["purchases"] == 0 and a["spend"] > 0:
        dry_spend += a["spend"]
        dry_days += 1
        dry_start = a["date"]
    elif a["purchases"] > 0:
        break

# The store's own orders inside that same window. Meta attributing zero
# purchases while Shopify takes orders is an attribution/pixel story, not a
# "conversion is broken" story — the alert has to distinguish the two or it
# reads as wrong the moment an organic order lands.
orders_in_dry, orders_in_dry_rev = [], 0.0
if dry_start:
    _dry_d = datetime.fromisoformat(dry_start).date()
    orders_in_dry = [o for o in live if o["date"] >= _dry_d]
    orders_in_dry_rev = money(sum(o["total"] for o in orders_in_dry))

active_campaigns = [c for c in raw["ads_campaigns"] if c["status"] == "ACTIVE"]
daily_budget_live = money(sum(c["daily_budget"] or 0 for c in active_campaigns))
campaigns = sorted(raw["ads_campaigns"], key=lambda c: -c["spend"])

# ------------------------------------------------------------- inventory
# Only a TRACKED variant that Shopify reports as unavailable is really out of
# stock. An untracked variant sits at qty 0 forever and still sells fine — its
# number is meaningless, so it gets its own bucket instead of polluting OOS.
inv = raw["inventory"]
sellable = [p for p in inv if p["status"] == "ACTIVE"]
tracked = [p for p in sellable if p.get("tracked", True)]
untracked = sorted([p for p in sellable if not p.get("tracked", True)],
                   key=lambda p: -p["price"])
oos = sorted([p for p in tracked if not p.get("available", p["qty"] > 0)],
             key=lambda p: -p["price"])
low = sorted([p for p in tracked if 0 < p["qty"] <= 3], key=lambda p: p["qty"])
watch = sorted([p for p in tracked if 3 < p["qty"] <= 5], key=lambda p: p["qty"])
healthy = [p for p in tracked if p["qty"] > 5]
stock_value = money(sum(p["qty"] * p["price"] for p in tracked))

# units sold per product over the last 30d -> flag sellers that are now empty
sold30 = {}
start30 = today - timedelta(days=29)
for o in live:
    if o["date"] >= start30:
        for it in o["items"]:
            sold30[it["title"]] = sold30.get(it["title"], 0) + it["quantity"]


def matches(p_title, sold_title):
    a, b = p_title.lower(), sold_title.lower()
    return a.startswith(b[:22]) or b.startswith(a[:22])


for p in oos:
    p["sold_30d"] = sum(q for t, q in sold30.items() if matches(p["title"], t))
oos_sellers = [p for p in oos if p["sold_30d"] > 0]

for p in low + watch:
    s = sum(q for t, q in sold30.items() if matches(p["title"], t))
    p["sold_30d"] = s
    p["days_cover"] = round(p["qty"] / (s / 30), 1) if s else None

top_sellers = sorted(sold30.items(), key=lambda kv: -kv[1])[:6]

# ---------------------------------------------------------------- alerts
alerts = []
if days_since_order is not None and days_since_order >= 2:
    alerts.append({
        "level": "critical" if days_since_order >= 4 else "warning",
        "title": f"No orders for {days_since_order} days",
        "body": f"Last order was {last_order['name']} on "
                f"{last_order['date'].strftime('%-d %b')}. "
                f"Ads have spent ${dry_spend:,.0f} since the last attributed purchase.",
    })
if dry_days >= 3 and dry_spend > 0:
    if orders_in_dry:
        alerts.append({
            "level": "serious",
            "title": f"${dry_spend:,.0f} ad spend, 0 Meta-attributed purchases "
                     f"({dry_days} days)",
            "body": f"The store DID take {len(orders_in_dry)} order(s) worth "
                    f"${orders_in_dry_rev:,.0f} in that window — Meta just isn't "
                    "claiming any of them. Either they're organic/direct sales, or "
                    "the pixel is missing purchases. Worth checking Events Manager "
                    "before reading this as zero conversion.",
        })
    else:
        alerts.append({
            "level": "critical",
            "title": f"${dry_spend:,.0f} ad spend, 0 purchases ({dry_days} days)",
            "body": f"No store orders in that window either. {len(active_campaigns)} "
                    f"campaign(s) live at ${daily_budget_live:,.0f}/day. Check "
                    "pixel/tracking and landing pages before topping up budget.",
        })
if oos_sellers:
    names = ", ".join(p["title"] for p in oos_sellers[:3])
    alerts.append({
        "level": "serious",
        "title": f"{len(oos_sellers)} product(s) sold in the last 30d are out of stock",
        "body": names + ("…" if len(oos_sellers) > 3 else "")
                + ". Still listed as active — they take traffic and convert nothing.",
    })
# NOTE: no alert for untracked SKUs — tracking-off is deliberate for the
# LaVida just-in-time dropship items (Akshay's call, 3 Aug 2026). They still
# get their own bucket in the stock panel, just not a standing warning.
if low:
    alerts.append({
        "level": "warning",
        "title": f"{len(low)} product(s) at 3 units or fewer",
        "body": ", ".join(f"{p['title']} ({p['qty']})" for p in low[:4]),
    })
if raw["unfulfilled_paid"]:
    alerts.append({
        "level": "serious",
        "title": f"{len(raw['unfulfilled_paid'])} paid order(s) awaiting fulfilment",
        "body": ", ".join(o["name"] for o in raw["unfulfilled_paid"][:6]),
    })

LABEL = {"shopify": "Shopify", "meta": "Meta Ads", "klaviyo": "Klaviyo"}
broken = {k: v for k, v in (raw.get("sources") or {}).items() if not v.get("ok")}
if broken:
    names_l = [LABEL.get(k, k) for k in broken]
    names = names_l[0] if len(names_l) == 1 else \
        " and ".join([", ".join(names_l[:-1]), names_l[-1]])
    ages = []
    for k, v in broken.items():
        try:
            h = (now_utc - parse(v["fetched_at"])).total_seconds() / 3600
            ages.append(f"{LABEL.get(k, k)} data is from {h:.0f}h ago"
                        if h >= 2 else f"{LABEL.get(k, k)} data is under 2h old")
        except Exception:                                         # noqa: BLE001
            pass
    age_note = (" " + "; ".join(ages) + ".") if ages else ""
    alerts.insert(0, {
        "level": "serious",
        "title": f"{names} didn't refresh",
        "body": "Those panels show the last figures that came through, not "
                f"current ones.{age_note} Everything else on this page is up "
                "to date.",
    })

order = {"critical": 0, "serious": 1, "warning": 2, "good": 3}
alerts.sort(key=lambda a: order.get(a["level"], 9))

# ----------------------------------------------------------------- output
out = {
    "meta": {
        "shop": raw["shop"]["name"],
        "domain": raw["shop"]["domain"],
        "currency": raw["shop"]["currency"],
        "generated_at_utc": raw["generated_at_utc"],
        "generated_at_local": now_syd.strftime("%a %-d %b %Y, %-I:%M%p AEST").replace("AM", "am").replace("PM", "pm"),
        "today": today.isoformat(),
        "sources": raw.get("sources", {}),
    },
    "alerts": alerts,
    "sales": {
        "today": today_w, "d7": d7, "d30": d30,
        "prev7_revenue": prev7_rev,
        "wow_pct": round((d7["revenue"] - prev7_rev) / prev7_rev * 100, 1) if prev7_rev else None,
        "pending_fulfilment": len(raw["unfulfilled_paid"]),
        "pending_orders": raw["unfulfilled_paid"],
        "abandoned_checkouts": raw["abandoned_checkouts_recent"],
        "customers_total": raw["customers_total"],
        "days_since_order": days_since_order,
        "last_order": {"name": last_order["name"], "when": last_order["local"],
                       "total": last_order["total"], "customer": last_order["customer"]} if last_order else None,
        "daily": daily_sales,
        "recent": [{"name": o["name"], "when": o["local"], "total": o["total"],
                    "customer": o["customer"], "fulfillment": o["fulfillment"],
                    "financial": o["financial"],
                    "items": ", ".join(f"{i['title']}" + (f" ×{i['quantity']}" if i["quantity"] > 1 else "")
                                       for i in o["items"])}
                   for o in orders[:8]],
        "top_sellers": [{"title": t, "units": u} for t, u in top_sellers],
    },
    "ads": {
        "today": ads_today, "d7": ads_7, "prev7": ads_prev7,
        "daily_budget_live": daily_budget_live,
        "active_campaigns": len(active_campaigns),
        "dry_days": dry_days, "dry_spend": money(dry_spend),
        "series": ads_series,
        "campaigns": campaigns,
    },
    "stock": {
        "sellable_skus": len(sellable),
        "tracked_skus": len(tracked),
        "out_of_stock": len(oos),
        "low": len(low),
        "watch": len(watch),
        "healthy": len(healthy),
        "untracked": len(untracked),
        "stock_value": stock_value,
        "oos_list": oos[:20],
        "oos_sellers": oos_sellers,
        "low_list": low + watch,
        "untracked_list": untracked[:20],
    },
    "klaviyo": raw["klaviyo"],
}

json.dump(out, open(OUT, "w"), indent=1, default=str)
print(f"wrote {OUT}: {len(alerts)} alerts, {d7['orders']} orders/7d, "
      f"${ads_7['spend']} ad spend/7d, {len(oos)} OOS")

