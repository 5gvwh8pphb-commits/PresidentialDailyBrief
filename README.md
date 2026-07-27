# Presidential Daily Brief

A phone-readable daily news briefing. The page is a fixed shell; each morning's
briefing is a small JSON file it reads. Automation writes facts, not HTML.

## Layout

```
index.html                  the dashboard (never changes day to day)
briefings/index.json        { latest, archive[] }  -- pointer + archive list
briefings/YYYY-MM-DD.json   one briefing per day
```

## Publishing a new briefing

1. Write `briefings/YYYY-MM-DD.json`.
2. Add the date to `archive` in `briefings/index.json` (newest first) and set `latest`.
3. Commit and push. GitHub Pages serves it within about a minute.

## Briefing file shape

```json
{
  "date": "2026-07-27",
  "dateLabel": "Monday, July 27, 2026",
  "slot": "5 AM CT",
  "window": "past 24h",
  "generated": "2026-07-27T22:50:00Z",
  "beats": [
    {
      "n": 1,
      "beat": "Market recap",
      "group": "markets",
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

`group` sets the card's left-border colour. Valid values:
`markets`, `security`, `ai`, `sports`, `community`.

`empty: true` renders the muted dashed style — use it when a beat genuinely has
no news. Say so plainly rather than manufacturing filler.

`url: null` makes the card non-tappable.

## The ten beats (fixed order)

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
| 9 | Indiana public schools | community |
| 10 | PCA | community |

## Rules the content must follow

- **Recency, beats 1-6:** past 24 hours preferred, 72 hours absolute maximum. Never older.
- **Spoiler rule, beats 7-8 only:** never reveal scores, results, or race/game
  outcomes from the past 7 days. Roster moves, injuries, and schedule info are fine.
- **Niche beats 7-10:** if the freshest item is older than 72h it may still run,
  but label its real age honestly in `age`.
- **Empty is allowed.** Set `empty: true` and say so. Never stretch to a
  loosely-related story.
- **Sourcing:** reputable or primary outlets only. No clickbait, especially on the
  Bears beat. Rumours are fine from reputable outlets, not tabloid speculation.
- **Tone:** report facts plainly. No ideological or partisan framing from any direction.

## Indiana districts covered (beat 9)

Eastern Pulaski · East Porter County · Hanover · Tri-Creek · MSD of Boone Twp ·
MSD of New Durham Twp · Michigan City Area Schools · New Prairie United ·
Union Twp School Corp · Valparaiso Community Schools · LaPorte Community ·
Duneland · South Central · School City of Hobart
