"""Refresh the Swipe File's media links from the Meta Ad Library via Apify.

Meta's video/thumbnail URLs expire after ~4-5 days, so this runs daily
(.github/workflows/refresh-swipe.yml). It only rewrites the media fields
(video / poster / image / active / started) of ads already in swipe/swipe.json;
the curated fields (tags, breakdown, iterate) are never touched. Nothing is
downloaded -- the dashboard streams the videos straight from Meta's CDN.

Env:
  APIFY_TOKEN  (required) -- repo secret, never committed
  APIFY_ACTOR  (optional) -- default "apify~facebook-ads-scraper"
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


def call(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def norm(k):
    return k.replace("_", "").lower()


def find(obj, keys):
    """First non-empty value for any of `keys` (normalized) anywhere in obj."""
    keys = {norm(k) for k in keys}
    stack = [obj]
    while stack:
        o = stack.pop(0)
        if isinstance(o, dict):
            for k, v in o.items():
                if norm(k) in keys and v not in (None, "", [], {}):
                    return v
            stack.extend(o.values())
        elif isinstance(o, list):
            stack.extend(o)
    return None


def run_actor(ids):
    inp = {"startUrls": [{"url": f"https://www.facebook.com/ads/library/?id={i}"} for i in ids],
           "resultsLimit": max(50, len(ids) * 2)}
    run = call("POST", f"{API}/acts/{ACTOR}/runs", inp)["data"]
    for _ in range(90):  # up to ~15 min
        st = call("GET", f"{API}/actor-runs/{run['id']}")["data"]
        if st["status"] in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        time.sleep(10)
    if st["status"] != "SUCCEEDED":
        sys.exit(f"Apify run {run['id']} ended {st['status']}")
    return call("GET", f"{API}/datasets/{st['defaultDatasetId']}/items?clean=true&format=json")


def main():
    if not TOKEN:
        sys.exit("APIFY_TOKEN is not set")
    data = json.load(open(PATH))
    ids = [a["id"] for a in data["ads"]]
    items = run_actor(ids)
    by_id = {}
    for it in items:
        aid = find(it, ["adArchiveID", "ad_archive_id", "adArchiveId"])
        if aid is not None:
            by_id.setdefault(str(aid), it)

    updated = 0
    for a in data["ads"]:
        it = by_id.get(a["id"])
        if not it:
            print(f"  {a['id']} {a['advertiser']}: not returned (kept old links)")
            continue
        video = find(it, ["video_hd_url", "video_sd_url"])
        image = None if video else find(it, ["original_image_url", "resized_image_url"])
        poster = find(it, ["video_preview_image_url"]) or image
        if not (video or image):
            print(f"  {a['id']} {a['advertiser']}: no media in result (kept old links)")
            continue
        a.update(video=video, image=image, poster=poster)
        active = find(it, ["is_active", "isActive"])
        if isinstance(active, bool):
            a["active"] = active
        start = find(it, ["start_date", "startDate"])
        if isinstance(start, (int, float)):
            a["started"] = datetime.datetime.utcfromtimestamp(start).date().isoformat()
        updated += 1

    print(f"Refreshed {updated}/{len(ids)} ads")
    if not updated:
        sys.exit("No ads refreshed -- check the actor output format")
    data["media_refreshed"] = datetime.date.today().isoformat()
    json.dump(data, open(PATH, "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
