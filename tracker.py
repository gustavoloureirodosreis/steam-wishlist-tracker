#!/usr/bin/env python3
"""Daily Steam wishlist tracker.

Reads state.json, fetches the wishlist and per-game data from Steam, writes
today.json (the day's changes), updates state.json and renders docs/index.html.
Standard library only.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STEAM_ID = "76561198126128927"
TOP_N = 20
PAUSE = 0.5
CONTROLLER_CATEGORIES = {18, 28, 55, 57, 59}
API_HOSTS = ["https://api.steampowered.com", "https://community.steam-api.com"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
STORE_COOKIES = "birthtime=0; lastagecheckage=1-0-1990; wants_mature_content=1"
TZ = ZoneInfo("America/Sao_Paulo")

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
TODAY_FILE = ROOT / "today.json"
DOCS_DIR = ROOT / "docs"

REVIEW_HISTORY_DAYS = 120
ALERT_LOG_MAX = 300
NEWS_SEEN_MAX = 6000
PAGE_NEWS_DAYS = 30
PAGE_NEWS_MAX = 60


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def fetch(url: str, *, headers: dict | None = None, tries: int = 4) -> bytes:
    hdrs = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    hdrs.update(headers or {})
    last_err: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (400, 403, 404):
                raise
            wait = 30 if e.code == 429 else 2 ** (attempt + 1)
        except Exception as e:  # network errors, timeouts
            last_err = e
            wait = 2 ** (attempt + 1)
        if attempt < tries - 1:
            print(f"  retry {attempt + 1} for {url} in {wait}s ({last_err})", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"giving up on {url}: {last_err}")


def fetch_json(url: str, **kw) -> dict:
    return json.loads(fetch(url, **kw).decode("utf-8"))


def fetch_api(path: str) -> dict:
    """Call a Steam Web API path, falling back to the second host."""
    err: Exception | None = None
    for host in API_HOSTS:
        try:
            return fetch_json(host + path)
        except Exception as e:
            err = e
            print(f"  {host} failed: {e}", file=sys.stderr)
    raise RuntimeError(f"all hosts failed for {path}: {err}")


# --------------------------------------------------------------------------- #
# Steam data
# --------------------------------------------------------------------------- #
def get_wishlist() -> list[dict]:
    data = fetch_api(f"/IWishlistService/GetWishlist/v1/?steamid={STEAM_ID}")
    items = data.get("response", {}).get("items", [])
    # Priority ascending. Priority 0 means "not ranked yet" (fresh adds), which
    # Steam shows at the bottom of the wishlist, so those go last, oldest first.
    def key(x):
        p = x.get("priority", 0) or 0
        return (p == 0, p, x.get("date_added", 0))
    return sorted(items, key=key)


def get_details(appid: int) -> dict | None:
    data = fetch_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=us")
    entry = data.get(str(appid)) or {}
    return entry.get("data") if entry.get("success") else None


def get_news(appid: int) -> list[dict]:
    data = fetch_api(
        f"/ISteamNews/GetNewsForApp/v2/?appid={appid}&count=10&maxlength=0"
        "&feeds=steam_community_announcements"
    )
    return data.get("appnews", {}).get("newsitems", [])


ROW_RE = re.compile(
    r'<a class="user_reviews_summary_row"[^>]*data-tooltip-html="([^"]*)"[^>]*>(.*?)</a>',
    re.S,
)
SUBTITLE_RE = re.compile(r'<div class="subtitle column[^"]*">(.*?)</div>', re.S)
LABEL_RE = re.compile(r'<span class="game_review_summary[^"]*"[^>]*>(.*?)</span>', re.S)
TOOLTIP_RE = re.compile(r"(\d+)% of the ([\d,]+) user reviews")


def get_recent_reviews(appid: int) -> dict | None:
    """Scrape the Recent Reviews block (or the only block for newer games)."""
    page = fetch(
        f"https://store.steampowered.com/app/{appid}/?cc=us&l=english",
        headers={"Cookie": STORE_COOKIES},
    ).decode("utf-8", "replace")
    block_start = page.find('id="userReviews"')
    if block_start < 0:
        return None
    rows = []
    for tooltip, inner in ROW_RE.findall(page[block_start:block_start + 20000]):
        sub = SUBTITLE_RE.search(inner)
        lab = LABEL_RE.search(inner)
        rows.append({
            "subtitle": html.unescape(re.sub(r"\s+", " ", sub.group(1))).strip() if sub else "",
            "label": html.unescape(lab.group(1)).strip() if lab else "",
            "tooltip": html.unescape(tooltip),
        })
    if not rows:
        return None
    row = next((r for r in rows if "recent" in r["subtitle"].lower()), rows[0])
    m = TOOLTIP_RE.search(row["tooltip"])
    return {
        "pct": int(m.group(1)) if m else None,
        "count": int(m.group(2).replace(",", "")) if m else None,
        "label": row["label"] or row["tooltip"],
        "scope": "recent" if "recent" in row["subtitle"].lower() else "all",
    }


def has_controller(d: dict) -> bool:
    if d.get("controller_support"):
        return True
    cats = {c.get("id") for c in d.get("categories", []) or []}
    return bool(cats & CONTROLLER_CATEGORIES)


FIRM_FORMATS = ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%b %d %Y", "%d %b %Y")


def is_firm_date(rd: dict | None) -> bool:
    if not rd:
        return False
    if not rd.get("coming_soon"):
        return True
    s = (rd.get("date") or "").strip()
    for fmt in FIRM_FORMATS:
        try:
            datetime.strptime(s, fmt)
            return True
        except ValueError:
            pass
    return False


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def empty_state() -> dict:
    return {
        "version": 1,
        "last_run": None,
        "wishlist": {},
        "reviews": {},
        "controller_watch": {},
        "release_watch": {},
        "alerts_sent": [],
        "alert_log": [],
        "news_seen": [],
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text())
        base = empty_state()
        base.update(st)
        return base
    return empty_state()


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #
def run() -> None:
    now = datetime.now(timezone.utc)
    today = now.astimezone(TZ).date().isoformat()
    state = load_state()
    baseline = state["last_run"] is None
    last_run_ts = (
        datetime.fromisoformat(state["last_run"]).timestamp() if state["last_run"] else 0
    )
    alerts_sent = set(state["alerts_sent"])
    news_seen = set(state["news_seen"])
    new_alerts: list[dict] = []

    print("Fetching wishlist…")
    wishlist = get_wishlist()
    if not wishlist:
        sys.exit("Wishlist came back empty (private profile or API problem); not touching state.")
    print(f"  {len(wishlist)} games")
    top_ids = [w["appid"] for w in wishlist[:TOP_N]]

    details: dict[int, dict] = {}
    reviews_today: dict[int, dict] = {}
    news_items: list[dict] = []
    new_announcements: list[dict] = []
    failures: list[str] = []

    for i, w in enumerate(wishlist, 1):
        appid = w["appid"]
        print(f"[{i}/{len(wishlist)}] {appid}")
        try:
            d = get_details(appid)
            if d:
                details[appid] = d
        except Exception as e:
            failures.append(f"details {appid}: {e}")
        time.sleep(PAUSE)

        name = (details.get(appid) or {}).get("name") or state["wishlist"].get(str(appid), {}).get("name") or str(appid)

        try:
            for n in get_news(appid):
                gid = str(n["gid"])
                item = {
                    "title": n.get("title", ""),
                    "url": n.get("url", ""),
                    "date": datetime.fromtimestamp(n.get("date", 0), timezone.utc).isoformat(),
                    "appid": appid,
                    "name": name,
                    "_ts": n.get("date", 0),
                }
                news_items.append(item)
                # "Since last run": unseen and posted after the last run (1-day buffer
                # so a just-added game doesn't dump its whole back catalog).
                if not baseline and gid not in news_seen and item["_ts"] > last_run_ts - 86400:
                    new_announcements.append({k: v for k, v in item.items() if not k.startswith("_")})
                news_seen.add(gid)
        except Exception as e:
            failures.append(f"news {appid}: {e}")
        time.sleep(PAUSE)

        if appid in top_ids:
            try:
                r = get_recent_reviews(appid)
                if r:
                    reviews_today[appid] = r
            except Exception as e:
                failures.append(f"reviews {appid}: {e}")
            time.sleep(PAUSE)

    def nm(appid: int) -> str:
        d = details.get(appid)
        if d and d.get("name"):
            return d["name"]
        return state["wishlist"].get(str(appid), {}).get("name") or str(appid)

    # ---- wishlist adds/removals --------------------------------------------
    current = {str(w["appid"]): w for w in wishlist}
    previous = state["wishlist"]
    added = [] if baseline else [
        {"appid": int(a), "name": nm(int(a))} for a in current if a not in previous
    ]
    removed = [] if baseline else [
        {"appid": int(a), "name": previous[a].get("name", a)} for a in previous if a not in current
    ]

    # ---- controller watch ----------------------------------------------------
    controller_alerts = []
    cwatch = state["controller_watch"]
    for appid, d in details.items():
        key = str(appid)
        if has_controller(d):
            if key in cwatch:
                alert_key = f"controller:{appid}"
                if alert_key not in alerts_sent:
                    level = d.get("controller_support") or "listed"
                    a = {"appid": appid, "name": nm(appid), "controller_support": level,
                         "message": f"{nm(appid)} now has controller support ({level})."}
                    controller_alerts.append(a)
                    new_alerts.append({"type": "controller", **a})
                    alerts_sent.add(alert_key)
                del cwatch[key]
        elif key not in cwatch and f"controller:{appid}" not in alerts_sent:
            cwatch[key] = {"name": nm(appid), "since": today}
    for key in list(cwatch):
        if key not in current:
            del cwatch[key]
        else:
            cwatch[key]["name"] = nm(int(key))

    # ---- release-date watch -------------------------------------------------
    release_changes = []
    rwatch = state["release_watch"]
    for appid, d in details.items():
        key = str(appid)
        rd = d.get("release_date") or {}
        date_str = (rd.get("date") or "").strip() or "Unknown"
        firm = is_firm_date(rd)
        if key in rwatch:
            old = rwatch[key]["date"]
            if date_str != old:
                alert_key = f"release:{appid}:{date_str}"
                if alert_key not in alerts_sent:
                    a = {"appid": appid, "name": nm(appid), "old": old, "new": date_str, "firm": firm,
                         "message": f"{nm(appid)}: release date {old!r} → {date_str!r}"
                                    + (" (firm date)" if firm else "")}
                    release_changes.append(a)
                    new_alerts.append({"type": "release", **a})
                    alerts_sent.add(alert_key)
                rwatch[key]["date"] = date_str
            if firm:
                del rwatch[key]
        elif not firm:
            rwatch[key] = {"name": nm(appid), "date": date_str, "since": today}
    for key in list(rwatch):
        if key not in current:
            del rwatch[key]
        else:
            rwatch[key]["name"] = nm(int(key))

    # ---- reviews ---------------------------------------------------------------
    review_changes = []
    cutoff = (now.astimezone(TZ).date() - timedelta(days=REVIEW_HISTORY_DAYS)).isoformat()
    hist = state["reviews"]
    for appid, r in reviews_today.items():
        key = str(appid)
        series = [e for e in hist.get(key, []) if e["date"] != today and e["date"] >= cutoff]
        prev = series[-1] if series else None
        entry = {"date": today, "pct": r["pct"], "count": r["count"], "label": r["label"], "scope": r["scope"]}
        series.append(entry)
        hist[key] = series
        if not baseline and prev and (prev.get("pct") != r["pct"] or prev.get("label") != r["label"]):
            review_changes.append({
                "appid": appid, "name": nm(appid),
                "old_pct": prev.get("pct"), "new_pct": r["pct"],
                "old_label": prev.get("label"), "new_label": r["label"],
                "since": prev["date"],
            })
    for key in list(hist):
        if key not in current:
            del hist[key]

    # ---- alert log --------------------------------------------------------------
    for a in new_alerts:
        state["alert_log"].insert(0, {"date": today, **a})
    state["alert_log"] = state["alert_log"][:ALERT_LOG_MAX]

    # ---- persist state --------------------------------------------------------
    state["wishlist"] = {
        k: {"name": nm(int(k)), "priority": w.get("priority"), "date_added": w.get("date_added")}
        for k, w in current.items()
    }
    state["alerts_sent"] = sorted(alerts_sent)
    state["news_seen"] = sorted(news_seen, key=lambda g: int(g) if g.isdigit() else 0)[-NEWS_SEEN_MAX:]
    state["last_run"] = now.isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")

    new_announcements.sort(key=lambda x: x["date"], reverse=True)
    today_doc = {
        "date": today,
        "generated_at": now.isoformat(timespec="seconds"),
        "baseline": baseline,
        "controller_alerts": controller_alerts,
        "release_date_changes": release_changes,
        "new_announcements": new_announcements,
        "review_changes": review_changes,
        "wishlist_added": added,
        "wishlist_removed": removed,
        "fetch_failures": failures,
    }
    TODAY_FILE.write_text(json.dumps(today_doc, indent=2, ensure_ascii=False) + "\n")

    # ---- page -----------------------------------------------------------------------
    top = []
    for rank, appid in enumerate(top_ids, 1):
        d = details.get(appid) or {}
        top.append({
            "rank": rank, "appid": appid, "name": nm(appid),
            "img": d.get("capsule_image") or d.get("header_image") or "",
            "release": (d.get("release_date") or {}).get("date") or "",
            "review": reviews_today.get(appid),
            "history": hist.get(str(appid), []),
        })
    recent_cut = now.timestamp() - PAGE_NEWS_DAYS * 86400
    dedup = {}
    for n in news_items:
        if n["_ts"] >= recent_cut:
            dedup[(n["appid"], n["url"])] = n
    page_news = sorted(dedup.values(), key=lambda x: x["_ts"], reverse=True)[:PAGE_NEWS_MAX]
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / ".nojekyll").write_text("")
    (DOCS_DIR / "index.html").write_text(render_page(state, today_doc, top, page_news, len(wishlist)))

    print(json.dumps({k: (len(v) if isinstance(v, list) else v) for k, v in today_doc.items()}, indent=2))
    if failures:
        print(f"{len(failures)} fetch failures:", *failures, sep="\n  ", file=sys.stderr)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def store_url(appid) -> str:
    return f"https://store.steampowered.com/app/{appid}/"


def sparkline(history: list[dict]) -> str:
    pts = [e["pct"] for e in history[-30:] if e.get("pct") is not None]
    if not pts:
        return '<span class="muted">—</span>'
    w, h = 96, 26
    lo, hi = min(pts), max(pts)
    if hi - lo < 4:  # keep tiny wiggles from looking dramatic
        mid = (hi + lo) / 2
        lo, hi = mid - 2, mid + 2
    n = len(pts)

    def xy(i, v):
        x = 2 + (w - 4) * (i / (n - 1) if n > 1 else 1)
        y = 2 + (h - 4) * (1 - (v - lo) / (hi - lo))
        return f"{x:.1f},{y:.1f}"

    line = " ".join(xy(i, v) for i, v in enumerate(pts))
    lx, ly = xy(n - 1, pts[-1]).split(",")
    title = f"{n} day(s): {pts[0]}% → {pts[-1]}%"
    return (
        f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{esc(title)}">'
        f"<title>{esc(title)}</title>"
        + (f'<polyline points="{line}" fill="none" stroke="currentColor" stroke-width="1.6" '
           f'stroke-linejoin="round" stroke-linecap="round"/>' if n > 1 else "")
        + f'<circle cx="{lx}" cy="{ly}" r="2.4" fill="currentColor"/></svg>'
    )


def tone(label: str) -> str:
    l = (label or "").lower()
    if "positive" in l:
        return "pos"
    if "negative" in l:
        return "neg"
    if "mixed" in l:
        return "mix"
    return "none"


def fmt_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%b %d, %Y")
    except Exception:
        return iso


def render_page(state, today_doc, top, news, wishlist_size) -> str:
    updated = datetime.fromisoformat(state["last_run"]).astimezone(TZ).strftime("%b %d, %Y · %H:%M")

    # Today
    t = today_doc
    today_items = []
    for a in t["controller_alerts"]:
        today_items.append(f'<li><span class="tag ctl">Controller</span>{esc(a["message"])}</li>')
    for a in t["release_date_changes"]:
        today_items.append(f'<li><span class="tag rel">Release</span>{esc(a["message"])}</li>')
    for r in t["review_changes"]:
        delta = ""
        if r["old_pct"] is not None and r["new_pct"] is not None:
            dd = r["new_pct"] - r["old_pct"]
            delta = f" ({'+' if dd >= 0 else ''}{dd} pts)"
        today_items.append(
            f'<li><span class="tag rev">Reviews</span><a href="{store_url(r["appid"])}">{esc(r["name"])}</a>: '
            f'{esc(r["old_label"])} {esc(r["old_pct"])}% → {esc(r["new_label"])} {esc(r["new_pct"])}%{esc(delta)}</li>'
        )
    for a in t["wishlist_added"]:
        today_items.append(f'<li><span class="tag add">Added</span><a href="{store_url(a["appid"])}">{esc(a["name"])}</a></li>')
    for a in t["wishlist_removed"]:
        today_items.append(f'<li><span class="tag rem">Removed</span>{esc(a["name"])}</li>')
    if t["new_announcements"]:
        today_items.append(f'<li><span class="tag news">News</span>{len(t["new_announcements"])} new announcement(s) — see below</li>')
    if t["baseline"]:
        today_body = '<p class="muted">First run: baseline recorded. Changes will show up here from tomorrow.</p>'
    elif today_items:
        today_body = '<ul class="list">' + "".join(today_items) + "</ul>"
    else:
        today_body = '<p class="muted">Nothing changed since the last run.</p>'

    # Top 20
    cards = []
    for g in top:
        r = g["review"]
        if r and r["pct"] is not None:
            rev = (f'<span class="label {tone(r["label"])}">{esc(r["label"])}</span>'
                   f'<span class="pct">{r["pct"]}%</span>'
                   f'<span class="muted small">{r["count"]:,} {"recent" if r["scope"] == "recent" else "total"}</span>')
        elif r:
            rev = f'<span class="label none">{esc(r["label"])}</span>'
        else:
            rev = '<span class="muted">No review data</span>'
        img = f'<img src="{esc(g["img"])}" alt="" loading="lazy" width="120" height="45">' if g["img"] else '<div class="noimg"></div>'
        cards.append(f"""
      <li class="game">
        <span class="rank">{g["rank"]}</span>
        <a class="thumb" href="{store_url(g["appid"])}">{img}</a>
        <div class="info">
          <a class="name" href="{store_url(g["appid"])}">{esc(g["name"])}</a>
          <div class="meta">{rev}</div>
          <div class="muted small">{esc(g["release"])}</div>
        </div>
        <div class="trend {tone(r["label"]) if r else "none"}">{sparkline(g["history"])}</div>
      </li>""")

    # News
    news_rows = "".join(
        f'<li><a href="{esc(n["url"])}">{esc(n["title"])}</a>'
        f'<div class="muted small">{esc(n["name"])} · {esc(fmt_date(n["date"]))}</div></li>'
        for n in news
    ) or '<li class="muted">No announcements in the last 30 days.</li>'

    def watch_list(watch: dict, show_date: bool) -> str:
        items = sorted(watch.items(), key=lambda kv: kv[1]["name"].lower())
        if not items:
            return '<p class="muted">None.</p>'
        return '<ul class="cols">' + "".join(
            f'<li><a href="{store_url(k)}">{esc(v["name"])}</a>'
            + (f' <span class="muted small">{esc(v["date"])}</span>' if show_date else "")
            + "</li>"
            for k, v in items
        ) + "</ul>"

    log = "".join(
        f'<li><span class="muted small">{esc(a["date"])}</span> '
        f'<span class="tag {"ctl" if a["type"] == "controller" else "rel"}">{"Controller" if a["type"] == "controller" else "Release"}</span>'
        f'{esc(a["message"])}</li>'
        for a in state["alert_log"]
    ) or '<li class="muted">No alerts yet.</li>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Steam Wishlist Tracker</title>
<style>
:root {{
  --bg:#f6f5f1; --card:#ffffff; --ink:#1c1d21; --muted:#6b6e76; --line:#e3e1da;
  --accent:#2d5bd7; --pos:#2f8a4d; --mix:#b07a12; --neg:#c0392b; --none:#8a8d94;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#121317; --card:#1b1d23; --ink:#e9eaee; --muted:#9a9ea8; --line:#2c2f37;
          --accent:#7fa2ff; --pos:#5cc47f; --mix:#e2b04a; --neg:#ff7a6b; --none:#8a8d94; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width:880px; margin:0 auto; padding:20px 16px 48px; }}
header h1 {{ font-size:22px; margin:0 0 2px; letter-spacing:-.01em; }}
a {{ color:var(--accent); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-top:16px; }}
h2 {{ font-size:15px; margin:0 0 10px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); }}
h2 .count {{ font-weight:400; }}
.muted {{ color:var(--muted); }} .small {{ font-size:12.5px; }}
ul {{ margin:0; padding:0; list-style:none; }}
.list li {{ padding:7px 0; border-top:1px solid var(--line); }} .list li:first-child {{ border-top:0; }}
.tag {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 7px; border-radius:999px; margin-right:7px;
  border:1px solid currentColor; vertical-align:1px; }}
.tag.ctl {{ color:var(--pos); }} .tag.rel {{ color:var(--accent); }} .tag.rev {{ color:var(--mix); }}
.tag.add {{ color:var(--pos); }} .tag.rem {{ color:var(--neg); }} .tag.news {{ color:var(--muted); }}
.game {{ display:grid; grid-template-columns:26px 120px 1fr 100px; gap:12px; align-items:center;
  padding:9px 0; border-top:1px solid var(--line); }}
.game:first-child {{ border-top:0; }}
.rank {{ color:var(--muted); font-variant-numeric:tabular-nums; text-align:right; }}
.thumb img, .noimg {{ display:block; width:120px; height:45px; border-radius:5px; object-fit:cover; background:var(--line); }}
.info {{ min-width:0; }}
.name {{ font-weight:600; color:var(--ink); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.meta {{ display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }}
.label {{ font-weight:600; font-size:13px; }}
.label.pos, .trend.pos {{ color:var(--pos); }} .label.mix, .trend.mix {{ color:var(--mix); }}
.label.neg, .trend.neg {{ color:var(--neg); }} .label.none, .trend.none {{ color:var(--none); }}
.pct {{ font-variant-numeric:tabular-nums; }}
.trend {{ justify-self:end; }}
.spark {{ display:block; }}
.cols {{ columns:2 220px; column-gap:24px; }}
.cols li {{ padding:3px 0; break-inside:avoid; }}
@media (max-width:560px) {{
  main {{ padding:16px 12px 40px; }}
  section {{ padding:12px; }}
  .game {{ grid-template-columns:18px minmax(0,1fr) 64px; gap:8px; grid-template-areas:"rank info trend"; }}
  .thumb {{ display:none; }}
  .rank {{ grid-area:rank; }} .info {{ grid-area:info; }} .trend {{ grid-area:trend; }}
  .name {{ white-space:normal; overflow-wrap:anywhere; }}
  .spark {{ width:64px; height:auto; }}
  .cols {{ columns:1; }}
}}
</style>
</head>
<body>
<main>
  <header>
    <h1>Steam Wishlist Tracker</h1>
    <div class="muted small">Updated {esc(updated)} (São Paulo) · {wishlist_size} games on the wishlist</div>
  </header>

  <section><h2>Today</h2>{today_body}</section>

  <section><h2>Top {len(top)} by priority</h2><ul>{"".join(cards)}</ul>
    <p class="muted small" style="margin:10px 0 0">Recent Reviews (last 30 days) from the store page; sparkline shows the last 30 runs.</p>
  </section>

  <section><h2>Announcements <span class="count">· last {PAGE_NEWS_DAYS} days</span></h2><ul class="list">{news_rows}</ul></section>

  <section><h2>No controller support yet <span class="count">· {len(state["controller_watch"])}</span></h2>
    {watch_list(state["controller_watch"], False)}</section>

  <section><h2>No firm release date <span class="count">· {len(state["release_watch"])}</span></h2>
    {watch_list(state["release_watch"], True)}</section>

  <section><h2>Alert log</h2><ul class="list">{log}</ul></section>
</main>
</body>
</html>
"""


if __name__ == "__main__":
    run()
