from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from rem_card.data.dto.remcard_dto import OrderDTO, OrderType

from .order_input_handler import OrderInputHandler


def _normalize_template_drugs(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Нормализует payload шаблона к простому списку препаратов.
    Поддерживает legacy-структуру "complex"/"schedule", но без автозаполнения
    временных ячеек: в лист добавляются только строки назначений.
    """
    drugs = template.get("drugs", [])
    if isinstance(drugs, list) and drugs:
        return drugs

    # Legacy fallback: сложные шаблоны могли хранить элементы в "schedule".
    schedule = template.get("schedule", [])
    if not isinstance(schedule, list) or not schedule:
        return []

    normalized: List[Dict[str, Any]] = []
    for item in schedule:
        if not isinstance(item, dict):
            continue

        drug_key = item.get("drug") or item.get("drug_key")
        if not drug_key:
            continue

        diluent = item.get("diluent")
        if not isinstance(diluent, dict):
            base = item.get("diluent_base")
            vol = item.get("diluent_volume")
            if base:
                try:
                    vol_num = float(vol) if vol is not None else 0
                except (TypeError, ValueError):
                    vol_num = 0
                diluent = {"base": base, "volume": vol_num}
            else:
                diluent = None

        normalized.append(
            {
                "drug": drug_key,
                "dose": item.get("dose", 0),
                "unit": item.get("unit"),
                "admin_type": item.get("admin_type"),
                "duration_min": item.get("duration_min", 0),
                "freq": item.get("freq", 1),
                "diluent": diluent,
            }
        )
    return normalized


def build_orders_from_template(
    *,
    template: Dict[str, Any],
    engine,
    admission_id: int,
    base_time: datetime,
) -> List[OrderDTO]:
    """Build draft OrderDTO list from selected template payload."""
    orders_to_add: List[OrderDTO] = []

    for d in _normalize_template_drugs(template):
        if d.get("is_multicomp") and d.get("raw_text"):
            order = OrderInputHandler.parse_input_to_dto(d["raw_text"], admission_id)
            # Multi-component orders are loaded as empty sheet rows.
            order.specific_times = []
            orders_to_add.append(order)
            continue

        drug_key = d.get("drug")
        drug_info = engine.drugs.get(drug_key, {})
        diluent = d.get("diluent")

        latin_name = drug_info.get("latin", drug_key)
        form_key = d.get("form_key", drug_info.get("form_key"))
        prefix = engine.forms.get(form_key, {}).get("latin_abbr", "").strip()

        # With diluent powder becomes solution notation.
        if diluent and prefix:
            prefix = "S."

        if prefix:
            if not prefix.endswith("."):
                prefix += "."
            latin_name = f"{prefix} {latin_name}"

        dose = d.get("dose", drug_info.get("default_dose", 0))
        unit = d.get("unit", drug_info.get("unit", "mg"))
        admin_type = d.get("admin_type", drug_info.get("admin_type", "bolus"))
        duration_min = d.get("duration_min", drug_info.get("duration_min", 0))
        freq = d.get("freq", 1)
        if admin_type == "continuous":
            freq = 1

        otype = (
            OrderType.INFUSION_CONTINUOUS
            if admin_type in ["infusion", "continuous"]
            else OrderType.MEDICATION
        )

        route_name = engine.admin_types.get(admin_type, {}).get("name_ru", "")
        comment_parts: List[str] = []
        if diluent:
            dil_base = diluent.get("base")
            dil_vol = diluent.get("volume", 0)
            dil_display = engine.dilutions.get(dil_base, {}).get("display", dil_base)
            comment_parts.append(f"+ S. {dil_display} - {dil_vol} мл")

        if route_name:
            comment_parts.append(f"[ROUTE:{route_name}]")

        if duration_min != 0:
            comment_parts.append(f"[DUR:{duration_min}]")

        comment = " ".join(comment_parts)

        new_order = OrderDTO(
            admission_id=admission_id,
            drug_key=drug_key,
            latin=latin_name,
            type=otype,
            dose_value=dose,
            dose_unit=unit,
            is_per_kg=False,
            frequency=freq,
            specific_times=[],
            duration_min=duration_min,
            is_committed=0,
            created_at=base_time,
            comment=comment,
        )
        orders_to_add.append(new_order)

    return orders_to_add
