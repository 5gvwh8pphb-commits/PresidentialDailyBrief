#!/usr/bin/env python3
"""Build feed/latest.json from RSS sources.

No model involved - pure parsing, so it is free and fast enough to run often.
Items missing a feed image fall back to the article's og:image tag.

Bears sources only run Tue/Wed/Fri/Sat (America/Chicago), and their headlines
are screened for game results because raw RSS cannot respect a spoiler rule.
"""
import concurrent.futures as cf
import datetime as dt
import io
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET

UA = {"User-Agent": "Mozilla/5.0 (compatible; SkynetFeed/1.0; +https://github.com)"}
PER_SOURCE = 6
TOTAL = 40
TIMEOUT = 25
MAX_AGE_DAYS = 3

# Mon=0 .. Sun=6  ->  Tue, Wed, Fri, Sat
BEARS_DAYS = {1, 2, 4, 5}

SOURCES = [
    ("Fox News",         "world",   "https://moxie.foxnews.com/google-publisher/latest.xml"),
    ("CNBC",             "markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("USNI News",        "defense", "https://news.usni.org/feed"),
    ("The War Zone",     "defense", "https://www.twz.com/feed"),
    ("Breaking Defense", "defense", "https://breakingdefense.com/feed/"),
    ("Defense News",     "defense", "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml"),
    ("Ars Technica",     "ai",      "https://feeds.arstechnica.com/arstechnica/index"),
    ("TechCrunch",       "ai",      "https://techcrunch.com/feed/"),
    ("VentureBeat AI",   "ai",      "https://venturebeat.com/category/ai/feed/"),
    ("NWI Times",        "local",   "https://www.nwitimes.com/search/?f=rss&t=article&c=news&l=50&s=start_time&sd=desc"),
    ("CHGO Bears",       "bears",   "https://allchgo.com/bears/feed/"),
    ("Windy City Gridiron", "bears", "https://www.windycitygridiron.com/rss/index.xml"),
    ("ChicagoBears.com", "bears",   "https://www.chicagobears.com/rss/news"),
    ("CBS Sports NFL",   "bears",   "https://www.cbssports.com/rss/headlines/nfl/"),
]

# Headlines that look like game outcomes. Deliberately broad - a dropped
# legitimate story costs less than a spoiled game.
SCORE = re.compile(r"\b\d{1,2}\s*[-–]\s*\d{1,2}\b")
RESULT_WORDS = re.compile(
    r"\b("
    r"beats?|beaten|defeats?|defeated|wins?|winner|won|loss|loses?|lost|"
    r"falls?\s+to|fell\s+to|holds?\s+off|held\s+off|edges?|tops?|topped|"
    r"routs?|upsets?|blowout|comeback|overtime|walk-?off|"
    r"recap|takeaways?|grades?|report\s+card|instant\s+analysis|"
    r"what\s+we\s+learned|final\s+score|highlights?|postgame|post-game|"
    r"snap\s+counts?|stock\s+up|stock\s+down|studs\s+and\s+duds|"
    r"winners?\s+and\s+losers?|film\s+review|scoreboard|standings"
    r")\b", re.I)

MRSS = "{http://search.yahoo.com/mrss/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
ATOM = "{http://www.w3.org/2005/Atom}"


def get(url, cap=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read(cap) if cap else r.read()


def text_of(item, *names):
    for n in names:
        el = item.find(n)
        if el is not None and el.text:
            return el.text.strip()
    return ""


def feed_image(item):
    for tag in (MRSS + "content", MRSS + "thumbnail"):
        el = item.find(tag)
        if el is not None and el.get("url"):
            return el.get("url")
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image"):
        return enc.get("url")
    for tag in (CONTENT + "encoded", "description", ATOM + "content", ATOM + "summary"):
        el = item.find(tag)
        if el is not None and el.text:
            m = re.search(r'<img[^>]+src="([^"]+)"', el.text)
            if m:
                return m.group(1)
    return None


def og_image(url):
    try:
        txt = get(url, cap=250000).decode("utf-8", "ignore")
    except Exception:
        return None
    for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)'):
        m = re.search(pat, txt)
        if m:
            return m.group(1)
    return None


def parse_date(raw):
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(raw)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc)
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def strip_html(s, limit=170):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = re.sub(r"&[a-z]+;|&#\d+;", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def looks_like_result(title):
    return bool(SCORE.search(title) or RESULT_WORDS.search(title))


def pull(name, topic, url):
    out = []
    dropped = 0
    try:
        root = ET.fromstring(get(url))
    except Exception as e:
        print("  FAIL %-20s %s" % (name, str(e)[:58]))
        return out
    items = root.findall(".//item") or root.findall(".//" + ATOM + "entry")
    for it in items[: PER_SOURCE * 4]:
        title = text_of(it, "title", ATOM + "title")
        link = text_of(it, "link", "guid")
        if not link:
            le = it.find(ATOM + "link")
            if le is not None:
                link = le.get("href") or ""
        if not title or not link.startswith("http"):
            continue
        title = strip_html(title, 150)
        if topic == "bears" and looks_like_result(title):
            dropped += 1
            continue
        out.append({
            "title": title,
            "url": link.strip(),
            "source": name,
            "topic": topic,
            "summary": strip_html(text_of(it, "description", ATOM + "summary")),
            "image": feed_image(it),
            "_dt": parse_date(text_of(it, "pubDate", "published",
                                      ATOM + "published", ATOM + "updated")),
        })
        if len(out) >= PER_SOURCE:
            break
    note = "  (%d screened as results)" % dropped if dropped else ""
    print("  ok   %-20s %2d items%s" % (name, len(out), note))
    return out


def humanise(then, now):
    if not then:
        return ""
    mins = int((now - then).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return "%dm ago" % mins
    hrs = mins // 60
    if hrs < 24:
        return "%dh ago" % hrs
    days = hrs // 24
    return "1d ago" if days == 1 else "%dd ago" % days


def main():
    now = dt.datetime.now(dt.timezone.utc)
    # Weekday in Chicago, not UTC - a late-evening run must not roll the day.
    chicago = now - dt.timedelta(hours=5)
    bears_day = chicago.weekday() in BEARS_DAYS
    print("Chicago weekday %d - Bears sources %s"
          % (chicago.weekday(), "INCLUDED" if bears_day else "skipped"))

    active = [s for s in SOURCES if bears_day or s[1] != "bears"]

    items = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for chunk in ex.map(lambda s: pull(*s), active):
            items.extend(chunk)

    items = [i for i in items if i["_dt"] and (now - i["_dt"]).days <= MAX_AGE_DAYS]

    # Round-robin by source so a high-volume publisher cannot crowd out the
    # quieter ones Jim picked on purpose. Newest first within each source.
    buckets = {}
    for i in items:
        buckets.setdefault(i["source"], []).append(i)
    for b in buckets.values():
        b.sort(key=lambda i: i["_dt"], reverse=True)

    order = sorted(buckets, key=lambda s: buckets[s][0]["_dt"], reverse=True)
    picked, round_no = [], 0
    while len(picked) < TOTAL:
        added = False
        for s in order:
            if round_no < len(buckets[s]):
                picked.append(buckets[s][round_no])
                added = True
                if len(picked) >= TOTAL:
                    break
        if not added:
            break
        round_no += 1

    picked.sort(key=lambda i: i["_dt"], reverse=True)
    items = picked

    missing = [i for i in items if not i["image"]]
    if missing:
        print("  fetching og:image for %d items" % len(missing))
        with cf.ThreadPoolExecutor(max_workers=10) as ex:
            for it, img in zip(missing, ex.map(lambda i: og_image(i["url"]), missing)):
                it["image"] = img

    for i in items:
        i["age"] = humanise(i["_dt"], now)
        i["published"] = i["_dt"].isoformat()
        del i["_dt"]

    data = {
        "generated": now.replace(microsecond=0).isoformat(),
        "count": len(items),
        "withImage": sum(1 for i in items if i["image"]),
        "bearsDay": bears_day,
        "sources": sorted({i["source"] for i in items}),
        "items": items,
    }
    if not items:
        print("no items - refusing to overwrite a good feed with an empty one")
        sys.exit(1)
    with io.open("feed/latest.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print("wrote feed/latest.json - %d items, %d with images"
          % (data["count"], data["withImage"]))


if __name__ == "__main__":
    main()
