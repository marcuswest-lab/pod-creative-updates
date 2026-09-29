"""Daily Swipe File refresh via Apify (runs from .github/workflows/refresh-swipe.yml).

Modes (argv[1] or SWIPE_MODE): links | discover | all (default).
1. links — refresh media links for every ad already in swipe/swipe.json. Meta's
   video/thumbnail URLs expire after ~4-5 days; only media fields change,
   curated fields (format, tags, breakdown, iterate) are never touched.
2. discover — run each niche's saved Ad Library searches (top by impressions)
   and add ads we don't have yet as source="auto" (unreviewed). Auto ads that
   stop showing up for 14 days are dropped; each niche keeps at most auto_max.

Nothing is downloaded -- the dashboard streams videos from Meta's CDN.

Env: APIFY_TOKEN (required, repo secret) · APIFY_ACTOR (default apify~facebook-ads-scraper)
"""
import base64
import datetime
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PATH = os.path.join(os.path.dirname(__file__), "..", "swipe", "swipe.json")
API = "https://api.apify.com/v2"
TOKEN = os.environ.get("APIFY_TOKEN")
ACTOR = os.environ.get("APIFY_ACTOR", "apify~facebook-ads-scraper")
TODAY = datetime.date.today().isoformat()
STALE_DAYS = 14
PER_SEARCH = 30
PER_ADVERTISER = 2  # keep the auto list diverse (one firm can run dozens of near-identical ads)


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def norm(k):
    return k.replace("_", "").lower()


def find(obj, keys):
    """First non-empty value for any of `keys` (normalized) anywhere in obj, breadth-first."""
    keys = {norm(k) for k in keys}
    queue = [obj]
    while queue:
        o = queue.pop(0)
        if isinstance(o, dict):
            for k, v in o.items():
                if norm(k) in keys and v not in (None, "", [], {}):
                    return v
            queue.extend(o.values())
        elif isinstance(o, list):
            queue.extend(o)
    return None


def run_actor(urls, limit):
    run = call("POST", f"{API}/acts/{ACTOR}/runs",
               {"startUrls": [{"url": u} for u in urls], "resultsLimit": limit})["data"]
    for _ in range(90):  # up to ~15 min
        st = call("GET", f"{API}/actor-runs/{run['id']}")["data"]
        if st["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(10)
    if st["status"] != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run['id']} ended {st['status']}")
    return call("GET", f"{API}/datasets/{st['defaultDatasetId']}/items?clean=true&format=json")


def ad_id(it):
    v = find(it, ["adArchiveID", "ad_archive_id", "adArchiveId"])
    return str(v) if v is not None else None


def asset_key(url):
    """Stable id of the underlying video/image, so a refresh keeps the SAME version of an ad
    (advertisers run several versions under one ad id; the actor lists them in any order)."""
    if not url:
        return None
    m = re.search(r"efg=([^&]+)", url)
    if m:
        try:
            raw = urllib.parse.unquote(m.group(1))
            raw += "=" * (-len(raw) % 4)
            v = json.loads(base64.urlsafe_b64decode(raw)).get("xpv_asset_id")
            if v:
                return f"v{v}"
        except Exception:
            pass
    m = re.search(r"/(\d+_\d+_\d+)_[nt]\.", url)
    return f"i{m.group(1)}" if m else None


def media_options(it):
    """Every (video, image, poster) version in an actor item, in the order found."""
    opts, queue = [], [it]
    while queue:
        o = queue.pop(0)
        if isinstance(o, dict):
            keys = {norm(k): v for k, v in o.items()}
            v = keys.get("videohdurl") or keys.get("videosdurl")
            img = keys.get("originalimageurl") or keys.get("resizedimageurl")
            if v or img:
                opts.append((v, None if v else img, keys.get("videopreviewimageurl") or img))
            queue.extend(o.values())
        elif isinstance(o, list):
            queue.extend(o)
    return opts


def media(it, want=None):
    opts = media_options(it)
    if want:
        for v, img, poster in opts:
            if asset_key(v or img) == want:
                return v, img, poster
        return None, None, None  # our version isn't in this result -- keep the old links
    return opts[0] if opts else (None, None, None)


def text_of(it):
    for key in (["body"], ["title"], ["link_description", "caption"]):
        b = find(it, key)
        if isinstance(b, dict):
            b = b.get("text")
        if isinstance(b, str) and b.strip() and "{{" not in b:  # skip catalog placeholders like {{product.brand}}
            return b
    return ""


def started(it):
    s = find(it, ["start_date", "startDate"])
    return datetime.datetime.utcfromtimestamp(s).date().isoformat() if isinstance(s, (int, float)) else None


def apply_media(a, it):
    video, image, poster = media(it, a.get("asset"))
    if not (video or image):
        return False
    a.update(video=video, image=image, poster=poster, asset=asset_key(video or image))
    act = find(it, ["is_active", "isActive"])
    if isinstance(act, bool):
        a["active"] = act
    s = started(it)
    if s:
        a["started"] = s
    return True


def main():
    if not TOKEN:
        sys.exit("APIFY_TOKEN is not set")
    data = json.load(open(PATH))
    ads = data["ads"]
    by_id = {a["id"]: a for a in ads}

    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SWIPE_MODE", "all")).lower()
    if mode not in ("links", "discover", "all"):
        sys.exit(f"unknown mode {mode!r} (use links | discover | all)")
    print(f"Mode: {mode}")
    refreshed = added = 0

    # 1. Refresh links for ads we already have (every 3 days — links expire after ~4-5 days).
    if mode in ("links", "all"):
        refreshed = refresh_links(by_id)

    # 2. Discover new top ads per niche (weekly, before the Friday review).
    if mode in ("discover", "all"):
        added = discover(data, ads, by_id)

    if mode in ("links", "all") and not refreshed:
        sys.exit("No links refreshed -- check the actor output format")
    data["media_refreshed"] = TODAY if refreshed else data.get("media_refreshed")
    json.dump(data, open(PATH, "w"), indent=1, ensure_ascii=False)


def refresh_links(by_id):
    items = run_actor([f"https://www.facebook.com/ads/library/?id={i}" for i in by_id], max(50, len(by_id) * 2))
    got = {}
    for it in items:
        i = ad_id(it)
        if i:
            got.setdefault(i, it)
    refreshed = sum(1 for i, a in by_id.items() if i in got and apply_media(a, got[i]))
    for i, a in by_id.items():
        if i not in got:
            print(f"  kept old links: {i} {a.get('advertiser')}")
    print(f"Refreshed {refreshed}/{len(by_id)} ads")
    return refreshed


def discover(data, ads, by_id):
    added = 0
    for n in data.get("niches", []):
        if not n.get("searches"):
            continue
        try:
            found = run_actor(n["searches"], PER_SEARCH * len(n["searches"]))
        except Exception as e:  # discovery failing must not block the link refresh
            print(f"  discovery failed for {n['key']}: {e}")
            continue
        words = [w.lower() for w in n.get("must_include", [])]
        skip_pages = {p.lower() for p in n.get("exclude_pages", [])}
        skip_ids = {str(x) for x in n.get("exclude_ids", [])}  # retired by the Friday review
        for it in found:
            i = ad_id(it)
            page = str(find(it, ["page_name", "pageName"]) or "")
            body = text_of(it)
            if not i or page.lower() in skip_pages or i in skip_ids:
                continue
            if words and not any(w in (body + " " + page).lower() for w in words):
                continue  # keyword search also returns unrelated apps/dramas
            if i in by_id:
                if by_id[i].get("source") == "auto":
                    by_id[i]["last_seen"] = TODAY
                continue
            a = {"id": i, "niche": n["key"], "source": "auto", "advertiser": page, "first_seen": TODAY,
                 "last_seen": TODAY, "body": body[:300], "tags": [], "active": True}
            if apply_media(a, it):
                ads.append(a)
                by_id[i] = a
                added += 1
        # prune stale auto ads, then cap per niche (keep most recently seen)
        cutoff = (datetime.date.today() - datetime.timedelta(days=STALE_DAYS)).isoformat()
        auto = [a for a in ads if a["niche"] == n["key"] and a.get("source") == "auto"]
        keep = sorted([a for a in auto if a.get("last_seen", "") >= cutoff],
                      key=lambda a: a.get("last_seen", ""), reverse=True)[: n.get("auto_max", 24)]
        per_page, capped = {}, []
        for a in keep:
            k = a.get("advertiser", "").lower()
            per_page[k] = per_page.get(k, 0) + 1
            if per_page[k] <= PER_ADVERTISER:
                capped.append(a)
        keep = capped[: n.get("auto_max", 24)]
        drop = {id(a) for a in auto} - {id(a) for a in keep}
        ads[:] = [a for a in ads if id(a) not in drop]
        print(f"  {n['key']}: {len(keep)} auto-pulled ads kept")
    added = sum(1 for a in ads if a.get("source") == "auto" and a.get("first_seen") == TODAY)
    print(f"New today (after caps): {added}")
    return added


if __name__ == "__main__":
    main()
