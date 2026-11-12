"""Owner dashboard (/admin): what is switched on, what is missing, library and usage.

Only Telegram accounts listed in OWNER_TELEGRAM_IDS can open it. It shows no secrets
(only whether a setting is present) and no manuscript text or personal data beyond
counts. Each missing item names the setting and the DEPLOYMENT.md section to follow.
"""
import html
import os

import aidetect
import auth
import institutions
import payments
import telegram_bot
import verify
import websearch
from engine_client import configured as engine_configured


def engine_problem():
    """Plain-language reason why the website cannot use the engine, or "" when the settings look right."""
    import engine_client
    url, key = os.environ.get("ENGINE_URL", "").strip(), os.environ.get("ENGINE_API_KEY", "")
    problems = []
    if not url:
        problems.append("ENGINE_URL is empty or not set on the website")
    elif "${{" in url:
        problems.append("ENGINE_URL still contains an unresolved ${{…}} reference")
    else:
        try:
            engine_client.engine_base_url()
        except engine_client.EngineError:
            host = url.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0]
            problems.append(f"ENGINE_URL does not point to the engine (host is “{host or 'empty'}”; expected "
                            "something like engine.railway.internal: the ${{engine.RAILWAY_PRIVATE_DOMAIN}} "
                            "reference did not resolve, or the engine service has another name)")
    if not key:
        problems.append("ENGINE_API_KEY is empty or not set on the website (the ${{engine.ENGINE_API_KEY}} "
                        "reference did not resolve: is the engine's own variable named exactly ENGINE_API_KEY?)")
    elif "${{" in key:
        problems.append("ENGINE_API_KEY still contains an unresolved ${{…}} reference")
    elif len(key) < 32:
        problems.append(f"ENGINE_API_KEY has only {len(key)} characters; it needs at least 32")
    return "; ".join(problems)


def checklist(library, probe=""):
    """[(area, ok, detail, how-to)]"""
    origin = os.environ.get("PUBLIC_ORIGIN", "")
    stats = auth.stats() if auth.enabled() else None
    providers = websearch.providers()
    rows = [
        ("Telegram sign-in", auth.enabled(), "on, @" + auth.public_config()["bot_username"] if auth.enabled() else "off",
         "TELEGRAM_BOT_TOKEN and TELEGRAM_BOT_USERNAME on the website (Part 3i)"),
        ("Email + password sign-in", auth.email_enabled() or None, "on (no email is sent; addresses are not verified)"
         if auth.email_enabled() else "off", "on by default; EMAIL_LOGIN=0 turns it off (Part 3i)"),
        ("Checks without an account", True if auth.anon_max_words() else None,
         f"up to {auth.anon_max_words():,} words (plagiarism only)" if auth.anon_max_words() else "off: every check needs sign-in",
         "ANON_MAX_WORDS on the website (Part 3i)"),
        ("Bot answers /start", telegram_bot.STATUS["ok"] if telegram_bot.enabled() else None,
         ("on" if telegram_bot.STATUS["ok"] else "being registered with Telegram" if telegram_bot.STATUS["ok"] is None
          else "Telegram refused the webhook: " + telegram_bot.STATUS["detail"])
         if telegram_bot.enabled() else "off (people who press /start in the bot get no reply)",
         "TELEGRAM_BOT_REPLIES=1 on the website, then redeploy (Part 3i)"),
        ("Visitors told apart (hourly limit)", os.environ.get("TRUSTED_PROXY_HOPS", "").strip() in ("1", "2", "3", "4", "5"),
         "on: each visitor has their own allowance" if os.environ.get("TRUSTED_PROXY_HOPS", "").strip() in ("1", "2", "3", "4", "5")
         else "off: all signed-out visitors share one allowance of 12 checks per hour",
         "TRUSTED_PROXY_HOPS=1 on the website (Part 1)"),
        ("Accounts kept across deploys", bool(stats and stats["on_volume"]),
         "stored on the /data volume" if stats and stats["on_volume"] else "not on a volume: accounts are lost at each deploy",
         "attach a volume at /data to the website service (Part 3i)"),
        ("QR verification", bool(verify.secret()) and origin.startswith("https://"),
         "on" if verify.secret() else "VERIFY_SECRET missing" if origin else "VERIFY_SECRET and PUBLIC_ORIGIN missing",
         "VERIFY_SECRET (40+ characters) and PUBLIC_ORIGIN on the website (Part 3f)"),
        ("OpenAlex key", bool(os.environ.get("OPENALEX_API_KEY")), "set" if os.environ.get("OPENALEX_API_KEY") else "missing",
         "OPENALEX_API_KEY on the website (Part 3c)"),
        ("More full texts (OpenAlex PDF copy)", os.environ.get("FULLTEXT_OPENALEX_PDF") == "1",
         "on (about $0.01 per paper)" if os.environ.get("FULLTEXT_OPENALEX_PDF") == "1" else "off: many publisher sites block downloads",
         "FULLTEXT_OPENALEX_PDF=1 on the website (Part 3e)"),
        ("Private engine connected", engine_configured() and bool(library and library.get("available")),
         "connected" if library and library.get("available") else
         (engine_problem() or "settings look right, but the engine did not answer") if not engine_configured()
         else engine_problem() or ("settings look right, but " + probe if probe else
                                   "settings look right, but the engine did not answer (is the engine Online? see its logs)"),
         "create the engine service and add ENGINE_URL and ENGINE_API_KEY to the website (Part 2)"),
        ("Web search", bool(providers), ", ".join(providers) if providers else "no key",
         "YANDEX_API_KEY + YANDEX_FOLDER_ID and/or BRAVE_API_KEY on the website (Part 3g)"),
        ("AI detector", aidetect.configured(), "Pangram" if aidetect.configured() else "no key",
         "PANGRAM_API_KEY on the website (Part 3j)"),
        ("Protected archive", engine_configured() and auth.enabled(), "on" if engine_configured() and auth.enabled() else "needs the engine and sign-in",
         "engine (Part 2) and Telegram sign-in (Part 3i); privacy notice reviewed (Part 3k)"),
        ("Payments (test mode only)", payments.enabled() or (None if os.environ.get("PAYMENTS_ENABLED") != "1" else False),
         "on: " + ", ".join(payments.providers()) + " (sandbox; no real money)" if payments.enabled() else
         "off (optional)" if os.environ.get("PAYMENTS_ENABLED") != "1" else payments.problem(),
         "PAYMENTS_ENABLED=1, PAYMENTS_MODE=test and the Payme, Click and/or Uzum test credentials on the website (Part 3o)"),
        ("Partner universities", bool(institutions.configured()) or None, ", ".join(institutions.configured()) or "none yet (optional)",
         "INSTITUTIONS on the website (Part 3l)"),
    ]
    return rows


def _size(n):
    return f"{n / 1024 ** 3:.2f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


NAMES = {"journals": "Uzbek and other journals", "cyberleninka": "CyberLeninka", "wikipedia": "Wikipedia",
         "university": "University archives", "archive": "Authors' archive", "other": "Other (OpenAlex etc.)"}
PROVIDERS = {"openalex": "OpenAlex import (open licence)", "core": "CORE import", "oai_journal": "Journal OAI feeds",
             "cyberleninka": "CyberLeninka", "wikipedia": "Wikipedia dumps", "author_deposit": "Author deposits (consent)",
             "institution_deposit": "University deposits (agreement)", "manual": "Owner manifests"}
DISK_WORDS = ("space", "disk is full", "paused: only")


def _table(head, rows):
    esc = html.escape
    return ("<table><tr>" + "".join(f"<th>{esc(h)}</th>" for h in head) + "</tr>"
            + "".join("<tr>" + "".join(f"<td>{esc(str(c))}</td>" for c in row) + "</tr>" for row in rows) + "</table>")


def details(inv):
    """Owner-only library details from the engine's /v1/inventory (no texts)."""
    esc = html.escape
    if not inv:
        return "<p>Details are not available: the engine did not answer /v1/inventory (redeploy the engine with this version).</p>"
    total = inv.get("total") or 0
    pct = lambda n: f"{n / total:.0%}" if total else "—"
    cov = inv.get("coverage") or {}
    out = ["<h3>Searchable full-text coverage</h3>",
           _table(["Language", "Indexed texts", "Full texts (300+ words)", "Short texts", "Searchable words", "Share"],
                  [(k.upper(), f"{v['sources']:,}", f"{v['full_texts']:,}", f"{v['short_texts']:,}", f"{v['words']:,}",
                    pct(v["sources"])) for k, v in sorted(cov.items())]),
           f'<p class="note">Every text listed here is held in full by the engine and searched word by word '
           f"({inv.get('windows', 0):,} seven-word fingerprints). OpenAlex records that have only metadata or an abstract are "
           "never stored here: in academic checks they are compared by their abstract only, or not at all when only "
           "metadata exists, and reports say so.</p>"]
    cl = inv.get("category_language") or {}
    out += ["<h3>By collection and language</h3>",
            _table(["Collection", "UZ", "RU", "EN", "Total", "Share"],
                   [(NAMES.get(c, c), *(f"{v.get(l, 0):,}" for l in ("uz", "ru", "en")), f"{sum(v.values()):,}",
                     pct(sum(v.values()))) for c, v in sorted(cl.items(), key=lambda kv: -sum(kv[1].values()))])]
    wiki = sum((cl.get("wikipedia") or {}).values())
    ceiling = (inv.get("limits") or {}).get("wikipedia_max_share", 0.10)
    if total and wiki / total > ceiling:
        out.append(f'<p class="warn">Wikipedia is {wiki / total:.0%} of the library (ceiling {ceiling:.0%}). Journals, '
                   "theses, conference papers and university repositories should come first: prefer AUTO_IMPORT_UZ_JOURNALS, "
                   "AUTO_IMPORT_CYBERLENINKA and AUTO_IMPORT, and see “Wikipedia share” below (DEPLOYMENT.md, Part 5e).</p>")
    out += ["<h3>By provider and rights basis</h3>",
            _table(["Provider", "Texts"], [(PROVIDERS.get(k, k), f"{v:,}") for k, v in sorted((inv.get("by_provider") or {}).items(), key=lambda kv: -kv[1])]),
            _table(["Rights basis", "Texts"], [(k, f"{v:,}") for k, v in sorted((inv.get("by_rights") or {}).items(), key=lambda kv: -kv[1])])]
    fresh = inv.get("freshness") or {}
    out += ["<h3>Indexing freshness</h3>",
            _table(["Collection", "Newest", "Oldest", "Added in 7 days", "Added in 30 days"],
                   [(NAMES.get(c, c), v.get("newest") or "—", v.get("oldest") or "—", v.get("last_7_days", 0), v.get("last_30_days", 0))
                    for c, v in sorted(fresh.items())])]
    limits = inv.get("limits") or {}
    deposits = sum(v for k, v in (inv.get("by_rights") or {}).items() if k in ("author_deposit", "institution_deposit"))
    rows = [("Imported sources (CORPUS_MAX_SOURCES)", f"{total - deposits:,} of {limits.get('corpus_max_sources', 0):,}"),
            ("Archive deposits (ARCHIVE_MAX_SOURCES)", f"{deposits:,} of {limits.get('archive_max_sources', 0):,}")]
    for lang in ("uz", "ru", "en"):
        have = (cl.get("wikipedia") or {}).get(lang, 0)
        if have:
            cap = limits.get("wikipedia_max_articles", 0)
            rows.append((f"Wikipedia {lang.upper()} (WIKIPEDIA_MAX_ARTICLES)", f"{have:,} indexed; cap {cap:,} per language"
                         + (" (the cap stops new imports only; texts already indexed are kept)" if have > cap else "")))
    out += ["<h3>Limits</h3>", _table(["Limit", "Use"], rows)]
    imports = inv.get("imports") or {}
    if imports:
        rows = []
        for name, v in sorted(imports.items()):
            error = str(v.get("error") or "")
            if error and any(w in error.lower() for w in DISK_WORDS) and not inv.get("disk_low_now"):
                error += " (at that time; there is enough free space now)"
            reasons = "; ".join(f"{k} ×{n}" for k, n in (v.get("failure_reasons") or {}).items())
            problem = (error[:220] + (f" [error at {str(v.get('error_at'))[:19]}]" if error and v.get("error_at") else "")) \
                if error else str(v.get("note") or "")[:200]
            rows.append((name, str(v.get("finished") or "—")[:19], str(v.get("last_success") or "—")[:19],
                         v.get("imported") if v.get("imported") is not None else "—", problem or "—", reasons[:300] or "—"))
        out += ["<h3>Import history</h3>",
                _table(["Import", "Last attempt", "Last success", "Imported", "Problem or note", "Main failure reasons (staged works)"], rows)]
    if inv.get("disk_low_now"):
        out.append(f'<p class="warn">Now: {esc(inv["disk_low_now"])}</p>')
    return "".join(out)


CONFIRM_WORDS = "DELETE WIKIPEDIA"


def prune_form(inv=None, result=None, error="", keep=100):
    """Owner form: keep the longest N Wikipedia articles per language, preview first, then confirm."""
    esc = html.escape
    out = []
    if inv:
        total = inv.get("total") or 0
        wiki = sum(((inv.get("category_language") or {}).get("wikipedia") or {}).values())
        ceiling = (inv.get("limits") or {}).get("wikipedia_max_share", 0.10)
        share = wiki / total if total else 0
        out.append(f'<p class="{"warn" if share > ceiling else "note"}">Wikipedia: {wiki:,} of {total:,} texts ({share:.0%}); '
                   f"ceiling {ceiling:.0%} (WIKIPEDIA_MAX_SHARE). While above it, no new Wikipedia articles are imported; "
                   "journal, thesis and university texts are never limited by it.</p>")
    out.append('<p class="note">Removing articles keeps the longest ones per language and never touches journals, theses, '
               "CyberLeninka or the authors' and universities' archives. <b>Turn on or take a Railway volume backup of the "
               "engine first.</b> The list of removed articles (title, address, language) is saved in the engine's staging "
               "folder; Wikipedia texts can be imported again later.</p>")
    out.append('<form method="post" action="/admin/prune" class="diag">'
               f'<label>Keep this many Wikipedia articles per language <input name="keep" type="number" min="0" max="100000" value="{int(keep)}"></label>'
               f'<label>To delete, type <b>{CONFIRM_WORDS}</b> (leave empty to only preview) <input name="confirm_text" maxlength="40"></label>'
               '<button type="submit">Preview or remove</button></form>')
    if error:
        out.append(f'<p class="warn">{esc(error)}</p>')
    if result:
        langs = sorted(set(result.get("remove") or {}) | set(result.get("keep") or {}))
        out.append(_table(["Language", "Remove", "Keep"], [(l.upper(), f"{result['remove'].get(l, 0):,}", f"{result['keep'].get(l, 0):,}") for l in langs]))
        after_total, after_wiki = result.get("total_after") or 0, result.get("collection_after") or 0
        verdict = (f"Removed. List saved as {result.get('record')}." if result.get("confirmed") else "Preview only: nothing was removed.")
        out.append(f"<p><b>{esc(verdict)}</b> Afterwards: Wikipedia {after_wiki:,} of {after_total:,} texts "
                   f"({after_wiki / after_total:.0%}).</p>" if after_total else f"<p><b>{esc(verdict)}</b></p>")
    return "".join(out)


def diagnostic_form(result=None, error="", doi="", title=""):
    esc = html.escape
    form = ('<form method="post" action="/admin/diagnostic" class="diag">'
            f'<label>DOI <input name="doi" maxlength="300" value="{esc(doi)}" placeholder="10.1234/abcd"></label>'
            f'<label>or title <input name="title" maxlength="500" value="{esc(title)}" placeholder="at least 8 characters"></label>'
            '<label>Passage to search (optional; otherwise 60 words from the middle of the indexed text are used, '
            'and never shown) <textarea name="passage" maxlength="5000" rows="3"></textarea></label>'
            '<button type="submit">Check source</button></form>')
    if error:
        form += f'<p class="warn">{esc(error)}</p>'
    if result is not None:
        if not result.get("results"):
            form += '<p class="warn">Not in the library: no indexed source has this DOI in its address or this title.</p>'
        for r in result.get("results") or []:
            p = r.get("passage") or {}
            verdict = (f"✅ found (rank {p.get('rank')}, {p.get('matched_words', 0)} matching words)" if p.get("found")
                       else "❌ not found by the search")
            form += _table(["Fact", "Value"], [
                ("Title", f"{r.get('title')} ({r.get('year') or '—'})"), ("Language", str(r.get("language", "")).upper()),
                ("Collection / provider", f"{NAMES.get(r.get('category'), r.get('category'))} / {PROVIDERS.get(r.get('provider'), r.get('provider'))}"),
                ("Rights basis", str(r.get("rights_basis")) + (" (unpublished deposit: its text is never shown)" if r.get("deposit") else "")),
                ("Address", r.get("url") or "—"), ("Words / indexed on", f"{r.get('words') or 0:,} / {r.get('imported_at') or '—'}"),
                ("Fingerprints", f"{r.get('fingerprints', 0):,} " + ("(searchable)" if r.get("searchable") else "(NOT searchable)")),
                ("Passage test", f"{p.get('source')}, {p.get('words')} words: {verdict}"
                 + (f"; {p.get('other_sources_matched')} other source(s) also matched" if p.get("other_sources_matched") else "")
                 + (f"; {p.get('boilerplate_windows')} common phrase(s) ignored as boilerplate" if p.get("boilerplate_windows") else ""))])
        if (result.get("matches") or 0) > 3:
            form += f"<p>{result['matches']} sources matched; the first 3 are shown. Use the DOI or a longer title.</p>"
    return form


def page(library, probe="", inventory=None, diagnostic="", pruning=None):
    esc = html.escape
    mark = lambda ok: "✅" if ok else "➖" if ok is None else "❌"          # None = optional, not set
    rows = "".join(f'<tr><td>{mark(ok)}</td><th>{esc(a)}</th><td>{esc(d)}</td><td class="h">{"" if ok else esc(h)}</td></tr>'
                   for a, ok, d, h in checklist(library, probe))
    lib = "<p>The engine is not connected, so there is no library yet.</p>"
    if library and library.get("available"):
        names = {"journals": "Uzbek and other journals", "cyberleninka": "CyberLeninka", "wikipedia": "Wikipedia",
                 "university": "University archives", "archive": "Authors' archive", "other": "Other (OpenAlex etc.)"}
        by = library.get("by_source") or {}
        lib = "<table>" + "".join(f"<tr><th>{esc(names.get(k, k))}</th><td>{v:,}</td></tr>" for k, v in by.items())
        lib += f"<tr><th>Total</th><td><b>{library.get('total', 0):,}</b></td></tr></table>"
        disk = library.get("disk")
        if disk and disk.get("total_bytes"):
            used = 1 - disk["free_bytes"] / disk["total_bytes"]
            warn = ' class="warn"' if used > 0.66 else ""
            lib += (f"<p{warn}>Engine disk: library file {_size(disk['db_bytes'])}; downloaded files waiting "
                    f"{_size(disk.get('staging_bytes', 0))}; volume {used:.0%} used "
                    f"({_size(disk['free_bytes'])} free of {_size(disk['total_bytes'])}). "
                    + ("Over two thirds used: do not raise CORPUS_MAX_SOURCES; enlarge the volume first." if used > 0.66 else
                       "Room to raise CORPUS_MAX_SOURCES step by step.") + "</p>")
        imports = library.get("imports") or {}
        if imports:
            lib += "<h3>Automatic imports</h3><table><tr><th>Import</th><th>Last run</th><th>Imported</th><th>Problem or note</th></tr>"
            lib += "".join(f"<tr><td>{esc(k)}</td><td>{esc(str(v.get('finished') or '—'))[:19]}</td>"
                           f"<td>{esc(str(v.get('imported') if v.get('imported') is not None else '—'))}</td>"
                           f"<td>{esc(str(v.get('error') or ''))[:160]}"
                           + (f"<span class=\"note\">{esc(str(v['note']))[:160]}</span>" if v.get("note") else "")
                           + "</td></tr>" for k, v in sorted(imports.items()))
            lib += "</table>"
        else:
            lib += "<p>No automatic import has run yet (AUTO_IMPORT… settings on the engine, Parts 2, 3m, 3n).</p>"
    stats = auth.stats() if auth.enabled() else None
    use = "<p>Sign-in is off, so usage is not counted.</p>"
    if stats:
        modes = ", ".join(f"{k} {v}" for k, v in sorted((stats["modes_week"] or {}).items())) or "—"
        use = (f"<table><tr><th>Accounts</th><td>{stats['users']:,}</td></tr><tr><th>Checks today</th><td>{stats['checks_today']:,}</td></tr>"
               f"<tr><th>Checks in the last 7 days</th><td>{stats['checks_week']:,} ({esc(modes)})</td></tr>"
               f"<tr><th>Checks in total</th><td>{stats['checks_total']:,}</td></tr></table>")
    pay = "<p>Payments are off.</p>"
    pstats = payments.stats() if payments.enabled() else None
    if pstats:
        rows_paid = "".join(f"<tr><th>{esc(p['provider'])} ({esc(p['mode'])})</th><td>{p['orders']:,} paid top-ups, "
                            f"{p['uzs']:,} so'm</td></tr>" for p in pstats["paid"]) or "<tr><th>Paid top-ups</th><td>none yet</td></tr>"
        pay = (f"<p class=\"warn\">Test mode: amounts are sandbox payments, not real money.</p><table>{rows_paid}"
               f"<tr><th>Unpaid orders</th><td>{pstats['pending']:,}</td></tr>"
               f"<tr><th>Paid checks</th><td>{pstats['checks_charged']:,} ({pstats['charged_uzs']:,} so'm after refunds)</td></tr>"
               f"<tr><th>Balances held for users</th><td>{pstats['balances_uzs']:,} so'm</td></tr></table>")
    backup = ('<ul><li><a href="/admin/backup/archive.jsonl.gz">Download the archive deposits</a> (students\' and universities\' '
              'works: the only library content that cannot be downloaded again). Restore with '
              '<code>python corpus.py --db /data/corpus.sqlite3 --restore-archive FILE</code> in the engine shell.</li>'
              '<li><a href="/admin/backup/accounts.sqlite3">Download the accounts database</a> (Telegram names, check log, balances and payment records).</li></ul>'
              '<p>Keep backups encrypted (for example in a password-protected archive) in a place only you control. '
              'Also turn on Railway volume backups for the engine and the website (DEPLOYMENT.md, Part 5b).</p>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>PlagIQ owner dashboard</title><style>body{{font-family:system-ui,sans-serif;margin:0;background:#f7f9f7;color:#122a32}}'
            f'main{{max-width:980px;margin:24px auto;padding:0 16px}}section{{background:#fff;border:1px solid #dce6e5;border-radius:14px;padding:18px;margin:0 0 16px;overflow-x:auto}}'
            f'h1{{font-size:24px}}h2{{font-size:18px;margin:0 0 10px}}h3{{font-size:15px}}table{{border-collapse:collapse;width:100%;font-size:14px}}'
            f'th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #e6eeed;vertical-align:top}}td.h{{color:#7a4a00;font-size:13px}}'
            f'.warn{{color:#9b2c16;font-weight:700}}.note{{color:#51676a}}.diag label{{display:block;margin:6px 0}}.diag input,.diag textarea{{width:100%;padding:6px;border:1px solid #cfe0dd;border-radius:8px;font:inherit}}.diag button{{margin-top:6px;padding:8px 14px;border-radius:8px;border:1px solid #006D70;background:#006D70;color:#fff;font-weight:700}}code{{background:#f2f4f4;padding:1px 4px;border-radius:4px}}</style></head><body><main>'
            f'<h1>PlagIQ owner dashboard</h1><section><h2>Setup checklist</h2><table>{rows}</table></section>'
            f'<section><h2>Library</h2>{lib}</section>'
            f'<section><h2>Library details</h2>{details(inventory) if library and library.get("available") else "<p>The engine is not connected.</p>"}</section>'
            f'<section id="source-check"><h2>Source check (DOI or title)</h2>{diagnostic or diagnostic_form()}</section>'
            f'<section id="wikipedia-share"><h2>Wikipedia share</h2>{pruning if pruning is not None else (prune_form(inventory) if inventory else "<p>The engine is not connected.</p>")}</section>'
            f'<section><h2>Usage</h2>{use}</section><section><h2>Payments</h2>{pay}</section>'
            f'<section><h2>Backups</h2>{backup}</section></main></body></html>')
