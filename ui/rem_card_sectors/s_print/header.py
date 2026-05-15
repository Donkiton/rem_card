from html import escape

from .balance import format_signed_ml, get_current_balance_totals


def render_header(data, start_str, end_str, show_hydrobalance=True):
    patient_name = escape(str(data.get("patient_name", "—")))
    diagnosis = escape(str(data.get("diagnosis", "—")))
    icu_day = escape(str(data.get("icu_day", "—")))
    start_label = escape(str(start_str))
    end_label = escape(str(end_str))
    hydrobalance_html = ""
    if show_hydrobalance:
        totals = get_current_balance_totals(data)
        if totals is not None:
            net_balance = escape(format_signed_ml(totals["net_balance"]))
            hydrobalance_html = (
                f' &nbsp;&nbsp; <strong>Гидробаланс</strong> = {net_balance} мл'
            )
    notice_number = escape(str(data.get("emergency_notice_number") or "").strip())
    notice_html = ""
    if notice_number:
        notice_html = f' &nbsp;&nbsp; <strong>Экстренное извещение</strong> N {notice_number}'

    return f"""
    <table class="print-header" width="100%">
        <tr>
            <td class="print-header-details">
                <div><strong>Пациент:</strong> {patient_name} &nbsp;&nbsp; <strong>Диагноз:</strong> {diagnosis}</div>
                <div><strong>Сутки в РАО:</strong> {icu_day} &nbsp;&nbsp; <strong>Период:</strong> {start_label} - {end_label}{hydrobalance_html}{notice_html}</div>
            </td>
            <td class="print-header-title">РЕАНИМАЦИОННАЯ КАРТА</td>
        </tr>
    </table>
    """
