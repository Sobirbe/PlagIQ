"""Cover page of the PDF report: a certificate of the CHECK, with a verification QR code.

It certifies what PlagIQ did (which document, when, in which scope, with which
result), never that the document is original or free of plagiarism. The figures
are the same signed facts that the QR code's /verify page confirms.
"""
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph
from reportlab.lib.styles import ParagraphStyle

C = {
    "en": dict(
        title="CERTIFICATE OF CHECK", number="Certificate No.",
        intro="This certifies that the document below was checked by PlagIQ on the date shown, "
              "with the scope and result stated on this page.",
        document="Document", date="Checked on", words="Words", language="Language", fingerprint="Document fingerprint (SHA-256)",
        result="Matched within the checked scope", not_evaluated="Not evaluated", before="before exclusions: {p}%",
        sources="Sources compared", src_academic="Full texts: {f} · abstracts: {a}{w}", src_web=" · web pages: {n}",
        src_ai="Not applicable (AI check only)", scope_ai="AI-writing detector only (Pangram); no plagiarism comparison", ai_row="AI detector (Pangram estimate)", ai_v="AI {a}% · AI-assisted {s}% · human {h}%", ai_unsupported="Not available for Uzbek (not sent)", ai_error="Could not be completed", ai_too_long="Too long for the AI detector (over {n} words)", ai_ring="AI-written or AI-assisted (Pangram estimate)", 
        src_reference="1 reference document", src_corpus="{n} indexed sources",
        exclusions="Exclusions", ex_none="No exclusions", ex_q="quotations", ex_b="bibliography", ex_s="matches under {m} words",
        integrity="AI and manipulation indicators", ind_none="No indicators found", ind_review="Review suggested ({n})",
        scope="Scope", scopes=dict(academic="Open academic publications found through OpenAlex{w}",
                                   reference="Comparison with one reference document supplied by the user",
                                   corpus="PlagIQ private corpus of authorized texts"),
        scope_web=" and the web ({p})", scope_library=", and the PlagIQ library", src_library=" · PlagIQ library: {n}",
        verify="Verify this certificate", verify_v="Scan the QR code, or open the link, to confirm that PlagIQ issued it with these figures.",
        verify_off="Online verification is not enabled on this server.",
        issued="Issued electronically by PlagIQ", details="Full evidence (every matched passage and source) on the following pages.",
        notice="This certifies the check only. It is not a certificate of originality or of the absence of plagiarism, "
               "and any AI figure is the named provider's estimate, not proof. A low result does not prove originality; matches need human review."),
    "ru": dict(
        title="СЕРТИФИКАТ ПРОВЕРКИ", number="Сертификат №",
        intro="Настоящим подтверждается, что указанный ниже документ проверен PlagIQ в указанную дату "
              "в охвате и с результатом, приведёнными на этой странице.",
        document="Документ", date="Дата проверки", words="Слов", language="Язык", fingerprint="Отпечаток документа (SHA-256)",
        result="Совпадения в проверенном охвате", not_evaluated="Не оценено", before="до исключений: {p}%",
        sources="Сравнено источников", src_academic="полных текстов {f} · аннотаций {a}{w}", src_web=" · веб-страниц {n}",
        src_ai="Не применимо (только ИИ-проверка)", scope_ai="Только ИИ-детектор (Pangram); сравнение на плагиат не проводилось", ai_row="ИИ-детектор (оценка Pangram)", ai_v="ИИ {a}% · с помощью ИИ {s}% · человек {h}%", ai_unsupported="Недоступно для узбекского (текст не отправлялся)", ai_error="Не удалось выполнить", ai_too_long="Слишком длинный для ИИ-детектора (более {n} слов)", ai_ring="Написано ИИ или с помощью ИИ (оценка Pangram)", 
        src_reference="1 документ-источник", src_corpus="индексированных источников: {n}",
        exclusions="Исключения", ex_none="Нет", ex_q="цитаты", ex_b="список литературы", ex_s="совпадения короче {m} слов",
        integrity="Индикаторы ИИ и манипуляций", ind_none="Не найдены", ind_review="Рекомендуется проверка ({n})",
        scope="Охват", scopes=dict(academic="Открытые научные публикации, найденные через OpenAlex{w}",
                                   reference="Сравнение с одним документом, предоставленным пользователем",
                                   corpus="Внутренний корпус PlagIQ с разрешёнными текстами"),
        scope_web=" и интернет ({p})",
        scope_library=", а также библиотека PlagIQ", src_library=" · библиотека PlagIQ: {n}",
        verify="Проверить сертификат", verify_v="Отсканируйте QR-код или откройте ссылку, чтобы убедиться, что PlagIQ выдал его с этими данными.",
        verify_off="Онлайн-проверка на этом сервере не настроена.",
        issued="Выдан в электронном виде PlagIQ", details="Полные доказательства (все совпадения и источники) — на следующих страницах.",
        notice="Подтверждается только факт проверки. Это не сертификат оригинальности или отсутствия плагиата, "
               "а любая оценка ИИ — оценка указанного провайдера, а не доказательство. Низкий результат не доказывает оригинальность; совпадения требуют проверки человеком."),
    "uz": dict(
        title="TEKSHIRUV SERTIFIKATI", number="Sertifikat №",
        intro="Ushbu sertifikat quyidagi hujjat ko‘rsatilgan sanada PlagIQ tomonidan shu sahifada "
              "keltirilgan doira va natija bilan tekshirilganini tasdiqlaydi.",
        document="Hujjat", date="Tekshirilgan sana", words="So‘zlar", language="Til", fingerprint="Hujjat izi (SHA-256)",
        result="Tekshirilgan doiradagi moslik", not_evaluated="Baholanmagan", before="istisnolargacha: {p}%",
        sources="Solishtirilgan manbalar", src_academic="{f} ta to‘liq matn · {a} ta annotatsiya{w}", src_web=" · {n} ta veb-sahifa",
        src_ai="Qo‘llanilmaydi (faqat AI tekshiruvi)", scope_ai="Faqat AI detektor (Pangram); plagiatga solishtirilmadi", ai_row="AI detektor (Pangram bahosi)", ai_v="AI {a}% · AI yordamida {s}% · inson {h}%", ai_unsupported="O‘zbek tili uchun mavjud emas (yuborilmadi)", ai_error="Bajarib bo‘lmadi", ai_too_long="AI detektor uchun juda uzun ({n} so‘zdan ortiq)", ai_ring="AI yozgan yoki AI yordamida (Pangram bahosi)", 
        src_reference="1 ta manba hujjat", src_corpus="{n} ta indekslangan manba",
        exclusions="Istisnolar", ex_none="Yo‘q", ex_q="iqtiboslar", ex_b="adabiyotlar ro‘yxati", ex_s="{m} so‘zdan qisqa mosliklar",
        integrity="Sun’iy intellekt va manipulyatsiya ko‘rsatkichlari", ind_none="Topilmadi", ind_review="Tekshirish tavsiya etiladi ({n})",
        scope="Doira", scopes=dict(academic="OpenAlex orqali topilgan ochiq ilmiy nashrlar{w}",
                                   reference="Foydalanuvchi bergan bitta hujjat bilan solishtirish",
                                   corpus="PlagIQ ichki korpusidagi ruxsat berilgan matnlar"),
        scope_web=" va internet ({p})",
        scope_library=" hamda PlagIQ kutubxonasi", src_library=" · PlagIQ kutubxonasi: {n}",
        verify="Sertifikatni tekshirish", verify_v="PlagIQ uni aynan shu ma’lumotlar bilan berganini tasdiqlash uchun QR kodni skanerlang yoki havolani oching.",
        verify_off="Bu serverda onlayn tekshirish sozlanmagan.",
        issued="PlagIQ tomonidan elektron shaklda berilgan", details="To‘liq dalillar (barcha mos parchalar va manbalar) keyingi sahifalarda.",
        notice="Faqat tekshiruv fakti tasdiqlanadi. Bu originallik yoki plagiat yo‘qligi sertifikati emas va har qanday AI ko‘rsatkichi "
               "ko‘rsatilgan provayderning bahosi, isbot emas. Past natija originallikni isbotlamaydi; mosliklarni inson ko‘rib chiqishi kerak."),
}

TEAL, INK, MUTED, LINE, TINT, PAPER = (colors.HexColor(x) for x in
                                       ("#006D70", "#122A32", "#4C6368", "#D3DFE3", "#EEF7F5", "#FBFDFC"))


def _short(name, limit=70):
    """At most two lines on the certificate; the full name is in the evidence pages."""
    name = str(name)
    return name if len(name) <= limit else name[:limit - 22] + "…" + name[-21:]


def facts(result, lang):
    """Certificate rows from the cached report summary (no manuscript text)."""
    t = C.get(lang, C["en"])
    mode = result.get("mode", "reference")
    web = result.get("web") or {}
    if mode == "academic":
        abstracts = result["abstract_count"] if "abstract_count" in result else \
            sum(1 for s in result.get("sources") or [] if s.get("coverage") == "abstract")
        library = result.get("library") or {}
        sources = t["src_academic"].format(f=(result.get("fulltext") or {}).get("compared", 0), a=abstracts,
                                           w=t["src_web"].format(n=web.get("compared", 0)) if web.get("enabled") else "")
        if library.get("searched"):
            sources += t["src_library"].format(n=library.get("size", 0))
    elif mode == "ai":
        sources = t["src_ai"]
    elif mode == "corpus":
        sources = t["src_corpus"].format(n=result.get("source_count") or 0)
    else:
        sources = t["src_reference"]
    c = result.get("citation") or {}
    o, chosen = c.get("exclusions") or {}, []
    if o.get("quotations"):
        chosen.append(t["ex_q"])
    if o.get("bibliography"):
        chosen.append(t["ex_b"])
    if (o.get("min_words") or 7) > 7:
        chosen.append(t["ex_s"].format(m=o["min_words"]))
    integrity = result.get("integrity") or {}
    signals = len(integrity.get("signals") or [])
    providers = ", ".join(web.get("providers") or [])
    scope = t["scope_ai"] if mode == "ai" else t["scopes"].get(mode, "").format(w=t["scope_web"].format(p=providers) if web.get("enabled") else "")
    if mode == "academic" and (result.get("library") or {}).get("searched"):
        scope += t["scope_library"]
    rows = [(t["sources"], sources)] + ([] if mode == "ai" else [(t["exclusions"], ", ".join(chosen) or t["ex_none"])])
    ai = result.get("ai") or {}
    if ai.get("status") == "done":
        rows.append((t["ai_row"], t["ai_v"].format(a=ai["fraction_ai"], s=ai["fraction_ai_assisted"], h=ai["fraction_human"])))
    elif ai.get("status") in ("unsupported_language", "error", "too_long"):
        rows.append((t["ai_row"], t["ai_unsupported"] if ai["status"] == "unsupported_language" else
                     t["ai_too_long"].format(n=ai.get("max_words", "")) if ai["status"] == "too_long" else t["ai_error"]))
    return rows + [
            (t["integrity"], t["ind_review"].format(n=signals) if integrity.get("status") == "review" else t["ind_none"]),
            (t["scope"], scope)]


class CertificatePage(Flowable):
    """Draws the whole first page inside the document frame."""

    def __init__(self, result, identifier, lang, verify_url, qr, fonts):
        super().__init__()
        self.result, self.identifier, self.lang, self.verify_url, self.qr = result, identifier, lang, verify_url, qr
        self.regular, self.bold = fonts

    def wrap(self, width, height):
        self.width, self.height = width, height - 1
        return self.width, self.height

    def _style(self, size=9, font=None, color=INK, align=0):
        return ParagraphStyle("c", fontName=font or self.regular, fontSize=size, leading=size * 1.35,
                              textColor=color, alignment=align, wordWrap="LTR", splitLongWords=1)

    def _para(self, text, x, y_top, width, size=9, font=None, color=INK, align=0):
        p = Paragraph(text, self._style(size, font, color, align))
        _, h = p.wrapOn(self.canv, width, 1000)
        p.drawOn(self.canv, x, y_top - h)
        return y_top - h

    def draw(self):
        import html
        c, t, r = self.canv, C.get(self.lang, C["en"]), self.result
        esc = lambda v: html.escape(str(v))
        w, h = self.width, self.height
        # frame
        c.setFillColor(PAPER)
        c.setStrokeColor(TEAL)
        c.setLineWidth(1.6)
        c.roundRect(0, 0, w, h, 6 * mm, stroke=1, fill=1)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.roundRect(3 * mm, 3 * mm, w - 6 * mm, h - 6 * mm, 4.5 * mm, stroke=1, fill=0)
        pad = 12 * mm
        inner = w - 2 * pad
        y = h - pad
        # header
        c.setFillColor(TEAL)
        c.setFont(self.bold, 20)
        c.drawString(pad, y - 18, "PlagIQ")
        number = "-".join(self.identifier[i:i + 4] for i in range(0, 12, 4)).upper()
        c.setFont(self.regular, 8.5)
        c.setFillColor(MUTED)
        c.drawRightString(w - pad, y - 9, t["number"])
        c.setFont(self.bold, 11)
        c.setFillColor(INK)
        c.drawRightString(w - pad, y - 22, number)
        y -= 34
        c.setStrokeColor(TEAL)
        c.setLineWidth(0.8)
        c.line(pad, y, w - pad, y)
        y -= 26
        c.setFont(self.bold, 21)
        c.setFillColor(INK)
        c.drawCentredString(w / 2, y - 6, t["title"])
        y = self._para(esc(t["intro"]), pad + 10 * mm, y - 18, inner - 20 * mm, 9.5, color=MUTED, align=1) - 14

        # document block: measure, draw the panel, then the text on top
        date = str(r.get("date", ""))[:16].replace("T", " ") + " UTC"
        cols = [(t["date"], date), (t["words"], f"{int(r.get('total_tokens') or 0):,}".replace(",", " ")),
                (t["language"], str(r.get("language", "")).upper()),
                (t["fingerprint"], " ".join(str(r.get("sha256", ""))[i:i + 4] for i in range(0, 16, 4)) + " …")]
        widths = [(inner - 12 * mm) * f for f in (0.25, 0.15, 0.15, 0.45)]
        lefts = [pad + 6 * mm + sum(widths[:i]) for i in range(4)]
        name = self._style(13, self.bold)
        name_h = Paragraph(esc(_short(r.get("document", ""))), name).wrap(inner - 12 * mm, 1000)[1]
        col_h = max(Paragraph(esc(k), self._style(7.5)).wrap(cw - 4, 1000)[1] + 2
                    + Paragraph(esc(v), self._style(9.5, self.bold)).wrap(cw - 4, 1000)[1] for (k, v), cw in zip(cols, widths))
        block_h = 8 * mm + name_h + 8 + col_h + 6 * mm
        c.setFillColor(TINT)
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.roundRect(pad, y - block_h, inner, block_h, 3 * mm, stroke=1, fill=1)
        rows_top = self._para(esc(_short(r.get("document", ""))), pad + 6 * mm, y - 8 * mm, inner - 12 * mm, 13, self.bold) - 8
        for (k, v), x, cw in zip(cols, lefts, widths):
            yy = self._para(esc(k), x, rows_top, cw - 4, 7.5, color=MUTED)
            self._para(esc(v), x, yy - 2, cw - 4, 9.5, self.bold)
        y -= block_h + 12 * mm

        # result ring + facts
        ring = 21 * mm
        cx, cy = pad + ring + 4 * mm, y - ring
        ai_mode = r.get("mode") == "ai"
        ai = r.get("ai") or {}
        percent = r.get("percent") if not ai_mode else (
            round(min(100.0, ai["fraction_ai"] + ai["fraction_ai_assisted"]), 1) if ai.get("status") == "done" else None)
        c.setLineWidth(7)
        c.setStrokeColor(LINE)
        c.circle(cx, cy, ring, stroke=1, fill=0)
        if percent:
            c.setStrokeColor(TEAL)
            path = c.beginPath()
            path.arc(cx - ring, cy - ring, cx + ring, cy + ring, startAng=90, extent=-max(2.0, 3.6 * float(percent)))
            c.drawPath(path, stroke=1, fill=0)
        c.setFillColor(TEAL)
        label = t["not_evaluated"] if percent is None else f"{percent}%"
        c.setFont(self.bold, 22 if percent is not None else 12)
        c.drawCentredString(cx, cy - 7, label)
        self._para(esc(t["ai_ring"] if ai_mode else t["result"]), cx - ring - 4 * mm, cy - ring - 6 * mm, 2 * ring + 8 * mm, 8.5, self.bold, align=1)
        citation = r.get("citation") or {}
        if percent is not None and citation.get("percent_all") not in (None, percent):
            self._para(esc(t["before"].format(p=citation["percent_all"])), cx - ring - 4 * mm, cy - ring - 13 * mm,
                       2 * ring + 8 * mm, 7.5, color=MUTED, align=1)
        fx = cx + ring + 12 * mm
        fy = y + 2
        for k, v in facts(r, self.lang):
            fy = self._para(esc(k), fx, fy, w - pad - fx, 7.5, color=MUTED)
            fy = self._para(esc(v), fx, fy - 1, w - pad - fx, 9.5, self.bold) - 7
        y = min(cy - ring - 20 * mm, fy - 6 * mm)

        # bottom: notice, issued line, then the verification block above them
        notice_h = Paragraph(esc(t["notice"]), self._style(7.5)).wrap(inner, 1000)[1]
        base = pad + notice_h
        self._para(esc(t["notice"]), pad, base, inner, 7.5, color=MUTED)
        self._para(esc(t["details"]), pad, base + 6 * mm, inner, 8, color=MUTED)
        self._para(esc(t["issued"]) + " · " + esc(date), pad, base + 12 * mm, inner, 8, color=MUTED)
        c.setStrokeColor(INK)
        c.setLineWidth(0.5)
        c.line(pad, base + 14 * mm, pad + 70 * mm, base + 14 * mm)
        size = 34 * mm
        top = base + 22 * mm + size
        c.setStrokeColor(LINE)
        c.setLineWidth(0.6)
        c.line(pad, top + 8 * mm, w - pad, top + 8 * mm)
        text_w = inner - size - 8 * mm
        if self.verify_url:
            self.qr(self.verify_url, size).drawOn(c, w - pad - size, top - size)
            ty = self._para(esc(t["verify"]), pad, top, text_w, 11, self.bold)
            ty = self._para(esc(t["verify_v"]), pad, ty - 3, text_w, 8.5, color=MUTED)
            self._para(f'<link href="{esc(self.verify_url)}" color="#006D70">{esc(self.verify_url)}</link>',
                       pad, ty - 4, text_w, 7, color=TEAL)
        else:
            c.setDash(3, 3)
            c.roundRect(w - pad - size, top - size, size, size, 2 * mm, stroke=1, fill=0)
            c.setDash()
            ty = self._para(esc(t["verify"]), pad, top, text_w, 11, self.bold)
            self._para(esc(t["verify_off"]), pad, ty - 3, text_w, 8.5, color=MUTED)
