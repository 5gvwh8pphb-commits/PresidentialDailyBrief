"""Board agendas and minutes: alert on admin-building personnel actions, with a link.

Called from tools/watch.py. Nothing is archived - only the IDs of documents
already read (watch/board_seen.json), so the same document is never read twice
and the same hire is never alerted twice.

Sources (per district, "board" in watch/districts.json):
  {"kind": "boarddocs", "site": "duneland"}   BoardDocs: every meeting dated within
        the last LOOKBACK_DAYS or in the future (agendas appear days before the vote).
        Reads the detailed agenda text plus attached PDFs that look like personnel
        reports or minutes.
  {"kind": "pages", "url": "...", "all": true} A district page that links agenda/minutes
        PDFs. New links are read; on the first run only the top FIRST_RUN_DOCS. Apptegy
        pages (links hidden in the page's JSON, 5il.co short links) work too. "all" takes
        every document link on the page - for a board page whose links are only dates.
  {"kind": "drive", "folder": "<id>"}         A public Google Drive folder of minutes. Year
        sub-folders are followed (newest two). PDF, Word .docx and old .doc are read.
  {"kind": "diligent", "host": "..."}         Diligent Community portal (...diligentoneplatform.com):
        the agenda text of each recent meeting plus its personnel/minutes attachments.
  {"kind": "icboard", "host": "..."}          ElectronicSchoolBoard (...ic-board.com): same.

The first time a district is read, its alerts are marked "catchup" - they may be weeks
old, and must not look like this week's news.
"""
import datetime as dt
import html
import http.cookiejar
import io
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

LOOKBACK_DAYS = 45
FIRST_RUN_DOCS = 3
MAX_PDF_PAGES = 40
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
DOC_NAME = re.compile(r"personnel|employ|hire|hiring|resign|retire|appoint|recommend|minutes|staff|human res|agenda", re.I)
# Attachments worth opening only for the vendor scan (insurance / benefits items).
VENDOR_DOC = re.compile(r"insur|benefit|aflac|fidelity|steele|colonial|cafeteria|section.?125|voluntary|supplemental|renewal", re.I)
# Jim's special alert: any mention of these, anywhere in an agenda or minutes. Plain
# text search over every page - not left to the model, so a mention cannot be missed.
VENDORS = [
    ("Aflac", re.compile(r"\baflac\b", re.I)),
    ("American Fidelity", re.compile(r"american\s+fidelity", re.I)),
    ("Steele Benefits", re.compile(r"\bsteele\s+benefit", re.I)),
    ("Colonial", re.compile(r"\bcolonial\b(?!\s+(elementary|school|middle|high|park|heights|drive|dr\b|street|st\b|avenue|ave\b|days|village))", re.I)),
]

PROMPT = """Below is text from {district} school board documents ({doc}). Lines starting
"[page N]" mark the PDF page the text after them came from.

Find every personnel action for a central-office administrative or business role:
superintendent (any level), CFO or chief financial officer, treasurer / deputy /
assistant treasurer, business manager or director of business services, payroll,
accounts payable, human resources, benefits or insurance, and similar district
finance or HR jobs. Actions: hired, appointed, employed, promoted, resigned, retired,
terminated, placed on leave, interim appointment, contract approved, search opened.

Ignore teachers, principals, assistant principals, coaches, aides, substitutes,
board members, secretaries and administrative assistants.

Copy names exactly as written. Never guess. If there is nothing, answer [].
Answer with ONLY a JSON array:
[{{"name": "...", "role": "...", "action": "Hired|Resigned|Retired|Appointed|Promoted|Interim|Contract|Search opened|Other",
  "effective": "date as written, or empty", "page": <PDF page number or null>}}]"""


# BoardDocs refuses (403) a burst of requests from a data-centre address, which is
# what a GitHub runner is. First live run 2026-10-04: three districts read, then 403 for
# the rest. So: keep cookies like a browser, pace requests, and back off on a refusal.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_LAST = {}
PACE_SECONDS = 3.0
BACKOFF = (30, 90, 180)


def _req(url, data=None, referer=None):
    host = urllib.parse.urlsplit(url).netloc
    for attempt in range(len(BACKOFF) + 1):
        wait = PACE_SECONDS - (time.time() - _LAST.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        headers = {"User-Agent": UA, "Accept": "text/html,application/json,application/pdf,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9"}
        if data:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["X-Requested-With"] = "XMLHttpRequest"
        if referer:
            headers["Referer"] = referer
        r = urllib.request.Request(url, data=data.encode() if data else None, headers=headers)
        try:
            _LAST[host] = time.time()
            with _OPENER.open(r, timeout=40) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 429, 503) and attempt < len(BACKOFF):
                print(f"  .. {host} said {e.code}; waiting {BACKOFF[attempt]}s", flush=True)
                time.sleep(BACKOFF[attempt])
                continue
            raise
        except (TimeoutError, urllib.error.URLError) as e:
            if attempt < len(BACKOFF):
                print(f"  .. {host} timed out ({e}); retrying", flush=True)
                time.sleep(10)
                continue
            raise


def _text(b):
    b = re.sub(r"<(script|style)\b.*?</\1>", " ", b, flags=re.S | re.I)
    b = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d|td)>", "\n", b, flags=re.I)
    return re.sub(r"[ \t ]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", b)))


def pdf_pages(raw):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(raw))
    return [(i + 1, (p.extract_text() or "")) for i, p in enumerate(r.pages[:MAX_PDF_PAGES])]


# ---------- BoardDocs ----------

def _bd_base(site):
    return f"https://go.boarddocs.com/in/{site}/Board.nsf"


def boarddocs_docs(site, seen, today):
    base = _bd_base(site)
    page = _req(base + "/Public").decode("utf-8", "replace")
    m = re.search(r'committee[_-]?id["\']?\s*[:=]\s*["\']?([A-Z0-9]{8,})', page, re.I)
    if not m:
        raise RuntimeError("BoardDocs committee not found")
    cid = m.group(1)
    ref = base + "/Public"
    meetings = json.loads(_req(base + "/BD-GetMeetingsList?open", "current_committee_id=" + cid, ref) or b"[]")
    cutoff = (today - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    docs = []
    for mt in meetings:
        nd = mt.get("numberdate", "")
        if not nd or nd < cutoff:
            continue
        b = _req(base + f"/PRINT-AgendaDetailed?open&id={mt['unique']}&current_committee_id={cid}", referer=ref).decode("utf-8", "replace")
        when = f"{nd[:4]}-{nd[4:6]}-{nd[6:]}"
        future = nd > today.strftime("%Y%m%d")
        link = f"{base}/goto?open&id={mt['unique']}"
        # The agenda text itself (changes as the district edits it - keyed on its content).
        txt = _text(b)
        docs.append({"id": f"bd:{site}:{mt['unique']}:{_h(txt)}", "kind": "agenda" if future else "agenda/minutes",
                     "meeting": when, "title": mt.get("name", ""), "url": link, "text": txt, "people": True})
        for f in sorted(set(re.findall(r'(/in/[^"\']*?/files/[^"\']+?\.pdf)', b, re.I))):
            name = urllib.parse.unquote(f.rsplit("/", 1)[-1])
            people = bool(DOC_NAME.search(name))
            if not (people or VENDOR_DOC.search(name)):
                continue
            docs.append({"id": f"bd:{site}:{f}", "kind": "minutes" if "minute" in name.lower() else
                         ("personnel report" if people else "attachment"), "people": people,
                         "meeting": when, "title": name, "url": "https://go.boarddocs.com" + f, "pdf": True, "ref": ref})
    return [d for d in docs if d["id"] not in seen]


def _h(s):
    import hashlib
    return hashlib.sha256(s.encode()).hexdigest()[:10]


# ---------- plain pages of PDF links ----------

DOC_LINK = re.compile(r"\.pdf($|\?)|resource-manager/view|//5il\.co/|thrillshare\.com/documents/\d|"
                      r"drive\.google\.com/file/d/", re.I)


def _unjson(b):
    """Apptegy pages carry their content as escaped HTML inside a JSON blob."""
    bs = chr(92)
    b = re.sub(re.escape(bs) + r"u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), b)
    for _ in range(3):
        b = b.replace(bs + '"', '"').replace(bs + "/", "/")
    return b


def _drive_fetch(url):
    m = re.search(r"/file/d/([\w-]+)", url)
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else None


def page_docs(url, seen, first_run, take_all=False):
    b = _unjson(_req(url).decode("utf-8", "replace"))
    out = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S | re.I):
        full = urllib.parse.quote(urllib.parse.urljoin(url, html.unescape(href).strip()), safe=":/?&=%#~+,;@")
        label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", label))).strip()
        name = urllib.parse.unquote(full.rsplit("/", 1)[-1])
        if not DOC_LINK.search(full):
            continue
        if not (take_all or DOC_NAME.search(label) or DOC_NAME.search(name)):
            continue
        if any(d["url"] == full for d in out):
            continue
        out.append({"id": "pg:" + full, "kind": "minutes" if re.search("minute", label + name, re.I) else
                    ("agenda" if re.search("agenda", label + name, re.I) else "agenda/minutes"),
                    "meeting": "", "title": label or name, "url": full, "fetch": _drive_fetch(full),
                    "pdf": True, "people": True})
    for d in out:
        n = _newest(d)
        if n % 10000 and n % 100:
            d["meeting"] = f"{n // 10000}-{n // 100 % 100:02d}-{n % 100:02d}"
    # Newest first: the latest date named in the link text (else the file name), page order
    # breaking ties. The URL itself is not trusted - a Finalsite link is a hex ID whose digits
    # can look like a year (Lake Station, 2026-10-05).
    return _first_run_cut(out, seen, first_run)


MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _newest(d):
    """Sortable yyyymmdd for a document: the latest date in its title, else its file name."""
    for s in (d["title"], urllib.parse.unquote(d["url"].rsplit("/", 1)[-1])):
        found = []
        for m, day, y in re.findall(r"(?<!\d)(\d{1,2})[-./_](\d{1,2})[-./_](\d{4}|\d{2})(?!\d)", s):
            found.append((int(y) + (2000 if len(y) == 2 else 0)) * 10000 + int(m) * 100 + int(day))
        for y, m, day in re.findall(r"(?<!\d)(20[0-4]\d)[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)", s):
            found.append(int(y) * 10000 + int(m) * 100 + int(day))
        for mon, day, y in re.findall(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})?,?\s*(20[0-4]\d)\b", s, re.I):
            if mon.lower() in MONTHS:
                found.append(int(y) * 10000 + (MONTHS.index(mon.lower()) + 1) * 100 + int(day or 0))
        found += [int(y) * 10000 for y in re.findall(r"(?<!\d)(20[0-4]\d)(?!\d)", s)]
        if found:
            return max(found)
    return 0


def _first_run_cut(out, seen, first_run):
    out = [d for _, d in sorted(enumerate(out), key=lambda x: (-_newest(x[1]), x[0]))]
    if first_run:
        return out[:FIRST_RUN_DOCS], [d["id"] for d in out[FIRST_RUN_DOCS:]]
    return [d for d in out if d["id"] not in seen], []


# ---------- public Google Drive folder ----------

def _drive_list(folder):
    b = _req(f"https://drive.google.com/embeddedfolderview?id={folder}").decode("utf-8", "replace")
    return [(html.unescape(t).strip(), h) for h, t in
            re.findall(r'<a href="([^"]+)"[^>]*>.*?flip-entry-title">([^<]*)<', b, re.S)]


def drive_docs(folder, seen, first_run):
    entries = _drive_list(folder)
    # Year sub-folders (Lake Ridge: 2021 .. 2026) - follow the newest two.
    years = sorted(((int(y.group()), h) for t, h in entries if "/folders/" in h
                    for y in [re.search(r"20[0-4]\d", t)] if y), reverse=True)[:2]
    for _, h in years:
        entries += _drive_list(re.search(r"/folders/([\w-]+)", h).group(1))
    out = []
    for title, h in entries:
        if "/file/d/" not in h or not re.search(r"\.(pdf|docx?)$", title, re.I):
            continue
        out.append({"id": "gd:" + re.search(r"/file/d/([\w-]+)", h).group(1),
                    "kind": "minutes" if "minute" in title.lower() else "agenda/minutes",
                    "meeting": "", "title": title, "url": h.split("?")[0], "fetch": _drive_fetch(h),
                    "pdf": True, "people": True})
    for d in out:
        n = _newest(d)
        if n % 10000 and n % 100:
            d["meeting"] = f"{n // 10000}-{n // 100 % 100:02d}-{n % 100:02d}"
    return _first_run_cut(out, seen, first_run)


# ---------- Diligent Community / ElectronicSchoolBoard ----------

# Agenda items worth opening on item-by-item portals. Narrower than DOC_NAME: "recommend"
# alone would open every donation and field trip.
PEOPLE_ITEM = re.compile(r"personnel|employ|hire|hiring|resign|retire|appoint|administrative assign|"
                         r"minutes|staff|human res|superintendent|contract", re.I)


def _attach_wanted(context):
    people = bool(PEOPLE_ITEM.search(context))
    return people, people or bool(VENDOR_DOC.search(context))


def _mdate(s):
    for fmt in ("%b %d %Y", "%B %d, %Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def diligent_docs(host, seen, today):
    """Union Twp (2026-10-05): MeetingTypeList shows the latest few meetings of each type.
    A meeting's agenda is served as HTML at its "print version" link; each agenda item's
    attachments are /document/<uuid> links (the PDF itself)."""
    base = f"https://{host}"
    lst = _req(base + "/Portal/MeetingTypeList.aspx").decode("utf-8", "replace")
    docs, done = [], set()
    for mid, name in re.findall(r'MeetingInformation\.aspx\?Id=(\d+)"[^>]*>([^<]+ - \w{3} \d{2} \d{4})<', lst):
        when = _mdate(name.rsplit(" - ", 1)[1])
        if mid in done or not when or when < today - dt.timedelta(days=LOOKBACK_DAYS):
            continue
        done.add(mid)
        page = _req(base + f"/Portal/MeetingInformation.aspx?Id={mid}").decode("utf-8", "replace")
        link = base + f"/Portal/MeetingInformation.aspx?Id={mid}"
        pv = re.search(r'href="https?://[^"]*?(/document/\d+/[^"]+)"[^>]*DocumentPrintVersion', page)
        if not pv:
            continue
        agenda = _req(base + html.unescape(pv.group(1)), referer=link).decode("utf-8", "replace")
        txt = _text(agenda)
        meta = {"meeting": when.isoformat(), "ref": link}
        docs.append({"id": f"dl:{host}:{mid}:{_h(txt)}", "kind": "agenda", "title": name, "url": link,
                     "text": txt, "people": True, **meta})
        for m in re.finditer(r'<a[^>]+href="(/document/[0-9a-f-]{36})"[^>]*>(.*?)</a>', agenda, re.S):
            context = _text(agenda[max(0, m.start() - 400):m.start()])[-160:] + " " + _text(m.group(2))
            people, want = _attach_wanted(context)
            if want:
                fname = re.sub(r"\s+", " ", _text(m.group(2))).strip()
                docs.append({"id": f"dl:{host}:{m.group(1)}", "kind": "minutes" if "minute" in context.lower()
                             else ("personnel report" if people else "attachment"), "people": people,
                             "title": fname, "url": base + m.group(1), "pdf": True, **meta})
    return [d for d in docs if d["id"] not in seen]


def icboard_docs(host, seen, today):
    """Hobart (2026-10-05): needs the site's session cookie first (the root page sets it);
    without it every page is an 'Unhandled Exception'. Each agenda item is its own page,
    and its attachments are /attachments/<uuid>.pdf."""
    base = f"https://{host}"
    _req(base + "/")
    lst = _req(base + "/com/agenda_list.aspx").decode("utf-8", "replace")
    docs, done = [], set()
    kinds = dict(re.findall(r'public_agendaview\.aspx\?mtgId=(\d+)[^"]*"[^>]*>\s*([A-Z][A-Za-z ]*?(?:Meeting|Session|Information))\s*<', lst))
    for mid, when in re.findall(r'public_agendaview\.aspx\?mtgId=(\d+)[^"]*"[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})', lst):
        d = _mdate(when)
        if mid in done or not d or d < today - dt.timedelta(days=LOOKBACK_DAYS):
            continue
        done.add(mid)
        link = base + f"/public_agendaview.aspx?mtgId={mid}&CS=No"
        agenda = _req(link).decode("utf-8", "replace")
        txt = _text(agenda)
        meta = {"meeting": d.isoformat(), "ref": link}
        docs.append({"id": f"ic:{host}:{mid}:{_h(txt)}", "kind": "agenda", "title": f"{kinds.get(mid, 'Meeting')} - {when}",
                     "url": link, "text": txt, "people": True, **meta})
        for href, title in re.findall(r'href="(/public_itemview\.aspx\?ItemId=[^"]+)"[^>]*>([^<]+)<', agenda):
            people, want = _attach_wanted(title)
            if not want:
                continue
            item = _req(base + html.unescape(href).replace("+", "%2B"), referer=link).decode("utf-8", "replace")
            for n, a in enumerate(sorted(set(re.findall(r'href="(https?://[^"]+/attachments/[0-9a-f-]+\.pdf)"', item)))):
                docs.append({"id": f"ic:{host}:{a.rsplit('/', 1)[-1]}", "kind": "minutes" if "minute" in title.lower()
                             else ("personnel report" if people else "attachment"), "people": people,
                             "title": f"{html.unescape(title).strip()} ({n + 1})", "url": a, "pdf": True, **meta})
    return [d for d in docs if d["id"] not in seen]


# ---------- reading ----------

def doc_pages(doc):
    """[(page number or None, full text)] for one document. Fetched files are read by
    what they actually are, not their name: PDF, Word .docx, old Word .doc, or a web page."""
    if not doc.get("pdf"):
        return [(None, doc["text"])]
    raw = _req(doc.get("fetch") or doc["url"], referer=doc.get("ref"))
    if raw[:5].startswith(b"%PDF"):
        pages = [(n, re.sub(r"[ \t]+", " ", t)) for n, t in pdf_pages(raw)]
        if not any(t.strip() for _, t in pages):
            raise RuntimeError("scanned PDF - no readable text")
        return pages
    if raw[:4] == b"PK\x03\x04":
        return [(None, _docx_text(raw))]
    if raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return [(None, _doc_text(raw))]
    head = raw[:600].lower()
    if b"<html" in head or b"<!doctype html" in head:
        return [(None, _text(raw.decode("utf-8", "replace")))]
    raise RuntimeError("not a document (image or unknown file)")


def _docx_text(raw):
    import zipfile
    x = zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode("utf-8", "replace")
    x = re.sub(r"</w:p>|<w:br/>|<w:tab/>", "\n", x)
    return html.unescape(re.sub(r"<[^>]+>", "", x))


def _doc_text(raw):
    """Old binary Word: no library on the runner reads it, but its text sits in the file as
    plain runs (8-bit or UTF-16). Good enough for minutes (Lake Ridge, 2026-10-05)."""
    a = [m.decode("cp1252", "replace") for m in re.findall(rb"[\x20-\x7e\r\n\t\x91-\x97]{20,}", raw)]
    u = [m.decode("utf-16le") for m in re.findall(rb"(?:[\x20-\x7e\r\n\t]\x00){20,}", raw)]
    runs = a if sum(map(len, a)) >= sum(map(len, u)) else u
    return "\n".join(r.replace("\r", "\n") for r in runs)


def for_claude(pages, excerpt):
    parts = []
    for n, t in pages:
        ex = excerpt(t)
        if ex.strip():
            parts.append((f"[page {n}]\n" if n else "") + ex)
    return "\n".join(parts)


def vendor_hits(pages):
    hits = []
    for n, t in pages:
        flat = re.sub(r"\s+", " ", t)
        for vendor, rx in VENDORS:
            for m in rx.finditer(flat):
                a, b = max(0, m.start() - 170), min(len(flat), m.end() + 170)
                hits.append({"vendor": vendor, "page": n,
                             "snippet": ("..." if a else "") + flat[a:b].strip() + ("..." if b < len(flat) else "")})
    return hits


def run(districts, today, seen_path, ask, excerpt, keep_role, person_key):
    """Returns (alerts, checked, seen_state). `ask(prompt, text)` -> parsed JSON list."""
    state = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.exists() else {"docs": {}, "alerted": {}}
    alerts, mentions, checked, calls = [], [], [], 0
    for d in districts:
        src = d.get("board")
        if not src:
            checked.append({"district": d["name"], "status": "not connected"})
            continue
        key = src.get("site") or src.get("url") or src.get("folder") or src.get("host")
        seen = set(state["docs"].get(key, []))
        first = key not in state["docs"]
        try:
            if src["kind"] == "boarddocs":
                docs, skip = boarddocs_docs(src["site"], seen, today), []
            elif src["kind"] == "drive":
                docs, skip = drive_docs(src["folder"], seen, first)
            elif src["kind"] == "diligent":
                docs, skip = diligent_docs(src["host"], seen, today), []
            elif src["kind"] == "icboard":
                docs, skip = icboard_docs(src["host"], seen, today), []
            else:
                docs, skip = page_docs(src["url"], seen, first, take_all=src.get("all", False))
        except Exception as e:  # noqa: BLE001
            checked.append({"district": d["name"], "status": "unreachable", "error": str(e)[:120]})
            continue
        read, failed = 0, []
        for doc in docs:
            try:
                pages = doc_pages(doc)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{doc['title'][:60]}: {str(e)[:80]}")
                if re.search(r"scanned|not a document", str(e)):
                    seen.add(doc["id"])      # permanent - don't retry; a refusal or timeout retries next week
                continue
            seen.add(doc["id"])
            read += 1
            for h in vendor_hits(pages):
                mkey = f"{d['id']}|{h['vendor']}|{_h(re.sub(r'[^a-z]', '', h['snippet'].lower()))}"
                if mkey in state["alerted"]:
                    continue
                state["alerted"][mkey] = today.strftime("%Y-%m-%d")
                mentions.append({"district": d["name"], "group": d["group"], **h,
                                 "meeting": doc["meeting"], "docKind": doc["kind"], "docTitle": doc["title"],
                                 "url": doc["url"] + (f"#page={h['page']}" if doc.get("pdf") and h["page"] else ""),
                                 "upcoming": bool(doc["meeting"]) and doc["meeting"] > today.strftime("%Y-%m-%d"),
                               "catchup": first and not (doc["meeting"] > today.strftime("%Y-%m-%d"))})
            text = for_claude(pages, excerpt) if doc.get("people") else ""
            if not text.strip():
                continue
            rows = ask(PROMPT.format(district=d["name"], doc=f"{doc['kind']}, {doc['title']}"), text)
            calls += 1
            words = set(re.findall(r"[a-z'-]+", text.lower()))
            for r in rows:
                name, role = str(r.get("name", "")).strip(), str(r.get("role", "")).strip()
                action = str(r.get("action", "")).strip() or "Other"
                if not role or not keep_role(role):
                    continue
                pk = person_key(name) if name else ""
                if name and not all(w in words for w in pk.split()):
                    continue                                   # not on the page - dropped
                akey = f"{d['id']}|{pk or role.lower()}|{action.lower()}"
                if akey in state["alerted"]:
                    continue                                   # already told (e.g. agenda, now minutes)
                state["alerted"][akey] = today.strftime("%Y-%m-%d")
                page = r.get("page")
                url = doc["url"] + (f"#page={page}" if doc.get("pdf") and isinstance(page, int) else "")
                alerts.append({"district": d["name"], "group": d["group"], "name": name, "role": role,
                               "action": action, "effective": str(r.get("effective", "") or ""),
                               "meeting": doc["meeting"], "docKind": doc["kind"], "docTitle": doc["title"],
                               "page": page if isinstance(page, int) else None, "url": url,
                               "upcoming": bool(doc["meeting"]) and doc["meeting"] > today.strftime("%Y-%m-%d"),
                               "catchup": first and not (doc["meeting"] > today.strftime("%Y-%m-%d"))})
        state["docs"][key] = sorted(seen | set(skip))
        checked.append({"district": d["name"], "status": "read" if read else "nothing new",
                        "documents": read, "failed": failed})
    # Forget alert keys after a year so the file stays small.
    cut = (today - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v >= cut}
    return alerts, mentions, checked, state, calls
