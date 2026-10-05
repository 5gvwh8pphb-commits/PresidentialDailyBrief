#!/usr/bin/env python3
"""District watch: weekly roster of central-office admins, compared with last week.

    python tools/watch.py            # fetch, extract, compare, write the report

Pipeline, deliberately split so the model can't skip or invent a comparison:
  1. Fetch every page in watch/districts.json (plain HTTP, no model).
  2. Apptegy staff directories are parsed directly from their staff cards.
     Every other page is trimmed to the passages around admin job titles; if that
     passage is byte-identical to last week, last week's names are reused and no
     model is called. Otherwise `claude -p` reads just that passage, once per page,
     and every name it returns must appear verbatim in the passage or it is dropped.
  3. Plain code compares this week's roster with the previous one.

A page that fails to load is reported as unread and its district carries last
week's roster forward - a failed fetch is never read as "everyone left".

Writes:
  watch/rosters/YYYY-MM-DD.json   everyone's name and title, per district, this week
  watch/YYYY-MM-DD.json           the report the page shell renders
  watch/index.json                { latest, archive[] }
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
WATCH = ROOT / "watch"
ROSTERS = WATCH / "rosters"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
TIMEOUT = 30
APPTEGY_MAX_PAGES = 60
EXCERPT_RADIUS = 260
EXCERPT_CAP = 14000
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
MODEL = os.environ.get("WATCH_MODEL", "claude-sonnet-5")

# Admin-building roles Jim cares about. Anything else is dropped, whoever found it.
ROLE = re.compile(
    r"superintend|treasur|business|financ|\bcfo\b|controller|comptroller|accounting|budget"
    r"|human res|\bhr\b|personnel|payroll|payable|benefit|insurance", re.I)
NOT_ROLE = re.compile(r"\bteacher\b|\bcoach\b|\bprincipal\b|\bboard member\b|\bstudent\b", re.I)
# "Secretary to the Superintendent" names the boss, not the job.
SECRETARIAL = re.compile(
    r"(secretary|administrative assistant|executive assistant)\s*(to|for|of|-|–)?\s*(the\s*)?superintendent"
    r"|assistant\s+(to|for)\s+(the\s*)?superintendent", re.I)
SCHOOL_DEPT = re.compile(r"school|elementary|academy|middle|high|center", re.I)

try:
    TZ = ZoneInfo("America/Chicago")
except Exception:  # noqa: BLE001 - Windows without tzdata; only local test runs land here
    TZ = dt.timezone(dt.timedelta(hours=-5))


# ---------- fetching ----------

def fetch(url):
    """Return (html, note). Retries once; tolerates a sloppy certificate chain."""
    last = None
    for attempt in range(2):
        for verify in (True, False):
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
                    return r.read().decode("utf-8", "replace"), ("" if verify else "certificate not verified")
            except Exception as e:  # noqa: BLE001 - any failure is reported, not raised
                last = e
                if verify and "CERTIFICATE_VERIFY_FAILED" in str(e):
                    continue
                break
        time.sleep(3)
    raise RuntimeError(short_error(last))


def short_error(e):
    s = str(e)
    m = re.search(r"HTTP Error \d+: [^>]*", s)
    return (m.group(0) if m else s)[:120]


def page_text(b):
    b = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", b, flags=re.S | re.I)
    b = re.sub(r"<br\s*/?>|</(p|div|li|tr|h\d|td)>", " \n ", b, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", " ", b))
    return re.sub(r"[ \t\r\f\v ]+", " ", re.sub(r"\s*\n\s*", "\n", t)).strip()


def excerpt(text):
    """The passages around admin job titles - small, and stable when only nav/news changes."""
    spans = []
    for m in ROLE.finditer(text):
        a, b = max(0, m.start() - EXCERPT_RADIUS), min(len(text), m.end() + EXCERPT_RADIUS)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return "\n...\n".join(text[a:b] for a, b in spans)[:EXCERPT_CAP]


CARD = re.compile(
    r'<div class="name" data-v-[^>]*>([^<]*)</div>\s*'
    r'<div class="title" data-v-[^>]*>([^<]*)</div>\s*'
    r'(?:<div class="department" data-v-[^>]*>([^<]*)</div>)?')


def apptegy(url):
    """Every staff card across every page of an Apptegy directory."""
    people, seen, notes = [], set(), set()
    for n in range(1, APPTEGY_MAX_PAGES + 1):
        b, note = fetch(f"{url}?page_no={n}")
        if note:
            notes.add(note)
        new = 0
        for name, title, dept in CARD.findall(b):
            name, title, dept = (html.unescape(x).strip() for x in (name, title, dept or ""))
            if (name, title) in seen:
                continue
            seen.add((name, title))
            new += 1
            if dept and SCHOOL_DEPT.search(dept) and not re.search(r"central|district|admin", dept, re.I):
                continue
            people.append({"name": name, "title": title})
        if not new:
            break
    return people, "; ".join(sorted(notes))


# ---------- extraction ----------

PROMPT = """Below is text taken from a page on the {district} school district website.

List every person it shows holding a central-office administrative or business role:
superintendent, assistant / deputy / associate superintendent, CFO or chief financial
officer, treasurer, deputy or assistant treasurer, business manager or director of
business services, payroll, accounts payable, human resources, benefits or insurance,
and similar district finance or HR jobs.

Leave out teachers, principals, assistant principals, school board members,
secretaries and administrative assistants, and anyone assigned to a single school.

Copy each name and title exactly as the page writes them. Never guess or fill in a
name the text does not show. If the text names nobody in those roles, answer [].

Answer with ONLY a JSON array, no other words:
[{{"name": "...", "title": "..."}}]"""


class ModelError(RuntimeError):
    """Claude itself failed - stop the run and publish nothing, rather than call the pages unread."""


def ask_claude(district, text):
    cmd = [CLAUDE, "-p", PROMPT.format(district=district), "--model", MODEL,
           "--output-format", "text",
           "--disallowedTools",
           "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,NotebookEdit,TodoWrite,"
           "Agent,Task,ScheduleWakeup,TaskCreate,TaskUpdate,TaskOutput,SendMessage,Workflow"]
    r = subprocess.run(cmd, input=text, capture_output=True, text=True, encoding="utf-8", timeout=300)
    if r.returncode != 0:
        raise ModelError(f"claude exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    out = r.stdout
    a, b = out.find("["), out.rfind("]")
    if a < 0 or b < a:
        raise ModelError(f"claude gave no JSON array: {out.strip()[:200]}")
    rows = json.loads(out[a:b + 1])
    squash = lambda s: re.sub(r"\s+", " ", s).lower()
    hay = squash(text)
    kept = []
    for p in rows:
        name, title = str(p.get("name", "")).strip(), str(p.get("title", "")).strip()
        if name and title and squash(name) in hay:   # must be on the page, verbatim
            kept.append({"name": name, "title": title})
    return kept


# ---------- comparison ----------

HONORIFIC = re.compile(r"^(dr|mr|mrs|ms|miss|rev)\.?\s+", re.I)
CREDENTIAL = re.compile(r",.*$|\b(ed\.?d|ph\.?d|ed\.?s|m\.?b\.?a|c\.?p\.?a|m\.?a|m\.?s|sphr|shrm-cp|jr|sr|ii|iii)\.?$", re.I)


def person_key(name):
    n = HONORIFIC.sub("", name.strip())
    n = CREDENTIAL.sub("", n).strip()
    parts = [p for p in re.sub(r"[^a-z\s'-]", " ", n.lower()).split() if len(p) > 1]
    if len(parts) > 2:
        parts = [parts[0], parts[-1]]
    return " ".join(parts)


ABBREV = {"asst": "assistant", "dir": "director", "supt": "superintendent", "mgr": "manager",
          "dept": "department", "corp": "corporation", "hr": "human resources", "a p": "accounts payable",
          "ap": "accounts payable", "exec": "executive", "coord": "coordinator"}
FILLER = {"of", "the", "and", "for", "to", "district", "corporation", "school", "schools"}


def title_key(t):
    """Same job, different punctuation or word order -> same key ("Asst. Supt / CFO" == "Assistant Superintendent/CFO")."""
    words = re.sub(r"[^a-z0-9]+", " ", t.lower()).split()
    words = " ".join(ABBREV.get(w, w) for w in words).split()
    return " ".join(sorted(set(w for w in words if w not in FILLER)))


def keep_role(title):
    t = SECRETARIAL.sub("", title)
    return bool(ROLE.search(t)) and not NOT_ROLE.search(title)


def dedupe(people):
    out, seen = [], set()
    for p in people:
        if not keep_role(p["title"]):
            continue
        k = (person_key(p["name"]), title_key(p["title"]))
        if k[0] and k not in seen:
            seen.add(k)
            out.append(p)
    return out


def previous_roster(today):
    files = sorted(f for f in ROSTERS.glob("*.json") if f.stem < today)
    if not files:
        return None, None
    return files[-1].stem, json.loads(files[-1].read_text(encoding="utf-8"))


def compare(cur, prev, districts):
    moved, titled, added, gone = [], [], [], []
    names = {d["id"]: d["name"] for d in districts}
    groups = {d["id"]: d["group"] for d in districts}
    comparable = [d["id"] for d in districts
                  if cur[d["id"]]["status"] == "read" and prev.get(d["id"], {}).get("status") == "read"]

    def index(r, did):
        return {person_key(p["name"]): p for p in r.get(did, {}).get("people", [])}

    # Where everyone is listed this week, across all districts - catches a move even
    # when the old district's website still lists the person (Cochran, Oct 2026).
    now_at = {}
    for did, d in cur.items():
        for p in d.get("people", []):
            now_at.setdefault(person_key(p["name"]), []).append((did, p))
    was_at = {}
    for did, d in prev.items():
        for p in d.get("people", []):
            was_at.setdefault(person_key(p["name"]), []).append((did, p))

    for did in comparable:
        before, after = index(prev, did), index(cur, did)
        for k, p in after.items():
            if k not in before:
                origin = [(o, q) for o, q in was_at.get(k, []) if o != did]
                if origin:
                    o, q = origin[0]
                    still = any(x == o for x, _ in now_at.get(k, []))
                    moved.append({"name": p["name"], "from": {"district": names.get(o, o), "group": groups.get(o), "title": q["title"]},
                                  "to": {"district": names[did], "group": groups[did], "title": p["title"]},
                                  "stillListedAtOld": still,
                                  "replaces": next((b["name"] for bk, b in before.items()
                                                    if bk not in after and title_key(b["title"]) == title_key(p["title"])), None)})
                else:
                    added.append({"district": names[did], "group": groups[did], "name": p["name"], "title": p["title"],
                                  "replaces": next((b["name"] for bk, b in before.items()
                                                    if bk not in after and title_key(b["title"]) == title_key(p["title"])), None)})
            elif title_key(before[k]["title"]) != title_key(p["title"]):
                titled.append({"district": names[did], "group": groups[did], "name": p["name"],
                               "old": before[k]["title"], "new": p["title"]})
        for k, p in before.items():
            if k in after:
                continue
            if any(m["name"] and person_key(m["name"]) == k for m in moved):
                continue
            elsewhere = [o for o, _ in now_at.get(k, []) if o != did]
            if elsewhere:      # moved, recorded from the arrival side
                continue
            held = any(title_key(q["title"]) == title_key(p["title"]) for q in after.values())
            gone.append({"district": names[did], "group": groups[did], "name": p["name"], "title": p["title"],
                         "seatEmpty": not held})
    replaced = {person_key(x["replaces"]) for x in moved + added if x.get("replaces")}
    gone = [g for g in gone if person_key(g["name"]) not in replaced]   # already told on the arrival's card
    return moved, titled, added, gone


# ---------- main ----------

def main():
    cfg = json.loads((WATCH / "districts.json").read_text(encoding="utf-8"))
    districts = cfg["districts"]
    now = dt.datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    prev_date, prev = previous_roster(today)
    prev = prev or {}

    cur, unread, calls = {}, [], 0
    for d in districts:
        did, old = d["id"], prev.get(d["id"], {})
        people, pages, failed = [], [], []
        for pg in d["pages"]:
            url = pg["url"]
            try:
                if pg["kind"] == "apptegy":
                    found, note = apptegy(url)
                    pages.append({"url": url, "how": "parsed", "note": note})
                    people += found
                    continue
                b, note = fetch(url)
                ex = excerpt(page_text(b))
                h = hashlib.sha256(ex.encode()).hexdigest()[:16]
                old_pg = next((p for p in old.get("pages", []) if p.get("url") == url), None)
                if old_pg and old_pg.get("hash") == h and "people" in old_pg:
                    found, how = old_pg["people"], "unchanged"
                elif not ex.strip():
                    found, how = [], "no admin titles on page"
                elif os.environ.get("WATCH_DRY"):
                    found, how = [], f"dry run: {len(ex)} chars would go to Claude"
                else:
                    found, how = ask_claude(d["name"], ex), "read by Claude"
                    calls += 1
                pages.append({"url": url, "how": how, "hash": h, "people": found, "note": note})
                people += found
            except ModelError:
                raise
            except Exception as e:  # noqa: BLE001
                failed.append({"url": url, "error": short_error(e)})
                print(f"  ! {d['name']}: {url} -> {short_error(e)}", file=sys.stderr)
        if failed:
            cur[did] = {**old, "status": "unread", "failed": failed,
                        "lastGood": old.get("lastGood") or prev_date}
            for f in failed:
                unread.append({"district": d["name"], "group": d["group"], **f,
                               "lastGood": cur[did]["lastGood"]})
        else:
            cur[did] = {"name": d["name"], "group": d["group"], "status": "read",
                        "lastGood": today, "people": dedupe(people), "pages": pages}
        print(f"{d['name']:20} {cur[did]['status']:7} {len(cur[did].get('people', [])):3} people  "
              + ", ".join(p['how'] for p in pages))

    ROSTERS.mkdir(parents=True, exist_ok=True)
    (ROSTERS / f"{today}.json").write_text(json.dumps(cur, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    baseline = not prev
    moved, titled, added, gone = ([], [], [], []) if baseline else compare(cur, prev, districts)
    no_names = [d["name"] for d in districts if cur[d["id"]]["status"] == "read" and not cur[d["id"]]["people"]]

    if baseline:
        note = (f"First run - this is the baseline. Recorded {sum(len(v.get('people', [])) for v in cur.values())} "
                f"admin-building names across {len(districts)} districts. Changes are reported from next week.")
    elif not (moved or titled or added or gone):
        note = "No changes at any admin building this week."
    else:
        note = ""

    report = {
        "date": today,
        "dateLabel": now.strftime("%A, %B %-d, %Y") if os.name != "nt" else now.strftime("%A, %B %d, %Y"),
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "comparedTo": prev_date,
        "baseline": baseline,
        "note": note,
        "counts": {"moved": len(moved), "titleChanged": len(titled), "added": len(added),
                   "seatEmpty": sum(1 for g in gone if g["seatEmpty"]), "gone": len(gone), "unread": len(unread)},
        "moved": moved, "titleChanged": titled, "added": added, "gone": gone,
        "unread": unread, "noNames": no_names,
        "districts": [{"name": d["name"], "group": d["group"], "status": cur[d["id"]]["status"],
                       "people": cur[d["id"]].get("people", []),
                       "pages": [p["url"] for p in d["pages"]]} for d in districts],
        "modelCalls": calls,
    }
    report["stories"] = stories(report)
    (WATCH / f"{today}.json").write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    idx_path = WATCH / "index.json"
    idx = json.loads(idx_path.read_text(encoding="utf-8")) if idx_path.exists() else {"archive": []}
    label = "Week of " + now.strftime("%b %d, %Y").replace(" 0", " ")
    idx["archive"] = [{"date": today, "label": label}] + [a for a in idx.get("archive", []) if a["date"] != today]
    idx["archive"] = idx["archive"][:60]
    idx["latest"] = today
    idx_path.write_text(json.dumps(idx, indent=1) + "\n", encoding="utf-8")

    print(f"\n{today}: moved {len(moved)}, title {len(titled)}, added {len(added)}, gone {len(gone)}, "
          f"unread {len(unread)}, model calls {calls}{' (baseline)' if baseline else ''}")


def stories(r):
    """The same changes in the brief 'stories' shape, so tools/brief_email.py can mail it."""
    out = []
    side = lambda g: "account" if g == "account" else "prospect"
    for m in r["moved"]:
        out.append({"district": m["to"]["district"], "kind": "admin", "age": "this week",
                    "headline": f"{m['name']} moved from {m['from']['district']} to {m['to']['district']}",
                    "body": f"{m['from']['title']} ({side(m['from']['group'])}) -> {m['to']['title']} ({side(m['to']['group'])})."
                            + (f" Replaces {m['replaces']}." if m.get("replaces") else "")
                            + (" Old district's site still lists them." if m["stillListedAtOld"] else ""),
                    "source": "District websites", "url": None})
    for t in r["titleChanged"]:
        out.append({"district": t["district"], "kind": "admin", "age": "this week",
                    "headline": f"{t['name']}: new title", "body": f"{t['old']} -> {t['new']}.",
                    "source": "District website", "url": None})
    for a in r["added"]:
        out.append({"district": a["district"], "kind": "admin", "age": "this week",
                    "headline": f"New: {a['name']}, {a['title']}",
                    "body": f"Replaces {a['replaces']}." if a["replaces"] else "Not listed last week.",
                    "source": "District website", "url": None})
    for g in r["gone"]:
        out.append({"district": g["district"], "kind": "admin", "age": "this week",
                    "headline": f"{'Seat now empty' if g['seatEmpty'] else 'Gone'}: {g['title']}",
                    "body": f"{g['name']} is no longer listed and was not found at any watched district.",
                    "source": "District website", "url": None})
    for i, s in enumerate(out, 1):
        s["n"] = i
    return out


if __name__ == "__main__":
    main()
