import re
import time
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from rem_card.data.dto.remcard_dto import OrderDTO, AdministrationDTO, OrderStatus
from rem_card.app.logger import logger
from rem_card.services.prescription_engine import engine

class BalanceCalculator:
    """РљР»Р°СЃСЃ РґР»СЏ СЂР°СЃС‡РµС‚Р° Р±Р°Р»Р°РЅСЃР° РІРІРµРґРµРЅРЅС‹С… Р¶РёРґРєРѕСЃС‚РµР№ РЅР° РѕСЃРЅРѕРІРµ РЅР°Р·РЅР°С‡РµРЅРёР№."""

    _engine_reload_interval_sec = 10.0
    _engine_last_reload_mono = 0.0
    _engine_reload_lock = threading.Lock()

    @staticmethod
    def _normalize_unit_token(token: str) -> str:
        """
        Приводит единицы измерения к нормальному виду.
        Нужен для совместимости с историческими записями, где встречаются
        "битые" строки типа 'рјр»'/'р»' вместо 'мл'/'л'.
        """
        value = str(token or "").lower().strip()
        value = value.replace("\u0440\u0458\u0440\u00bb", "\u043c\u043b")  # рјр» -> мл
        value = value.replace("\u0440\u00bb", "\u043b")  # р» -> л
        return value

    @staticmethod
    def parse_volume(text: str) -> float:
        """РР·РІР»РµРєР°РµС‚ РѕР±СЉРµРј РІ РјР» РёР· СЃС‚СЂРѕРєРё (РЅР°РїСЂ. '250 ml', '0.5 Р»')."""
        if not text:
            return 0.0
        
        text = BalanceCalculator._normalize_unit_token(text).replace(",", ".")
        l_match = re.search(r"(\d+\.?\d*)\s*(l|\u043b|\u043b\u0438\u0442\u0440(?:\u0430|\u043e\u0432)?|litr)\b", text)
        if l_match:
            return float(l_match.group(1)) * 1000
            
        ml_match = re.search(r"(\d+\.?\d*)\s*(ml|\u043c\u043b)\b", text)
        if ml_match:
            return float(ml_match.group(1))
        
        return 0.0

    @staticmethod
    def _normalize_text_for_classification(text: str) -> str:
        value = BalanceCalculator._normalize_unit_token(str(text or "").lower())
        value = value.replace("ё", "е")
        value = re.sub(r"[^0-9a-zа-я]+", "", value)
        return value

    @classmethod
    def _detect_blood_or_plasma_category(cls, order: OrderDTO, d_key: str, drug_info: dict) -> Optional[str]:
        if d_key in ("blood", "plasma"):
            return d_key

        is_candidate = (
            d_key in ("ruchnoivvod", "ruki")
            or drug_info.get("group") == "comp_blood"
            or drug_info.get("form_key") == "comp_blood"
        )
        if not is_candidate:
            return None

        candidate_texts = [
            d_key,
            str(order.latin or ""),
            str(order.comment or ""),
            str(drug_info.get("latin", "")),
        ]

        for raw in candidate_texts:
            normalized = cls._normalize_text_for_classification(raw)
            if not normalized:
                continue

            if any(token in normalized for token in ("plasma", "сзп", "плазм", "ffp")):
                return "plasma"
            if any(token in normalized for token in ("blood", "эрмас", "эритро", "кров", "rbc")):
                return "blood"

            lookup_text = re.sub(r"[^0-9a-zа-яё]+", " ", str(raw or "").lower()).strip()
            if lookup_text:
                found_key, _ = engine.detect_drug(lookup_text)
                if found_key in ("blood", "plasma"):
                    return found_key

        return None

    @staticmethod
    def _calc_preparat_vol(order: OrderDTO, drug_info: dict) -> float:
        d_unit_lower = BalanceCalculator._normalize_unit_token(order.dose_unit)
        if d_unit_lower in ("ml", "\u043c\u043b", "l", "\u043b"):
            vol = order.dose_value
            if d_unit_lower in ("l", "\u043b"): vol *= 1000
            return vol
            
        form_key = drug_info.get("form_key", "")
        clean_latin = str(order.latin or "")
        prefix_match = re.match(r'^([A-Za-z]+\.)\s+', clean_latin)
        if prefix_match:
            prefix = prefix_match.group(1).strip()
            for f_k, f_v in engine.forms.items():
                if f_v.get("latin_abbr") == prefix:
                    form_key = f_k
                    break
        
        form_unit = BalanceCalculator._normalize_unit_token(drug_info.get("form_unit", drug_info.get("unit", "")))
        is_volume_form = form_unit in ("ml", "\u043c\u043b", "l", "\u043b")
        form_name_ru = engine.forms.get(form_key, {}).get("name_ru", "").lower()
        is_solution = (
            "\u0440\u0430\u0441\u0442\u0432\u043e\u0440" in form_name_ru
            or "СЂР°СЃС‚РІРѕСЂ" in form_name_ru
            or (form_key and form_key.startswith("solution"))
        )
        
        if is_volume_form and is_solution:
            forms = drug_info.get("forms", [])
            default_dose = drug_info.get("default_dose", 0)
            if forms and default_dose > 0:
                try:
                    form_vol = float(forms[0])
                    if form_unit in ("l", "\u043b"):
                        form_vol *= 1000
                    return (order.dose_value / float(default_dose)) * form_vol
                except (ValueError, TypeError):
                    pass
        return 0.0

    @staticmethod
    def get_order_volumes(order: OrderDTO) -> Tuple[float, float, str]:
        """РћРїСЂРµРґРµР»СЏРµС‚ РєР°С‚РµРіРѕСЂРёСЋ Рё РѕР±СЉРµРјС‹ РґР»СЏ РѕСЂРґРµСЂР° (РёРЅС„СѓР·РёСЏ, РїСЂРµРїР°СЂР°С‚, РєР°С‚РµРіРѕСЂРёСЏ)."""
        infusion_vol = 0.0
        preparat_vol = 0.0
        category = "preparat"

        d_key = str(order.drug_key or "").lower().strip()
        drug_info = engine.drugs.get(d_key, {})
        if not drug_info or "group" not in drug_info:
            clean_latin = str(order.latin or "")
            prefix_match = re.match(r'^([A-Za-z]+\.)\s+', clean_latin)
            if prefix_match: clean_latin = clean_latin[prefix_match.end():]
            found_key, found_data = engine.detect_drug(clean_latin)
            if found_data: drug_info = found_data
                
        group_key = drug_info.get("group")
        blood_or_plasma_category = BalanceCalculator._detect_blood_or_plasma_category(order, d_key, drug_info)

        inf_vol_from_comment = BalanceCalculator.parse_volume(order.comment)
        if inf_vol_from_comment > 0:
            infusion_vol = inf_vol_from_comment

        if drug_info.get("is_multicomp"):
            if infusion_vol == 0:
                infusion_vol = BalanceCalculator.parse_volume(order.comment)
            
            parts = str(order.latin or "").split("+")
            for part in parts:
                normalized_part = BalanceCalculator._normalize_unit_token(part)
                m = re.search(r'-\s*(\d+\.?\d*)\s*(ml|\u043c\u043b)\b', normalized_part)
                if m: preparat_vol += float(m.group(1))
            category = "infusion" if infusion_vol > 0 else "preparat"

        elif blood_or_plasma_category:
            category = blood_or_plasma_category
            preparat_vol = order.dose_value
            dose_unit = BalanceCalculator._normalize_unit_token(order.dose_unit)
            if dose_unit in ("l", "\u043b"):
                preparat_vol *= 1000
        elif group_key == "fluids":
            category = "infusion"
            vol = order.dose_value
            dose_unit = BalanceCalculator._normalize_unit_token(order.dose_unit)
            if dose_unit in ("l", "\u043b"): vol *= 1000
            infusion_vol = vol
        elif d_key in ("ruchnoivvod", "ruki"):
            preparat_vol = BalanceCalculator._calc_preparat_vol(order, drug_info)
            if infusion_vol > 0: category = "infusion"
        else:
            preparat_vol = BalanceCalculator._calc_preparat_vol(order, drug_info)
            if infusion_vol > 0: category = "infusion"
        
        return infusion_vol, preparat_vol, category

    @classmethod
    def _maybe_reload_engine(cls):
        now_mono = time.monotonic()
        if (now_mono - cls._engine_last_reload_mono) < cls._engine_reload_interval_sec:
            return
        with cls._engine_reload_lock:
            now_mono = time.monotonic()
            if (now_mono - cls._engine_last_reload_mono) < cls._engine_reload_interval_sec:
                return
            cls._engine_last_reload_mono = now_mono
            try:
                engine.reload_if_changed()
            except Exception:
                pass

    @classmethod
    def _resolve_terminal_limit(
        cls,
        end_of_card: datetime,
        transfer_time: Optional[datetime] = None,
        outcome_time: Optional[datetime] = None,
    ) -> datetime:
        daily_limit = end_of_card
        valid_transfer = transfer_time and transfer_time.year > 2020
        terminal_time = outcome_time if outcome_time else (transfer_time if valid_transfer else None)
        # Автоматическое введение останавливается ровно в момент исхода.
        # Часовая отсрочка после исхода относится только к ручному выведению жидкости.
        if terminal_time and terminal_time < daily_limit:
            daily_limit = terminal_time
        return daily_limit

    @classmethod
    def calculate(cls, orders: List[OrderDTO], current_time: datetime, end_of_card: datetime, 
                  transfer_time: Optional[datetime] = None, active_intervals: List[Tuple[datetime, datetime]] = None,
                  outcome_time: Optional[datetime] = None) -> Dict[str, Dict[str, float]]:
        """
        Р¤РРќРђР›Р¬РќР«Р™ РњР•РўРћР” Р РђРЎР§Р•РўРђ:
        1. Р’ Current (РЅР° С‚РµРєСѓС‰РёР№ С‡Р°СЃ) - С‚РѕР»СЊРєРѕ С„Р°РєС‚ (executed) РїСЂРѕРїРѕСЂС†РёРѕРЅР°Р»СЊРЅРѕ.
        2. Р’ Daily (РЅР° РєРѕРЅРµС† СЃСѓС‚РѕРє) - С„Р°РєС‚ + РїР»Р°РЅС‹ РЅР° Р±СѓРґСѓС‰РµРµ (РёРіРЅРѕСЂРёСЂСѓСЏ РїСЂРѕСЃСЂРѕС‡РµРЅРЅС‹Рµ).
        """
        res = {
            "current": {"infusion": 0.0, "preparats": 0.0, "blood": 0.0, "plasma": 0.0, "total": 0.0},
            "daily": {"infusion": 0.0, "preparats": 0.0, "blood": 0.0, "plasma": 0.0, "total": 0.0}
        }

        daily_limit = cls._resolve_terminal_limit(end_of_card, transfer_time, outcome_time)

        cls._maybe_reload_engine()

        for order in orders:
            if order.status in (OrderStatus.DELETED, OrderStatus.CANCELLED): continue
            
            inf_v, prep_v, cat = cls.get_order_volumes(order)
            total_v_order = inf_v + prep_v
            if total_v_order <= 0: continue

            chains = {} 
            singles = []
            for admin in order.administrations:
                if admin.status in ("deleted", "cancelled"): continue
                if admin.big_chain_id:
                    if admin.big_chain_id not in chains: chains[admin.big_chain_id] = []
                    chains[admin.big_chain_id].append(admin)
                else: singles.append(admin)

            # РћРїСЂРµРґРµР»СЏРµРј, СЏРІР»СЏРµС‚СЃСЏ Р»Рё Р·Р°РєР°Р· С‡РµСЂРЅРѕРІРёРєРѕРј
            is_draft = getattr(order, 'is_committed', 1) == 0

            # 1. Р РђРЎР§Р•Рў DAILY (РЎСѓС‚РѕС‡РЅС‹Р№ РїСЂРѕРіРЅРѕР·)
            # Р’РєР»СЋС‡Р°РµС‚: РІС‹РїРѕР»РЅРµРЅРЅС‹Рµ + Р±СѓРґСѓС‰РёРµ РїР»Р°РЅС‹. РСЃРєР»СЋС‡Р°РµС‚: РїСЂРѕСЃСЂРѕС‡РµРЅРЅС‹Рµ + РЅРµРІС‹РїРѕР»РЅРµРЅРЅС‹Рµ.
            for admin in singles:
                cls._process_item_daily(res, order, admin, inf_v, prep_v, total_v_order, cat, daily_limit, is_draft)
            
            for chain_id, admins in chains.items():
                admins.sort(key=lambda x: x.planned_time)
                cls._process_chain_daily(res, order, admins, inf_v, prep_v, total_v_order, cat, daily_limit, end_of_card, is_draft)

            # 2. Р РђРЎР§Р•Рў CURRENT (РќР° С‚РµРєСѓС‰РёР№ РјРѕРјРµРЅС‚)
            # РўРѕР»СЊРєРѕ РїРѕРґС‚РІРµСЂР¶РґРµРЅРЅС‹Р№ С„Р°РєС‚ (executed) РёР»Рё РЅРёС‡РµРіРѕ РµСЃР»Рё С‡РµСЂРЅРѕРІРёРє/РїР»Р°РЅ.
            if not is_draft:
                for admin in singles:
                    cls._process_item_current(res, order, admin, inf_v, prep_v, total_v_order, cat, current_time, daily_limit)
                
                for chain_id, admins in chains.items():
                    admins.sort(key=lambda x: x.planned_time)
                    cls._process_chain_current(res, order, admins, inf_v, prep_v, total_v_order, cat, current_time, daily_limit, end_of_card)

        for p in ("current", "daily"):
            for k in ["infusion", "preparats", "blood", "plasma"]: res[p][k] = round(res[p][k], 1)
            res[p]["total"] = round(sum(res[p][k] for k in ["infusion", "preparats", "blood", "plasma"]), 1)

        return res

    @classmethod
    def calculate_hourly_actual_input(
        cls,
        orders: List[OrderDTO],
        start_time: datetime,
        current_time: datetime,
        end_of_card: datetime,
        transfer_time: Optional[datetime] = None,
        outcome_time: Optional[datetime] = None,
    ) -> Dict[int, Dict[str, float]]:
        """Builds hourly actual input buckets for print reports."""
        hourly = {
            i: {"infusion": 0.0, "preparats": 0.0, "blood": 0.0, "plasma": 0.0}
            for i in range(24)
        }
        if current_time < start_time:
            return hourly

        daily_limit = cls._resolve_terminal_limit(end_of_card, transfer_time, outcome_time)

        period_end = min(current_time, daily_limit, start_time + timedelta(hours=24))
        if period_end <= start_time:
            return hourly

        cls._maybe_reload_engine()

        for order in orders or []:
            status_val = getattr(getattr(order, "status", None), "value", getattr(order, "status", None))
            if str(status_val) in ("deleted", "cancelled"):
                continue
            if getattr(order, "is_committed", 1) == 0:
                continue

            inf_v, prep_v, cat = cls.get_order_volumes(order)
            if (inf_v + prep_v) <= 0:
                continue

            chains = {}
            singles = []
            for admin in getattr(order, "administrations", []) or []:
                admin_status = str(getattr(admin, "status", "") or "")
                if admin_status in ("deleted", "cancelled"):
                    continue
                if getattr(admin, "big_chain_id", None):
                    chains.setdefault(admin.big_chain_id, []).append(admin)
                else:
                    singles.append(admin)

            for admin in singles:
                cls._add_single_actual_to_hourly(
                    hourly, start_time, period_end, daily_limit, order, admin, inf_v, prep_v, cat
                )

            for admins in chains.values():
                admins.sort(key=lambda item: item.planned_time)
                cls._add_chain_actual_to_hourly(
                    hourly, start_time, period_end, daily_limit, end_of_card, order, admins, inf_v, prep_v, cat
                )

        for bucket in hourly.values():
            for key in ("infusion", "preparats", "blood", "plasma"):
                bucket[key] = round(bucket[key], 1)
        return hourly

    @classmethod
    def _add_single_actual_to_hourly(cls, hourly, start_time, period_end, daily_limit, order, admin, inf_v, prep_v, cat):
        if cls._admin_mark(admin) != "nurse_executed":
            return

        planned_time = admin.planned_time
        if planned_time > period_end or planned_time >= daily_limit:
            return

        dur = float(getattr(order, "duration_min", 0) or 0)
        if dur <= 0:
            cls._add_point_to_hourly(hourly, start_time, planned_time, inf_v, prep_v, cat)
            return

        interval_end = planned_time + timedelta(minutes=dur)
        visible_end = min(interval_end, period_end, daily_limit)
        cls._add_interval_to_hourly(
            hourly,
            start_time,
            planned_time,
            interval_end,
            inf_v,
            prep_v,
            cat,
            visible_end=visible_end,
        )

    @classmethod
    def _add_chain_actual_to_hourly(cls, hourly, start_time, period_end, daily_limit, end_of_card, order, admins, inf_v, prep_v, cat):
        if not admins:
            return

        if cls._chain_has_segment_marks(admins):
            cls._add_segmented_chain_actual_to_hourly(
                hourly, start_time, period_end, daily_limit, end_of_card, order, admins, inf_v, prep_v, cat
            )
            return

        executed_times = [admin.planned_time for admin in admins if cls._admin_mark(admin) == "nurse_executed"]
        if not any(planned_time <= period_end for planned_time in executed_times):
            return

        start_of_chain = admins[0].planned_time
        dur_val = float(getattr(order, "duration_min", 0) or 0)
        if dur_val > 0:
            chain_end = start_of_chain + timedelta(minutes=dur_val)
        else:
            chain_end = end_of_card

        if chain_end <= start_of_chain:
            return

        visible_end = min(period_end, daily_limit, chain_end)
        cls._add_interval_to_hourly(
            hourly,
            start_time,
            start_of_chain,
            chain_end,
            inf_v,
            prep_v,
            cat,
            visible_end=visible_end,
        )

    @classmethod
    def _add_segmented_chain_actual_to_hourly(cls, hourly, start_time, period_end, daily_limit, end_of_card, order, admins, inf_v, prep_v, cat):
        start_of_chain, chain_end, planned_chain_end = cls._chain_bounds(order, admins, daily_limit, end_of_card)
        if chain_end <= start_of_chain or period_end <= start_of_chain:
            return

        effective_period_end = min(period_end, chain_end)
        stop_times = [
            admin.planned_time
            for admin in admins
            if cls._admin_mark(admin) == "nurse_not_executed"
            and start_of_chain <= admin.planned_time < chain_end
        ]
        stop_time = min(stop_times) if stop_times else None

        has_executed_fact = any(
            cls._admin_mark(admin) == "nurse_executed"
            and getattr(admin, "cell_role", "") in ("start", "single", "body")
            and admin.planned_time <= effective_period_end
            for admin in admins
        )
        has_stop_fact = bool(stop_time and start_of_chain < stop_time <= effective_period_end)
        if not has_executed_fact and not has_stop_fact:
            return
        if stop_time is not None and stop_time <= start_of_chain:
            return

        replacement_times = sorted(
            admin.planned_time
            for admin in admins
            if cls._admin_mark(admin) == "nurse_executed"
            and getattr(admin, "cell_role", "") == "body"
            and start_of_chain < admin.planned_time < chain_end
        )

        segment_start = start_of_chain
        boundary_limit = min(effective_period_end, stop_time) if stop_time is not None else effective_period_end
        for replacement_time in replacement_times:
            if replacement_time > boundary_limit:
                break
            if replacement_time <= segment_start:
                continue
            cls._add_interval_to_hourly(hourly, start_time, segment_start, replacement_time, inf_v, prep_v, cat)
            segment_start = replacement_time

        active_end = stop_time if stop_time is not None and stop_time <= effective_period_end else effective_period_end
        cls._add_segment_partial_to_hourly(
            hourly, start_time, segment_start, active_end, planned_chain_end, inf_v, prep_v, cat
        )

    @classmethod
    def _add_segment_partial_to_hourly(cls, hourly, start_time, segment_start, active_end, segment_end, inf_v, prep_v, cat):
        if active_end <= segment_start or segment_end <= segment_start:
            return
        active_end = min(active_end, segment_end)
        active_min = max(0.0, (active_end - segment_start).total_seconds() / 60.0)
        segment_min = max(1.0, (segment_end - segment_start).total_seconds() / 60.0)
        ratio = max(0.0, min(1.0, active_min / segment_min))
        if ratio <= 0:
            return
        cls._add_interval_to_hourly(
            hourly,
            start_time,
            segment_start,
            active_end,
            inf_v * ratio,
            prep_v * ratio,
            cat,
        )

    @classmethod
    def _add_point_to_hourly(cls, hourly, start_time, moment, inf_v, prep_v, cat):
        idx = int((moment - start_time).total_seconds() // 3600)
        if 0 <= idx < 24:
            cls._add_vol_to_hourly_bucket(hourly[idx], inf_v, prep_v, cat)

    @classmethod
    def _add_interval_to_hourly(
        cls,
        hourly,
        start_time,
        interval_start,
        interval_end,
        inf_v,
        prep_v,
        cat,
        *,
        visible_end=None,
    ):
        if interval_end <= interval_start:
            return

        report_start = start_time
        report_end = start_time + timedelta(hours=24)
        visible_start = max(interval_start, report_start)
        visible_end = min(visible_end or interval_end, interval_end, report_end)
        if visible_end <= visible_start:
            return

        total_seconds = max(1.0, (interval_end - interval_start).total_seconds())
        cursor = visible_start
        while cursor < visible_end:
            idx = int((cursor - start_time).total_seconds() // 3600)
            if idx < 0:
                cursor = start_time
                continue
            if idx >= 24:
                break
            bucket_end = min(visible_end, start_time + timedelta(hours=idx + 1))
            part_seconds = max(0.0, (bucket_end - cursor).total_seconds())
            if part_seconds > 0:
                ratio = part_seconds / total_seconds
                cls._add_vol_to_hourly_bucket(hourly[idx], inf_v * ratio, prep_v * ratio, cat)
            cursor = bucket_end

    @staticmethod
    def _add_vol_to_hourly_bucket(bucket, inf_v, prep_v, cat):
        if cat in ("blood", "plasma"):
            bucket[cat] += (inf_v + prep_v)
        else:
            bucket["infusion"] += inf_v
            bucket["preparats"] += prep_v

    @classmethod
    def _process_item_daily(cls, res, order, admin, inf_v, prep_v, total_v, cat, daily_limit, is_draft):
        """РЎС‡РёС‚Р°РµС‚ РѕР±СЉРµРј РѕРґРёРЅРѕС‡РЅРѕРіРѕ РІРІРµРґРµРЅРёСЏ РІ СЃСѓС‚РѕС‡РЅС‹Р№ Р±Р°Р»Р°РЅСЃ."""
        mark = getattr(admin, 'comment', '') or ''
        
        if not is_draft and mark == "nurse_not_executed":
            return

        if admin.planned_time >= daily_limit:
            return

        dur = float(order.duration_min or 0)
        if dur <= 0:
            cls._add_vol_to_res(res, "daily", inf_v, prep_v, cat)
        else:
            # РћР±СЂРµР·Р°РЅРёРµ РїРѕ 08:00
            end_t = admin.planned_time + timedelta(minutes=dur)
            effective_end = min(end_t, daily_limit)
            active_min = max(0.0, (effective_end - admin.planned_time).total_seconds() / 60.0)
            ratio = active_min / dur if dur > 0 else 1.0
            cls._add_vol_to_res(res, "daily", inf_v * ratio, prep_v * ratio, cat)

    @classmethod
    def _process_chain_daily(cls, res, order, admins, inf_v, prep_v, total_v, cat, daily_limit, end_of_card, is_draft):
        """Считает объем цепочки в суточный баланс."""
        if not admins: return

        if is_draft or not cls._chain_has_segment_marks(admins):
            cls._process_chain_linear_daily(res, order, admins, inf_v, prep_v, total_v, cat, daily_limit, end_of_card, is_draft)
            return

        cls._process_chain_segmented(
            res=res,
            period="daily",
            order=order,
            admins=admins,
            inf_v=inf_v,
            prep_v=prep_v,
            cat=cat,
            period_end=daily_limit,
            daily_limit=daily_limit,
            end_of_card=end_of_card,
            require_fact=False,
        )

    @classmethod
    def _process_chain_linear_daily(cls, res, order, admins, inf_v, prep_v, total_v, cat, daily_limit, end_of_card, is_draft):
        """Считает цепочку по исходной равномерной скорости без смен шприца."""
        if not admins: return

        start_of_chain = admins[0].planned_time
        dur_val = float(order.duration_min or 0)
        if dur_val > 0:
            total_dur_min = dur_val
            chain_end = min(start_of_chain + timedelta(minutes=dur_val), daily_limit)
        else:
            total_dur_min = (end_of_card - start_of_chain).total_seconds() / 60.0
            chain_end = min(end_of_card, daily_limit)

        if total_dur_min <= 0: total_dur_min = 1.0

        speed_inf = inf_v / total_dur_min
        speed_prep = prep_v / total_dur_min

        for admin in admins:
            mark = getattr(admin, 'comment', '') or ''
            if not is_draft and mark == "nurse_not_executed":
                continue

            q_start = admin.planned_time
            if q_start >= daily_limit or q_start >= chain_end:
                continue

            q_dur = 60.0
            eff_q_end = min(q_start + timedelta(minutes=q_dur), daily_limit, chain_end)
            eff_q_min = max(0.0, (eff_q_end - q_start).total_seconds() / 60.0)

            cls._add_vol_to_res(res, "daily", speed_inf * eff_q_min, speed_prep * eff_q_min, cat)

    @classmethod
    def _process_item_current(cls, res, order, admin, inf_v, prep_v, total_v, cat, current_time, daily_limit):
        """РЎС‡РёС‚Р°РµС‚ РѕР±СЉРµРј РѕРґРёРЅРѕС‡РЅРѕРіРѕ РІРІРµРґРµРЅРёСЏ РІ С‚РµРєСѓС‰РёР№ Р±Р°Р»Р°РЅСЃ."""
        mark = getattr(admin, 'comment', '') or ''

        # Р’ Current РїРѕРїР°РґР°РµС‚ РўРћР›Р¬РљРћ РІС‹РїРѕР»РЅРµРЅРЅРѕРµ
        if mark != "nurse_executed":
            return

        dur = float(order.duration_min or 0)
        start_t = admin.planned_time

        if dur <= 0:
            # Р’С‹РїРѕР»РЅРµРЅРЅС‹Р№ Р±РѕР»СЋСЃ
            cls._add_vol_to_res(res, "current", inf_v, prep_v, cat)
        else:
            # Р’С‹РїРѕР»РЅРµРЅРЅР°СЏ РёРЅС„СѓР·РёСЏ (РїСЂРѕРїРѕСЂС†РёРѕРЅР°Р»СЊРЅРѕ РґРѕ РјРѕРјРµРЅС‚Р° СЃРµР№С‡Р°СЃ РёР»Рё РєРѕРЅС†Р° РёРЅС„СѓР·РёРё)
            end_t = start_t + timedelta(minutes=dur)
            if start_t <= current_time:
                effective_end = min(end_t, current_time, daily_limit)
                active_min = max(0.0, (effective_end - start_t).total_seconds() / 60.0)
                ratio = active_min / dur
                cls._add_vol_to_res(res, "current", inf_v * ratio, prep_v * ratio, cat)

    @classmethod
    def _process_chain_current(cls, res, order, admins, inf_v, prep_v, total_v, cat, current_time, daily_limit, end_of_card):
        """Считает объем цепочки в текущий баланс."""
        if not admins: return

        if cls._chain_has_segment_marks(admins):
            cls._process_chain_segmented(
                res=res,
                period="current",
                order=order,
                admins=admins,
                inf_v=inf_v,
                prep_v=prep_v,
                cat=cat,
                period_end=min(current_time, daily_limit),
                daily_limit=daily_limit,
                end_of_card=end_of_card,
                require_fact=True,
            )
            return

        cls._process_chain_linear_current(res, order, admins, inf_v, prep_v, total_v, cat, current_time, daily_limit, end_of_card)

    @classmethod
    def _process_chain_linear_current(cls, res, order, admins, inf_v, prep_v, total_v, cat, current_time, daily_limit, end_of_card):
        """Считает текущий баланс цепочки по исходной равномерной скорости."""
        if not admins: return

        start_of_chain = admins[0].planned_time
        dur_val = float(order.duration_min or 0)
        if dur_val > 0:
            total_dur_min = dur_val
            chain_end = min(start_of_chain + timedelta(minutes=dur_val), daily_limit)
        else:
            total_dur_min = (end_of_card - start_of_chain).total_seconds() / 60.0
            chain_end = min(end_of_card, daily_limit)

        if total_dur_min <= 0: total_dur_min = 1.0

        speed_inf = inf_v / total_dur_min
        speed_prep = prep_v / total_dur_min

        # В интерфейсе медсестры для цепочек обычно отмечается только start/single.
        # Если требовать nurse_executed у каждой ячейки (body/end), current занижается.
        has_chain_executed = any(
            (getattr(a, 'comment', '') or '') == "nurse_executed"
            for a in admins
        )
        if not has_chain_executed:
            return

        for admin in admins:
            mark = getattr(admin, 'comment', '') or ''
            if mark == "nurse_not_executed":
                continue

            q_start = admin.planned_time
            if q_start > current_time or q_start >= daily_limit or q_start >= chain_end:
                continue

            q_dur = 60.0
            eff_q_end = min(q_start + timedelta(minutes=q_dur), current_time, daily_limit, chain_end)
            eff_q_min = max(0.0, (eff_q_end - q_start).total_seconds() / 60.0)

            cls._add_vol_to_res(res, "current", speed_inf * eff_q_min, speed_prep * eff_q_min, cat)

    @staticmethod
    def _admin_mark(admin) -> str:
        return getattr(admin, 'comment', '') or ''

    @classmethod
    def _chain_has_segment_marks(cls, admins) -> bool:
        for admin in admins:
            mark = cls._admin_mark(admin)
            if mark == "nurse_not_executed":
                return True
            if mark == "nurse_executed" and getattr(admin, "cell_role", "") == "body":
                return True
        return False

    @classmethod
    def _chain_bounds(cls, order, admins, daily_limit, end_of_card=None):
        start_of_chain = admins[0].planned_time
        dur_val = float(order.duration_min or 0)
        if dur_val > 0:
            planned_end = start_of_chain + timedelta(minutes=dur_val)
        else:
            planned_end = end_of_card or daily_limit

        last_slot_end = admins[-1].planned_time + timedelta(hours=1)
        planned_chain_end = min(planned_end, last_slot_end)
        visible_end = min(planned_chain_end, daily_limit)
        if visible_end <= start_of_chain:
            visible_end = start_of_chain
        if planned_chain_end <= start_of_chain:
            planned_chain_end = start_of_chain
        return start_of_chain, visible_end, planned_chain_end

    @classmethod
    def _add_segment_ratio(cls, res, period, inf_v, prep_v, cat, segment_start, active_end, segment_end):
        if active_end <= segment_start or segment_end <= segment_start:
            return

        active_end = min(active_end, segment_end)
        active_min = max(0.0, (active_end - segment_start).total_seconds() / 60.0)
        segment_min = max(1.0, (segment_end - segment_start).total_seconds() / 60.0)
        ratio = max(0.0, min(1.0, active_min / segment_min))
        if ratio <= 0:
            return
        cls._add_vol_to_res(res, period, inf_v * ratio, prep_v * ratio, cat)

    @classmethod
    def _process_chain_segmented(
        cls,
        *,
        res,
        period: str,
        order,
        admins,
        inf_v,
        prep_v,
        cat,
        period_end,
        daily_limit,
        end_of_card,
        require_fact: bool,
    ):
        """
        Считает длительную инфузию с отметками медсестры на теле цепочки:
        body + executed = смена шприца, not_executed = остановка с этого часа.
        """
        if not admins:
            return

        start_of_chain, chain_end, planned_chain_end = cls._chain_bounds(order, admins, daily_limit, end_of_card)
        if chain_end <= start_of_chain or period_end <= start_of_chain:
            return

        effective_period_end = min(period_end, chain_end)
        stop_times = [
            admin.planned_time
            for admin in admins
            if cls._admin_mark(admin) == "nurse_not_executed"
            and start_of_chain <= admin.planned_time < chain_end
        ]
        stop_time = min(stop_times) if stop_times else None

        if require_fact:
            has_executed_fact = any(
                cls._admin_mark(admin) == "nurse_executed"
                and getattr(admin, "cell_role", "") in ("start", "single", "body")
                and admin.planned_time <= effective_period_end
                for admin in admins
            )
            has_stop_fact = bool(stop_time and start_of_chain < stop_time <= effective_period_end)
            if not has_executed_fact and not has_stop_fact:
                return

        if stop_time is not None and stop_time <= start_of_chain:
            return

        replacement_times = sorted(
            admin.planned_time
            for admin in admins
            if cls._admin_mark(admin) == "nurse_executed"
            and getattr(admin, "cell_role", "") == "body"
            and start_of_chain < admin.planned_time < chain_end
        )

        segment_start = start_of_chain
        boundary_limit = effective_period_end
        if stop_time is not None:
            boundary_limit = min(boundary_limit, stop_time)

        for replacement_time in replacement_times:
            if replacement_time > boundary_limit:
                break
            if replacement_time <= segment_start:
                continue

            # Отметка "выполнено" на теле означает: предыдущий шприц закончился
            # полностью к началу выбранного часа, дальше стартует новый такой же.
            cls._add_vol_to_res(res, period, inf_v, prep_v, cat)
            segment_start = replacement_time

        if stop_time is not None and stop_time <= effective_period_end:
            cls._add_segment_ratio(
                res,
                period,
                inf_v,
                prep_v,
                cat,
                segment_start,
                stop_time,
                planned_chain_end,
            )
            return

        cls._add_segment_ratio(
            res,
            period,
            inf_v,
            prep_v,
            cat,
            segment_start,
            effective_period_end,
            planned_chain_end,
        )

    @staticmethod
    def _add_vol_to_res(res, period, inf_v, prep_v, cat):
        """Р”РѕР±Р°РІР»СЏРµС‚ РѕР±СЉРµРјС‹ РІ СЃС‚СЂСѓРєС‚СѓСЂСѓ СЂРµР·СѓР»СЊС‚Р°С‚Р°."""
        target = res[period]
        if cat in ("blood", "plasma"):
            target[cat] += (inf_v + prep_v)
        else:
            target["infusion"] += inf_v
            target["preparats"] += prep_v

