"""Scope-limited evidence report (PDF). Never an originality certificate.

Rendered from the cached report summary only: it contains source excerpts
from the authorized corpus, never the manuscript text itself.
"""
import html
import io
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

PDF_SOURCES = 15
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
REGULAR, BOLD = "Helvetica", "Helvetica-Bold"
if (FONT_DIR / "DejaVuSans.ttf").exists():
    pdfmetrics.registerFont(TTFont("PlagIQSans", str(FONT_DIR / "DejaVuSans.ttf")))
    REGULAR = BOLD = "PlagIQSans"
    if (FONT_DIR / "DejaVuSans-Bold.ttf").exists():
        pdfmetrics.registerFont(TTFont("PlagIQSans-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
        BOLD = "PlagIQSans-Bold"
UNICODE_FONT = REGULAR != "Helvetica"

INK, MUTED, TEAL, LINE, TINT = (colors.HexColor(x) for x in ("#122A32", "#4C6368", "#006D70", "#D3DFE3", "#EEF7F5"))

T = {
"en": dict(
    methods=dict(reference="Exact seven-word matching after normalization, verified word by word", academic="The same exact seven-word matching, against open-access full texts and abstracts", corpus="The same exact seven-word matching, against indexed full texts"),
    scopes=dict(reference="One user-provided reference; no academic databases searched.", academic="OpenAlex search; open-access full texts of the most relevant works compared for this check and not stored; other works by abstract. No web-wide search.", corpus="Authorized texts indexed in the private PlagIQ corpus only; coverage depends on what has been lawfully imported."),
    subtitle="Scope-limited evidence report",
    disclaimer="This report lists exact text overlap found within the scope stated below. It is not an accredited "
               "originality certificate, a determination of plagiarism or misconduct, or an assessment of AI authorship. "
               "Matches require human review: properly quoted and cited text can match.",
    s_document="1. Document", s_scope="2. Scope and method", s_result="3. Result", s_sources="4. Sources and evidence",
    s_limits="5. Limitations",
    document="File name", date="Checked (UTC)", sha="SHA-256 of extracted text", language="Manuscript language",
    mode="Comparison", method="Method", scope="Searched scope", status="Search status", expiry="Report availability",
    expiry_v="Deleted from PlagIQ within {m} min; downloadable only in the submitting browser session",
    corpus_by_lang="Indexed sources by language", discovered="Records found / with abstract / full texts compared (not stored)",
    reference="Reference",
    matched="Matched portion within scope", basis_tokens="Share of the manuscript's words that lie inside at least one verified match of seven or more consecutive words. Each word is counted once, whatever the number of sources. The same measure is used in every mode.",
    tokens_v="{a} of {b} words", chars_v="{a} of {b} characters (supporting detail)", version="Corpus version (snapshot ID)",
    units_tokens="Matched words", units_chars="Matched characters",
    not_evaluated="Not evaluated", identical="Document and reference are identical",
    reasons=dict(provider_unavailable="The discovery provider was unavailable, so no text was compared.",
                 no_abstracts="No returned record had an abstract to compare.",
                 no_sources_in_language="The private corpus has no indexed sources in this language."),
    searches="OpenAlex searches (excerpts of at most 14 words)",
    shown_records="Showing {n} of {m} discovered records, most matched words first.", truncated_records="The list is shortened; the percentage above uses all records.",
    lang_detected=" (detected from the text; selected: {s})",
    ft_label="Full-text downloads", ft_line="{a} tried, {c} compared; not available: {r}",
    qr_caption="Scan to verify",
    verify="Online verification",
    verify_v="Scan the QR code or open this link to confirm PlagIQ issued report {id} with these figures:",
    verify_none="Not set up on this server (VERIFY_SECRET missing)",
    cov_web="Web page (compared, not stored)", web_label="Internet search (Yandex)", web_line="{s} searches, {p} pages tried, {c} compared; not available: {r}", web_scope=" Also searched the web with Yandex (pages compared, not stored).",
    pasted="Pasted reference",
    before="Matched before exclusions", applied="Exclusions chosen by the user",
    ex_none="No exclusions — every verified match is counted", ex_q="quotations ({n} words)", ex_b="bibliography ({n} words)",
    ex_s="matches under {m} words ({n} words)", where="Where the matched words are",
    locs=dict(other="other text", quotation_cited="in quotations with a nearby reference",
             quotation_uncited="in quotations without a nearby reference", bibliography="in the bibliography"),
    detected="Detected structure", bib_yes="bibliography from heading “{h}”", bib_no="no bibliography heading",
    quotes_v="{n} quotation(s), {c} with a reference marker within 80 characters",
    rules="Quotations, references and the bibliography are located by fixed rules (quotation marks, markers such as [3] or (Author, 2020), a references heading). The rules do not judge whether a citation is correct or sufficient.",
    ai="AI-writing detection", ai_v="Not performed",
    no_sources="No matching source with comparable text was found in the stated scope. This does not establish originality.",
    shown="Showing {n} of {m} sources.", truncated="The source list is truncated; totals above include all matches.",
    authors="Authors", year="Year", rights="Rights basis", coverage="Coverage", passage="Passage {i}",
    passages_more="{n} further passage(s) for this source are not listed.",
    loc="Your text: characters {qs}–{qe} · Source: characters {ss}–{se}",
    loc_note="Character positions are zero-based, end-exclusive, counted in the extracted text.",
    limits=["Only the scope named above was searched. No web-wide, translation or paraphrase screening was performed.",
            "Metadata-only records were discovered but not text-compared.",
            "A low or zero result does not show that a document is original.",
            "Text extraction from PDF/DOC/DOCX can omit headers, footnotes, text boxes or images."],
    footer="PlagIQ · Scope-limited evidence · not a certificate", page="Page {p} of {n}",
    modes=dict(reference="Your own reference", academic="OpenAlex discovery (open-access full texts and abstracts)", corpus="Private authorized corpus"),
    states=dict(partial="Partly searched (some searches failed)", searched="Searched", unavailable="Unavailable", no_query="No search phrase", not_searched="Not searched"),
    rights_v=dict(written_permission="Written permission", open_license="Open licence", public_domain="Public domain", unknown="Unknown"),
    cov=dict(fulltext="Full text (compared, not stored)", abstract="Abstract", metadata_only="Metadata only", reference="Your reference"), cov_full="Indexed full text",
    yes="Yes", no="No",
),
"ru": dict(
    methods=dict(reference="Точное совпадение семи слов после нормализации, проверенное пословно", academic="То же точное сравнение семи слов по открытым полным текстам и аннотациям", corpus="То же точное сравнение семи слов по индексированным полным текстам"),
    scopes=dict(reference="Один источник, предоставленный пользователем; научные базы не проверялись.", academic="Поиск в OpenAlex; открытые полные тексты наиболее релевантных работ сравнены для этой проверки и не сохранены; остальные — по аннотациям. Поиск по интернету не проводился.", corpus="Только разрешённые тексты во внутреннем корпусе PlagIQ; охват зависит от законно загруженных источников."),
    subtitle="Отчёт о совпадениях в ограниченном охвате",
    disclaimer="Отчёт перечисляет точные текстовые совпадения только в указанном ниже охвате. Это не аккредитованный "
               "сертификат оригинальности, не заключение о плагиате или нарушении и не оценка авторства ИИ. "
               "Совпадения требуют проверки человеком: корректно оформленные цитаты тоже могут совпадать.",
    s_document="1. Документ", s_scope="2. Охват и метод", s_result="3. Результат", s_sources="4. Источники и доказательства",
    s_limits="5. Ограничения",
    document="Имя файла", date="Проверено (UTC)", sha="SHA-256 извлечённого текста", language="Язык рукописи",
    mode="Сравнение", method="Метод", scope="Охват поиска", status="Статус поиска", expiry="Доступность отчёта",
    expiry_v="Удаляется из PlagIQ в течение {m} мин; скачивание только в сессии браузера отправителя",
    corpus_by_lang="Индексированные источники по языкам", discovered="Найдено записей / с аннотацией / сравнено полных текстов (не сохранены)",
    reference="Источник",
    matched="Доля совпадений в охвате", basis_tokens="Доля слов рукописи, входящих хотя бы в одно проверенное совпадение не менее чем из семи слов подряд. Каждое слово учитывается один раз независимо от числа источников. Мера одинакова во всех режимах.",
    tokens_v="{a} из {b} слов", chars_v="{a} из {b} символов (дополнительно)", version="Версия корпуса (идентификатор снимка)",
    units_tokens="Совпавшие слова", units_chars="Совпавшие символы",
    not_evaluated="Не оценено", identical="Документ и источник идентичны",
    reasons=dict(provider_unavailable="Сервис поиска был недоступен, текст не сравнивался.",
                 no_abstracts="Ни у одной найденной записи нет аннотации для сравнения.",
                 no_sources_in_language="Во внутреннем корпусе нет источников на этом языке."),
    searches="Запросов в OpenAlex (отрывки до 14 слов)",
    shown_records="Показано {n} из {m} найденных записей, сначала с наибольшим числом совпавших слов.", truncated_records="Список сокращён; процент выше рассчитан по всем записям.",
    lang_detected=" (определён по тексту; выбран: {s})",
    ft_label="Загрузка полных текстов", ft_line="попыток {a}, сравнено {c}; не удалось: {r}", reasons_tr={"too little text on page": "на странице слишком мало текста", "timed out": "истекло время", "blocked by robots.txt": "запрещено robots.txt", "refused by the website (403)": "сайт отказал (403)", "refused by the website (401)": "сайт отказал (401)", "not found (404)": "не найдено (404)", "not a PDF": "не PDF", "no PDF link on page": "на странице нет ссылки на PDF", "scanned PDF (no text)": "скан PDF (без текста)", "website not reachable": "сайт недоступен", "address refused": "адрес отклонён", "file too large": "файл слишком большой", "cost limit": "лимит расходов", "other error": "другая ошибка", "no open-access copy listed": "открытая копия не указана"},
    qr_caption="Сканируйте для проверки",
    verify="Онлайн-проверка",
    verify_v="Отсканируйте QR-код или откройте ссылку, чтобы убедиться, что PlagIQ выдал отчёт {id} с этими данными:",
    verify_none="На этом сервере не настроена (нет VERIFY_SECRET)",
    cov_web="Веб-страница (сравнена, не сохранена)", web_label="Поиск в интернете (Яндекс)", web_line="запросов {s}, страниц {p}, сравнено {c}; не удалось: {r}", web_scope=" Также выполнен поиск в интернете через Яндекс (страницы сравнены, не сохранены).",
    pasted="Вставленный текст источника",
    before="Совпадения до исключений", applied="Исключения, выбранные пользователем",
    ex_none="Нет — учитываются все проверенные совпадения", ex_q="цитаты ({n} слов)", ex_b="список литературы ({n} слов)",
    ex_s="совпадения короче {m} слов ({n} слов)", where="Где находятся совпавшие слова",
    locs=dict(other="остальной текст", quotation_cited="в цитатах со ссылкой рядом",
             quotation_uncited="в цитатах без ссылки рядом", bibliography="в списке литературы"),
    detected="Обнаруженная структура", bib_yes="список литературы по заголовку «{h}»", bib_no="заголовок списка литературы не найден",
    quotes_v="цитат: {n}, из них {c} с меткой ссылки в пределах 80 символов",
    rules="Цитаты, ссылки и список литературы определяются фиксированными правилами (кавычки, метки вида [3] или (Автор, 2020), заголовок списка литературы). Правила не оценивают правильность или достаточность цитирования.",
    ai="Определение текста ИИ", ai_v="Не проводилось",
    no_sources="В указанном охвате не найдено совпадающих источников с текстом для сравнения. Это не доказывает оригинальность.",
    shown="Показано {n} из {m} источников.", truncated="Список источников сокращён; итоги выше учитывают все совпадения.",
    authors="Авторы", year="Год", rights="Основание прав", coverage="Охват", passage="Фрагмент {i}",
    passages_more="Ещё {n} фрагмент(ов) этого источника не показаны.",
    loc="Ваш текст: символы {qs}–{qe} · Источник: символы {ss}–{se}",
    loc_note="Позиции символов считаются с нуля, конец не включается, по извлечённому тексту.",
    limits=["Проверен только указанный охват. Поиск по интернету, переводу и перефразированию не проводился.",
            "Записи только с метаданными найдены, но текст не сравнивался.",
            "Низкий или нулевой результат не доказывает оригинальность.",
            "Извлечение текста из PDF/DOC/DOCX может пропускать колонтитулы, сноски, надписи и изображения."],
    footer="PlagIQ · Доказательства в ограниченном охвате · не сертификат", page="Стр. {p} из {n}",
    modes=dict(reference="Ваш источник", academic="Поиск OpenAlex (открытые полные тексты и аннотации)", corpus="Внутренний разрешённый корпус"),
    states=dict(partial="Поиск выполнен частично (часть запросов не удалась)", searched="Поиск выполнен", unavailable="Недоступно", no_query="Нет поисковой фразы", not_searched="Поиск не проводился"),
    rights_v=dict(written_permission="Письменное разрешение", open_license="Открытая лицензия", public_domain="Общественное достояние", unknown="Неизвестно"),
    cov=dict(fulltext="Полный текст (сравнён, не сохранён)", abstract="Аннотация", metadata_only="Только метаданные", reference="Ваш источник"), cov_full="Индексированный полный текст",
    yes="Да", no="Нет",
),
"uz": dict(
    methods=dict(reference="Normallashtirishdan keyin aniq yetti so‘zli moslik, so‘zma-so‘z tekshirilgan", academic="Xuddi shu aniq yetti so‘zli moslik, ochiq to‘liq matnlar va annotatsiyalar bo‘yicha", corpus="Xuddi shu aniq yetti so‘zli moslik, indekslangan to‘liq matnlar bo‘yicha"),
    scopes=dict(reference="Foydalanuvchi bergan bitta manba; ilmiy bazalar qidirilmagan.", academic="OpenAlex qidiruvi; eng mos ishlarning ochiq to‘liq matnlari shu tekshiruv uchun solishtirildi va saqlanmadi; qolganlari annotatsiya bo‘yicha. Internet qidirilmadi.", corpus="Faqat PlagIQ ichki korpusidagi ruxsat berilgan matnlar; qamrov qonuniy yuklangan manbalarga bog‘liq."),
    subtitle="Cheklangan doiradagi dalillar hisoboti",
    disclaimer="Ushbu hisobot faqat quyida ko‘rsatilgan doirada topilgan aniq matn mosliklarini sanab o‘tadi. U akkreditatsiyalangan "
               "originallik sertifikati, plagiat yoki qoidabuzarlik haqidagi xulosa yoki sun’iy intellekt muallifligini baholash emas. "
               "Mosliklarni inson tekshirishi kerak: to‘g‘ri iqtibos qilingan matn ham mos kelishi mumkin.",
    s_document="1. Hujjat", s_scope="2. Doira va usul", s_result="3. Natija", s_sources="4. Manbalar va dalillar",
    s_limits="5. Cheklovlar",
    document="Fayl nomi", date="Tekshirilgan (UTC)", sha="Ajratilgan matnning SHA-256", language="Hujjat tili",
    mode="Solishtirish", method="Usul", scope="Qidiruv doirasi", status="Qidiruv holati", expiry="Hisobot mavjudligi",
    expiry_v="PlagIQ’dan {m} daqiqa ichida o‘chiriladi; faqat yuborgan brauzer seansida yuklab olinadi",
    corpus_by_lang="Tillar bo‘yicha indekslangan manbalar", discovered="Topilgan yozuvlar / annotatsiyasi borlari / solishtirilgan to‘liq matnlar (saqlanmagan)",
    reference="Manba",
    matched="Doira ichidagi moslik ulushi", basis_tokens="Hujjatdagi so‘zlardan kamida yetti ketma-ket so‘zli tasdiqlangan moslik ichida bo‘lganlari ulushi. Har bir so‘z manbalar sonidan qat’i nazar bir marta sanaladi. O‘lchov barcha rejimlarda bir xil.",
    tokens_v="{b} ta so‘zdan {a} tasi", chars_v="{b} ta belgidan {a} tasi (qo‘shimcha)", version="Korpus versiyasi (holat identifikatori)",
    units_tokens="Mos so‘zlar", units_chars="Mos belgilar",
    not_evaluated="Baholanmagan", identical="Hujjat va manba bir xil",
    reasons=dict(provider_unavailable="Qidiruv xizmati ishlamadi, matn solishtirilmadi.",
                 no_abstracts="Topilgan yozuvlarning hech birida solishtirish uchun annotatsiya yo‘q.",
                 no_sources_in_language="Ichki korpusda bu tildagi manbalar yo‘q."),
    searches="OpenAlex so‘rovlari (14 so‘zgacha parchalar)",
    shown_records="Topilgan {m} ta yozuvdan {n} tasi ko‘rsatilgan, eng ko‘p mos so‘zlilari birinchi.", truncated_records="Ro‘yxat qisqartirilgan; yuqoridagi foiz barcha yozuvlar bo‘yicha hisoblangan.",
    lang_detected=" (matndan aniqlandi; tanlangan: {s})",
    ft_label="To‘liq matnlarni yuklash", ft_line="{a} ta urinish, {c} tasi solishtirildi; olinmadi: {r}", reasons_tr={"too little text on page": "sahifada matn juda kam", "timed out": "vaqt tugadi", "blocked by robots.txt": "robots.txt taqiqlagan", "refused by the website (403)": "sayt rad etdi (403)", "refused by the website (401)": "sayt rad etdi (401)", "not found (404)": "topilmadi (404)", "not a PDF": "PDF emas", "no PDF link on page": "sahifada PDF havolasi yo‘q", "scanned PDF (no text)": "skanerlangan PDF (matnsiz)", "website not reachable": "saytga ulanib bo‘lmadi", "address refused": "manzil rad etildi", "file too large": "fayl juda katta", "cost limit": "xarajat chegarasi", "other error": "boshqa xato", "no open-access copy listed": "ochiq nusxa ko‘rsatilmagan"},
    qr_caption="Tekshirish uchun skanerlang",
    verify="Onlayn tekshirish",
    verify_v="PlagIQ {id} raqamli hisobotni shu ma’lumotlar bilan berganini tasdiqlash uchun QR kodni skanerlang yoki havolani oching:",
    verify_none="Bu serverda sozlanmagan (VERIFY_SECRET yo‘q)",
    cov_web="Veb-sahifa (solishtirildi, saqlanmadi)", web_label="Internetda qidiruv (Yandex)", web_line="{s} ta qidiruv, {p} ta sahifa, {c} tasi solishtirildi; olinmadi: {r}", web_scope=" Internetda Yandex orqali ham qidirildi (sahifalar solishtirildi, saqlanmadi).",
    pasted="Kiritilgan manba matni",
    before="Istisnolargacha moslik", applied="Foydalanuvchi tanlagan istisnolar",
    ex_none="Yo‘q — barcha tasdiqlangan mosliklar hisoblanadi", ex_q="iqtiboslar ({n} so‘z)", ex_b="adabiyotlar ro‘yxati ({n} so‘z)",
    ex_s="{m} so‘zdan qisqa mosliklar ({n} so‘z)", where="Mos so‘zlar qayerda",
    locs=dict(other="boshqa matn", quotation_cited="yaqinida havolasi bor iqtiboslarda",
             quotation_uncited="yaqinida havolasi yo‘q iqtiboslarda", bibliography="adabiyotlar ro‘yxatida"),
    detected="Aniqlangan tuzilma", bib_yes="“{h}” sarlavhasi bo‘yicha adabiyotlar ro‘yxati", bib_no="adabiyotlar sarlavhasi topilmadi",
    quotes_v="{n} ta iqtibos, ulardan {c} tasida 80 belgi ichida havola belgisi bor",
    rules="Iqtiboslar, havolalar va adabiyotlar ro‘yxati qat’iy qoidalar bilan aniqlanadi (qo‘shtirnoq, [3] yoki (Muallif, 2020) kabi belgilar, adabiyotlar sarlavhasi). Qoidalar iqtibosning to‘g‘ri yoki yetarli ekanini baholamaydi.",
    ai="Sun’iy intellekt matnini aniqlash", ai_v="Bajarilmagan",
    no_sources="Ko‘rsatilgan doirada solishtiriladigan matnli mos manba topilmadi. Bu originallikni isbotlamaydi.",
    shown="{m} ta manbadan {n} tasi ko‘rsatilgan.", truncated="Manbalar ro‘yxati qisqartirilgan; yuqoridagi jami barcha mosliklarni o‘z ichiga oladi.",
    authors="Mualliflar", year="Yil", rights="Huquqiy asos", coverage="Qamrov", passage="Parcha {i}",
    passages_more="Bu manba bo‘yicha yana {n} ta parcha ko‘rsatilmagan.",
    loc="Sizning matn: {qs}–{qe} belgilar · Manba: {ss}–{se} belgilar",
    loc_note="Belgi o‘rinlari noldan sanaladi, oxirgisi kirmaydi, ajratilgan matn bo‘yicha.",
    limits=["Faqat yuqorida ko‘rsatilgan doira tekshirildi. Internet, tarjima yoki parafraz tekshiruvi bajarilmadi.",
            "Faqat metadata yozuvlari topildi, lekin matni solishtirilmadi.",
            "Past yoki nol natija hujjat original ekanini isbotlamaydi.",
            "PDF/DOC/DOCX’dan matn ajratishda sarlavhalar, izohlar, matn bloklari yoki rasmlar tushib qolishi mumkin."],
    footer="PlagIQ · Cheklangan doiradagi dalillar · sertifikat emas", page="{n} sahifadan {p}",
    modes=dict(reference="O‘z manbangiz", academic="OpenAlex qidiruvi (ochiq to‘liq matnlar va annotatsiyalar)", corpus="Ruxsat berilgan ichki korpus"),
    states=dict(partial="Qisman qidirildi (ayrim so‘rovlar bajarilmadi)", searched="Qidirildi", unavailable="Mavjud emas", no_query="Qidiruv iborasi yo‘q", not_searched="Qidirilmagan"),
    rights_v=dict(written_permission="Yozma ruxsat", open_license="Ochiq litsenziya", public_domain="Jamoat mulki", unknown="Noma’lum"),
    cov=dict(fulltext="To‘liq matn (solishtirildi, saqlanmadi)", abstract="Annotatsiya", metadata_only="Faqat metadata", reference="Sizning manbangiz"), cov_full="Indekslangan to‘liq matn",
    yes="Ha", no="Yo‘q",
),
}


def _styles():
    base = dict(fontName=REGULAR, textColor=INK, fontSize=9.5, leading=13.5, wordWrap="LTR", splitLongWords=1)
    return dict(
        title=ParagraphStyle("title", **{**base, "fontName": BOLD, "fontSize": 24, "leading": 28, "textColor": TEAL}),
        subtitle=ParagraphStyle("subtitle", **{**base, "fontName": BOLD, "fontSize": 13, "leading": 17, "spaceAfter": 6}),
        h=ParagraphStyle("h", **{**base, "fontName": BOLD, "fontSize": 11.5, "leading": 15, "textColor": TEAL,
                                 "spaceBefore": 14, "spaceAfter": 6}),
        body=ParagraphStyle("body", **base),
        small=ParagraphStyle("small", **{**base, "fontSize": 8.5, "leading": 12, "textColor": MUTED}),
        key=ParagraphStyle("key", **{**base, "fontName": BOLD, "fontSize": 8.5, "leading": 12}),
        cell=ParagraphStyle("cell", **{**base, "fontSize": 8.5, "leading": 12}),
        big=ParagraphStyle("big", **{**base, "fontName": BOLD, "fontSize": 22, "leading": 26, "textColor": TEAL}),
        source=ParagraphStyle("source", **{**base, "fontName": BOLD, "fontSize": 10, "leading": 14, "spaceBefore": 8}),
        quote=ParagraphStyle("quote", **{**base, "fontSize": 8.5, "leading": 12, "leftIndent": 10, "borderPadding": (4, 6, 4, 6),
                                         "backColor": TINT, "borderColor": LINE, "borderWidth": 0.5, "spaceBefore": 3, "spaceAfter": 6}),
        notice=ParagraphStyle("notice", **{**base, "fontSize": 8.5, "leading": 12, "backColor": colors.HexColor("#FFF4E7"),
                                           "borderColor": colors.HexColor("#E8C994"), "borderWidth": 0.5,
                                           "borderPadding": (6, 8, 6, 8), "spaceBefore": 6, "spaceAfter": 10}),
    )


def _numbered_canvas(t, identifier):
    class NumberedCanvas(pdfcanvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self.setFont(REGULAR, 7.5)
                self.setFillColor(MUTED)
                self.setStrokeColor(LINE)
                self.line(18 * mm, 14 * mm, A4[0] - 18 * mm, 14 * mm)
                self.drawString(18 * mm, 10 * mm, t["footer"] + " · " + identifier[:12])
                self.drawRightString(A4[0] - 18 * mm, 10 * mm, t["page"].format(p=self._pageNumber, n=total))
                super().showPage()
            super().save()
    return NumberedCanvas


def _qr(url, size):
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    widget = QrCodeWidget(url, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    return drawing


def _scope(t, mode, result):
    text = t["scopes"].get(mode, result.get("scope", ""))
    if mode == "academic" and (result.get("web") or {}).get("enabled"):
        text = text.rsplit(". ", 1)[0] + "." + t["web_scope"]     # replace the "no web search" sentence
    return text


def build_pdf(result, identifier, lang="en", minutes_left=15, verify_url=None):
    t = T.get(lang, T["en"])
    st = _styles()
    esc = lambda value: html.escape(str(value))

    def table(rows):
        data = [[Paragraph(esc(k), st["key"]), Paragraph(esc(v), st["cell"])] for k, v in rows]
        tbl = Table(data, colWidths=[52 * mm, None], hAlign="LEFT")
        tbl.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (0, -1), TINT),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("BOX", (0, 0), (-1, -1), 0.4, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return tbl

    heading = [Paragraph("PlagIQ", st["title"]), Paragraph(t["subtitle"], st["subtitle"])]
    if verify_url:
        head = Table([[heading, [_qr(verify_url, 27 * mm), Paragraph(esc(t["qr_caption"]), st["small"])]]],
                     colWidths=[None, 30 * mm], hAlign="LEFT")
        head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("ALIGN", (1, 0), (1, 0), "CENTER")]))
        heading = [head]
    story = heading + [
             Paragraph(esc(t["disclaimer"]), st["notice"])]

    story += [Paragraph(t["s_document"], st["h"]), table([
        (t["document"], result.get("document", "")), (t["date"], result.get("date", "")),
        (t["language"], str(result.get("language", "")).upper() + (t["lang_detected"].format(
            s=result["language_note"]["selected"].upper()) if result.get("language_note") else "")),
        (t["sha"], result.get("sha256", "")),
        (t["expiry"], t["expiry_v"].format(m=minutes_left)),
        (t["verify"], t["verify_v"].format(id=identifier[:12]) if verify_url else t["verify_none"])])]
    if verify_url:
        story.append(Paragraph(f'<link href="{esc(verify_url)}" color="#006D70">{esc(verify_url)}</link>', st["small"]))

    mode = result.get("mode", "reference")
    scope_rows = [(t["mode"], t["modes"].get(mode, mode)), (t["method"], t["methods"].get(mode, result.get("method", ""))),
                  (t["scope"], _scope(t, mode, result)),
                  (t["status"], t["states"].get(result.get("database_status"), t["states"]["not_searched"]))]
    pasted = lambda name: t["pasted"] if name == "Pasted reference" else name
    if mode == "reference":
        scope_rows.append((t["reference"], pasted(result.get("reference", ""))))
    if mode == "academic":
        scope_rows.append((t["searches"], str(result.get("queries") or 1)))
        compared = (result.get("fulltext") or {}).get("compared", 0)
        with_abstract = sum(1 for src in result.get("sources") or [] if src.get("coverage") == "abstract")
        scope_rows.append((t["discovered"], f"{result.get('source_count', 0)} / {with_abstract} / {compared}"))
        wb = result.get("web") or {}
        if wb.get("enabled"):
            reasons = ", ".join(f"{t.get('reasons_tr', {}).get(k, k)} ×{v}" for k, v in (wb.get("reasons") or {}).items()) or "—"
            scope_rows.append((t["web_label"], t["web_line"].format(s=wb.get("searches", 0), p=wb.get("attempted", 0),
                                                                   c=wb.get("compared", 0), r=reasons)))
        ft = result.get("fulltext") or {}
        if ft.get("attempted"):
            reasons = ", ".join(f"{k} ×{v}" for k, v in (ft.get("reasons") or {}).items()) or "—"
            reasons = ", ".join(f"{t.get('reasons_tr', {}).get(k, k)} ×{v}" for k, v in (ft.get("reasons") or {}).items()) or "—"
            scope_rows.append((t["ft_label"], t["ft_line"].format(
                a=ft.get("attempted", 0), c=ft.get("compared", 0), r=reasons)))
    if mode == "corpus":
        counts = result.get("language_counts") or {}
        scope_rows.append((t["corpus_by_lang"], " · ".join(
            f"{k.upper()} {counts.get(k, 0) or t['not_evaluated']}" for k in ("uz", "ru", "en"))
            + f" · Σ {result.get('source_count', 0)}"))
        if result.get("corpus_version"):
            scope_rows.append((t["version"], result["corpus_version"]))
    story += [Paragraph(t["s_scope"], st["h"]), table(scope_rows)]

    percent = result.get("percent")
    if percent is None:
        headline = t["not_evaluated"]
        basis = t["reasons"].get(result.get("not_evaluated_reason"), "")
    else:
        headline = f"{percent}%"
        basis = t["basis_tokens"]
    c = result.get("citation")
    counted = c["matched_words_counted"] if c else (result.get("matched_tokens") or 0)
    result_rows = [(t["units_tokens"], t["tokens_v"].format(a=counted, b=result.get("total_tokens") or 0)),
                   (t["units_chars"], t["chars_v"].format(a=result.get("matched_chars") or 0, b=result.get("total_chars") or 0))]
    if c:
        o, ex = c["exclusions"], c["excluded"]
        chosen = ([t["ex_q"].format(n=ex["quotations"])] if o["quotations"] else []) + \
                 ([t["ex_b"].format(n=ex["bibliography"])] if o["bibliography"] else []) + \
                 ([t["ex_s"].format(m=o["min_words"], n=ex["short"])] if o["min_words"] > 7 else [])
        result_rows += [
            (t["before"], f"{c['percent_all']}% · " + t["tokens_v"].format(a=c["matched_words_all"], b=c["total_words"])),
            (t["applied"], "; ".join(chosen) or t["ex_none"]),
            (t["where"], "; ".join(f"{t['locs'][k]}: {v}" for k, v in c["locations"].items() if v) or "—"),
            (t["detected"], (t["bib_yes"].format(h=c["bibliography"]["heading"]) if c.get("bibliography") else t["bib_no"])
             + "; " + t["quotes_v"].format(n=c["quotations_found"], c=c["quotations_cited"]))]
    result_rows.append((t["ai"], t["ai_v"]))
    if mode == "reference":
        result_rows.append((t["identical"], t["yes"] if result.get("self_comparison") else t["no"]))
    story += [Paragraph(t["s_result"], st["h"]),
              KeepTogether([Paragraph(esc(t["matched"]), st["small"]), Paragraph(esc(headline), st["big"]),
                            Paragraph(esc(basis), st["small"]), Spacer(1, 6)]),
              table(result_rows)]
    if c:
        story.append(Paragraph(esc(t["rules"]), st["small"]))

    story.append(Paragraph(t["s_sources"], st["h"]))
    sources = result.get("sources") or []
    sources = sorted(sources, key=lambda s: -(s.get("matched_tokens") or 0))
    # Academic mode lists every discovered record (matched or not); the corpus lists
    # matched sources only, possibly fewer than the engine found.
    total = len(sources) if mode == "academic" else result.get("matched_source_count", len(sources))
    if not sources and mode != "reference":
        story.append(Paragraph(esc(t["no_sources"]), st["body"]))
    if sources:
        key = "shown_records" if mode == "academic" else "shown"
        story.append(Paragraph(esc(t[key].format(n=min(len(sources), PDF_SOURCES), m=total)), st["small"]))
    if result.get("sources_truncated") or len(sources) > PDF_SOURCES:
        story.append(Paragraph(esc(t["truncated_records" if mode == "academic" else "truncated"]), st["small"]))
    if any(s.get("passages") for s in sources):
        story.append(Paragraph(esc(t["loc_note"]), st["small"]))
    for number, source in enumerate(sources[:PDF_SOURCES], 1):
        block = [Paragraph(f"{number}. " + esc(pasted(source.get("title") or "—")), st["source"])]
        meta = []
        if source.get("authors"):
            meta.append(f"{t['authors']}: {source['authors']}")
        if source.get("year"):
            meta.append(f"{t['year']}: {source['year']}")
        coverage = t["cov_web"] if source.get("coverage") == "web" else t["cov"].get(source.get("coverage"), t["cov_full"])
        meta.append(f"{t['coverage']}: {coverage}")
        if source.get("rights_basis"):
            meta.append(f"{t['rights']}: {t['rights_v'].get(source['rights_basis'], t['rights_v']['unknown'])}")
        meta.append(f"{t['units_tokens']}: {source.get('matched_tokens') or 0}")
        block.append(Paragraph(esc(" · ".join(meta)), st["small"]))
        if source.get("url"):
            block.append(Paragraph(esc(source["url"]), st["small"]))
        passages = source.get("passages") or []
        for i, passage in enumerate(passages, 1):
            block.extend([
                Paragraph(esc(t["passage"].format(i=i) + " — " + t["loc"].format(
                    qs=passage.get("query_start"), qe=passage.get("query_end"),
                    ss=passage.get("source_start"), se=passage.get("source_end"))), st["small"]),
                Paragraph(esc(passage.get("source_excerpt", "")), st["quote"])])
            story.append(KeepTogether(block))  # first passage stays with the source heading
            block = []
        if block:
            story.append(KeepTogether(block))
        hidden = (source.get("passage_count") or len(passages)) - len(passages)
        if hidden > 0:
            story.append(Paragraph(esc(t["passages_more"].format(n=hidden)), st["small"]))

    story += [Paragraph(t["s_limits"], st["h"])]
    story += [Paragraph("• " + esc(item), st["body"]) for item in t["limits"]]

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=20 * mm, title="PlagIQ scope-limited evidence report", author="PlagIQ",
                            subject=t["subtitle"])
    doc.build(story, canvasmaker=_numbered_canvas(t, identifier))
    return out.getvalue()
