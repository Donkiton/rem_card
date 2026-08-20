from __future__ import annotations

import html
import os
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as reportlab_canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder


class ProcedureReportLabBuilder:
    FONT_REGULAR = ReportLabReportBuilder.FONT_REGULAR
    FONT_BOLD = ReportLabReportBuilder.FONT_BOLD
    FONT_ITALIC = "RemCardArial-Italic"
    FONT_MONO = "RemCardCourierNew"

    @classmethod
    def build_pdf(cls, document_kind: str, context: dict[str, str], output_path) -> None:
        kind = str(document_kind or "").strip()
        if kind == "cvc_protocol":
            cls.build_cvc_protocol(context, output_path)
            return
        if kind == "cvc_removal":
            cls.build_cvc_removal(context, output_path)
            return
        if kind == "cvc_consent":
            cls.build_cvc_consent(context, output_path)
            return
        if kind == "lp_protocol":
            cls.build_lp_protocol(context, output_path)
            return
        if kind == "lp_consent":
            cls.build_lp_consent(context, output_path)
            return
        if kind == "transfusion_protocol":
            cls.build_transfusion_protocol(context, output_path)
            return
        if kind == "transfusion_consent":
            cls.build_transfusion_consent(context, output_path)
            return
        raise ValueError(f"Неизвестная печатная форма процедуры: {kind}")

    @classmethod
    def build_cvc_protocol(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        story = [
            cls._p("ПРОТОКОЛ", "protocol_title"),
            cls._p(context.get("procedure_title", ""), "procedure_title"),
            cls._spacer(12),
        ]
        story.extend(cls._cvc_protocol_body(context))
        if context.get("removal_block_present"):
            story.append(cls._spacer(10))
            story.extend(cls._cvc_removal_body(context))
        cls._build(story, output_path, framed=True)

    @classmethod
    def build_cvc_removal(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        if not context.get("removal_block_present"):
            raise ValueError("Блок удаления/переустановки катетера не заполнен.")
        story = [
            cls._p("УДАЛЕНИЕ / ПЕРЕУСТАНОВКА КАТЕТЕРА", "protocol_title"),
            cls._spacer(12),
            *cls._cvc_removal_body(context, start_number=1),
        ]
        cls._build(story, output_path, framed=True)

    @classmethod
    def build_cvc_consent(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        is_direct = context.get("consent_mode") in {"patient", "representative", ""}
        is_consilium = context.get("consent_mode") == "consilium"
        representative_line = cls._join_present(
            context.get("representative_name", ""),
            context.get("representative_details", ""),
        )
        story = [
            cls._p("ИНФОРМИРОВАННОЕ ДОБРОВОЛЬНОЕ СОГЛАСИЕ НА МЕДИЦИНСКОЕ ВМЕШАТЕЛЬСТВО", "consent_title"),
            cls._spacer(12),
            cls._p(
                "катетеризация подключичной и других центральных вен и катетеризация подключичной и других центральных вен "
                "с использованием туннельного катетера",
                "consent_subtitle",
            ),
            cls._rich_p(
                "На основании ст. 20 "
                f"<font name=\"{cls.FONT_ITALIC}\">«Информированное добровольное согласие на медицинское вмешательство "
                "и на отказ от медицинского вмешательства»</font> Федерального закона «Об основах охраны здоровья граждан "
                "в Российской Федерации» от 21.11.2011 г. № 323-ФЗ.",
                "consent_normal",
            ),
            cls._spacer(12),
            cls._rich_p(
                f"Я, {cls._text(context.get('patient_name', ''))}, пациент "
                "(КГБУЗ \"Городская больница\" им М.И. Шевчук МЗХК г. Амурск) "
                "(или законный представитель пациента),",
                "consent_normal",
            ),
            cls._underlined_line(representative_line),
            cls._p(
                "(Ф.И.О. законного представителя, реквизиты документа, подтверждающего право представлять интересы больного)",
                "consent_footnote",
            ),
            cls._p(
                "даю информированное добровольное информированное согласие на предложенное мне медицинское вмешательство "
                "в виде катетеризации подключичной, внутренней яремной, бедренной вены (нужное подчеркнуть).",
                "consent_normal",
            ),
            cls._p(f"О своем заболевании: {context.get('diagnosis', '')} информирован.", "consent_normal"),
            cls._p(
                "Мне в доступной форме предоставлена полная информация о целях катетеризации вены, связанном с ним риске, "
                "возможных вариантах медицинского вмешательства, о его последствиях, а также о предполагаемых результатах "
                "оказания медицинской помощи. По моей просьбе были объяснены непонятные медицинские термины, мною были заданы "
                "все интересующие меня вопросы. Я не буду иметь каких-либо претензий к КГБУЗ \"Городская больница\" им М.И. Шевчук "
                "МЗХК г. Амурск в случае развития негативных последствий моего решения.",
                "consent_normal",
            ),
        ]
        if is_direct:
            story.extend(
                [
                    cls._p("Пациент (законный представитель) ________________    Подпись ________________", "consent_normal"),
                    cls._p(
                        "Количественные и качественные нарушения сознания в момент получения информированного добровольного согласия отсутствуют.",
                        "consent_normal",
                    ),
                    cls._p(f"Дата, время: {context.get('print_datetime', '')}", "consent_normal"),
                    cls._doctor_signature_row(context.get("doctor", "")),
                ]
            )
        story.extend(cls._cvc_consent_note(context))
        if is_consilium:
            story.extend(cls._cvc_consent_consilium(context))
        cls._build(story, output_path, framed=True)

    @classmethod
    def build_lp_protocol(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        story = [
            cls._p("ПРОТОКОЛ", "protocol_title"),
            cls._p("люмбальная пункция", "procedure_title"),
            cls._spacer(12),
            *cls._lp_protocol_body(context),
        ]
        cls._build(story, output_path, framed=True)

    @classmethod
    def build_transfusion_protocol(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        story = cls.transfusion_protocol_flowables(context)
        cls._build(story, output_path, framed=False)

    @classmethod
    def transfusion_protocol_flowables(cls, context: dict[str, str]) -> list:
        cls._ensure_fonts()
        return [cls._transfusion_table(context)]

    @classmethod
    def build_transfusion_consent(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pages = cls._transfusion_consent_pages(context)
        pdf = reportlab_canvas.Canvas(str(output_path), pagesize=A4, pageCompression=1)
        doc_proxy = type("DocProxy", (), {"pagesize": A4})()
        for page_lines in pages:
            cls._draw_page_background(pdf, doc_proxy)
            cls._draw_mono_page(pdf, page_lines)
            pdf.showPage()
        pdf.save()
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise OSError(f"PDF file was not created: {output_path}")

    @classmethod
    def build_lp_consent(cls, context: dict[str, str], output_path) -> None:
        cls._ensure_fonts()
        consent_mode = context.get("consent_mode") or "patient"
        story = [
            cls._p("Добровольное информированное согласие пациента на выполнение люмбальной пункции", "consent_title"),
            cls._spacer(10),
            cls._p(f"ИБ № {context.get('history_number', '')}", "consent_normal"),
            cls._underlined_line(context.get("patient_name", "")),
            cls._p("(ФИО пациента и/или его законного представителя)", "consent_footnote"),
        ]
        if consent_mode == "consilium":
            story.extend(cls._lp_consent_consilium(context))
            cls._build(story, output_path, framed=True)
            return

        story.extend(
            [
                cls._p(
                    "Мне подробно разъяснены моим лечащим врачом состояние моего здоровья, цели, характер и объем планируемого обследования и лечения.",
                    "consent_normal",
                ),
                cls._p(
                    "Я согласен(а) на выполнение инвазивной манипуляции: люмбальной пункции и знаю обо всех возможных осложнениях, связанных с этой манипуляцией.",
                    "consent_normal",
                ),
                cls._p(
                    "Я добровольно выбрал(а) метод обследования: люмбальная пункция с диагностической и/или лечебной целью.",
                    "consent_normal",
                ),
                cls._p(
                    "Мой лечащий врач проинформировал меня и моих родственников об осложнениях, их частоте в ходе и после манипуляции: "
                    "непосредственная травма тканей при проколе; повреждение образований, располагающихся в непосредственной близости от укола; "
                    "постпункционный синдром, проявляющийся головной болью, тошнотой, рвотой; геморрагические осложнения в виде гематом; "
                    "ликвородинамические нарушения, дислокация головного мозга; тератогенные осложнения: развитие холестеатомы позвоночного канала; "
                    "инфицирование мягких тканей, мозговых оболочек, менингеальная реакция и т.д.",
                    "consent_normal",
                ),
                cls._p(
                    "Я осознаю процентную вероятность этих осложнений и их последствий, и не буду иметь претензий при их возникновении.",
                    "consent_normal",
                ),
                cls._p(
                    "Я понимаю, что в ходе выполнения указанной выше манипуляции может возникнуть необходимость выполнения другого или дополнительного вмешательства, "
                    "исследования или операции. Я доверяю врачу и его коллегам принять решение в соответствии с их профессиональным суждением и согласен на выполнение "
                    "любых медицинских действий, которые врач сочтет необходимыми для улучшения моего состояния.",
                    "consent_normal",
                ),
                cls._p(
                    "Я ознакомлен(а) со всеми возможными способами обезболивания, необходимого для выполнения манипуляции, а также с их возможными осложнениями "
                    "(общими - анафилактический шок, острые нарушения со стороны сердечно-сосудистой, дыхательной, гепатолиенальной систем, местными - гематома, "
                    "инфицирование) и согласен(а) на метод анестезии, выбранный совместно с моим врачом.",
                    "consent_normal",
                ),
                cls._p("Со мною подробно обсуждены последствия отказа от обследования и лечения.", "consent_normal"),
                cls._p(
                    "Я получил(а) исчерпывающие и понятные мне ответы на все поставленные мною вопросы и имел(а) достаточно времени на обдумывание решения "
                    "о согласии на предложенное обследование и лечение.",
                    "consent_normal",
                ),
                cls._p(
                    "Я согласен(а) с предложенным мне методом (способом) обследования и/или видом лечения, а также на обработку моих персональных данных "
                    "в установленном законом порядке.",
                    "consent_normal",
                ),
                cls._spacer(8),
            ]
        )
        if consent_mode == "representative":
            story.extend(
                [
                    cls._p(
                        "Если пациент не может подписать документ самостоятельно вследствие тяжести состояния или по другим причинам "
                        "(является несовершеннолетним, недееспособным):",
                        "consent_normal",
                    ),
                    cls._p("Дата ____ 20__ г. Ф.И.О. и подпись законного представителя ______________________________", "consent_normal"),
                    cls._p("Дата ____ 20__ г. Ф.И.О. и подпись врача ______________________________", "consent_normal"),
                ]
            )
        else:
            story.extend(
                [
                    cls._p("Дата ____ 20__ г. Ф.И.О. и подпись пациента ______________________________", "consent_normal"),
                    cls._p(
                        f"Дата ____ 20__ г. Ф.И.О. и подпись врача {context.get('doctor', '')} ______________________________",
                        "consent_normal",
                    ),
                ]
            )
        cls._build(story, output_path, framed=True)

    @classmethod
    def _build(cls, story, output_path, *, framed: bool = False) -> None:
        cls._ensure_fonts()
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            leftMargin=15 * mm,
            rightMargin=15 * mm,
            topMargin=13 * mm,
            bottomMargin=13 * mm,
            pageCompression=1,
        )
        page_callback = cls._draw_protocol_page if framed else cls._draw_page_background
        doc.build(story, onFirstPage=page_callback, onLaterPages=page_callback)
        if not output_path.exists() or output_path.stat().st_size <= 0:
            raise OSError(f"PDF file was not created: {output_path}")

    @staticmethod
    def _draw_page_background(canvas, doc) -> None:
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, doc.pagesize[0], doc.pagesize[1], stroke=0, fill=1)
        canvas.restoreState()

    @staticmethod
    def _draw_protocol_page(canvas, doc) -> None:
        ProcedureReportLabBuilder._draw_page_background(canvas, doc)
        canvas.saveState()
        try:
            inset = 9 * mm
            width = doc.pagesize[0] - inset * 2
            height = doc.pagesize[1] - inset * 2
            canvas.setStrokeColor(colors.black)
            canvas.setLineWidth(0.9)
            canvas.rect(inset, inset, width, height, stroke=1, fill=0)
        finally:
            canvas.restoreState()

    @classmethod
    def _styles(cls) -> dict[str, ParagraphStyle]:
        cls._ensure_fonts()
        return {
            "normal": ParagraphStyle(
                "ProcedureNormal",
                fontName=cls.FONT_REGULAR,
                fontSize=10,
                leading=12,
                alignment=TA_LEFT,
                spaceAfter=4,
            ),
            "protocol_title": ParagraphStyle(
                "ProcedureProtocolTitle",
                fontName=cls.FONT_BOLD,
                fontSize=14,
                leading=16,
                alignment=TA_CENTER,
                spaceAfter=4,
            ),
            "procedure_title": ParagraphStyle(
                "ProcedureSelectedTitle",
                fontName=cls.FONT_BOLD,
                fontSize=12,
                leading=14,
                alignment=TA_CENTER,
                spaceAfter=0,
            ),
            "center_bold": ParagraphStyle(
                "ProcedureCenterBold",
                fontName=cls.FONT_BOLD,
                fontSize=8.2,
                leading=9.0,
                alignment=TA_CENTER,
                spaceAfter=1.5,
            ),
            "center_title": ParagraphStyle(
                "ProcedureCenterTitle",
                fontName=cls.FONT_BOLD,
                fontSize=9.0,
                leading=10.0,
                alignment=TA_CENTER,
                spaceAfter=3,
            ),
            "center_small": ParagraphStyle(
                "ProcedureCenterSmall",
                fontName=cls.FONT_REGULAR,
                fontSize=6.8,
                leading=7.6,
                alignment=TA_CENTER,
                spaceAfter=1.5,
            ),
            "left": ParagraphStyle(
                "ProcedureLeft",
                fontName=cls.FONT_REGULAR,
                fontSize=10,
                leading=12,
                alignment=TA_LEFT,
                spaceAfter=4,
            ),
            "consent_title": ParagraphStyle(
                "ProcedureConsentTitle",
                fontName=cls.FONT_BOLD,
                fontSize=14,
                leading=16,
                alignment=TA_CENTER,
                spaceAfter=0,
            ),
            "consent_subtitle": ParagraphStyle(
                "ProcedureConsentSubtitle",
                fontName=cls.FONT_BOLD,
                fontSize=12,
                leading=14,
                alignment=TA_CENTER,
                spaceAfter=6,
            ),
            "consent_normal": ParagraphStyle(
                "ProcedureConsentNormal",
                fontName=cls.FONT_REGULAR,
                fontSize=12,
                leading=14,
                alignment=TA_LEFT,
                spaceAfter=4,
            ),
            "consent_note": ParagraphStyle(
                "ProcedureConsentNote",
                fontName=cls.FONT_REGULAR,
                fontSize=10,
                leading=12,
                alignment=TA_LEFT,
                spaceAfter=3,
            ),
            "consent_footnote": ParagraphStyle(
                "ProcedureConsentFootnote",
                fontName=cls.FONT_REGULAR,
                fontSize=6,
                leading=7,
                alignment=TA_CENTER,
                spaceAfter=5,
            ),
            "transfusion_title": ParagraphStyle(
                "ProcedureTransfusionTitle",
                fontName=cls.FONT_BOLD,
                fontSize=12,
                leading=13,
                alignment=TA_CENTER,
                spaceAfter=0,
            ),
            "transfusion_cell": ParagraphStyle(
                "ProcedureTransfusionCell",
                fontName=cls.FONT_REGULAR,
                fontSize=6.6,
                leading=7.2,
                alignment=TA_LEFT,
                spaceAfter=0,
            ),
            "transfusion_cell_center": ParagraphStyle(
                "ProcedureTransfusionCellCenter",
                fontName=cls.FONT_REGULAR,
                fontSize=6.6,
                leading=7.2,
                alignment=TA_CENTER,
                spaceAfter=0,
            ),
            "transfusion_cell_bold": ParagraphStyle(
                "ProcedureTransfusionCellBold",
                fontName=cls.FONT_BOLD,
                fontSize=6.8,
                leading=7.4,
                alignment=TA_LEFT,
                spaceAfter=0,
            ),
            "transfusion_section": ParagraphStyle(
                "ProcedureTransfusionSection",
                fontName=cls.FONT_BOLD,
                fontSize=7.2,
                leading=7.8,
                alignment=TA_CENTER,
                spaceAfter=0,
            ),
        }

    @classmethod
    def _p(cls, text: str, style: str = "normal") -> Paragraph:
        styles = cls._styles()
        return Paragraph(cls._text(text).replace("\n", "<br/>"), styles.get(style, styles["normal"]))

    @classmethod
    def _rich_p(cls, text: str, style: str = "normal") -> Paragraph:
        styles = cls._styles()
        return Paragraph(str(text or "").replace("\n", "<br/>"), styles.get(style, styles["normal"]))

    @staticmethod
    def _spacer(height: float) -> Spacer:
        return Spacer(1, float(height))

    @classmethod
    def _patient_table(cls, context: dict[str, str]) -> Table:
        styles = cls._styles()
        data = [
            [
                Paragraph(cls._text(f"Пациент {context.get('patient_name', '')}"), styles["left"]),
                Paragraph(cls._text(f"ИБ № {context.get('history_number', '')}"), styles["left"]),
            ],
            [
                Paragraph(cls._text(f"Пол {context.get('sex', '')}"), styles["left"]),
                Paragraph(cls._text(f"Возраст {context.get('age', '')}"), styles["left"]),
            ],
            [
                Paragraph(cls._text(f"Диагноз {context.get('diagnosis', '')}"), styles["left"]),
                Paragraph("", styles["left"]),
            ],
        ]
        table = Table(data, colWidths=[82 * mm, 83 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        return table

    @staticmethod
    def _line(label: str, value: str) -> str:
        return f"{label}: {value or '____________________________'}"

    @classmethod
    def _underlined_line(cls, text: str):
        styles = cls._styles()
        value = cls._text(text) if text else "&nbsp;"
        table = Table([[Paragraph(value, styles["consent_normal"])]], colWidths=[165 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, -1), 0.65, colors.black),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ]
            )
        )
        return table

    @classmethod
    def _doctor_signature_row(cls, doctor_name: str):
        styles = cls._styles()
        data = [
            [
                Paragraph(cls._text(f"Врач анестезиолог-реаниматолог: {doctor_name}"), styles["consent_normal"]),
                Paragraph(cls._text("Подпись ______________"), styles["consent_normal"]),
            ]
        ]
        table = Table(data, colWidths=[100 * mm, 65 * mm])
        table.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        return table

    @classmethod
    def _draw_mono_page(cls, pdf, lines: list[str]) -> None:
        pdf.saveState()
        try:
            pdf.setFont(cls.FONT_MONO, 10)
            pdf.setFillColor(colors.black)
            x = 30 * mm
            y = A4[1] - 38
            line_height = 12
            for line in lines:
                text = cls._plain_pdf_text(line)
                pdf.drawString(x, y, text)
                cls._draw_transfusion_consent_underlines(pdf, text, x, y)
                y -= line_height
            if any("Этот  раздел  бланка" in line for line in lines):
                box_top = A4[1] - 38 - (15 - 1) * line_height + 4
                box_bottom = A4[1] - 38 - (24 - 1) * line_height - 4
                pdf.setLineWidth(0.8)
                pdf.rect(x + 3, box_bottom, 66 * 6, box_top - box_bottom, stroke=1, fill=0)
        finally:
            pdf.restoreState()

    @classmethod
    def _transfusion_consent_pages(cls, context: dict[str, str]) -> list[list[str]]:
        lines = cls._transfusion_consent_lines(context)
        page_1 = lines[:67]
        page_2 = lines[67:]
        return [page_1, page_2]

    @classmethod
    def _transfusion_consent_lines(cls, context: dict[str, str]) -> list[str]:
        patient_name = cls._plain_pdf_text(context.get("patient_name", ""))
        birth_year = cls._plain_pdf_text(context.get("birth_year", ""))
        doctor = cls._plain_pdf_text(context.get("doctor", ""))
        consent_date = cls._plain_pdf_text(context.get("transfusion_consent_date", ""))
        consent_mode = cls._plain_pdf_text(context.get("consent_mode", "patient"))
        show_consilium = consent_mode == "consilium"

        lines = [
            "    ",
            "            ФЕДЕРАЛЬНОЕ МЕДИКО-БИОЛОГИЧЕСКОЕ АГЕНТСТВО",
            "           ЦМСЧ/МСЧ/КБ/ИНСТИТУТ _______________________",
            "",
            " ",
            "              Информированное добровольное согласие",
            "      на оперативное вмешательство, в т.ч. переливание крови",
            "                         и ее компонентов",
            "",
            cls._field_line("Я ", patient_name, 66),
            "               (фамилия, имя, отчество - полностью)",
            f"{cls._birth_year_field(birth_year)} года рождения, проживающий(ая) по адресу: ___________",
            "__________________________________________________________________",
            "",
            "",
            " Этот  раздел  бланка  заполняется  только  на  лиц, не достигших",
            " возраста 15 лет, или недееспособных граждан: Я, паспорт: ______,",
            " выдан: _________________________________________________________",
            " являюсь  законным   представителем  (мать,   отец,  усыновитель,",
            " опекун,    попечитель)    ребенка    или    лица,    признанного",
            " недееспособным: ________________________________________________",
            "                 (Ф.И.О. ребенка или недееспособного гражданина -",
            "                             полностью, год рождения)            ",
            "",
            "",
            "находясь на лечении  (обследовании,  родоразрешении)  в  отделении",
            "________________________ОАРИТ №3__________________________________",
            "                (название отделения, номер палаты)",
            "__________________________________________________________________",
            "добровольно даю свое согласие на проведение мне (представляемому):",
            "операции: ______гемотрансфузия (плазмотрансфузия)_________________",
            "__________________________________________________________________",
            "__________________________________________________________________",
            "              (название медицинского вмешательства)",
            "и прошу персонал медицинского учреждения о ее проведении.",
            "Подтверждаю,   что   я   ознакомлен   (ознакомлена)  с  характером",
            "предстоящей мне (представляемому) операции. Мне  разъяснены,  и  я",
            "понимаю особенности и ход предстоящего оперативного лечения.",
            "-  Мне  разъяснено  и  я  осознаю,  что  во  время  операции могут",
            "возникнуть  непредвиденные  обстоятельства  и  осложнения. В таком",
            "случае  я  согласен  (согласна) на то, что ход операции может быть",
            "изменен врачами по их усмотрению.",
            "-  Я  предупрежден (предупреждена) о факторах риска и понимаю, что",
            "проведение  операции сопряжено с риском потери крови, возможностью",
            "инфекционных  осложнений, нарушений со стороны сердечно-сосудистой",
            "и  других  систем  жизнедеятельности  организма, непреднамеренного",
            "причинения вреда здоровью и даже неблагоприятного исхода.",
            "-  Я  предупрежден  (предупреждена),  что  в  ряде  случаев  могут",
            "потребоваться  повторные  операции,  в  т.ч.  в связи с возможными",
            "послеоперационными   осложнениями   или  с  особенностями  течения",
            "заболевания, и даю свое согласие на это.",
            "-  Я  поставил (поставила) в известность врача обо всех проблемах,",
            "связанных  со  здоровьем, в том числе об аллергических проявлениях",
            "или  индивидуальной  непереносимости лекарственных препаратов, обо",
            "всех  перенесенных  мною (представляемым) и известных мне травмах,",
            "операциях,   заболеваниях,   в   т.ч.  носительстве  ВИЧ-инфекции,",
            "вирусных  гепатитах,  туберкулезе, инфекциях, передаваемых половым",
            "путем,  об  экологических  и производственных факторах физической,",
            "химической  или  биологической  природы,  воздействующих  на  меня",
            "(представляемого)    во   время   жизнедеятельности,   принимаемых",
            "лекарственных  средствах, проводившихся ранее переливаниях крови и",
            "ее   компонентов.   Сообщил   (сообщила)   правдивые   сведения  о",
            "наследственности,  а также об употреблении алкоголя, наркотических",
            "и токсических средств.",
            "-  Я  знаю, что во время операции возможна потеря крови и",
            "даю согласие на переливание донорской или ауто (собственной) крови",
            "и ее компонентов.",
            f"- Я {cls._field_value(patient_name, 30)} согласен (согласна) на запись хода операции на",
            "информационные   носители   и  демонстрацию  лицам  с  медицинским",
            "образованием исключительно в медицинских,  научных  или  обучающих",
            "целях с учетом сохранения врачебной тайны.",
            "-  Мне  была  предоставлена  возможность  задать вопросы о степени",
            "риска  и  пользе  оперативного  вмешательства,  в т.ч. переливаний",
            "донорской или ауто (собственной) крови и/или ее компонентов и врач",
            "дал понятные мне исчерпывающие ответы.",
            "- Я ознакомлен  (ознакомлена)  и  согласен   (согласна)  со  всеми",
            "пунктами настоящего  документа, положения которого мне разъяснены,",
            "мною поняты и добровольно даю свое согласие на гемотрансфузию",
            "(плазмотрансфузию)._______________________________________________",
            "__________________________________________________________________",
            "",
            "                                                             ----",
            cls._patient_signature_date_line(consent_date),
            "                                 представителя               ----",
            "",
            "Расписался в моем присутствии:                               ----",
            cls._doctor_consent_line(doctor),
            "              (должность, И.О. Фамилия)                      ----                     ",
            "",
            "ПРИМЕЧАНИЕ:",
            "    Согласие  на  медицинское  вмешательство  в  отношении лиц, не",
            "достигших  возраста  15 лет, и граждан, признанных в установленном",
            "законом  порядке  недееспособными,  дают их законные представители",
            "(родители,   усыновители,  опекуны  или  попечители)  с  указанием",
            "Ф.И.О.,  паспортных  данных, родственных отношений после сообщения",
            "им  сведений  о результатах обследования, наличии заболевания, его",
            "диагнозе  и  прогнозе,  методах  лечения,  связанном с ними риске,",
            "возможных  вариантах медицинского вмешательства, их последствиях и",
            "результатах проведенного лечения.",
            "    При  отсутствии  законных представителей решение о медицинском",
            "вмешательстве  принимает  консилиум,  а  при невозможности собрать",
            "консилиум  - непосредственно лечащий (дежурный) врач с последующим",
            "уведомлением  главного врача/начальника ЦМСЧ/МСЧ/КБ/Института, а в",
            "выходные,   праздничные   дни,   вечернее   и   ночное   время   -",
            "ответственного дежурного врача и законных представителей.",
            "    В   случаях,  когда  состояние  гражданина  не  позволяет  ему",
            "выразить  свою волю, а медицинское вмешательство неотложно, вопрос",
            "о  его  проведении  в интересах гражданина решает консилиум, а при",
            "невозможности   собрать   консилиум   -   непосредственно  лечащий",
            "(дежурный)    врач    с    последующим    уведомлением    главного",
            "врача/начальника ЦМСЧ/МСЧ/КБ/Института,  а в выходные, праздничные",
            "дни, вечернее и ночное время - ответственного дежурного врача.",
            "",
            "Дополнительная информация:",
            "__________________________________________________________________",
            "__________________________________________________________________",
            "__________________________________________________________________",
            "",
            "                                                             ----",
            cls._patient_signature_date_line(consent_date),
            "                                 представителя               ----",
            "",
            "Расписался в моем присутствии:                               ----",
            cls._doctor_consent_line(doctor),
            "              (должность, И.О. Фамилия)                      ----",
        ]
        if show_consilium:
            lines.extend(
                [
                    "",
                    "Консилиум врачей в составе:",
                    cls._consilium_line(context.get("consilium_doctor_1", "")),
                    cls._consilium_line(context.get("consilium_doctor_2", "")),
                    cls._consilium_line(context.get("consilium_doctor_3", "")),
                    "",
                    cls._plain_pdf_text(consent_date) or '"__" ___________ 20__ года',
                ]
            )
        return lines

    @staticmethod
    def _field_value(value: str, width: int) -> str:
        text = str(value or "").strip().replace("\n", " ")
        if not text:
            return "_" * width
        return (text + "_" * width)[:width]

    @classmethod
    def _draw_transfusion_consent_underlines(cls, pdf, text: str, x: float, y: float) -> None:
        phrases = (
            "гемотрансфузия (плазмотрансфузия)",
            "гемотрансфузию",
            "(плазмотрансфузию).",
        )
        char_width = pdf.stringWidth("0", cls.FONT_MONO, 10)
        for phrase in phrases:
            start = text.find(phrase)
            while start >= 0:
                end = start + len(phrase)
                pdf.line(x + start * char_width, y - 1.4, x + end * char_width, y - 1.4)
                start = text.find(phrase, end)

    @staticmethod
    def _birth_year_field(value: str) -> str:
        text = str(value or "").strip().replace("\n", " ")
        if not text:
            return "_" * 12
        return text[:12].ljust(12)

    @classmethod
    def _field_line(cls, prefix: str, value: str, width: int, suffix: str = "") -> str:
        text = cls._plain_pdf_text(value).strip().replace("\n", " ")
        available = max(0, width - len(prefix) - len(suffix))
        if text:
            field = (text + " " + "_" * available)[:available]
        else:
            field = "_" * available
        return f"{prefix}{field}{suffix}"

    @classmethod
    def _patient_signature_date_line(cls, date_text: str) -> str:
        date_value = cls._plain_pdf_text(date_text) or '"__" ___________ 20__ года'
        return f"{date_value:<29}      Подпись пациента/законного "

    @classmethod
    def _doctor_consent_line(cls, doctor_name: str) -> str:
        return cls._field_line("Врач ", doctor_name, 66, " (подпись)")

    @classmethod
    def _consilium_line(cls, doctor_name: str) -> str:
        return cls._field_line("Должность, Ф.И.О. и подпись ", doctor_name, 66)

    @staticmethod
    def _plain_pdf_text(value) -> str:
        return html.unescape(str(value or "")).replace("\r", " ").replace("\n", " ")

    @classmethod
    def _cvc_consent_note(cls, context: dict[str, str]) -> list:
        reason = context.get("emergency_reason", "") or "______________________________________________"
        return [
            cls._rich_p(
                f"<font name=\"{cls.FONT_BOLD}\">Примечание.</font> Если речь идет о несовершеннолетних детях, пациентах, "
                "находящихся в бессознательном состоянии или с различными степенями нарушения сознания, речевыми нарушениями, "
                "на основании ст. 20 № 323-ФЗ - все моменты обсуждаются между медицинским работником (врачом анестезиологом-"
                "реаниматологом) и одним из родителей или иным законным представителем. В экстренном случае, при отсутствии "
                "сознания у пострадавшего, отсутствия одного из родителей или иного законного представителя, при необходимости "
                "начала проведения интенсивной терапии или оказания реанимационных мероприятий, принятие решения осуществляется "
                "консилиумом медицинской организации.",
                "consent_note",
            ),
            cls._p(
                "Если получить информированное (осознанное) согласие пациента на катетеризацию подключичной и других центральных вен "
                f"не представляется возможным вследствие тяжести состояния больного (энцефалопатия, кома, шоковое состояние, др.), "
                f"{reason} катетеризация проводится по экстренным показаниям. Альтернативных методов проведения интенсивной терапии нет.",
                "consent_note",
            ),
        ]

    @classmethod
    def _cvc_consent_consilium(cls, context: dict[str, str]) -> list:
        flowables = [
            cls._spacer(18),
            cls._p("Решение о проведении катетеризации центральной вены принято решением консилиума в составе:", "consent_normal"),
            cls._p(f"1. {context.get('consilium_doctor_1', '')}    Подпись ____________________", "consent_normal"),
            cls._p(f"2. {context.get('consilium_doctor_2', '')}    Подпись ____________________", "consent_normal"),
            cls._p(f"3. {context.get('consilium_doctor_3', '')}    Подпись ____________________", "consent_normal"),
        ]
        if context.get("consilium_notes"):
            flowables.append(cls._p(context.get("consilium_notes", ""), "consent_normal"))
        flowables.append(
            cls._p(
                "В случае если собрать консилиум или провести его по телефону невозможно, решение принимается непосредственно "
                "лечащим (дежурным) врачом с внесением такого решения в медицинскую документацию пациента и последующим уведомлением "
                "должностных лиц медицинской организации (руководителя медицинской организации или руководителя отделения медицинской организации).",
                "consent_normal",
            )
        )
        return flowables

    @classmethod
    def _lp_consent_consilium(cls, context: dict[str, str]) -> list:
        doctor_1 = context.get("consilium_doctor_1", "") or "ФИО врача, должность"
        doctor_2 = context.get("consilium_doctor_2", "") or "ФИО врача, должность"
        doctor_3 = context.get("consilium_doctor_3", "") or "ФИО врача, должность"
        flowables = [
            cls._spacer(8),
            cls._p(
                "Учитывая не возможность пациентом выразить свою волю, а так же отсутствия законных представителей, "
                "решение о проведении люмбальной пункции принято консилиумом в составе:",
                "consent_normal",
            ),
            cls._p(f"1. {doctor_1}, подпись __________________________________________________", "consent_normal"),
            cls._p(f"2. {doctor_2}, подпись __________________________________________________", "consent_normal"),
            cls._p(f"3. {doctor_3}, подпись __________________________________________________", "consent_normal"),
        ]
        if context.get("consilium_notes"):
            flowables.append(cls._p(context.get("consilium_notes", ""), "consent_normal"))
        flowables.append(
            cls._p(
                "В случае если собрать консилиум или провести его по телефону невозможно, решение принимается непосредственно "
                "лечащим (дежурным) врачом с внесением такого решения в медицинскую документацию пациента и последующим уведомлением "
                "должностных лиц медицинской организации (руководителя медицинской организации или руководителя отделения медицинской организации).",
                "consent_normal",
            )
        )
        return flowables

    @classmethod
    def _transfusion_table(cls, context: dict[str, str]) -> Table:
        styles = cls._styles()
        rows = 41
        cols = 11
        data = [["" for _ in range(cols)] for _ in range(rows)]

        def cell(text: str, style: str = "transfusion_cell") -> Paragraph:
            return Paragraph(cls._text(text).replace("\n", "<br/>"), styles[style])

        def rich_cell(text: str, style: str = "transfusion_cell") -> Paragraph:
            return Paragraph(str(text or "").replace("\n", "<br/>"), styles[style])

        def put(row: int, col: int, text: str, style: str = "transfusion_cell"):
            data[row - 1][col - 1] = cell(text, style)

        def put_rich(row: int, col: int, text: str, style: str = "transfusion_cell"):
            data[row - 1][col - 1] = rich_cell(text, style)

        put(1, 1, "ПРОТОКОЛ ТРАНСФУЗИИ", "transfusion_title")
        put(2, 1, "ФИО реципиента:", "transfusion_cell_bold")
        put(2, 4, "Дата и время подачи заявки:", "transfusion_cell_bold")
        put(2, 7, "Дата трансфузии:", "transfusion_cell_bold")
        put(3, 1, context.get("patient_name", ""))
        put(3, 4, context.get("request_datetime", ""))
        put(3, 7, context.get("transfusion_date", ""))
        profile_text = cls._text(context.get("department", ""))
        put_rich(5, 1, f'<font name="{cls.FONT_BOLD}">Профиль:</font> {profile_text}')
        put(5, 4, "№:", "transfusion_cell_bold")
        put(5, 5, context.get("history_number", ""))
        put(5, 7, "Время начала трансфузии:", "transfusion_cell_bold")
        put(5, 10, context.get("start_time", ""), "transfusion_cell_center")
        put(6, 7, "Время окончания трансфузии:", "transfusion_cell_bold")
        put(6, 10, context.get("finish_time", ""), "transfusion_cell_center")
        put(7, 1, "Данные медицинского обследования реципиента", "transfusion_section")
        put(8, 1, "Группа крови реципиента ABO:", "transfusion_cell_bold")
        put(8, 4, context.get("recipient_abo", ""), "transfusion_cell_center")
        put(8, 7, "Резус-принадлежность:", "transfusion_cell_bold")
        put(8, 10, context.get("recipient_rh", ""), "transfusion_cell_center")
        put(9, 1, "Антигены:", "transfusion_cell_bold")
        put(9, 2, context.get("recipient_antigens", ""))
        put(9, 7, "Аллоиммунные антитела:", "transfusion_cell_bold")
        put(9, 10, context.get("alloimmune_antibodies", ""), "transfusion_cell_center")
        indication_text = cls._text(context.get("indication_print", ""))
        put_rich(
            10,
            1,
            f'<font name="{cls.FONT_BOLD}">Показания к трансфузии:</font> {indication_text}'.strip(),
        )
        put(12, 1, "Трансфузии компонентов крови в анамнезе:", "transfusion_cell_bold")
        put(12, 4, "Реакции и осложнения на трансфузии в анамнезе:", "transfusion_cell_bold")
        put(12, 8, "Трансфузии по индивидуальному подбору:", "transfusion_cell_bold")
        put(14, 1, context.get("transfusions_history", ""), "transfusion_cell_center")
        reactions_text = context.get("reactions_history", "")
        if context.get("reactions_history_details"):
            reactions_text = f"{reactions_text}; {context.get('reactions_history_details')}"
        put(14, 4, reactions_text, "transfusion_cell_center")
        put(14, 8, context.get("individual_selection_history", ""), "transfusion_cell_center")
        put(15, 1, "Данные о донорской крови и ее компоненте", "transfusion_section")
        put(16, 1, "Наименование компонента донорской крови:", "transfusion_cell_bold")
        put(16, 6, f"Наименование организации осуществившей заготовку: {context.get('procurement_org', '')}")
        put(17, 1, context.get("donor_component_name", ""))
        put(18, 1, "Группа крови донора ABO, Rh фактор:", "transfusion_cell_bold")
        put(18, 6, "Антигены эритроцитов донора :", "transfusion_cell_bold")
        put(19, 1, context.get("donor_abo", ""), "transfusion_cell_center")
        put(19, 3, context.get("donor_rh", ""), "transfusion_cell_center")
        put(19, 6, context.get("donor_antigens", ""))
        put(20, 1, "№ единицы компонента крови:", "transfusion_cell_bold")
        put(20, 4, "Количество, мл:", "transfusion_cell_bold")
        put(21, 1, context.get("unit_number", ""))
        put(21, 4, context.get("volume_ml", ""), "transfusion_cell_center")
        put(22, 1, "Дата заготовки:", "transfusion_cell_bold")
        put(22, 3, context.get("collection_date", ""), "transfusion_cell_center")
        put(22, 6, "Срок годности до:", "transfusion_cell_bold")
        put(22, 9, context.get("expiration_date", ""), "transfusion_cell_center")
        put(23, 1, "Результаты индивидуального подбора", "transfusion_section")
        put(24, 1, "Наименование МО", "transfusion_cell_bold")
        put(24, 6, context.get("selection_medical_org", ""))
        put(25, 1, "Дата исследования", "transfusion_cell_bold")
        put(25, 6, context.get("selection_study_date", ""))
        put(26, 1, "ФИО ответственного лица:", "transfusion_cell_bold")
        put(26, 6, "Заключение :", "transfusion_cell_bold")
        put(27, 1, context.get("selection_responsible_name", ""))
        put(27, 6, context.get("selection_conclusion", ""))
        put(28, 1, "Пробы на индивидуальную совместимость в отделении", "transfusion_section")
        put(29, 1, "Наименования реагентов:", "transfusion_cell_bold")
        put(29, 7, "Цоликлоны", "transfusion_cell_center")
        put(30, 1, context.get("reagent_series_text", ""))
        put(30, 7, context.get("reagent_expiration_text", ""))
        put(32, 1, "На плоскости :", "transfusion_cell_bold")
        put(32, 6, "Биологическая проба:", "transfusion_cell_bold")
        put(33, 1, context.get("plane_compatibility", ""), "transfusion_cell_center")
        put(33, 6, context.get("biological_test", ""), "transfusion_cell_center")
        put(34, 1, "Реакции и осложнения", "transfusion_section")
        put(35, 1, "Основные симптомы:", "transfusion_cell_bold")
        put(35, 2, context.get("reaction_symptoms", ""))
        put(35, 6, "Степень тяжести:", "transfusion_cell_bold")
        put(35, 8, context.get("reaction_severity", ""))
        put(36, 1, "Наблюдение за состоянием реципиента", "transfusion_section")
        put(37, 1, "Время", "transfusion_cell_bold")
        put(37, 4, "АД(мм.рт.ст)", "transfusion_cell_bold")
        put(37, 6, "ЧСС(уд/мин)", "transfusion_cell_bold")
        put(37, 8, "Температура(С)", "transfusion_cell_bold")
        put(37, 10, "Диурез,цвет мочи", "transfusion_cell_bold")
        for row, prefix, label in (
            (38, "before", "Перед началом переливания"),
            (39, "hour1", "Через 1 час"),
            (40, "hour2", "Через 2 часа"),
        ):
            put(row, 1, label)
            put(row, 4, context.get(f"obs_{prefix}_bp", ""), "transfusion_cell_center")
            put(row, 6, context.get(f"obs_{prefix}_pulse", ""), "transfusion_cell_center")
            put(row, 8, context.get(f"obs_{prefix}_temp", ""), "transfusion_cell_center")
            put(row, 10, context.get(f"obs_{prefix}_diuresis", ""), "transfusion_cell_center")
        put(41, 1, "Врач, осуществивший трансфузию:", "transfusion_cell_bold")
        put(41, 7, context.get("doctor", ""))

        spans = [
            ("SPAN", (0, 0), (0, 10)),
            ("SPAN", (1, 0), (1, 2)), ("SPAN", (1, 3), (1, 5)), ("SPAN", (1, 6), (1, 10)),
            ("SPAN", (2, 0), (3, 2)), ("SPAN", (2, 3), (3, 5)), ("SPAN", (2, 6), (3, 10)),
            ("SPAN", (4, 0), (5, 2)), ("SPAN", (4, 3), (5, 3)), ("SPAN", (4, 4), (5, 5)),
            ("SPAN", (4, 6), (4, 8)), ("SPAN", (4, 9), (4, 10)),
            ("SPAN", (5, 6), (5, 8)), ("SPAN", (5, 9), (5, 10)),
            ("SPAN", (6, 0), (6, 10)),
            ("SPAN", (7, 0), (7, 2)), ("SPAN", (7, 3), (7, 5)), ("SPAN", (7, 6), (7, 8)), ("SPAN", (7, 9), (7, 10)),
            ("SPAN", (8, 1), (8, 5)), ("SPAN", (8, 6), (8, 8)), ("SPAN", (8, 9), (8, 10)),
            ("SPAN", (9, 0), (10, 10)),
            ("SPAN", (11, 0), (12, 2)), ("SPAN", (11, 3), (12, 6)), ("SPAN", (11, 7), (12, 10)),
            ("SPAN", (13, 0), (13, 2)), ("SPAN", (13, 3), (13, 6)), ("SPAN", (13, 7), (13, 10)),
            ("SPAN", (14, 0), (14, 10)),
            ("SPAN", (15, 0), (15, 4)), ("SPAN", (15, 5), (16, 10)), ("SPAN", (16, 0), (16, 4)),
            ("SPAN", (17, 0), (17, 4)), ("SPAN", (17, 5), (17, 10)),
            ("SPAN", (18, 0), (18, 1)), ("SPAN", (18, 2), (18, 4)), ("SPAN", (18, 5), (20, 10)),
            ("SPAN", (19, 0), (19, 2)), ("SPAN", (19, 3), (19, 4)),
            ("SPAN", (20, 0), (20, 2)), ("SPAN", (20, 3), (20, 4)),
            ("SPAN", (21, 0), (21, 1)), ("SPAN", (21, 2), (21, 4)), ("SPAN", (21, 5), (21, 7)), ("SPAN", (21, 8), (21, 10)),
            ("SPAN", (22, 0), (22, 10)),
            ("SPAN", (23, 0), (23, 4)), ("SPAN", (23, 5), (23, 10)),
            ("SPAN", (24, 0), (24, 4)), ("SPAN", (24, 5), (24, 10)),
            ("SPAN", (25, 0), (25, 4)), ("SPAN", (25, 5), (26, 10)), ("SPAN", (26, 0), (26, 4)),
            ("SPAN", (27, 0), (27, 10)),
            ("SPAN", (28, 0), (28, 5)), ("SPAN", (28, 6), (28, 10)),
            ("SPAN", (29, 0), (30, 5)), ("SPAN", (29, 6), (30, 10)),
            ("SPAN", (31, 0), (31, 4)), ("SPAN", (31, 5), (31, 10)),
            ("SPAN", (32, 0), (32, 4)), ("SPAN", (32, 5), (32, 10)),
            ("SPAN", (33, 0), (33, 10)),
            ("SPAN", (34, 0), (34, 4)), ("SPAN", (34, 5), (34, 6)), ("SPAN", (34, 7), (34, 10)),
            ("SPAN", (35, 0), (35, 10)),
            ("SPAN", (36, 0), (36, 2)), ("SPAN", (36, 3), (36, 4)), ("SPAN", (36, 5), (36, 6)),
            ("SPAN", (36, 7), (36, 8)), ("SPAN", (36, 9), (36, 10)),
            ("SPAN", (37, 0), (37, 2)), ("SPAN", (37, 3), (37, 4)), ("SPAN", (37, 5), (37, 6)),
            ("SPAN", (37, 7), (37, 8)), ("SPAN", (37, 9), (37, 10)),
            ("SPAN", (38, 0), (38, 2)), ("SPAN", (38, 3), (38, 4)), ("SPAN", (38, 5), (38, 6)),
            ("SPAN", (38, 7), (38, 8)), ("SPAN", (38, 9), (38, 10)),
            ("SPAN", (39, 0), (39, 2)), ("SPAN", (39, 3), (39, 4)), ("SPAN", (39, 5), (39, 6)),
            ("SPAN", (39, 7), (39, 8)), ("SPAN", (39, 9), (39, 10)),
            ("SPAN", (40, 0), (40, 5)), ("SPAN", (40, 6), (40, 8)), ("SPAN", (40, 9), (40, 10)),
        ]
        span_commands = [
            ("SPAN", (start[1], start[0]), (end[1], end[0]))
            for _command, start, end in spans
        ]
        section_background = colors.Color(0.93, 0.93, 0.93)
        table = Table(
            data,
            colWidths=[16.35 * mm] * cols,
            rowHeights=[6.35 * mm] * rows,
            repeatRows=0,
            hAlign="CENTER",
        )
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.35, colors.black),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("BACKGROUND", (0, 6), (10, 6), section_background),
                    ("BACKGROUND", (0, 14), (10, 14), section_background),
                    ("BACKGROUND", (0, 22), (10, 22), section_background),
                    ("BACKGROUND", (0, 27), (10, 27), section_background),
                    ("BACKGROUND", (0, 33), (10, 33), section_background),
                    ("BACKGROUND", (0, 35), (10, 35), section_background),
                    *span_commands,
                ]
            )
        )
        return table

    @classmethod
    def _cvc_protocol_body(cls, context: dict[str, str]) -> list:
        flowables = [
            cls._point(1, "ФИО пациента", context.get("patient_name", "")),
            cls._point(2, "Номер истории болезни", context.get("history_number", "")),
            cls._point(3, "ФИО врача", context.get("doctor", "")),
            cls._point(4, "Отделение", context.get("department", "")),
            cls._point(5, "Дата процедуры", cls._procedure_time_text(context)),
            cls._point(6, "Показания к катетеризации", context.get("indications_text", "")),
            cls._point(7, "Место проведения операции", context.get("place_text", "")),
            cls._point(8, "Вид анестезии", context.get("anesthesia_text", "")),
            cls._point(9, "Локализация доступа", context.get("access_text", "")),
            cls._point(10, "Методика катетеризации", context.get("method_text", "")),
            cls._point(11, "УЗ-контроль", context.get("ultrasound_text", "")),
            cls._point(12, "Количество попыток", context.get("catheter_params_text", "")),
            cls._point(13, "Катетер фиксирован", context.get("fixation_text", "")),
            cls._point(14, "Корректное положение катетера подтверждено", cls._confirmation_text(context)),
            cls._point(
                15,
                "Технические трудности/осложнения во время выполнения и предпринятые действия",
                context.get("difficulty_text", ""),
            ),
        ]
        if context.get("actions_taken"):
            flowables.append(cls._p(f"Предпринятые действия: {context.get('actions_taken', '')}", "left"))
        flowables.append(cls._p(f"ФИО врача: {context.get('doctor', '')}    Подпись ____________________", "left"))
        return flowables

    @classmethod
    def _lp_protocol_body(cls, context: dict[str, str]) -> list:
        flowables = [
            cls._point(1, "ФИО пациента", context.get("patient_name", "")),
            cls._point(2, "Номер истории болезни", context.get("history_number", "")),
            cls._point(3, "ФИО врача", context.get("doctor", "")),
            cls._point(4, "Отделение", context.get("department", "")),
            cls._point(5, "Дата процедуры", cls._procedure_time_text(context)),
            cls._point(6, "Показания к люмбальной пункции", context.get("indications_text", "")),
            cls._point(7, "Место проведения", context.get("place_text", "")),
            cls._point(8, "Вид анестезии", context.get("anesthesia_text", "")),
            cls._point(9, "Доступ", context.get("access_text", "")),
            cls._point(10, "Уровень", context.get("level_text", "")),
            cls._point(
                11,
                "Технические трудности/осложнения во время выполнения и предпринятые действия",
                context.get("difficulty_text", ""),
            ),
            cls._point(12, "Результат", context.get("result_text", "")),
            cls._point(13, "Характеристики ликвора", context.get("csf_characteristics", "")),
            cls._point(14, "Примечания", context.get("result_notes", "")),
        ]
        if context.get("actions_taken"):
            flowables.append(cls._p(f"Предпринятые действия: {context.get('actions_taken', '')}", "left"))
        if context.get("notes"):
            flowables.append(cls._p(f"Общее примечание: {context.get('notes', '')}", "left"))
        flowables.append(cls._p(f"ФИО врача: {context.get('doctor', '')}    Подпись ____________________", "left"))
        return flowables

    @classmethod
    def _cvc_removal_body(cls, context: dict[str, str], *, start_number: int = 16) -> list:
        return [
            cls._point(start_number, "Катетер удален / переустановлен", context.get("removal_summary", "")),
            cls._point(start_number + 1, "Осложнения во время использования катетера", context.get("usage_complications_text", "")),
            cls._point(start_number + 2, "Отмеченные осложнения в виде", cls._usage_complications_detail(context)),
            cls._p(f"ФИО врача: {context.get('doctor', '')}    Подпись ____________________", "left"),
        ]

    @classmethod
    def _point(cls, number: int, label: str, value: str) -> Paragraph:
        suffix = f": {value}" if value else ":"
        return cls._p(f"{number}. {label}{suffix}", "left")

    @staticmethod
    def _procedure_time_text(context: dict[str, str]) -> str:
        parts = []
        if context.get("procedure_date"):
            parts.append(context.get("procedure_date", ""))
        if context.get("start_time"):
            parts.append(f"время начала: {context.get('start_time')}")
        if context.get("finish_time"):
            parts.append(f"время окончания: {context.get('finish_time')}")
        if context.get("duration"):
            parts.append(f"длительность: {context.get('duration')} мин")
        return "; ".join(parts)

    @staticmethod
    def _confirmation_text(context: dict[str, str]) -> str:
        parts = []
        if context.get("confirmation_time"):
            parts.append(f"время: {context.get('confirmation_time')}")
        if context.get("confirmation_text"):
            parts.append(context.get("confirmation_text", ""))
        if context.get("confirmation_comment"):
            parts.append(f"комментарий: {context.get('confirmation_comment')}")
        return "; ".join(parts)

    @staticmethod
    def _usage_complications_detail(context: dict[str, str]) -> str:
        parts = []
        if context.get("usage_complications_description"):
            parts.append(context.get("usage_complications_description", ""))
        if context.get("additional_treatment"):
            parts.append(f"требовали дополнительных лечебных мероприятий: {context.get('additional_treatment')}")
        return "; ".join(parts)

    @staticmethod
    def _text(value) -> str:
        return html.escape(html.unescape(str(value or "")))

    @staticmethod
    def _join_present(*values) -> str:
        return ", ".join(str(value).strip() for value in values if str(value or "").strip())

    @classmethod
    def _ensure_fonts(cls) -> None:
        ReportLabReportBuilder._ensure_fonts_registered()
        cls.FONT_REGULAR = ReportLabReportBuilder.FONT_REGULAR
        cls.FONT_BOLD = ReportLabReportBuilder.FONT_BOLD
        if cls.FONT_MONO not in pdfmetrics.getRegisteredFontNames():
            windir = os.environ.get("WINDIR", r"C:\Windows")
            mono_candidates = [
                os.path.join(windir, "Fonts", "cour.ttf"),
                os.path.join(windir, "Fonts", "consola.ttf"),
                "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            ]
            for mono_path in mono_candidates:
                if os.path.exists(mono_path):
                    pdfmetrics.registerFont(TTFont(cls.FONT_MONO, mono_path))
                    break
            else:
                cls.FONT_MONO = cls.FONT_REGULAR
        if cls.FONT_ITALIC in pdfmetrics.getRegisteredFontNames():
            return

        windir = os.environ.get("WINDIR", r"C:\Windows")
        candidates = [
            os.path.join(windir, "Fonts", "ariali.ttf"),
            os.path.join(windir, "Fonts", "segoeuii.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
        ]
        for italic_path in candidates:
            if os.path.exists(italic_path):
                pdfmetrics.registerFont(TTFont(cls.FONT_ITALIC, italic_path))
                return
        cls.FONT_ITALIC = cls.FONT_REGULAR
