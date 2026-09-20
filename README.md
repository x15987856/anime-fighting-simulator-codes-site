# animefightingsimulators.com

Public source for the Anime Fighting Simulator codes page, plus the patrol that keeps it honest.

**Live site:** https://animefightingsimulators.com/
**Hosting:** Cloudflare Pages, project `anime-fighting-codes`, branch `main`.

## What is in here

| Path | What it is |
|---|---|
| `site/` | The whole static site. This directory is what gets deployed, unchanged. |
| `patrol.py` | The patrol: fetch sources -> parse -> diff -> tier gate -> edit -> log. |
| `MAINTENANCE.md` | The patrol log, one row per round. **Lives in the repo, never in `site/`.** |
| `.github/workflows/patrol.yml` | Runs the patrol every 6 hours, deploys, then commits if the page changed. |

## How the patrol decides anything

Sources are graded in two tiers. The rule exists so the page never quietly picks a
favourite list.

- **Tier 1 - the developer's own channels** (the game's Roblox page, the official group
  walls). First-hand, so one of them on its own is enough to put a code up. The row names
  which channel it came from.
- **Tier 2 - third-party code lists** (Beebom, GamesRadar). A code from this tier goes up
  only when **both** carry it. If they disagree, the disagreement is printed on the page
  instead of being resolved.

Additional rules the script enforces:

- A code moves to the expired table only on **positive evidence**: gone from every working
  section *and* present in an expired section. A list that simply has not been updated is
  not evidence a code is dead.
- Rewards and gates are written only when a source states them. Otherwise the row says the
  source did not publish it. Nothing is estimated.
- If nothing changed, the page is not touched and no date is bumped, and the run commits
  nothing.

## Running it locally

```bash
python patrol.py --dry-run   # fetch + diff + report, writes nothing
python patrol.py             # full round including deploy (needs CLOUDFLARE_API_TOKEN)
python patrol.py --no-deploy # used by CI: deploy is handled by the wrangler action
python patrol.py --verify-only
```

## Setup

The Cloudflare token is read from the environment as `CLOUDFLARE_API_TOKEN`. In CI it comes
from the repository secret of the same name. It is never written to a file in this repo and
never echoed into logs.
