import re
from datetime import datetime
from rem_card.data.dto.remcard_dto import OrderDTO, OrderType, ScheduleEngine
from rem_card.services.prescription_engine import engine

class OrderInputHandler:
    """Логика парсинга текстового ввода назначений в объекты OrderDTO."""
    
    @staticmethod
    def parse_input_to_dto(text: str, admission_id: int) -> OrderDTO:
        engine.reload_if_changed(force_check=True)
        explicit_key_match = re.search(r'\[KEY:(.*?)\]', text)
        multicomp = OrderInputHandler._parse_multicomp(text, admission_id, explicit_key_match)
        if multicomp is not None:
            return multicomp

        drug_part, diluent_part = OrderInputHandler._split_drug_and_diluent(text)
        clean_drug_part, found_prefix = OrderInputHandler._strip_latin_prefix(drug_part)
        search_term = OrderInputHandler._extract_search_term(clean_drug_part)

        if OrderInputHandler._is_manual_input(text):
            return OrderInputHandler._parse_manual_input(
                text,
                admission_id,
                explicit_key_match,
            )

        return OrderInputHandler._parse_standard_input(
            text,
            admission_id,
            explicit_key_match,
            drug_part,
            diluent_part,
            search_term,
            found_prefix,
        )

    @staticmethod
    def _parse_multicomp(text: str, admission_id: int, explicit_key_match) -> OrderDTO | None:
        if not explicit_key_match:
            return None

        drug_key = explicit_key_match.group(1)
        drug_data = engine.drugs.get(drug_key, {})
        if not drug_data.get("is_multicomp"):
            return None

        text = text.replace(explicit_key_match.group(0), "").strip()
        text, diluent_part = OrderInputHandler._extract_diluent_tag(text)
        text, route_from_tag = OrderInputHandler._extract_route_value(text)
        text, duration = OrderInputHandler._extract_duration_value(text)
        text = text.replace("[RU]", "").strip()

        admin_type = drug_data.get("admin_type", "bolus")
        otype = OrderInputHandler._order_type_for_admin(admin_type, text)
        freq = OrderInputHandler._extract_frequency(text)
        diluent_info = OrderInputHandler._build_multicomp_comment(
            diluent_part,
            route_from_tag,
            duration,
        )

        return OrderDTO(
            admission_id=admission_id,
            drug_key=drug_key,
            latin=text,
            type=otype,
            dose_value=0.0,
            dose_unit="",
            is_per_kg=False,
            frequency=freq,
            specific_times=ScheduleEngine.generate_times(freq),
            duration_min=duration,
            is_committed=0,
            created_at=datetime.now(),
            comment=diluent_info.strip()
        )

    @staticmethod
    def _extract_diluent_tag(text: str) -> tuple[str, str]:
        diluent_tag_match = re.search(r'\[DIL:(.*?)\]', text)
        if not diluent_tag_match:
            return text, ""
        diluent_part = diluent_tag_match.group(1).strip()
        text = text.replace(diluent_tag_match.group(0), "").strip()
        return text, diluent_part

    @staticmethod
    def _extract_route_value(text: str) -> tuple[str, str]:
        route_match = re.search(r'\[ROUTE:(.*?)\]', text)
        if not route_match:
            return text, ""
        route_from_tag = route_match.group(1).strip()
        text = text.replace(route_match.group(0), "").strip()
        return text, route_from_tag

    @staticmethod
    def _extract_duration_value(text: str) -> tuple[str, int]:
        dur_match = re.search(r'\[DUR:(.*?)\]', text)
        duration = 0
        if not dur_match:
            return text, duration
        try:
            duration = int(dur_match.group(1))
        except ValueError:
            pass
        text = text.replace(dur_match.group(0), "").strip()
        return text, duration

    @staticmethod
    def _build_multicomp_comment(diluent_part: str, route_from_tag: str, duration: int) -> str:
        diluent_info = diluent_part if diluent_part else ""
        if route_from_tag:
            diluent_info += f" [ROUTE:{route_from_tag}]"
        if duration != 0:
            diluent_info += f" [DUR:{duration}]"
        return diluent_info

    @staticmethod
    def _split_drug_and_diluent(text: str) -> tuple[str, str]:
        if text.count("+") > 1:
            last_plus_idx = text.rfind("+")
            drug_part = text[:last_plus_idx].strip()
            diluent_part = text[last_plus_idx+1:].strip()
        else:
            parts = text.split("+")
            drug_part = parts[0].strip()
            diluent_part = parts[1].strip() if len(parts) > 1 else ""
        return drug_part, diluent_part

    @staticmethod
    def _strip_latin_prefix(drug_part: str) -> tuple[str, str]:
        clean_drug_part = drug_part
        found_prefix = ""
        prefix_match = re.match(r'^([A-Za-z]+\.)\s+', drug_part)
        if prefix_match:
            found_prefix = prefix_match.group(1)
            clean_drug_part = drug_part[prefix_match.end():]
        return clean_drug_part, found_prefix

    @staticmethod
    def _extract_search_term(clean_drug_part: str) -> str:
        dose_match = re.search(r"(\d+\.?\d*)", clean_drug_part)
        if dose_match:
            search_term = clean_drug_part[:dose_match.start()].strip()
        else:
            search_term = clean_drug_part.strip()

        if not search_term:
            search_term = clean_drug_part.split()[0] if clean_drug_part else ""
        return search_term

    @staticmethod
    def _is_manual_input(text: str) -> bool:
        text_lower = text.lower()
        return "ruki" in text_lower or "ruchnoivvod" in text_lower or "[ru]" in text_lower

    @staticmethod
    def _parse_manual_input(text: str, admission_id: int, explicit_key_match) -> OrderDTO:
        display_latin = text.replace("[RU]", "").replace("[ru]", "").strip()
        resolved_drug_key = None
        resolved_drug_data = {}

        if explicit_key_match:
            explicit_key = explicit_key_match.group(1).strip().lower()
            if explicit_key:
                resolved_drug_key = explicit_key
                resolved_drug_data = engine.drugs.get(explicit_key, {}) or {}
            display_latin = display_latin.replace(explicit_key_match.group(0), "").strip()

        display_latin, dose_val = OrderInputHandler._extract_manual_dose(display_latin)
        display_latin, dose_unit = OrderInputHandler._extract_manual_unit(display_latin)
        display_latin, comment_parts = OrderInputHandler._extract_manual_comment_parts(
            display_latin,
            resolved_drug_data,
        )
        duration_min = OrderInputHandler._manual_duration(comment_parts, resolved_drug_data)
        display_latin, comment_parts = OrderInputHandler._split_manual_diluent(display_latin, comment_parts)
        display_latin = OrderInputHandler._normalize_manual_latin(display_latin)
        resolved_drug_key = OrderInputHandler._resolve_manual_blood_plasma_key(
            resolved_drug_key,
            display_latin,
        )

        freq = OrderInputHandler._extract_frequency(text)
        admin_type = str(resolved_drug_data.get("admin_type", "") or "").lower()
        route_hint = " ".join(comment_parts).lower()
        otype = OrderType.MEDICATION
        if duration_min != 0 or admin_type in ("infusion", "continuous") or "инф" in route_hint:
            otype = OrderType.INFUSION_CONTINUOUS

        return OrderDTO(
            admission_id=admission_id,
            drug_key=resolved_drug_key or "ruchnoivvod",
            latin=display_latin,
            type=otype,
            dose_value=dose_val,
            dose_unit=dose_unit,
            frequency=freq,
            specific_times=ScheduleEngine.generate_times(freq) if resolved_drug_key else [],
            duration_min=duration_min,
            is_committed=0,
            created_at=datetime.now(),
            comment=" ".join(comment_parts)
        )

    @staticmethod
    def _extract_manual_dose(display_latin: str) -> tuple[str, float]:
        dose_val = 0.0
        dose_match = re.search(r'\[DOSE:(.*?)\]', display_latin)
        if not dose_match:
            return display_latin, dose_val
        try:
            dose_val = float(dose_match.group(1))
        except Exception:
            pass
        display_latin = display_latin.replace(dose_match.group(0), "").strip()
        return display_latin, dose_val

    @staticmethod
    def _extract_manual_unit(display_latin: str) -> tuple[str, str]:
        dose_unit = ""
        unit_match = re.search(r'\[UNIT:(.*?)\]', display_latin)
        if not unit_match:
            return display_latin, dose_unit
        dose_unit = unit_match.group(1)
        display_latin = display_latin.replace(unit_match.group(0), "").strip()
        return display_latin, dose_unit

    @staticmethod
    def _extract_manual_comment_parts(display_latin: str, resolved_drug_data: dict) -> tuple[str, list[str]]:
        comment_parts = []

        route_match = re.search(r'(\[ROUTE:.*?\])', display_latin)
        if route_match:
            comment_parts.append(route_match.group(1))
            display_latin = display_latin.replace(route_match.group(1), "").strip()

        dur_match = re.search(r'(\[DUR:(.*?)\])', display_latin)
        if dur_match:
            comment_parts.append(dur_match.group(1))
            display_latin = display_latin.replace(dur_match.group(1), "").strip()
        elif resolved_drug_data:
            # Keep the old branch shape: default duration is applied later.
            pass

        return display_latin, comment_parts

    @staticmethod
    def _manual_duration(comment_parts: list[str], resolved_drug_data: dict) -> int:
        duration_min = 0
        dur_text = next((part for part in comment_parts if part.startswith("[DUR:")), None)
        if dur_text:
            dur_match = re.search(r'\[DUR:(.*?)\]', dur_text)
            if dur_match:
                try:
                    duration_min = int(dur_match.group(1))
                except Exception:
                    pass
        elif resolved_drug_data:
            try:
                duration_min = int(resolved_drug_data.get("duration_min", 0) or 0)
            except Exception:
                duration_min = 0
        return duration_min

    @staticmethod
    def _split_manual_diluent(display_latin: str, comment_parts: list[str]) -> tuple[str, list[str]]:
        if "+" not in display_latin:
            return display_latin, comment_parts
        parts = display_latin.split("+", 1)
        display_latin = parts[0].strip()
        diluent = parts[1].strip()
        if diluent:
            comment_parts.insert(0, diluent)
        return display_latin, comment_parts

    @staticmethod
    def _normalize_manual_latin(display_latin: str) -> str:
        if display_latin.lower() == "ruki" or display_latin.lower() == "ruchnoivvod":
            display_latin = "Ручной ввод"
        return " ".join(display_latin.split())

    @staticmethod
    def _resolve_manual_blood_plasma_key(resolved_drug_key, display_latin: str):
        if resolved_drug_key or not display_latin:
            return resolved_drug_key

        normalized = re.sub(r"[^0-9a-zа-яё]+", "", display_latin.lower())
        if any(token in normalized for token in ("сзп", "плазм", "plasma", "ffp")):
            return "plasma"
        if any(token in normalized for token in ("эрмас", "эритро", "кров", "blood", "rbc")):
            return "blood"

        cleaned_search = re.sub(r"[^0-9a-zа-яё]+", " ", display_latin.lower()).strip()
        if cleaned_search:
            found_key, _ = engine.detect_drug(cleaned_search)
            if found_key in ("blood", "plasma"):
                return found_key
        return resolved_drug_key

    @staticmethod
    def _parse_standard_input(
        text: str,
        admission_id: int,
        explicit_key_match,
        drug_part: str,
        diluent_part: str,
        search_term: str,
        found_prefix: str,
    ) -> OrderDTO:
        drug_data = None

        if explicit_key_match:
            drug_key = explicit_key_match.group(1)
            drug_data = engine.drugs.get(drug_key)
            if diluent_part:
                diluent_part = diluent_part.replace(explicit_key_match.group(0), "").strip()
        else:
            drug_key = search_term.lower()
            for k, v in engine.drugs.items():
                if v.get("latin", "").lower() == search_term.lower():
                    drug_key = k
                    drug_data = v
                    break
                    
            if not drug_data:
                drug_key, drug_data = engine.detect_drug(search_term)

        dose = engine.parse_dose(drug_part)
        if dose is None:
            dose = drug_data.get("default_dose", 1.0) if drug_data else 1.0

        freq = OrderInputHandler._extract_frequency(text)
        duration, diluent_part = OrderInputHandler._standard_duration_and_diluent(
            text,
            diluent_part,
            drug_data,
        )
        diluent_part = OrderInputHandler._clean_standard_diluent(diluent_part)
        latin_name, unit, admin_type = OrderInputHandler._standard_drug_display(
            drug_data,
            drug_key,
            search_term,
            found_prefix,
            dose,
        )
        otype = OrderInputHandler._order_type_for_admin(admin_type, text)

        return OrderDTO(
            admission_id=admission_id,
            drug_key=drug_key,
            latin=latin_name,
            type=otype,
            dose_value=dose,
            dose_unit=unit,
            is_per_kg=("/кг" in text or "мкг" in text.lower()),
            frequency=freq,
            specific_times=ScheduleEngine.generate_times(freq),
            duration_min=duration,
            is_committed=0,
            created_at=datetime.now(),
            comment=diluent_part
        )

    @staticmethod
    def _extract_frequency(text: str) -> int:
        freq_match = re.search(r"(\d+)р", text)
        return int(freq_match.group(1)) if freq_match else 1

    @staticmethod
    def _standard_duration_and_diluent(text: str, diluent_part: str, drug_data) -> tuple[int, str]:
        duration = 0
        dur_match = re.search(r'\[DUR:(.*?)\]', text)
        if dur_match:
            try:
                duration = int(dur_match.group(1))
            except ValueError:
                pass
            if diluent_part:
                diluent_part = diluent_part.replace(dur_match.group(0), "").strip()
        elif "до конца суток" in text.lower():
            duration = -1
        else:
            duration_match = re.search(r"(\d+)\s*мин", text.lower())
            duration = int(duration_match.group(1)) if duration_match else (drug_data.get("duration_min", 0) if drug_data else 0)
        return duration, diluent_part

    @staticmethod
    def _clean_standard_diluent(diluent_part: str) -> str:
        if not diluent_part:
            return diluent_part
        diluent_part = re.sub(r'\[ROUTE:.*?\]', '', diluent_part).strip()
        diluent_part = re.sub(r'\[UNIT:.*?\]', '', diluent_part).strip()
        diluent_part = re.sub(r'\[DOSE:.*?\]', '', diluent_part).strip()
        return diluent_part

    @staticmethod
    def _standard_drug_display(drug_data, drug_key: str, search_term: str, found_prefix: str, dose: float):
        if drug_data:
            latin_name = drug_data.get("latin", "").capitalize() or drug_key.capitalize()
            unit = drug_data.get("unit", "mg")
            admin_type = drug_data.get("admin_type", "bolus")
            return latin_name, unit, admin_type

        latin_name = f"{found_prefix} {search_term.capitalize()}" if found_prefix else search_term.capitalize()
        unit = "g" if dose < 10 else "mg"
        return latin_name, unit, "bolus"

    @staticmethod
    def _order_type_for_admin(admin_type: str, text: str):
        otype = OrderType.MEDICATION
        if admin_type in ["infusion", "continuous"] or "инф" in text.lower():
            otype = OrderType.INFUSION_CONTINUOUS
        return otype
