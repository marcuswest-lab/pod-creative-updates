"""Daily Swipe File refresh via Apify (runs from .github/workflows/refresh-swipe.yml).

1. Refresh media links for every ad already in swipe/swipe.json. Meta's
   video/thumbnail URLs expire after ~4-5 days; only media fields change,
   curated fields (format, tags, breakdown, iterate) are never touched.
2. Discover: run each niche's saved Ad Library searches (top by impressions)
   and add ads we don't have yet as source="auto" (unreviewed). Auto ads that
   stop showing up for 14 days are dropped; each niche keeps at most auto_max.

Nothing is downloaded -- the dashboard streams videos from Meta's CDN.

Env: APIFY_TOKEN (required, repo secret) · APIFY_ACTOR (default apify~facebook-ads-scraper)
"""
import datetime
import json
import os
import sys
import time
import urllib.request

PATH = os.path.join(os.path.dirname(__file__), "..", "swipe", "swipe.json")
API = "https://api.apify.com/v2"
TOKEN = os.environ.get("APIFY_TOKEN")
ACTOR = os.environ.get("APIFY_ACTOR", "apify~facebook-ads-scraper")
TODAY = datetime.date.today().isoformat()
STALE_DAYS = 14
PER_SEARCH = 30


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


def media(it):
    video = find(it, ["video_hd_url", "video_sd_url"])
    image = None if video else find(it, ["original_image_url", "resized_image_url"])
    return video, image, find(it, ["video_preview_image_url"]) or image


def text_of(it):
    b = find(it, ["body"])
    if isinstance(b, dict):
        b = b.get("text")
    return b if isinstance(b, str) else ""


def started(it):
    s = find(it, ["start_date", "startDate"])
    return datetime.datetime.utcfromtimestamp(s).date().isoformat() if isinstance(s, (int, float)) else None


def apply_media(a, it):
    video, image, poster = media(it)
    if not (video or image):
        return False
    a.update(video=video, image=image, poster=poster)
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

    # 1. Refresh links for ads we already have.
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

    # 2. Discover new top ads per niche.
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
        for it in found:
            i = ad_id(it)
            page = str(find(it, ["page_name", "pageName"]) or "")
            body = text_of(it)
            if not i or page.lower() in skip_pages:
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
        drop = {id(a) for a in auto} - {id(a) for a in keep}
        ads[:] = [a for a in ads if id(a) not in drop]
    print(f"Added {added} new auto-pulled ads")

    if not refreshed and not added:
        sys.exit("Nothing refreshed or added -- check the actor output format")
    data["media_refreshed"] = TODAY
    json.dump(data, open(PATH, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
