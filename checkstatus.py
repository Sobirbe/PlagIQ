"""One explicit status for every check, so a failed or partial check never reads as a clean result.

Finished checks (HTTP 200):
  completed_matches      every planned source was searched; verified matches were found
  completed_no_matches   every planned source was searched; no verified match (this is not proof of originality)
  incomplete_coverage    part of the planned coverage could not be searched; the percentage covers only what was
                         compared and is shown as "at least". `coverage_issues` says what was missing.
  failed_search          nothing could be compared because the search service failed (no percentage)
  ai_only                AI-detector check; no plagiarism comparison
Errors (HTTP 4xx/5xx) carry `status` too: failed_extraction, failed_search, failed_internal or rejected.

Only shortfalls that a later check may not have are "incomplete": failed search queries, downloads cut off by the
time limit or a website that could not be reached, the PlagIQ library unreachable, web-search errors, no source in
the document's language. A publisher that refuses downloads or offers no PDF is a property of that source, reported
in the coverage figures but not as incomplete.
"""
import logging

log = logging.getLogger("plagiq.check")

EXTRACTION_CODES = {"empty_file", "unsupported_type", "txt_encoding", "doc_protected", "doc_not_word", "extract_failed",
                    "docx_expanded", "pdf_pages", "no_text", "ocr_too_many_pages", "ocr_timeout", "ocr_busy",
                    "file_too_large", "text_too_long", "too_short", "reference_too_short", "reference_too_long"}
SEARCH_CODES = {"engine_unavailable", "engine_busy", "engine_timeout", "engine_not_configured", "engine_error",
                "provider_unavailable"}
TRANSIENT_DOWNLOAD = {"timed out", "website not reachable"}

T = {
    "en": dict(
        label="Check status",
        eyebrow_done="ANALYSIS COMPLETE", eyebrow_incomplete="CHECK INCOMPLETE", eyebrow_failed="SEARCH FAILED",
        completed_matches="Completed: matches found", completed_no_matches="Completed: no matches found",
        incomplete_coverage="Incomplete: part of the sources could not be searched", failed_search="Failed: source search",
        ai_only="AI-detector check", at_least="at least {p}%",
        incomplete_note="The percentage covers only the sources that were compared; the missing ones could contain more "
                        "matching text. Run the check again later for full coverage.",
        issues=dict(search_partial="{n} of the search queries failed", downloads_cut_off="{n} full text(s) could not be downloaded in time",
                    library_unavailable="the PlagIQ library could not be searched", web_search_failed="web search failed ({n} error(s))",
                    no_comparable_sources="no source with comparable text was found", no_sources_in_language="the library has no texts in this language")),
    "ru": dict(
        label="Статус проверки",
        eyebrow_done="ПРОВЕРКА ЗАВЕРШЕНА", eyebrow_incomplete="ПРОВЕРКА НЕПОЛНАЯ", eyebrow_failed="ПОИСК НЕ ВЫПОЛНЕН",
        completed_matches="Завершено: совпадения найдены", completed_no_matches="Завершено: совпадений не найдено",
        incomplete_coverage="Не полностью: часть источников не удалось проверить", failed_search="Ошибка: поиск источников",
        ai_only="Проверка ИИ-детектором", at_least="не менее {p}%",
        incomplete_note="Процент относится только к сравнённым источникам; в непроверенных может быть ещё совпадающий текст. "
                        "Повторите проверку позже для полного охвата.",
        issues=dict(search_partial="не выполнено поисковых запросов: {n}", downloads_cut_off="полных текстов не скачано вовремя: {n}",
                    library_unavailable="библиотека PlagIQ была недоступна", web_search_failed="ошибка веб-поиска (ошибок: {n})",
                    no_comparable_sources="не найдено источников с текстом для сравнения", no_sources_in_language="в библиотеке нет текстов на этом языке")),
    "uz": dict(
        label="Tekshiruv holati",
        eyebrow_done="TEKSHIRUV TUGADI", eyebrow_incomplete="TEKSHIRUV TO‘LIQ EMAS", eyebrow_failed="QIDIRUV BAJARILMADI",
        completed_matches="Yakunlandi: mosliklar topildi", completed_no_matches="Yakunlandi: moslik topilmadi",
        incomplete_coverage="To‘liq emas: manbalarning bir qismi tekshirilmadi", failed_search="Xato: manbalarni qidirish",
        ai_only="AI detektor tekshiruvi", at_least="kamida {p}%",
        incomplete_note="Foiz faqat solishtirilgan manbalarga tegishli; tekshirilmaganlarida yana mos matn bo‘lishi mumkin. "
                        "To‘liq qamrov uchun keyinroq qayta tekshiring.",
        issues=dict(search_partial="qidiruv so‘rovlaridan {n} tasi bajarilmadi", downloads_cut_off="{n} ta to‘liq matn vaqtida yuklab olinmadi",
                    library_unavailable="PlagIQ kutubxonasi ishlamadi", web_search_failed="veb-qidiruv xatosi ({n} ta)",
                    no_comparable_sources="solishtirish uchun matnli manba topilmadi", no_sources_in_language="kutubxonada bu tildagi matnlar yo‘q")),
}


def texts(lang):
    return T.get(lang, T["en"])


def coverage_issues(result):
    """What part of the planned coverage is missing, as [{"code", "count"}]."""
    issues = []
    if result.get("mode") == "academic":
        if result.get("database_status") == "partial":
            failed = max(1, int(result.get("failed_queries") or 1))
            issues.append({"code": "search_partial", "count": failed})
        fulltext = result.get("fulltext") or {}
        reasons = fulltext.get("reasons") or {}
        cut = sum(n for label, n in reasons.items() if label in TRANSIENT_DOWNLOAD)
        if cut:
            issues.append({"code": "downloads_cut_off", "count": cut})
        library = result.get("library") or {}
        if library.get("error"):
            issues.append({"code": "library_unavailable", "count": 1})
        web = result.get("web") or {}
        if web.get("enabled") and web.get("errors"):
            issues.append({"code": "web_search_failed", "count": len(web["errors"])})
    reason = result.get("not_evaluated_reason")
    if result.get("percent") is None and reason in ("no_abstracts", "no_sources_in_language"):
        issues.append({"code": "no_comparable_sources" if reason == "no_abstracts" else reason, "count": 1})
    return issues


def assess(result):
    """Set result["status"] and result["coverage_issues"]; log anything that is not a clean completion."""
    issues = coverage_issues(result)
    if result.get("mode") == "ai":
        status = "ai_only"
    elif result.get("percent") is None and result.get("not_evaluated_reason") == "provider_unavailable":
        status = "failed_search"
    elif issues or result.get("percent") is None:
        status = "incomplete_coverage"
    elif int(result.get("matched_tokens") or 0) > 0:
        status = "completed_matches"            # before exclusions: excluded quotations are still matches found
    else:
        status = "completed_no_matches"
    result["status"], result["coverage_issues"] = status, issues
    if status in ("incomplete_coverage", "failed_search"):
        log.warning("check %s: mode=%s language=%s issues=%s", status, result.get("mode"), result.get("language"),
                    ",".join(f"{i['code']}:{i['count']}" for i in issues) or result.get("not_evaluated_reason"))
    return result


def error_status(code, http_status):
    """Status for an error response (no result was produced)."""
    if code in EXTRACTION_CODES:
        return "failed_extraction"
    if code in SEARCH_CODES or str(code).startswith("engine_"):
        return "failed_search"
    if http_status >= 500:
        return "failed_internal"
    return "rejected"


def headline(result, lang):
    """The percentage as it should be read: "at least X%" when coverage was incomplete."""
    percent = result.get("percent")
    if percent is None:
        return None
    if result.get("status") == "incomplete_coverage":
        return texts(lang)["at_least"].format(p=percent)
    return f"{percent}%"


def rows(result, lang):
    """(label, value) rows for the PDF report and certificate: the status, and what was missing."""
    label, issues = describe(result, lang)
    out = [(texts(lang)["label"], label + (": " + "; ".join(issues) if issues else ""))]
    if result.get("status") == "incomplete_coverage" and result.get("percent") is not None:
        out[0] = (out[0][0], out[0][1] + ". " + texts(lang)["incomplete_note"])
    return out


def describe(result, lang):
    """(status label, list of issue sentences)."""
    t = texts(lang)
    status = result.get("status") or "completed_no_matches"
    return t.get(status, status), [t["issues"].get(i["code"], i["code"]).format(n=i.get("count", 1))
                                   for i in result.get("coverage_issues") or []]
