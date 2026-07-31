# Presidential Daily Brief

Phone-readable news briefs. Each page is a fixed shell; each issue is a small JSON
file the shell reads. Automation writes facts, not HTML.

Two briefs live here:

| Brief | Page | Schedule | Workflow |
|---|---|---|---|
| Daily news brief | `/` | by 6 AM CT (cron 08:40 UTC) | `.github/workflows/briefing.yml` |
| My district news | `/district/` | Sundays ~6 PM CT (cron 22:50 UTC) | `.github/workflows/district.yml` |
| Prospect districts | `/prospects/` | Sundays ~7 PM CT (cron 23:50 UTC) | `.github/workflows/prospects.yml` |
| Skynet console (hub) | `/skynet/` | — | — |

**Scheduling reality:** GitHub's scheduled runs are best-effort and queue behind
everyone else's. Top-of-the-hour slots were arriving up to two hours late, so
every cron here sits off the hour and deliberately early. Treat the times as
"by" rather than "at". Runs are occasionally skipped entirely — the next one
recovers, and Run now always works.

Prospects runs an hour after the district brief so the two never race to push.
Each publish step retries with a rebase, so a collision can never lose an issue.

Both schedules are pinned to UTC cron and drift back one hour when CST resumes
in November.

## Layout

```
index.html                  daily brief shell
skynet/index.html           console / hub
district/index.html         district brief shell

briefings/index.json        { latest, archive[] }
briefings/YYYY-MM-DD.json   one daily brief per day

district/index.json         { latest, archive[] }
district/YYYY-MM-DD.json    one district brief per week

prospects/index.json        { latest, archive[] }
prospects/YYYY-MM-DD.json   one prospect brief per week

icon.png                    1024x1024 app icon
apple-touch-icon.png        180x180 home-screen icon
```

## Publishing an issue

1. Write the dated JSON file.
2. Set `latest` and prepend to `archive` in that folder's `index.json`.
3. Commit and push. GitHub Pages serves it within about a minute.

---

# Daily brief

## File shape

```json
{
  "date": "2026-07-27",
  "dateLabel": "Monday, July 27, 2026",
  "slot": "5 AM CT",
  "window": "past 24h",
  "generated": "2026-07-28T00:48:19Z",
  "beats": [
    {
      "n": 2,
      "beat": "US-related international",
      "group": "security",
      "age": "~4h ago",
      "empty": false,
      "headline": "One line, sentence case",
      "body": "One or two sentences of substance.",
      "source": "Outlet name",
      "url": "https://..."
    }
  ]
}
```

`group` sets the card's left-border colour: `markets`, `security`, `ai`, `sports`,
`community`. `empty: true` renders the muted dashed style. `url: null` makes the
card non-tappable.

### Beat 1: Market recap (special shape)

Beat 1 renders as a three-tile scorecard instead of a generic card when it
carries an `indices` array. Same top-level fields, plus:

```json
{
  "n": 1,
  "beat": "Market recap",
  "group": "markets",
  "age": "~14h ago",
  "empty": false,
  "kicker": "Prior-day market scorecard",
  "subline": "Thursday, July 30, 2026 close — a sharp rebound led by Microsoft's blowout earnings.",
  "indices": [
    { "name": "S&P 500", "close": "7,410.87", "change": "+94.72", "changePct": "+1.29%", "sixMo": "+8.4%" },
    { "name": "Dow",     "close": "52,058.95", "change": "+464.81", "changePct": "+0.90%", "sixMo": "+5.1%" },
    { "name": "Nasdaq",  "close": "25,015.87", "change": "+572.92", "changePct": "+2.34%", "sixMo": "-2.7%" }
  ],
  "body": "Bounce-back day after Wednesday's Fed-driven slide. Microsoft jumped ~16% (its best day since 2008) after Azure topped $100B in quarterly revenue; Meta fell ~9%.",
  "source": "Yahoo Finance",
  "url": "https://..."
}
```

Field rules:
- `kicker` — the card's top-left label. Defaults to `beat` if omitted. Use
  "Prior-day market scorecard" Tue–Fri, "Weekly market scorecard" on Mondays.
- `subline` — the muted line below the kicker. Include the close date and a
  short characterisation of the session (or the week).
- `indices` — exactly three entries in this order: S&P 500, Dow, Nasdaq. Use
  the names shown above verbatim so the tiles line up.
  - `close` — as a formatted string with commas, e.g. `"52,058.95"`.
  - `change` — point change with sign, e.g. `"+464.81"` or `"-55.17"`. The
    sign drives green/red colouring and the up/down arrow. On Monday's weekly
    recap, use the weekly percent here instead (e.g. `"-0.5%"`) and set
    `heroLabel: "week"` in place of `changePct`.
  - `changePct` — percent with sign and % sign, e.g. `"+1.29%"`. Omit on the
    Monday weekly variant; use `heroLabel: "week"` instead.
  - `sixMo` — 6-month percent change with sign, e.g. `"+8.4%"` or `"-2.7%"`.
    Coloured independently of the day/week hero — a green hero can sit above
    a red 6-mo and vice versa.
- `body` — one italic paragraph below the tiles. Do NOT include the source in
  `body`; the shell appends "Source: X." automatically.

Beat 1 has no `headline` field when the scorecard shape is used.

## The nine beats (fixed order)

| # | Beat | Group |
|---|------|-------|
| 1 | Market recap | markets |
| 2 | US-related international | security |
| 3 | China watch | security |
| 4 | Military news | security |
| 5 | Military technology | security |
| 6 | AI news | ai |
| 7 | Chicago Bears | sports |
| 8 | William Byron | sports |
| 9 | PCA | community |

School district news is **not** a daily beat. It has its own weekly brief.

## Rules

- **Recency, beats 1-6:** past 24 hours preferred, 72 hours absolute maximum. Never older.
- **Recency, beats 7-9:** past 24 hours preferred. If nothing new, fall back to
  48 hours; if still nothing, fall back to 72 hours. Any 48h or 72h item must
  not have been reported in a prior brief — check the most recent briefings and
  skip anything already covered. Nothing older than 72 hours, ever. If nothing
  qualifies, the beat is empty and says so. No exceptions, no niche waiver.
- **Spoiler rule, beats 7-8 only:** never reveal scores, results, or race/game outcomes
  from the past 7 days. Roster moves, injuries, and schedule info are fine.
- **Empty is allowed.** Set `empty: true` and say so. Never stretch to a loosely-related story.
- **Sourcing, general:** reputable or primary outlets only. No clickbait.
- **Sourcing, beat 7 (Chicago Bears):** rumours are welcome, but only from this
  allowlist — NFL.com, ESPN.com, Adam Schefter, Adam Hoge, Adam Jahns, Fox
  Sports, Chicago Sun-Times, Chicago Tribune, Brad Biggs, WSCR The Score, ESPN
  Radio, CHGO. If a rumour appears only in outlets outside that list, skip it.
- **Tone:** facts plainly, no partisan framing from any direction.

---

# District brief

Weekly. Who is coming and going in administration across the districts Jim sells to.

## File shape

```json
{
  "date": "2026-08-02",
  "dateLabel": "Sunday, August 2, 2026",
  "slot": "Sundays 6 PM CT",
  "window": "past 7 days",
  "generated": "2026-08-02T23:10:00Z",
  "note": "Quiet week; one superintendent search and a bond vote.",
  "covered": ["Eastern Pulaski", "East Porter Co", "..."],
  "stories": [
    {
      "n": 1,
      "district": "Valparaiso Community Schools",
      "kind": "admin",
      "age": "~3 days ago",
      "headline": "One line, sentence case",
      "body": "One or two sentences. Name names and titles.",
      "source": "Outlet name",
      "url": "https://..."
    }
  ]
}
```

`kind` sets the card colour and tag: `admin` (amber), `trust` (purple),
`other` (blue), `student` (teal). An empty `stories` array renders an honest
quiet-week state — that is expected some weeks, not a failure.

## Priority order

1. **admin** — Superintendent, Assistant/Deputy Superintendent, Business Manager,
   CFO, HR Director, Treasurer, Deputy Treasurer, Principal, Assistant Principal.
   Hired, appointed, promoted, resigned, retiring, fired, placed on leave,
   contract approved or not renewed, interim appointments, open searches, finalists.
2. **other** — referendums and levies, budgets, bonds and construction, board
   conflict, closings or consolidation, enrollment, accountability, litigation, labor.
3. **student** — only when 1 and 2 are thin. State titles, major awards,
   significant incidents. No routine activities.

**Hard recency rule: nothing older than 7 days. No exceptions.** Cap at 12 stories,
keeping all admin items first.

## Entities covered

| # | District | Town |
|---|---|---|
| 1 | Eastern Pulaski Community School Corporation | Winamac |
| 2 | East Porter County School Corporation | Kouts / Morgan Twp |
| 3 | Hanover Community School Corporation | Cedar Lake |
| 4 | LaPorte Community School Corporation | LaPorte |
| 5 | Michigan City Area Schools | Michigan City |
| 6 | MSD of Boone Township | Hebron |
| 7 | MSD of New Durham Township | Westville |
| 8 | New Prairie United School Corporation | New Carlisle, Rolling Prairie |
| 9 | NISEC (NW Indiana Special Education Cooperative) | — |
| 10 | School City of Hobart | Hobart |
| 11 | Tri-Creek School Corporation | Lowell |
| 12 | Union Township School Corporation | Valparaiso area |
| 13 | Valparaiso Community Schools | Valparaiso |
| 14 | Porter County Trust | benefit trust |
| 15 | MASE Trust | benefit trust |

---

# Prospect brief

Weekly. Same shape as the district brief, but districts Jim does **not** yet serve.

Differences from the district brief:

- **Window is 30 days**, not 7.
- **Administration only.** No fallback to other district news, no student stories.
  If nothing qualifies, the brief is empty and says so.
- Every story is `kind: "admin"`.

## Prospect districts

| # | District | Town |
|---|---|---|
| 1 | Duneland School Corporation | Chesterton |
| 2 | Portage Township Schools | Portage |
| 3 | School Town of Highland | Highland |
| 4 | Lake Station Community Schools | Lake Station |
| 5 | School City of Hammond | Hammond |
| 6 | School City of Whiting | Whiting |
| 7 | Gary Community School Corporation | Gary |
| 8 | Lake Ridge Schools | Gary |
