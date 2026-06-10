from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, unquote

from rem_card.services.settings.settings_service import OPERBLOCK_SETTINGS_KEY, get_settings_service


OPERBLOCK_ROUTE_SETTINGS_SCOPE = "operblock"
OPERBLOCK_GROUP_ROUTES_KEY = "group_routes"
OPERBLOCK_GROUP_ROUTES_VERSION = 1
OPERBLOCK_DEFAULT_ROUTE_CODE = "bolus"
OPERBLOCK_LEGACY_DEFAULT_ROUTE_CODE = "iv"
OPERBLOCK_ORDER_ROUTE_TAG_RE = re.compile(r"\[OB_ROUTE:(?P<route>[^\]]+)\]", flags=re.IGNORECASE)


_ROUTE_CODE_ALIASES = {
    "": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "iv": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "вв": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "в/в": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "intravenous": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "bolus": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "в/в струйно": OPERBLOCK_DEFAULT_ROUTE_CODE,
    "im": "im",
    "вм": "im",
    "в/м": "im",
    "intramuscular": "im",
    "внутримышечно": "im",
    "в/мышечно": "im",
}

_SHORT_ROUTE_LABELS = {
    OPERBLOCK_DEFAULT_ROUTE_CODE: "в/в",
    OPERBLOCK_LEGACY_DEFAULT_ROUTE_CODE: "в/в",
    "im": "в/м",
    "infusion": "в/в",
    "dozator": "в/в доз.",
    "sc": "п/к",
    "id": "в/к",
    "per_os": "per os",
    "per_rectum": "per rectum",
    "spray": "ингал.",
}

_SHORT_ROUTE_LABEL_ALIASES = {
    "в/венно": "в/в",
    "внутривенно": "в/в",
    "в/в струйно": "в/в",
    "в/в болюсно": "в/в",
    "в/в капельно": "в/в",
    "в/в инфузия": "в/в",
    "в/в ч/з дозозатор": "в/в доз.",
    "в/в через дозозатор": "в/в доз.",
    "в/мышечно": "в/м",
    "внутримышечно": "в/м",
    "п/кожно": "п/к",
    "подкожно": "п/к",
    "в/кожно": "в/к",
    "внутрикожно": "в/к",
    "п/невр": "п/н",
    "п/неврально": "п/н",
    "периневрально": "п/н",
    "и/тек": "и/тек",
    "и/текально": "и/тек",
    "интратекально": "и/тек",
    "интратекальный": "и/тек",
    "э/дур": "э/дур",
    "э/дурально": "э/дур",
    "эпидурально": "э/дур",
    "эпидуральный": "э/дур",
    "ингаляционно": "ингал.",
}

_ROUTE_SLASH_LABEL_RE = re.compile(r"^\s*([A-Za-zА-Яа-яЁё])\s*/\s*([A-Za-zА-Яа-яЁё])")


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _fold_route_label(value: Any) -> str:
    return _clean_text(value).casefold().replace("ё", "е")


def normalize_operblock_route_code(value: Any) -> str:
    text = _clean_text(value)
    return _ROUTE_CODE_ALIASES.get(_fold_route_label(text), text)


def is_operblock_default_route(route_code: Any) -> bool:
    return normalize_operblock_route_code(route_code) == OPERBLOCK_DEFAULT_ROUTE_CODE


def _payload_label(payload: Mapping[str, Any], fallback: str) -> str:
    return (
        _clean_text(payload.get("name_ru"))
        or _clean_text(payload.get("display_name"))
        or _clean_text(payload.get("name"))
        or _clean_text(fallback)
    )


def _load_operblock_preset_group_codes() -> list[str]:
    try:
        from rem_card.services.operblock_medication_presets import load_operblock_medication_presets

        presets = load_operblock_medication_presets(include_disabled=True)
    except Exception:
        return []
    result: list[str] = []
    for preset in presets:
        clean_code = _clean_text((preset or {}).get("drug_group"))
        if clean_code and clean_code not in result:
            result.append(clean_code)
    return result


def load_operblock_drug_groups() -> list[dict[str, str]]:
    snapshot = get_settings_service().drug_catalog_snapshot()
    result: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for code, payload in (snapshot.groups or {}).items():
        clean_code = _clean_text(code)
        if not clean_code:
            continue
        data = payload if isinstance(payload, Mapping) else {}
        seen_codes.add(clean_code)
        result.append({"code": clean_code, "label": _payload_label(data, clean_code)})
    for clean_code in _load_operblock_preset_group_codes():
        if clean_code in seen_codes:
            continue
        seen_codes.add(clean_code)
        result.append({"code": clean_code, "label": f"Группа оперблока: {clean_code}"})
    result.sort(key=lambda item: (item["label"].casefold(), item["code"].casefold()))
    return result


def load_operblock_administration_routes() -> list[dict[str, str]]:
    snapshot = get_settings_service().drug_catalog_snapshot()
    result: list[dict[str, str]] = []
    for code, payload in (snapshot.routes or {}).items():
        clean_code = normalize_operblock_route_code(code)
        if not clean_code or any(item["code"] == clean_code for item in result):
            continue
        data = payload if isinstance(payload, Mapping) else {}
        result.append({"code": clean_code, "label": _payload_label(data, clean_code)})
    return result


def _shorten_route_label(code: str, label: Any) -> str:
    if code in _SHORT_ROUTE_LABELS:
        return _SHORT_ROUTE_LABELS[code]
    clean = _clean_text(label) or code
    folded = _fold_route_label(clean)
    if folded in _SHORT_ROUTE_LABEL_ALIASES:
        return _SHORT_ROUTE_LABEL_ALIASES[folded]
    if folded.startswith("per os"):
        return "per os"
    if folded.startswith("per rectum"):
        return "per rectum"
    slash_match = _ROUTE_SLASH_LABEL_RE.match(clean)
    if slash_match:
        return f"{slash_match.group(1).casefold()}/{slash_match.group(2).casefold()}"
    return clean


def operblock_route_label(route_code: Any, *, short: bool = False, routes: list[dict[str, str]] | None = None) -> str:
    code = normalize_operblock_route_code(route_code)
    source_routes = routes if routes is not None else load_operblock_administration_routes()
    for route in source_routes:
        if normalize_operblock_route_code((route or {}).get("code")) == code:
            label = _clean_text((route or {}).get("label"))
            return _shorten_route_label(code, label) if short else label
    return _shorten_route_label(code, code) if short else code


def operblock_route_from_comment(comment: Any) -> str | None:
    match = OPERBLOCK_ORDER_ROUTE_TAG_RE.search(str(comment or ""))
    if not match:
        return None
    encoded = str(match.group("route") or "").strip()
    if not encoded:
        return None
    return normalize_operblock_route_code(unquote(encoded))


def strip_operblock_route_tag(comment: Any) -> str:
    return re.sub(r"\s+", " ", OPERBLOCK_ORDER_ROUTE_TAG_RE.sub("", str(comment or ""))).strip()


def operblock_comment_with_route(comment: Any, route_code: Any) -> str:
    clean_comment = strip_operblock_route_tag(comment)
    code = normalize_operblock_route_code(route_code)
    if is_operblock_default_route(code):
        return clean_comment
    encoded = quote(code, safe="")
    return f"{clean_comment} [OB_ROUTE:{encoded}]".strip()


def _effective_default_route(routes: list[dict[str, str]]) -> str:
    codes = [normalize_operblock_route_code((route or {}).get("code")) for route in routes or []]
    if OPERBLOCK_DEFAULT_ROUTE_CODE in codes:
        return OPERBLOCK_DEFAULT_ROUTE_CODE
    return next((code for code in codes if code), OPERBLOCK_DEFAULT_ROUTE_CODE)


def normalize_operblock_group_routes_payload(
    payload: Any,
    *,
    groups: list[dict[str, str]] | None = None,
    routes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    source = payload if isinstance(payload, Mapping) else {}
    group_options = groups if groups is not None else load_operblock_drug_groups()
    route_options = routes if routes is not None else load_operblock_administration_routes()
    route_codes = {normalize_operblock_route_code((route or {}).get("code")) for route in route_options}
    default_route = _effective_default_route(route_options)
    raw_matrix = source.get("routes_by_group") or source.get("matrix") or {}
    if not isinstance(raw_matrix, Mapping):
        raw_matrix = {}

    matrix: dict[str, list[str]] = {}
    for group in group_options:
        group_code = _clean_text((group or {}).get("code"))
        if not group_code:
            continue
        raw_routes = raw_matrix.get(group_code)
        if raw_routes is None:
            raw_routes = [default_route]
        if not isinstance(raw_routes, list):
            raw_routes = []
        selected: list[str] = []
        for raw_route in raw_routes:
            route_code = normalize_operblock_route_code(raw_route)
            if route_code in route_codes and route_code not in selected:
                selected.append(route_code)
        if not selected and default_route in route_codes:
            selected.append(default_route)
        matrix[group_code] = selected

    return {"version": OPERBLOCK_GROUP_ROUTES_VERSION, "routes_by_group": matrix}


def load_operblock_group_route_settings() -> dict[str, Any]:
    payload = get_settings_service().get_app_setting(
        OPERBLOCK_ROUTE_SETTINGS_SCOPE,
        OPERBLOCK_GROUP_ROUTES_KEY,
        default={},
    )
    return normalize_operblock_group_routes_payload(payload)


def save_operblock_group_route_settings(routes_by_group: Mapping[str, Any]) -> dict[str, Any]:
    payload = normalize_operblock_group_routes_payload({"routes_by_group": dict(routes_by_group or {})})
    get_settings_service().set_app_setting(
        OPERBLOCK_ROUTE_SETTINGS_SCOPE,
        OPERBLOCK_GROUP_ROUTES_KEY,
        payload,
        catalog_key=OPERBLOCK_SETTINGS_KEY,
        entity_type="operblock_group_routes",
        operation="update",
        changed_by_role="doctor",
    )
    return payload


def operblock_routes_for_drug_group(group_code: Any) -> list[dict[str, str]]:
    code = _clean_text(group_code)
    routes = load_operblock_administration_routes()
    route_by_code = {normalize_operblock_route_code(route.get("code")): dict(route) for route in routes}
    settings = load_operblock_group_route_settings()
    selected_codes = list((settings.get("routes_by_group") or {}).get(code) or [])
    if not selected_codes:
        selected_codes = [_effective_default_route(routes)]
    result: list[dict[str, str]] = []
    for selected_code in selected_codes:
        normalized = normalize_operblock_route_code(selected_code)
        route = route_by_code.get(normalized)
        if route and normalized not in {item["code"] for item in result}:
            result.append({"code": normalized, "label": _clean_text(route.get("label")) or normalized})
    if not result:
        default_code = _effective_default_route(routes)
        result.append({"code": default_code, "label": operblock_route_label(default_code, short=False, routes=routes)})
    return result


def operblock_default_route_for_drug_group(group_code: Any) -> str:
    return _effective_default_route(operblock_routes_for_drug_group(group_code))
