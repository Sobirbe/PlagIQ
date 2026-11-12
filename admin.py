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
        ("Bot answers /start", telegram_bot.STATUS["ok"] if telegram_bot.enabled() else None,
         ("on" if telegram_bot.STATUS["ok"] else "being registered with Telegram" if telegram_bot.STATUS["ok"] is None
          else "Telegram refused the webhook: " + telegram_bot.STATUS["detail"])
         if telegram_bot.enabled() else "off (people who press /start in the bot get no reply)",
         "TELEGRAM_BOT_REPLIES=1 on the website, then redeploy (Part 3i)"),
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


def page(library, probe=""):
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
            lib += "<h3>Automatic imports</h3><table><tr><th>Import</th><th>Last run</th><th>Imported</th><th>Problem</th></tr>"
            lib += "".join(f"<tr><td>{esc(k)}</td><td>{esc(str(v.get('finished') or '—'))[:19]}</td>"
                           f"<td>{esc(str(v.get('imported') if v.get('imported') is not None else '—'))}</td>"
                           f"<td>{esc(str(v.get('error') or ''))[:160]}</td></tr>" for k, v in sorted(imports.items()))
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
            f'.warn{{color:#9b2c16;font-weight:700}}code{{background:#f2f4f4;padding:1px 4px;border-radius:4px}}</style></head><body><main>'
            f'<h1>PlagIQ owner dashboard</h1><section><h2>Setup checklist</h2><table>{rows}</table></section>'
            f'<section><h2>Library</h2>{lib}</section><section><h2>Usage</h2>{use}</section><section><h2>Payments</h2>{pay}</section>'
            f'<section><h2>Backups</h2>{backup}</section></main></body></html>')
