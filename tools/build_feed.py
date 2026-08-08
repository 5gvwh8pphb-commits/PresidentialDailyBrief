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

# Mon=0 .. Sun=6
DAY_GATE = {
    "bears":  {1, 2, 4, 5},        # Tue, Wed, Fri, Sat
    "nascar": {0, 1, 2, 3, 4},     # all weekdays
}

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
    # General Cup feeds almost never put Byron in a headline (1 hit in 80), so
    # NASCAR comes from a targeted query instead. It still surfaces Frontstretch
    # and Motorsport.com whenever they actually write about him.
    ("Byron & Hendrick", "nascar",
     "https://news.google.com/rss/search?q=%22William+Byron%22+OR+%22Hendrick+Motorsports%22"
     "+NASCAR&hl=en-US&gl=US&ceid=US:en"),
]

# NASCAR is Byron/Hendrick only - the rest of the Cup field is noise to Jim.
BYRON = re.compile(r"\b(byron|hendrick)\b|\bno\.?\s*24\b|#24\b", re.I)

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

# Motorsport outcomes use a different vocabulary than football.
NASCAR_RESULT = re.compile(
    r"\b("
    r"wins?|winner|won|victory|victory\s+lane|checkered|chequered|"
    r"sweeps?|swept|dominates?|dominated|holds?\s+off|held\s+off|"
    r"podium|finishe?[sd]?|results?|recap|standings|points\s+lead|"
    r"pole|qualifying|starting\s+lineup|stage\s+(?:win|one|two)|"
    r"playoff\s+picture|eliminat\w*|advanc\w*|cutline|"
    r"top-?(?:5|10|five|ten)|p\d\b|\d+(?:st|nd|rd|th)-place|"
    r"crash(?:es|ed)?|wreck(?:s|ed)?|dnf|penali[sz]ed|disqualified|"
    r"trophy|celebrat\w*|power\s+rankings?|moves?\s+up|leads?|"
    r"champion\w*|clinch\w*|streak"
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


def looks_like_result(title, topic):
    if topic == "nascar":
        return bool(SCORE.search(title) or NASCAR_RESULT.search(title))
    return bool(SCORE.search(title) or RESULT_WORDS.search(title))


def pull(name, topic, url):
    out = []
    dropped = 0
    offtopic = 0
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
        # Google News titles carry the publisher as a " - Publisher" suffix.
        item_source = name
        if "news.google.com" in url and " - " in title:
            head, _, pub = title.rpartition(" - ")
            if head and 2 < len(pub) <= 34:
                title, item_source = head.strip(), pub.strip()
        if topic == "nascar" and not BYRON.search(title):
            offtopic += 1
            continue
        if topic in ("bears", "nascar") and looks_like_result(title, topic):
            dropped += 1
            continue
        # Summaries leak outcomes even when the headline does not, and they
        # only ever render on the lead card. Not worth the risk.
        summary = "" if topic in ("bears", "nascar") else \
            strip_html(text_of(it, "description", ATOM + "summary"))
        out.append({
            "title": title,
            "url": link.strip(),
            "source": item_source,
            "group": name,
            "topic": topic,
            "summary": summary,
            "image": feed_image(it),
            "_dt": parse_date(text_of(it, "pubDate", "published",
                                      ATOM + "published", ATOM + "updated")),
        })
        if len(out) >= PER_SOURCE:
            break
    bits = []
    if dropped:
        bits.append("%d screened as results" % dropped)
    if offtopic:
        bits.append("%d not Byron/Hendrick" % offtopic)
    note = "  (%s)" % ", ".join(bits) if bits else ""
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
    wd = chicago.weekday()
    gated = {t: (wd in days) for t, days in DAY_GATE.items()}
    print("Chicago weekday %d - %s" % (wd, ", ".join(
        "%s %s" % (t, "INCLUDED" if ok else "skipped") for t, ok in sorted(gated.items()))))

    active = [s for s in SOURCES if gated.get(s[1], True)]

    items = []
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for chunk in ex.map(lambda s: pull(*s), active):
            items.extend(chunk)

    items = [i for i in items if i["_dt"] and (now - i["_dt"]).days <= MAX_AGE_DAYS]

    # Drop repeats - query feeds and wire pickups produce the same story twice.
    seen, unique = set(), []
    for i in items:
        key = re.sub(r"[^a-z0-9]+", "", i["title"].lower())[:70]
        if key in seen:
            continue
        seen.add(key)
        unique.append(i)
    if len(unique) != len(items):
        print("  deduped %d repeats" % (len(items) - len(unique)))
    items = unique

    # Round-robin by configured source so a high-volume publisher cannot crowd
    # out the quieter ones Jim picked on purpose. Newest first within each.
    buckets = {}
    for i in items:
        buckets.setdefault(i.get("group") or i["source"], []).append(i)
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
        "bearsDay": gated.get("bears", False),
        "nascarDay": gated.get("nascar", False),
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
