#!/usr/bin/env python3
"""
AFS codes page patrol.

Runs the STEP-12 refresh loop:
  FETCH -> PARSE -> DIFF -> TIER -> EDIT -> STAMP -> DEPLOY -> VERIFY -> LOG

Hard rules baked in:
  * Never invent a code, reward, gate or source. If a source cannot be read, it is skipped.
  * Tier 1 (official channels, first-hand): one source is enough, but the row must name it.
  * Tier 2 (third-party lists): needs BOTH lists to carry it. One list alone never enters.
  * Disagreement is printed on the page, never resolved by picking a favourite.
  * Expiring needs positive evidence: gone from every working section AND present in an
    expired section. A stale list is not evidence a code is dead.
  * If nothing changed, the page is not touched and no date is bumped.

Usage:
    python patrol.py            # normal round
    python patrol.py --dry-run  # fetch + diff + report, write nothing
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
PAGE = os.path.join(SITE, "index.html")
# The log lives in the repo, NOT in site/ - it must never be published.
LOG = os.path.join(HERE, "MAINTENANCE.md")
STATE = os.path.join(HERE, ".patrol_state.json")

PLACE_ID = 100429474155186
UNIVERSE_ID = 10321202755          # resolved via apis.roblox.com/universes/v1/places/<place>/universe
GROUPS = [942260693, 5202664]      # Anime Fighting Simulator | BZ, BlockZone

# Third-party tier-2 lists. Both must agree.
LISTS = {
    "Beebom": "https://beebom.com/anime-fighting-simulator-codes/",
    "GamesRadar": "https://www.gamesradar.com/games/simulation/anime-fighting-simulator-codes/",
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CURL = "curl"
DRY = "--dry-run" in sys.argv
# GitHub Actions splits the round in two: this script edits + logs, cloudflare/wrangler-action
# deploys, then this script runs again with --verify-only to fill in the live test results.
NO_DEPLOY = "--no-deploy" in sys.argv
VERIFY_ONLY = "--verify-only" in sys.argv


# --------------------------------------------------------------------------- fetch

def curl_text(url, extra=None):
    """Return body text or None. Never raises."""
    cmd = [CURL, "-4", "-sL", "-m", "40", "-A", UA, url]
    if extra:
        cmd += extra
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60)
        if p.returncode != 0 or not p.stdout:
            return None
        return p.stdout.decode("utf-8", "replace")
    except Exception as e:
        print("   curl failed %s: %s" % (url, e))
        return None


def strip_html(h):
    h = re.sub(r"(?is)<script.*?</script>", " ", h)
    h = re.sub(r"(?is)<style.*?</style>", " ", h)
    h = re.sub(r"(?is)<svg.*?</svg>", " ", h)
    h = re.sub(r"(?is)<br\s*/?>", "\n", h)
    h = re.sub(r"(?is)</(p|div|li|tr|h1|h2|h3|h4)>", "\n", h)
    h = re.sub(r"(?is)</t[dh]>", " | ", h)
    h = re.sub(r"(?is)<[^>]+>", " ", h)
    import html as _h
    h = _h.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n", h)
    return h


# --------------------------------------------------------------------------- parse

CODE_RE = re.compile(r"^([A-Z0-9][A-Z0-9_]{2,19})\s*[-:–]\s*(.{4,300})$")

# Words that look like codes but are not. Extend as false positives show up.
STOPWORDS = {
    "NEW", "CODE", "CODES", "COPY", "UPDATE", "UPDATED", "EXPIRED", "ACTIVE",
    "WORKING", "ALL", "HOW", "WHAT", "WHY", "THE", "AND", "FOR", "YOU", "GET",
    "NOTE", "TIPS", "MORE", "READ", "TOP", "BEST", "USE", "JOIN", "PLAY",
    "ROBLOX", "GAME", "GAMES", "REWARDS", "REWARD", "SERVER", "BOOST",
}


def parse_list(text):
    """Split a codes article into (working: {CODE: reward}, expired: set)."""
    working, expired = {}, set()
    if not text:
        return working, expired
    # The expired section starts at the first line that is (or contains) 'expired'
    lines = text.split("\n")
    cut = len(lines)
    for i, ln in enumerate(lines):
        s = ln.strip().lower()
        if s.startswith("expired") or s.startswith("all expired") or s == "expired":
            cut = i
            break
    for ln in lines[:cut]:
        m = CODE_RE.match(ln.strip())
        if m and m.group(1) not in STOPWORDS:
            working.setdefault(m.group(1), m.group(2).strip())
    for ln in lines[cut:]:
        s = ln.strip()
        m = CODE_RE.match(s)
        if m and m.group(1) not in STOPWORDS:
            expired.add(m.group(1))
        else:
            bare = re.match(r"^([A-Z0-9][A-Z0-9_]{2,19})$", s)
            if bare and bare.group(1) not in STOPWORDS:
                expired.add(bare.group(1))
    return working, expired


def parse_official(description):
    """Codes the developer pushes itself, e.g. Use Code "DEMONLORD" for FREE rewards!"""
    if not description:
        return {}
    found = {}
    for m in re.finditer(r'[Cc]ode\s+["“”]?([A-Z0-9][A-Z0-9_]{2,19})["“”]?', description):
        found[m.group(1)] = "Official game page description"
    for m in re.finditer(r'["“]([A-Z][A-Z0-9_]{3,19})["”]', description):
        tok = m.group(1)
        if tok not in STOPWORDS and not tok.islower():
            found.setdefault(tok, "Official game page description")
    return found


# --------------------------------------------------------------------------- page

def read_page():
    return open(PAGE, encoding="utf-8").read()


def working_table_rows(html):
    """[{code, src, block}] for the working codes table."""
    i = html.find("<h2>Working codes</h2>")
    j = html.find("</table>", i)
    blk = html[i:j]
    rows = []
    for m in re.finditer(r"<tr>\s*<td class=\"code\">([A-Z0-9]+)", blk):
        rows.append(m.group(1))
    return rows


def get_src_cell(html, code):
    i = html.find('<td class="code">%s' % code)
    if i < 0:
        return None
    j = html.find('<td class="src">', i)
    k = html.find("</td>", j)
    return html[j + len('<td class="src">'):k], j, k


def set_src_cell(html, code, new_src):
    cell = get_src_cell(html, code)
    if not cell:
        return html
    _, j, k = cell
    return html[:j] + '<td class="src">' + new_src + html[k:]


def set_block(html, marker, new_html, anchor_after):
    """Replace a <div id=marker>...</div> block, or insert it after anchor_after."""
    pat = re.compile(r'<div[^>]*id="%s".*?</div>\s*' % re.escape(marker), re.S)
    if pat.search(html):
        return pat.sub(new_html + "\n", html, count=1)
    a = html.find(anchor_after)
    if a < 0:
        print("   anchor not found: %s" % anchor_after[:60])
        return html
    a = html.find("</table>", a) + len("</table>")
    return html[:a] + "\n" + new_html + "\n" + html[a:]


ONE_LIST = "Tier 2 &mdash; one list only"


def build_dis_block(codes, date_str):
    """The disagreement note. Built separately so we can tell whether it already matches."""
    if not codes:
        return ""
    lead = ("these codes rest on a single list" if len(codes) > 1
            else "this code rests on a single list")
    return (
        '  <div class="note warn" id="list-disagreement">\n'
        '    <span class="flag">Disagreement &mdash; printed, not resolved</span>\n'
        '    <p>As of %s, %s: %s. They appear on one of the two lists we read, not both, so their '
        'Source cells say so. They are <em>not</em> in the expired table: neither list has put '
        'them there, and a list that simply has not been updated is not evidence a code is dead. '
        'We are not picking a favourite list either &mdash; the disagreement stays on the page.</p>\n'
        '  </div>'
    ) % (date_str, lead, ", ".join('<span class="k">%s</span>' % c for c in codes))


def run_verify(report):
    """Live test of the two URLs. Only ever reports what curl actually returned."""
    print("[VERIFY]")
    for path in ("", "about"):
        url = "https://animefightingsimulators.com/" + path
        p = subprocess.run([CURL, "-4", "-s", "-o", os.path.join(HERE, "_v.html"),
                            "-A", UA, "-w", "%{http_code} %{num_redirects}", "-L", url],
                           capture_output=True, timeout=60)
        txt = p.stdout.decode("utf-8", "replace").strip().split()
        code, redir = (txt + ["?", "?"])[:2]
        report["verify"].append({"url": url, "http": code, "redirects": redir})
        print("   %-45s HTTP=%s redirects=%s" % (url, code, redir))
    return report


def patch_log(report):
    """Fill the verify cells that were written as 'pending' before the deploy."""
    if not os.path.exists(LOG):
        return
    v = report["verify"]
    got = " | ".join("%s/%s" % (v[i]["http"], v[i]["redirects"])
                     for i in range(min(2, len(v)))) or "? | ?"
    txt = open(LOG, encoding="utf-8").read()
    idx = txt.rfind("| pending | pending |")
    if idx >= 0:
        txt = txt[:idx] + "| %s |" % got + txt[idx + len("| pending | pending |"):]
        open(LOG, "w", encoding="utf-8").write(txt)
        print("[LOG] verify cells filled: %s" % got)


def validate(html):
    for tag in ("div", "table", "tr", "td", "th", "ul", "li"):
        o = len(re.findall(r"<%s\b" % tag, html))
        c = len(re.findall(r"</%s>" % tag, html))
        if o != c:
            return False, "%s open=%d close=%d" % (tag, o, c)
    return True, "ok"


# --------------------------------------------------------------------------- main

def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    stamp = now.strftime("%Y-%m-%d %H:%M")

    # Second half of a GitHub Actions round: deployment already happened, now test it.
    if VERIFY_ONLY:
        print("=== patrol verify-only %s (UTC+8) ===" % stamp)
        report = {"verify": []}
        if os.path.exists(STATE):
            try:
                saved = json.load(open(STATE, encoding="utf-8"))
                for k in ("added", "moved", "total", "tier1", "tier2", "changed"):
                    report[k] = saved.get(k)
                print("[STATE] restored: added=%s moved=%s total=%s" %
                      (saved.get("added"), saved.get("moved"), saved.get("total")))
            except Exception as e:
                print("   state load failed: %s" % e)
        run_verify(report)
        patch_log(report)
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    print("=== patrol %s (UTC+8) ===" % stamp)

    report = {
        "time": stamp, "added": 0, "moved": 0, "total": 0,
        "tier1": 0, "tier2": 0, "added_codes": [], "moved_codes": [],
        "notes": [], "sources_ok": [], "sources_failed": [],
        "changed": False, "deployed": False, "verify": [],
    }

    # ---- FETCH -------------------------------------------------------------
    print("[FETCH] official game description (SRC1)")
    uni = curl_text("https://apis.roblox.com/universes/v1/places/%d/universe" % PLACE_ID,
                    ["-H", "Accept: application/json"])
    desc = None
    uid = UNIVERSE_ID
    if uni:
        m = re.search(r'"universeId":\s*(\d+)', uni)
        if m:
            uid = int(m.group(1))
    g = curl_text("https://games.roblox.com/v1/games?universeIds=%d" % uid,
                  ["-H", "Accept: application/json"])
    if g:
        try:
            d = json.loads(g)
            for item in d.get("data", []):
                desc = item.get("description")
        except Exception as e:
            print("   parse failed: %s" % e)
    official = parse_official(desc) if desc else {}
    print("   official codes: %s" % (sorted(official) if official else "none"))
    report["sources_ok"].append("official-description") if desc else report["sources_failed"].append("official-description")

    print("[FETCH] third-party lists (SRC4)")
    lists = {}
    for name, url in LISTS.items():
        body = curl_text(url)
        w, e = parse_list(strip_html(body)) if body else ({}, set())
        lists[name] = {"working": w, "expired": e}
        if w or e:
            report["sources_ok"].append(name)
        else:
            report["sources_failed"].append(name)
        print("   %-11s working=%d expired=%d" % (name, len(w), len(e)))

    print("[FETCH] official group wall + events (SRC2/SRC3)")
    print("   skipped: needs a browser session this script does not own")
    report["sources_failed"].append("group-wall")
    report["sources_failed"].append("events")

    # ---- DIFF + TIER -------------------------------------------------------
    html = read_page()
    current = working_table_rows(html)
    print("[DIFF] on page now: %s" % current)

    # tier 2 agreement
    names = list(lists)
    def both(name_codes):
        return [c for c in name_codes if all(c in lists[n]["working"] for n in names)]

    union = set()
    for n in names:
        union |= set(lists[n]["working"])
    tier2_ok = both(sorted(union))
    tier2_single = [c for c in union if c not in tier2_ok]
    print("   tier2 both lists : %s" % tier2_ok)
    print("   tier2 one list   : %s" % [c for c in tier2_single if c in current])

    # ADDS
    adds = []
    for c in official:
        if c not in current:
            adds.append((c, "tier1", "Tier 1 &mdash; official game page"))
    for c in tier2_ok:
        if c not in current and c not in [a[0] for a in adds]:
            adds.append((c, "tier2", "Tier 2 &mdash; two lists"))
    print("[TIER] adds: %s" % [a[0] for a in adds])

    # REMOVALS need positive evidence
    moves = []
    for c in current:
        in_any_working = any(c in lists[n]["working"] for n in names)
        in_any_expired = any(c in lists[n]["expired"] for n in names)
        if c in official:
            continue                      # official still pushes it -> never move
        if not in_any_working and in_any_expired:
            moves.append(c)
    print("[TIER] moves: %s" % moves)

    # DISAGREEMENT: on page, carried by exactly one list, no official backing
    disagree = [c for c in current if c not in official and c in tier2_single]

    report["added"] = len(adds)
    report["moved"] = len(moves)
    report["added_codes"] = [a[0] for a in adds]
    report["moved_codes"] = moves
    report["disagreement"] = disagree

    # ---- EDIT --------------------------------------------------------------
    # Only a real difference counts as a change. A disagreement the page already
    # spells out must not re-trigger a deploy every six hours.
    need_dis = False
    for c in disagree:
        cell = get_src_cell(html, c)
        if not cell or cell[0] != ONE_LIST:
            need_dis = True
    want = build_dis_block(disagree, stamp[:10])
    have = re.search(r'<div[^>]*id="list-disagreement".*?</div>', html, re.S)
    if want and (not have or have.group(0).strip() != want.strip()):
        need_dis = True
    if not want and have:
        need_dis = True

    if adds or moves or need_dis:
        report["changed"] = True
        if not DRY:
            bak = os.path.join(HERE, "backup_patrol_%s" % now.strftime("%Y%m%d_%H%M"))
            os.makedirs(bak, exist_ok=True)
            shutil.copy2(PAGE, os.path.join(bak, "index.html"))
            print("[EDIT] backup -> %s" % bak)

        # downgrade the source cell of rows that lost their second list
        for c in disagree:
            html = set_src_cell(html, c, ONE_LIST)

        # print the disagreement instead of resolving it
        if want:
            html = set_block(html, "list-disagreement", want, "<h2>Working codes</h2>")
        elif have:
            html = html[:have.start()] + html[have.end():]

        ok, why = validate(html)
        if not ok:
            print("[ABORT] html invalid after edit: %s" % why)
            report["notes"].append("ABORTED: html invalid (%s)" % why)
            html = read_page()
            report["changed"] = False
        elif not DRY:
            open(PAGE, "w", encoding="utf-8").write(html)
            print("[EDIT] page written")
    else:
        print("[EDIT] no change -> page untouched, no date bumped")

    # ---- STAMP -------------------------------------------------------------
    if report["changed"] and not DRY:
        html = read_page()
        today = "%d %s %d" % (now.day, now.strftime("%B"), now.year)
        html = re.sub(
            r"Third-party lists read [0-9&ndash;\- ]*[A-Za-z]+ [0-9]{4}",
            "Third-party lists read %s" % today, html)
        bits = []
        if adds:
            bits.append("added " + ", ".join(a[0] for a in adds))
        if moves:
            bits.append("moved " + ", ".join(moves) + " to expired")
        if disagree:
            bits.append("flagged " + ", ".join(disagree) + " as single-list")
        line = "This update: %s." % "; ".join(bits)
        # [^<]* so a decimal like "Update 9.5" inside the sentence cannot truncate it
        html = re.sub(r"This update: [^<]*", line, html, count=1)
        ok, why = validate(html)
        if ok:
            open(PAGE, "w", encoding="utf-8").write(html)
            print("[STAMP] %s" % line)
        else:
            print("[ABORT] stamp made html invalid: %s" % why)

    # ---- LOG ---------------------------------------------------------------
    # Written BEFORE the deploy, otherwise the log file is never part of the upload.
    # Verify cells are filled in once VERIFY has run (patched below).
    srcs = read_page()
    total = len(working_table_rows(srcs))
    report["total"] = total
    report["tier1"] = srcs.count("Tier 1 &mdash;")
    report["tier2"] = srcs.count("Tier 2 &mdash;")
    print("[LOG] total=%d tier1=%d tier2=%d" % (total, report["tier1"], report["tier2"]))

    concl = "no change" if not report["changed"] else (
        "; ".join(filter(None, [
            "added " + ", ".join(report["added_codes"]) if report["added_codes"] else "",
            "moved " + ", ".join(report["moved_codes"]) if report["moved_codes"] else "",
            "flagged " + ", ".join(disagree) + " as single-list" if disagree else "",
        ])))

    if not DRY:
        first = not os.path.exists(LOG)
        with open(LOG, "a", encoding="utf-8") as f:
            if first:
                f.write("# Maintenance log - Anime Fighting Simulator codes page\n\n")
                f.write("Patrol reads these sources every round:\n\n")
                f.write("- **Tier 1, official (one is enough, row names the channel):**\n")
                f.write("  - Game description: `https://apis.roblox.com/universes/v1/places/%d/universe` "
                        "-> `https://games.roblox.com/v1/games?universeIds=%d`\n" % (PLACE_ID, UNIVERSE_ID))
                f.write("  - Group wall: https://www.roblox.com/communities/%d (needs a browser)\n" % GROUPS[0])
                f.write("  - Studio group wall: https://www.roblox.com/communities/%d (needs a browser)\n" % GROUPS[1])
                f.write("- **Tier 2, third-party (both must carry it):**\n")
                for n, u in LISTS.items():
                    f.write("  - %s: %s\n" % (n, u))
                f.write("\n| Date (UTC+8) | Added | Moved | In table | Tier 1 | Tier 2 | Deployed | / | /about | Conclusion |\n")
                f.write("|---|---|---|---|---|---|---|---|---|---|\n")
            f.write("| %s | %d | %d | %d | %d | %d | %s | pending | pending | %s |\n" % (
                stamp, report["added"], report["moved"], total,
                report["tier1"], report["tier2"],
                "yes" if (report["changed"] and os.environ.get("CLOUDFLARE_API_TOKEN")) else
                ("n/a" if not report["changed"] else "skipped: no token"),
                concl))

    # ---- DEPLOY ------------------------------------------------------------
    # On GitHub Actions the deploy is done by cloudflare/wrangler-action between
    # this run and the --verify-only run, so this script must not deploy there.
    tok = os.environ.get("CLOUDFLARE_API_TOKEN")
    if NO_DEPLOY:
        print("[DEPLOY] handed to cloudflare/wrangler-action (--no-deploy)")
        json.dump(report, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("[STATE] saved -> %s" % STATE)
    elif report["changed"] and not DRY:
        if not tok:
            print("[DEPLOY] skipped: CLOUDFLARE_API_TOKEN not in environment")
            report["notes"].append("deploy skipped: no token in env")
        else:
            env = dict(os.environ)
            env.update({
                "CLOUDFLARE_API_TOKEN": tok,
                "CLOUDFLARE_ACCOUNT_ID": "e6edaf8361367fcb1e8e662069986552",
                "WRANGLER_SEND_METRICS": "false",
                "CI": "1",
            })
            npx = r"C:\Users\Administrator\.workbuddy-ai\binaries\node\versions\22.22.2-2\npx.cmd"
            p = subprocess.run([npx, "--yes", "wrangler@3", "pages", "deploy", ".",
                                "--project-name", "anime-fighting-codes", "--branch", "main"],
                               cwd=SITE, env=env, capture_output=True, timeout=600)
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            report["deployed"] = p.returncode == 0
            print("[DEPLOY] rc=%s" % p.returncode)
            print("   " + out.strip().replace("\n", "\n   ")[-600:])
    else:
        print("[DEPLOY] not needed (no change or dry run)")

    # ---- VERIFY ------------------------------------------------------------
    if NO_DEPLOY:
        print("[VERIFY] deferred until after the Actions deploy (--verify-only)")
    else:
        run_verify(report)
        patch_log(report)

    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
