import re
import threading
from typing import Dict, Any, Tuple, List, Optional
from datetime import datetime, timedelta

from rem_card.services.settings.settings_service import (
    DRUG_CATALOG_KEY,
    ORDER_TEMPLATES_KEY,
    get_settings_service,
)

TEMPLATE_ORDER_KEY = "template_order"


class PrescriptionEngine:
    _DATASETS = (
        ("drugs", "drugs"),
        ("groups", "groups"),
        ("dilutions", "diluents"),
        ("templates", "templates"),
        ("forms", "forms"),
        ("admin_types", "admin_types"),
    )

    def __init__(self):
        # Храним данные в памяти
        self._lock = threading.RLock()
        self._last_loaded_signature = None
        self.drugs = {}
        self.groups = {}
        self.dilutions = {}
        self.templates = {}
        self.forms = {}
        self.admin_types = {}
        self.reload(force=True)

    def reload(self, *, force: bool = True) -> bool:
        """Перезагрузка всех данных (сначала seed, потом накатываем overrides)"""
        with self._lock:
            return self._reload_locked(force=force)

    def reload_if_changed(self) -> bool:
        """Перезагружает справочники только если seed/override файлы изменились."""
        return self.reload(force=False)

    def _reload_locked(self, *, force: bool) -> bool:
        signature = self._current_signature()
        if not force and signature == self._last_loaded_signature:
            return False

        loaded = self._load_all()
        for attr_name, _dict_name in self._DATASETS:
            setattr(self, attr_name, loaded.get(attr_name, {}))
        self._last_loaded_signature = self._current_signature()
        return True

    def _current_signature(self):
        service = get_settings_service()
        return (
            service.get_catalog_version(DRUG_CATALOG_KEY),
            service.get_catalog_version(ORDER_TEMPLATES_KEY),
        )

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        return get_settings_service().load_prescription_datasets()

    def _ordered_templates(self, templates: Dict[str, Any], order_keys: Any = None) -> Dict[str, Any]:
        if isinstance(order_keys, list):
            ordered = {}
            seen = set()
            for key in order_keys:
                if key in templates and key not in seen:
                    ordered[key] = templates[key]
                    seen.add(key)
            for key, data in templates.items():
                if key not in seen:
                    ordered[key] = data
            return ordered

        return dict(sorted(templates.items(), key=lambda x: x[1].get("name", x[0])))

    def ordered_templates_items(self) -> List[Tuple[str, Dict[str, Any]]]:
        return list(self.templates.items())

    def _load_merged(self, name: str) -> Dict[str, Any]:
        """Возвращает справочник из центральной settings DB."""
        with self._lock:
            loaded = get_settings_service().load_prescription_datasets()
            attr_name = {
                "diluents": "dilutions",
            }.get(name, name)
            return dict(loaded.get(attr_name, {}))

    def _save_override(self, name: str, key: str, data: Dict[str, Any]):
        """Сохраняет правки в settings DB."""
        with self._lock:
            self._save_override_locked(name, key, data)

    def _save_override_locked(self, name: str, key: str, data: Dict[str, Any]):
        get_settings_service().save_prescription_item(name, key, dict(data or {}))
        self._last_loaded_signature = None

    def _delete_override(self, name: str, key: str):
        """
        Скрывает элемент справочника в settings DB.
        """
        with self._lock:
            self._delete_override_locked(name, key)

    def _delete_override_locked(self, name: str, key: str):
        get_settings_service().delete_prescription_item(name, key)
        self._last_loaded_signature = None

    def _save_and_update(self, attr_name: str, dict_name: str, key: str, data: Dict[str, Any]):
        with self._lock:
            self._save_override_locked(dict_name, key, data)
            self._reload_locked(force=True)

    def _delete_and_reload(self, dict_name: str, key: str):
        with self._lock:
            self._delete_override_locked(dict_name, key)
            self._reload_locked(force=True)

    def save_template_order(self, order_keys: List[str]):
        """Сохраняет пользовательский порядок клинических протоколов."""
        with self._lock:
            valid_order = []
            seen = set()
            for key in order_keys:
                if key in self.templates and key not in seen:
                    valid_order.append(key)
                    seen.add(key)
            for key in self.templates:
                if key not in seen:
                    valid_order.append(key)
                    seen.add(key)

            get_settings_service().save_template_order(valid_order)
            self.templates = self._ordered_templates(self.templates, valid_order)
            self._last_loaded_signature = None

    # --- CRUD operations for Admin Panel ---
    
    def save_custom_drug(self, key: str, data: Dict[str, Any]):
        self._save_and_update("drugs", "drugs", key, data)
        
    def delete_custom_drug(self, key: str):
        self._delete_and_reload("drugs", key)

    def save_custom_group(self, key: str, data: Dict[str, Any]):
        self._save_and_update("groups", "groups", key, data)

    def delete_custom_group(self, key: str):
        self._delete_and_reload("groups", key)

    def save_custom_dilution(self, key: str, data: Dict[str, Any]):
        self._save_and_update("dilutions", "diluents", key, data)
        
    def delete_custom_dilution(self, key: str):
        self._delete_and_reload("diluents", key)
        
    def save_custom_template(self, key: str, data: Dict[str, Any]):
        self._save_and_update("templates", "templates", key, data)
        
    def delete_custom_template(self, key: str):
        self._delete_and_reload("templates", key)

    def save_custom_form(self, key: str, data: Dict[str, Any]):
        self._save_and_update("forms", "forms", key, data)
        
    def delete_custom_form(self, key: str):
        self._delete_and_reload("forms", key)

    def save_custom_admin_type(self, key: str, data: Dict[str, Any]):
        self._save_and_update("admin_types", "admin_types", key, data)
        
    def delete_custom_admin_type(self, key: str):
        self._delete_and_reload("admin_types", key)

    # --- Core Search Logic ---

    def normalize(self, text: str) -> str:
        return text.lower().replace(",", ".").strip()

    def detect_star_drug(self, text: str) -> Tuple[Optional[str], Optional[Dict]]:
        """Ищет препарат по star_alias (быстрый ввод)"""
        text = self.normalize(text)
        for key, data in self.drugs.items():
            star = data.get("star_alias", "").replace("*", "") # Убираем звездочку для сравнения
            if not star: continue
            
            # Если ввод начинается с этого алиаса (например, ввод "омез 20" начинается с "омез")
            if text.startswith(star):
                return key, data
        return None, None

    def detect_drug(self, text: str) -> Tuple[Optional[str], Optional[Dict]]:
        """Ищет препарат по обычным алиасам или латыни"""
        text = self.normalize(text)
        # Разбиваем на слова для более точного поиска
        tokens = text.split()
        if not tokens: return None, None
        
        first_token = tokens[0]
        
        # 1. Сначала проверяем точное совпадение ключа (case-insensitive)
        for key, data in self.drugs.items():
            if key.lower() == first_token:
                return key, data

        # 2. Проверяем латынь и алиасы
        for key, data in self.drugs.items():
            if data.get("latin", "").lower() == first_token:
                return key, data
                
            for alias in data.get("aliases", []):
                if self.normalize(alias) == first_token:
                    return key, data
                    
        # 3. Частичное совпадение (если ввели часть названия)
        for key, data in self.drugs.items():
            if first_token in key.lower() or first_token in data.get("latin", "").lower():
                return key, data
            for alias in data.get("aliases", []):
                if first_token in self.normalize(alias):
                    return key, data
                    
        return None, None

    def parse_dose(self, text: str) -> Optional[float]:
        match = re.search(r"(\d+(\.\d+)?)", text)
        if not match:
            return None
        return float(match.group(1))

    @staticmethod
    def component_dose_is_positive(value: Any) -> bool:
        try:
            return float(value) > 0
        except (TypeError, ValueError):
            return False

    def build_full_prescription(self, drug_key: str, data: Dict[str, Any]):
        """Формирует строку на основе данных из DrugAssignmentDialog"""
        drug = self.drugs.get(drug_key, {})
        latin = drug.get("latin", drug_key)
        
        dose = data.get("dose", 0)
        unit = data.get("unit", "mg")
        admin_type = data.get("admin_type", "bolus")
        duration = data.get("duration_min", 0)
        dilution = data.get("dilution")
        
        # Определяем префикс из формы
        form_key = data.get("form_key", drug.get("form_key"))
        form_info = self.forms.get(form_key, {})
        prefix = form_info.get("latin_abbr", "").strip()
        
        if prefix:
            if not prefix.endswith("."):
                prefix += "."
            res = f"{prefix} {latin} {dose:g} {unit}"
        else:
            res = f"{latin} {dose:g} {unit}"
        
        # Разведение
        # Растворитель для обычного добавления (аналогично ручному, идет через +)
        # Это распарсится в OrderInputHandler и попадет в comment
        can_dilute = form_info.get("can_dilute", True)
        if dilution and can_dilute:
            dil_base = dilution.get("base")
            dil_vol = dilution.get("volume", 0)
            dil_info = self.dilutions.get(dil_base, {})
            dil_display = dil_info.get("display", dil_base)
            res += f" + S. {dil_display} - {dil_vol} ml"
            
        # Способ введения (через тег)
        admin_type_info = self.admin_types.get(admin_type, {})
        admin_type_name = admin_type_info.get("name_ru", admin_type)
        if admin_type_name:
            res += f" [ROUTE:{admin_type_name.lower()}]"
        
        # Длительность (через тег). Для форм без длительности явно пишем 0,
        # чтобы парсер не подставил дефолт из карточки препарата.
        can_dilute = form_info.get("can_dilute", True)
        if can_dilute:
            if duration == -1:
                res += f" [DUR:-1]"
            else:
                # Всегда добавляем тег, даже если 0, чтобы перебить дефолт из базы
                res += f" [DUR:{duration}]"
        else:
            res += " [DUR:0]"
            
        return res

    def build_prescription(self, text: str, dilution_choice: Optional[Dict] = None):
        """Формирует строку назначения для одиночного ввода"""
        # Сначала ищем препарат в исходном тексте с пробелами
        # Это важно, так как нормализация может склеить название и дозу "ceftriaxone1.0"
        
        # 1. Поиск препарата (сначала по звездочке, потом обычный)
        drug_key, drug = self.detect_star_drug(text)
        if not drug:
            drug_key, drug = self.detect_drug(text)
            
        if not drug:
            return {"error": f"Препарат не найден (ввод: {text})"}
            
        # Теперь можно нормализовать текст для парсинга дозы
        text = self.normalize(text)

        # 2. Парсим дозу
        dose = self.parse_dose(text)
        if dose is None:
            dose = drug.get("default_dose", 0)

        unit = drug.get("unit", "mg")
        latin = drug.get("latin", drug_key)
        admin_type = drug.get("admin_type", "bolus")
        
        # Проверяем форму на необходимость разведения
        form_key = drug.get("form_key")
        form_info = self.forms.get(form_key, {})
        can_dilute = form_info.get("can_dilute", True)

        # 3. Базовая часть (Префикс)
        # Берем префикс из формы
        prefix = form_info.get("latin_abbr", "").strip()
        if prefix:
            if not prefix.endswith("."):
                prefix += "."
                
        if drug.get("is_multicomp"):
            # Строим строку из компонентов
            comp_strs = []
            for comp in drug.get("components", []):
                c_key = comp.get("drug_key")
                c_dose = comp.get("default_dose", 0)
                if not self.component_dose_is_positive(c_dose):
                    continue
                c_drug = self.drugs.get(c_key, {})
                c_lat = c_drug.get("latin", c_key)
                c_unit = c_drug.get("unit", "")
                
                # Используем общий префикс, так как форма задана для всего препарата
                c_str = f"{prefix} {c_lat} - {c_dose:g} {c_unit}" if prefix else f"{c_lat} - {c_dose:g} {c_unit}"
                comp_strs.append(c_str)
            if not comp_strs:
                return {"error": "Не указана дозировка компонентов многокомпонентного препарата"}
            base_presc = " + ".join(comp_strs)
        else:
            if prefix:
                base_presc = f"{prefix} {latin} {dose:g} {unit}"
            else:
                base_presc = f"{latin} {dose:g} {unit}"

        # 4. Логика разведения
        if can_dilute and admin_type in ["infusion", "continuous"]:
            if not dilution_choice:
                # Если у препарата есть дефолтное разведение, предлагаем его
                return {
                    "stage": "choose_dilution",
                    "base": base_presc,
                    "default_dilution": drug.get("default_dilution"),
                }
            
            # Применяем выбор разведения
            dil_base = dilution_choice.get("base")
            dil_vol = dilution_choice.get("volume", 0)
            dil_info = self.dilutions.get(dil_base, {})
            dil_display = dil_info.get("display", dil_base)
            
            res = f"{base_presc} + S. {dil_display} - {dil_vol} ml"
            return {"result": res}
            
        return {"result": base_presc}

    # --- Scheduling & Conflicts Logic ---
    
    def generate_schedule(self, template: Dict, start_time: datetime) -> List[Dict]:
        """Генерирует расписание по шаблону от времени start_time"""
        result = []
        
        for d in template.get("drugs", []):
            drug_key = d.get("drug")
            drug_info = self.drugs.get(drug_key, {})
            group_key = drug_info.get("group")
            group_info = self.groups.get(group_key, {})
            
            freq = d.get("freq", 1)
            priority = group_info.get("priority_level", 3)
            shift = group_info.get("offset_min", 0)
            duration_min = drug_info.get("duration_min", 0)
            
            # Для continuous игнорируем freq, ставим 1 блок на сутки
            admin_type = drug_info.get("admin_type", "bolus")
            if admin_type == "continuous":
                freq = 1
                duration_min = 1440 # 24 часа
                
            interval = 24 // freq if freq > 0 else 24
            base_time = start_time + timedelta(minutes=shift)

            for i in range(freq):
                t = base_time + timedelta(hours=i * interval)

                item = {
                    "drug_key": drug_key,
                    "drug_latin": drug_info.get("latin", drug_key),
                    "dose": d.get("dose", drug_info.get("default_dose", 0)),
                    "unit": drug_info.get("unit", "mg"),
                    "time": t,
                    "duration_min": duration_min,
                    "admin_type": admin_type,
                    "uses_line": drug_info.get("uses_line", False),
                    "color": group_info.get("color", "#bdc3c7"),
                    "group_name": group_info.get("name_ru", "")
                }
                
                # Если требует разведения
                if "diluent" in d:
                    dil_key = d["diluent"].get("base")
                    dil_vol = d["diluent"].get("volume", 0)
                    if dil_key in self.dilutions:
                        item["diluent_short"] = self.dilutions[dil_key].get("short", "S.")
                        item["diluent_display"] = self.dilutions[dil_key].get("display", dil_key)
                        item["diluent_volume"] = dil_vol
                            
                result.append(item)

        return sorted(result, key=lambda x: x["time"])

    def check_conflicts(self, schedule: List[Dict], lines_total: int = 2) -> List[Tuple[Dict, Dict]]:
        """Проверяет превышение допустимого количества линий в любой момент времени"""
        conflicts = []
        
        # Собираем все точки изменения состояния (start и end) для событий, занимающих линию
        events = []
        for i, item in enumerate(schedule):
            if not item.get("uses_line"):
                continue
                
            start = item["time"]
            duration = item.get("duration_min", 0)
            # Если это continuous (дозатор), считаем, что он занимает линию "навсегда" в рамках суток
            end = start + timedelta(minutes=duration) if duration > 0 else start + timedelta(hours=24)
            
            events.append((start, 1, i)) # 1 = линия занята
            events.append((end, -1, i))  # -1 = линия освобождена
            
        # Сортируем события по времени
        events.sort(key=lambda x: x[0])
        
        active_items = set()
        for time, diff, item_idx in events:
            if diff == 1:
                active_items.add(item_idx)
            else:
                active_items.discard(item_idx)
                
            if len(active_items) > lines_total:
                # Нашли пересечение, превышающее лимит портов
                # Выдаем первые два из активных как пример конфликта
                active_list = list(active_items)
                conflicts.append((schedule[active_list[0]], schedule[active_list[1]]))
                
        # Удаляем дубликаты конфликтов
        unique_conflicts = []
        seen = set()
        for a, b in conflicts:
            pair_id = tuple(sorted([a["drug_key"], b["drug_key"]]))
            if pair_id not in seen:
                seen.add(pair_id)
                unique_conflicts.append((a, b))

        return unique_conflicts

# Global instance
engine = PrescriptionEngine()
