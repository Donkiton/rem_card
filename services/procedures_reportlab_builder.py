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
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from rem_card.ui.rem_card_sectors.s_print.reportlab_builder import ReportLabReportBuilder


class ProcedureReportLabBuilder:
    FONT_REGULAR = ReportLabReportBuilder.FONT_REGULAR
    FONT_BOLD = ReportLabReportBuilder.FONT_BOLD
    FONT_ITALIC = "RemCardArial-Italic"

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
