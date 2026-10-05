"""Board agendas and minutes: alert on admin-building personnel actions, with a link.

Called from tools/watch.py. Nothing is archived - only the IDs of documents
already read (watch/board_seen.json), so the same document is never read twice
and the same hire is never alerted twice.

Sources (per district, "board" in watch/districts.json):
  {"kind": "boarddocs", "site": "duneland"}   BoardDocs: every meeting dated within
        the last LOOKBACK_DAYS or in the future (agendas appear days before the vote).
        Reads the detailed agenda text plus attached PDFs that look like personnel
        reports or minutes.
  {"kind": "pages", "url": "..."}             A district page that links agenda/minutes
        PDFs. New links are read; on the first run only the top FIRST_RUN_DOCS.
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

def page_docs(url, seen, first_run):
    b = _req(url).decode("utf-8", "replace")
    out = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', b, re.S | re.I):
        full = urllib.parse.quote(urllib.parse.urljoin(url, html.unescape(href).strip()), safe=":/?&=%#~+,;@")
        label = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", label))).strip()
        name = urllib.parse.unquote(full.rsplit("/", 1)[-1])
        if not re.search(r"\.pdf($|\?)|resource-manager/view", full, re.I):
            continue
        if not (DOC_NAME.search(label) or DOC_NAME.search(name)):
            continue
        if any(d["url"] == full for d in out):
            continue
        out.append({"id": "pg:" + full, "kind": "minutes" if re.search("minute", label + name, re.I) else "agenda",
                    "meeting": "", "title": label or name, "url": full, "pdf": True, "people": True})
    # Newest first: the latest year named in the link text or file name, page order breaking ties.
    year = lambda d: max([int(y) for y in re.findall(r"20[0-4]\d", d["title"] + d["url"])] or [0])
    out = [d for _, d in sorted(enumerate(out), key=lambda x: (-year(x[1]), x[0]))]
    if first_run:
        return out[:FIRST_RUN_DOCS], [d["id"] for d in out[FIRST_RUN_DOCS:]]
    return [d for d in out if d["id"] not in seen], []


# ---------- reading ----------

def doc_pages(doc):
    """[(page number or None, full text)] for one document."""
    if not doc.get("pdf"):
        return [(None, doc["text"])]
    raw = _req(doc["url"], referer=doc.get("ref"))
    if not raw[:5].startswith(b"%PDF"):
        raise RuntimeError("not a PDF")
    pages = [(n, re.sub(r"[ \t]+", " ", t)) for n, t in pdf_pages(raw)]
    if not any(t.strip() for _, t in pages):
        raise RuntimeError("scanned PDF - no readable text")
    return pages


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
        key = src.get("site") or src.get("url")
        seen = set(state["docs"].get(key, []))
        try:
            if src["kind"] == "boarddocs":
                docs, skip = boarddocs_docs(src["site"], seen, today), []
            else:
                docs, skip = page_docs(src["url"], seen, first_run=key not in state["docs"])
        except Exception as e:  # noqa: BLE001
            checked.append({"district": d["name"], "status": "unreachable", "error": str(e)[:120]})
            continue
        read, failed = 0, []
        for doc in docs:
            try:
                pages = doc_pages(doc)
            except Exception as e:  # noqa: BLE001
                failed.append(f"{doc['title'][:60]}: {str(e)[:80]}")
                if re.search(r"scanned|not a PDF", str(e)):
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
                                 "upcoming": bool(doc["meeting"]) and doc["meeting"] > today.strftime("%Y-%m-%d")})
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
                               "upcoming": bool(doc["meeting"]) and doc["meeting"] > today.strftime("%Y-%m-%d")})
        state["docs"][key] = sorted(seen | set(skip))
        checked.append({"district": d["name"], "status": "read" if read else "nothing new",
                        "documents": read, "failed": failed})
    # Forget alert keys after a year so the file stays small.
    cut = (today - dt.timedelta(days=365)).strftime("%Y-%m-%d")
    state["alerted"] = {k: v for k, v in state["alerted"].items() if v >= cut}
    return alerts, mentions, checked, state, calls
