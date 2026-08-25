"""Расчёт канонических графических артефактов аналитической платформы."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from functools import cached_property
from statistics import median
from typing import Any, Callable, Mapping

from rem_card.services.analytics.period import parse_analytics_datetime


class GraphArtifactBuilder:
    """Один расчётный контекст для графика, таблицы, PDF и drill-through."""

    def __init__(self, engine_class, definition, cases, period, denominator_cases=None):
        self.engine = engine_class
        self.definition = definition
        self.cases = tuple(cases)
        self.period = period
        self.key = definition.graph_key or definition.id
        self.denominator_population = tuple(denominator_cases) if denominator_cases is not None else self.cases

    @staticmethod
    def rows(counter: Mapping[str, float | int]):
        return tuple({"label": label, "value": value} for label, value in sorted(counter.items()))

    @staticmethod
    def ranked(counter: Mapping[str, float | int], limit=None):
        items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        if limit is not None:
            items = items[:limit]
        return tuple({"label": label, "value": value} for label, value in items)

    @staticmethod
    def text(item, name: str, default="Не указан") -> str:
        return str(item.attributes.get(name) or default)

    @staticmethod
    def gender(item) -> str:
        return str(item.attributes.get("sex") or item.attributes.get("patient_gender") or "Не указан")

    @staticmethod
    def age_group(item) -> str:
        from rem_card.services.analytics.platform.core import normalize_age_years

        raw_age = item.attributes.get("age")
        age_unit = "" if raw_age is not None else item.attributes.get("patient_age_unit")
        try:
            age = normalize_age_years(
                raw_age if raw_age is not None else item.attributes.get("patient_age"),
                age_unit,
            )
        except (TypeError, ValueError):  # pragma: no cover - defensive adapter
            age = None
        if age is None:
            return "Не указан"
        if age < 1:
            return "до 1 г"
        if age < 18:
            return "1–17"
        if age <= 44:
            return "18–44"
        if age <= 60:
            return "45–60"
        if age <= 75:
            return "61–75"
        return "76+"

    def death(self, item) -> bool:
        return self.engine._is_terminal_death(item, self.period)

    def outcome(self, item) -> str:
        from rem_card.services.analytics.platform.core import _effective_rao_outcome

        return _effective_rao_outcome(item.attributes, self.period)

    def duration(self, item) -> float:
        return self.engine._duration_days(item, self.period)

    @cached_property
    def deaths(self):
        return tuple(item for item in self.cases if self.death(item))

    @cached_property
    def durations(self):
        return {item.id: self.duration(item) for item in self.cases}

    @cached_property
    def calendar_days(self):
        cursor = self.period.start
        result = []
        while cursor < self.period.end:
            result.append(cursor)
            cursor += timedelta(days=1)
        return tuple(result)

    @cached_property
    def daily_census(self):
        values: Counter[str] = Counter()
        for day in self.calendar_days:
            events = []
            for item in self.cases:
                start = max(item.started_at or day, day)
                end = min(self.engine._terminal_end(item.attributes, self.period.end), day + timedelta(days=1))
                if start < end:
                    events.extend(((start, 1), (end, -1)))
            active = peak = 0
            for _, delta in sorted(events, key=lambda event: (event[0], event[1])):
                active += delta
                peak = max(peak, active)
            values[day.strftime("%Y-%m-%d")] = peak
        return values

    @cached_property
    def monthly_bed_days(self):
        values: Counter[str] = Counter()
        for item in self.cases:
            start = max(item.started_at or self.period.start, self.period.start)
            end = min(self.engine._terminal_end(item.attributes, self.period.end), self.period.end)
            cursor = start
            while cursor < end:
                next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                boundary = min(next_month, end)
                values[cursor.strftime("%Y-%m")] += (boundary - cursor).total_seconds() / 86400.0
                cursor = boundary
        return values

    @cached_property
    def episodes(self):
        result = []
        for item in self.cases:
            for raw in item.attributes.get("ivl_episodes") or ():
                start = parse_analytics_datetime(
                    raw.get("start_time") or raw.get("started_at") or raw.get("start_datetime")
                )
                end = parse_analytics_datetime(
                    raw.get("end_time") or raw.get("ended_at") or raw.get("end_datetime")
                ) or self.period.end
                if start and start < self.period.end and end > self.period.start:
                    result.append((item, max(start, self.period.start), min(end, self.period.end)))
        return tuple(result)

    def related(self, attribute: str):
        result = []
        for item in self.cases:
            for event in item.attributes.get(attribute) or ():
                stamp = parse_analytics_datetime(
                    event.get("operation_datetime") or event.get("datetime") or event.get("performed_at")
                )
                if stamp and self.period.start <= stamp < self.period.end:
                    result.append((item, event, stamp))
        return tuple(result)

    def ivl_months(self):
        monthly: Counter[str] = Counter()
        for _, start, end in self.episodes:
            cursor = start
            while cursor < end:
                next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                boundary = min(next_month, end)
                monthly[cursor.strftime("%Y-%m")] += (boundary - cursor).total_seconds() / 86400.0
                cursor = boundary
        return monthly

    def flow_month(self):
        return self.rows(Counter(item.started_at.strftime("%Y-%m") if item.started_at else "Не указан" for item in self.cases))

    def flow_weekday(self):
        return self.rows(Counter(item.started_at.strftime("%A") if item.started_at else "Не указан" for item in self.cases))

    def flow_day(self):
        return self.rows(Counter(item.started_at.strftime("%Y-%m-%d") if item.started_at else "Не указан" for item in self.cases))

    def source(self):
        return self.rows(Counter(self.text(item, "source_department") for item in self.cases))

    def source_type(self):
        return self.rows(Counter(
            str(item.attributes.get("admission_source") or item.attributes.get("source_type") or self.text(item, "source_department"))
            for item in self.cases
        ))

    def diagnoses(self, kind="full", only_deaths=False):
        base = self.deaths if only_deaths else self.cases
        if kind == "class":
            counter = Counter(str(item.attributes.get("diagnosis_code") or "")[:3] for item in base)
            counter.pop("", None)
            return self.ranked(counter, 10)
        limit = 10 if kind == "top10" else 15 if kind == "top15" else None
        return self.ranked(Counter(self.text(item, "diagnosis") for item in base), limit)

    def ratio_series(self, labels: Callable[[Any], str]):
        all_counts = Counter(labels(item) for item in self.cases)
        died = Counter(labels(item) for item in self.deaths)
        return self.rows({label: died[label] * 100.0 / count if count else 0.0 for label, count in all_counts.items()})

    def distribution(self, values):
        bins = Counter()
        for value in values:
            label = "0–1" if value <= 1 else "2–3" if value <= 3 else "4–7" if value <= 7 else "8–14" if value <= 14 else ">14"
            bins[label] += 1
        return self.rows(bins)

    def death_elapsed(self, item) -> float:
        stamp = parse_analytics_datetime(item.attributes.get("death_datetime"))
        return max(0.0, (stamp - item.started_at).total_seconds() / 3600.0) if stamp and item.started_at else 0.0

    def mortality_strata(self):
        hours = [self.death_elapsed(item) for item in self.deaths]
        return self.rows({
            "<24 ч": sum(value < 24 for value in hours),
            "1–3 суток": sum(24 <= value < 72 for value in hours),
            "4–7 суток": sum(72 <= value < 168 for value in hours),
            ">7 суток": sum(value >= 168 for value in hours),
        })

    def km_curve(self):
        observations = [(self.durations[item.id], self.death(item)) for item in self.cases]
        at_risk, survival, result = len(observations), 1.0, []
        for point in sorted({value for value, _ in observations}):
            events = sum(value == point and happened for value, happened in observations)
            censored = sum(value == point and not happened for value, happened in observations)
            if at_risk and events:
                survival *= 1.0 - events / at_risk
            result.append({"label": f"{point:.2f}", "x": point, "value": survival})
            at_risk -= events + censored
        return tuple(result)

    def high_load_runs(self):
        from rem_card.services.analytics.constants import STATISTICAL_HIGH_LOAD_THRESHOLD

        runs, current = [], 0
        for value in self.daily_census.values():
            if value >= STATISTICAL_HIGH_LOAD_THRESHOLD:
                current += 1
            elif current:
                runs.append(current)
                current = 0
        if current:
            runs.append(current)
        return tuple({"label": f"Период {index + 1}", "value": value} for index, value in enumerate(runs))

    def monthly_occupancy(self):
        from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT

        return self.rows({
            month: value * 100 / (
                sum(1 for day in self.calendar_days if day.strftime("%Y-%m") == month) * STATISTICAL_BED_COUNT
            )
            for month, value in self.monthly_bed_days.items()
        })

    def weekday_occupancy(self):
        from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT

        weekdays = {datetime.strptime(label, "%Y-%m-%d").strftime("%A") for label in self.daily_census}
        values = {}
        for weekday in weekdays:
            matching = [
                value for label, value in self.daily_census.items()
                if datetime.strptime(label, "%Y-%m-%d").strftime("%A") == weekday
            ]
            values[weekday] = sum(matching) * 100 / max(1, len(matching) * STATISTICAL_BED_COUNT)
        return self.rows(values)

    def _group_1_18(self):
        from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT, STATISTICAL_HIGH_LOAD_THRESHOLD

        days = max(1, len(self.calendar_days))
        calculators = {
            "g1": self.flow_month,
            "g2": self.flow_weekday,
            "g3": self.flow_day,
            "g4": self.source_type,
            "g5": self.source,
            "g6": lambda: self.rows(self.monthly_bed_days),
            "g7": self.monthly_occupancy,
            "g8": lambda: self.rows(Counter(self.text(item, "bed_number") for item in self.cases)),
            "g9": self.bed_turnover,
            "g10": lambda: ({"label": "Среднесуточная занятость", "value": sum(self.daily_census.values()) / max(1, len(self.daily_census))},),
            "g11": lambda: self.rows(self.daily_census),
            "g12": lambda: ({"label": "Интенсивность", "value": sum(self.monthly_bed_days.values()) * 100 / (days * STATISTICAL_BED_COUNT)},),
            "g13": self.monthly_occupancy,
            "g14": self.high_load_days,
            "g15": self.high_load_runs,
            "g16": lambda: ({"label": "Максимум", "value": max(self.daily_census.values(), default=0)},),
            "g17": lambda: ({"label": "Доля времени высокой загрузки", "value": sum(value >= STATISTICAL_HIGH_LOAD_THRESHOLD for value in self.daily_census.values()) * 100 / max(1, len(self.daily_census))},),
            "g18": lambda: self.rows(self.daily_census),
        }
        return calculators[self.key]()

    def _group_19_41(self):
        calculators = {
            "g19": lambda: self.rows(Counter(self.age_group(item) for item in self.cases)),
            "g20": lambda: self.rows(Counter(self.gender(item) for item in self.cases)),
            "g21": lambda: self.rows(Counter(self.age_group(item) for item in self.deaths)),
            "g22": lambda: self.rows(Counter(self.age_group(item) for item in self.cases)),
            "g23": lambda: self.diagnoses("top10"),
            "g24": lambda: self.diagnoses("class"),
            "g25": lambda: self.diagnoses("top15"),
            "g26": lambda: self.diagnoses("top10", True),
            "g27": lambda: self.ratio_series(lambda item: self.text(item, "diagnosis")),
            "g28": lambda: self.rows(Counter(self.outcome(item) for item in self.cases)),
            "g29": lambda: self.ratio_series(lambda item: item.started_at.strftime("%Y-%m") if item.started_at else "Не указан"),
            "g30": lambda: self.ratio_series(self.gender),
            "g31": lambda: self.ratio_series(self.age_group),
            "g32": lambda: self.ratio_series(lambda item: self.text(item, "source_department")),
            "g33": lambda: tuple({"label": f"Случай {index + 1}", "value": self.durations[item.id]} for index, item in enumerate(self.cases)),
            "g34": self.average_duration_by_month,
            "g35": lambda: ({"label": ">7 суток", "value": sum(value > 7 for value in self.durations.values()) * 100 / max(1, len(self.durations))}, {"label": "≤7 суток", "value": sum(value <= 7 for value in self.durations.values()) * 100 / max(1, len(self.durations))}),
            "g36": lambda: ({"label": ">14 суток", "value": sum(value > 14 for value in self.durations.values()) * 100 / max(1, len(self.durations))}, {"label": "≤14 суток", "value": sum(value <= 14 for value in self.durations.values()) * 100 / max(1, len(self.durations))}),
            "g37": lambda: tuple({"label": f"Случай {index + 1}", "value": self.death_elapsed(item)} for index, item in enumerate(self.deaths)),
            "g38": self.mortality_strata,
            "g39": lambda: ({"label": "Ранние смерти", "value": sum(self.death_elapsed(item) < 24 for item in self.deaths) * 100 / max(1, len(self.deaths))},),
            "g40": lambda: ({"label": "Индекс тяжести", "value": sum(self.death_elapsed(item) < 24 for item in self.deaths) * 100 / max(1, len(self.deaths))},),
            "g41": self.km_curve,
        }
        return calculators[self.key]()

    def average_duration_by_month(self):
        counts = Counter(item.started_at.strftime("%Y-%m") for item in self.cases if item.started_at)
        return self.rows({
            month: sum(
                self.durations[item.id] for item in self.cases
                if item.started_at and item.started_at.strftime("%Y-%m") == month
            ) / count
            for month, count in counts.items()
        })

    def average_duration_by(self, label_function):
        counts = Counter(label_function(item) for item in self.cases)
        return self.rows({
            label: sum(self.durations[item.id] for item in self.cases if label_function(item) == label) / count
            for label, count in counts.items()
        })

    def top_average_duration_by(self, label_function, limit=5):
        counts = Counter(label_function(item) for item in self.cases)
        averages = (
            (
                label,
                sum(self.durations[item.id] for item in self.cases if label_function(item) == label) / count,
            )
            for label, count in counts.items()
        )
        return tuple(
            {"label": label, "value": value}
            for label, value in sorted(averages, key=lambda item: (-item[1], item[0]))[:limit]
        )

    def bed_turnover(self):
        from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT

        admissions = Counter(
            item.started_at.strftime("%Y-%m") if item.started_at else "Не указан"
            for item in self.cases
        )
        return self.rows({month: count / STATISTICAL_BED_COUNT for month, count in admissions.items()})

    def high_load_days(self):
        from rem_card.services.analytics.constants import STATISTICAL_HIGH_LOAD_THRESHOLD

        return self.rows({
            day: int(value >= STATISTICAL_HIGH_LOAD_THRESHOLD)
            for day, value in self.daily_census.items()
        })

    def terminal_outcome(self, item):
        death_at = parse_analytics_datetime(item.attributes.get("death_datetime"))
        transfer_at = parse_analytics_datetime(item.attributes.get("transfer_datetime"))
        terminal_at = min((stamp for stamp in (death_at, transfer_at) if stamp is not None), default=None)
        if terminal_at is None or not (self.period.start <= terminal_at < self.period.end):
            return None
        if death_at is not None and death_at == terminal_at:
            return "умер"
        raw_outcome = str(item.attributes.get("raw_outcome") or item.attributes.get("outcome") or "").casefold()
        if raw_outcome in {"выписан", "выписана", "discharged"}:
            return "выписан"
        return None

    @cached_property
    def completed_outcome_cases(self):
        return tuple(item for item in self.cases if self.terminal_outcome(item) is not None)

    def average_completed_duration(self):
        counts = Counter(self.terminal_outcome(item) for item in self.completed_outcome_cases)
        return self.rows({
            label: sum(
                self.durations[item.id]
                for item in self.completed_outcome_cases
                if self.terminal_outcome(item) == label
            ) / count
            for label, count in counts.items()
        })

    def _group_42_65(self):
        ivl_ids = {item.id for item, _, _ in self.episodes}
        calculators = {
            "g42": lambda: ({"label": "ИВЛ", "value": len(ivl_ids) * 100 / max(1, len(self.denominator_population))}, {"label": "Без ИВЛ", "value": (len(self.denominator_population) - len(ivl_ids)) * 100 / max(1, len(self.denominator_population))}),
            "g43": lambda: ({"label": "Эпизоды ИВЛ", "value": len(self.episodes)},),
            "g44": lambda: tuple({"label": f"Эпизод {index + 1}", "value": (end - start).total_seconds() / 86400.0} for index, (_, start, end) in enumerate(self.episodes)),
            "g45": lambda: self.rows(self.ivl_months()),
            "g46": self.monthly_occupancy,
            "g47": self.weekday_occupancy,
            "g48": lambda: ({"label": "Максимум", "value": max(self.daily_census.values(), default=0)},),
            "g49": self.average_completed_duration,
            "g50": lambda: self.top_average_duration_by(lambda item: self.text(item, "diagnosis"), 5),
            "g51": self.weekday_occupancy,
            "g52": lambda: self.rows(Counter(self.text(item, "bed_number") for item in self.cases)),
            "g53": lambda: self.rows(self.daily_census),
            "g54": lambda: ({"label": "Краткие", "value": sum(value for value in self.durations.values() if value < 3) / max(1, sum(value < 3 for value in self.durations.values()))},),
            "g55": lambda: ({"label": "Длительные", "value": sum(value for value in self.durations.values() if value >= 14) / max(1, sum(value >= 14 for value in self.durations.values()))},),
            "g56": lambda: self.rows(Counter(stamp.strftime("%Y-%m") for _, _, stamp in self.related("operations"))),
            "g57": lambda: self.rows(Counter(str(event.get("description") or event.get("operation_type") or "Не указано") for _, event, _ in self.related("operations"))),
            "g58": lambda: self.rows(Counter(stamp.strftime("%Y-%m") for _, _, stamp in self.related("transfusions"))),
            "g59": lambda: self.rows(Counter(str(event.get("type") or event.get("component") or "Не указано") for _, event, _ in self.related("transfusions"))),
            "g60": self.average_after_operation,
            "g61": self.source,
            "g62": lambda: self.average_duration_by(lambda item: self.text(item, "source_department")),
            "g63": lambda: tuple({"group": self.text(item, "source_department"), "label": f"Случай {index + 1}", "value": self.durations[item.id]} for index, item in enumerate(self.cases)),
            "g65": lambda: self.rows(Counter(item.started_at.strftime("%H") if item.started_at else "Не указан" for item in self.cases)),
        }
        return calculators[self.key]()

    def average_after_operation(self):
        operations = self.related("operations")
        total = sum(
            max(0.0, (min(self.engine._terminal_end(item.attributes, self.period.end), self.period.end) - stamp).total_seconds() / 86400.0)
            for item, _, stamp in operations
        )
        return ({"label": "После операции", "value": total / max(1, len(operations))},)

    def recovery_series(self):
        from rem_card.services.analytics.platform.core import is_recovery_case

        recovery = tuple(item for item in self.cases if is_recovery_case(item.attributes))
        calculators = {
            "recovery_flow_table": lambda: self.recovery_table_series(recovery),
            "recovery_flow_months": lambda: self.rows(Counter(item.started_at.strftime("%Y-%m") if item.started_at else "Не указан" for item in recovery)),
            "recovery_flow_duration": lambda: self.recovery_duration_series(recovery),
            "recovery_flow_outcomes": lambda: self.rows(Counter(self.outcome(item) for item in recovery)),
        }
        return calculators[self.key]()

    def recovery_table_series(self, recovery):
        durations_hours = [self.duration(item) * 24.0 for item in recovery]
        patient_ids = set()
        for item in recovery:
            patient_id = item.attributes.get("patient_id")
            identity = str(patient_id) if patient_id not in (None, "") else f"case:{item.local_id}"
            patient_ids.add((item.source_db_id, identity))
        outcomes = Counter(self.outcome(item) for item in recovery)
        return (
            {"label": "Госпитализаций через койки пробуждения", "value": len(recovery), "unit": "случаев"},
            {"label": "Уникальных пациентов", "value": len(patient_ids), "unit": "пациентов"},
            {"label": "Общая длительность", "value": sum(durations_hours), "unit": "часов"},
            {"label": "Средняя длительность", "value": sum(durations_hours) / len(durations_hours) if durations_hours else 0, "unit": "часов"},
            {"label": "Медиана длительности", "value": median(durations_hours) if durations_hours else 0, "unit": "часов"},
            {"label": "Минимальная длительность", "value": min(durations_hours, default=0), "unit": "часов"},
            {"label": "Максимальная длительность", "value": max(durations_hours, default=0), "unit": "часов"},
            {"label": "Переведены", "value": outcomes.get("переведен", 0), "unit": "случаев"},
            {"label": "Умерли", "value": outcomes.get("умер", 0), "unit": "случаев"},
            {"label": "Без конечного исхода", "value": outcomes.get("в отделении", 0), "unit": "случаев"},
        )

    def recovery_duration_series(self, recovery):
        buckets = (
            ("до 2 часов", 0.0, 2.0),
            ("2-6 часов", 2.0, 6.0),
            ("6-24 часа", 6.0, 24.0),
            ("более 24 часов", 24.0, None),
        )
        durations = [self.duration(item) * 24.0 for item in recovery]
        return tuple({
            "label": label,
            "value": sum(
                value >= lower and (upper is None or value < upper)
                for value in durations
            ),
        } for label, lower, upper in buckets)

    def series(self):
        if self.key.startswith("recovery_"):
            return self.recovery_series()
        number = int(self.key[1:])
        if number <= 18:
            return self._group_1_18()
        if number <= 41:
            return self._group_19_41()
        return self._group_42_65()

    @staticmethod
    def cases_for_events(cases, events):
        identifiers = dict.fromkeys(item.id for item, *_ in events)
        by_id = {item.id: item for item in cases}
        return tuple(by_id[identifier] for identifier in identifiers if identifier in by_id)

    def selected_cases(self):
        if self.key in {"g21", "g26", "g27", "g29", "g30", "g31", "g32", "g37", "g38", "g39", "g40"}:
            return self.deaths
        if self.key == "g35":
            return tuple(item for item in self.cases if self.durations[item.id] > 7)
        if self.key == "g36":
            return tuple(item for item in self.cases if self.durations[item.id] > 14)
        if self.key in {"g42", "g43", "g44", "g45"}:
            return self.cases_for_events(self.cases, self.episodes)
        if self.key.startswith("recovery_"):
            from rem_card.services.analytics.platform.core import is_recovery_case
            return tuple(item for item in self.cases if is_recovery_case(item.attributes))
        if self.key == "g49":
            return self.completed_outcome_cases
        if self.key == "g50":
            selected_labels = {row["label"] for row in self.top_average_duration_by(lambda item: self.text(item, "diagnosis"), 5)}
            return tuple(item for item in self.cases if self.text(item, "diagnosis") in selected_labels)
        if self.key == "g54":
            return tuple(item for item in self.cases if self.durations[item.id] < 3)
        if self.key == "g55":
            return tuple(item for item in self.cases if self.durations[item.id] >= 14)
        if self.key in {"g56", "g57", "g60"}:
            return self.cases_for_events(self.cases, self.related("operations"))
        if self.key in {"g58", "g59"}:
            return self.cases_for_events(self.cases, self.related("transfusions"))
        return self.cases

    def numerator_denominator(self, series, selected):
        numerator: int | float | None = len(self.cases)
        denominator: int | float | None = None
        if self.key in {"g27", "g29", "g30", "g31", "g32", "g35", "g36", "g39", "g40", "g42"}:
            denominator = len(self.denominator_population) if self.key == "g42" else len(self.deaths) if self.key in {"g39", "g40"} else len(self.cases)
        if self.key in {"g21", "g26", "g27", "g29", "g30", "g31", "g32", "g37", "g38", "g41"}:
            numerator = len(self.deaths)
        elif self.key in {"g39", "g40"}:
            numerator = sum(self.death_elapsed(item) < 24 for item in self.deaths)
        elif self.key in {"g35", "g36", "g42"}:
            numerator = len(selected)
        elif self.key == "g43":
            numerator = len(self.episodes)
        elif self.key in {"g44", "g45"}:
            numerator = sum(float(item["value"]) for item in series)
            denominator = len(self.episodes) if self.key == "g44" else None
        elif self.key in {"g56", "g57"}:
            numerator = len(self.related("operations"))
        elif self.key in {"g58", "g59"}:
            numerator = len(self.related("transfusions"))
        elif self.key in {"g6", "g7", "g12", "g13", "g46"}:
            numerator = sum(self.monthly_bed_days.values())
        elif self.key == "g9":
            from rem_card.services.analytics.constants import STATISTICAL_BED_COUNT
            numerator, denominator = len(self.cases), STATISTICAL_BED_COUNT
        elif self.key == "g14":
            numerator = sum(float(item["value"]) for item in series)
        elif self.key in {"g10", "g11", "g15", "g16", "g17", "g18", "g47", "g48", "g51", "g53"}:
            numerator = sum(self.daily_census.values())
        elif self.key in {"g49", "g50"}:
            numerator = sum(self.durations[item.id] for item in selected)
            denominator = len(selected)
        elif self.key in {"g33", "g34", "g54", "g55", "g60", "g62", "g63"}:
            numerator = sum(float(item["value"]) for item in series) if series else 0
        return numerator, denominator

    def chart_kind(self):
        if self.key == "recovery_flow_table":
            return "table"
        if self.key in {"g28", "recovery_flow_outcomes"}:
            return "pie"
        if self.key == "g41":
            return "step"
        if self.key == "g33":
            return "histogram"
        if self.key == "g63":
            return "ward_histograms"
        line_graphs = {"g1", "g3", "g7", "g13", "g18", "g29", "g34", "g45", "g46", "g53", "g56", "g58"}
        return "line" if self.key in line_graphs else "bar"

    def build(self):
        from rem_card.services.analytics.graph_catalog import GRAPH_GROUPS
        from rem_card.services.analytics.platform.core import GraphMetricArtifact

        exposed = {item for group in GRAPH_GROUPS.values() for item in group}
        if self.key not in exposed:
            raise KeyError(f"График {self.key} отсутствует в каноническом каталоге.")
        series = self.series()
        selected = self.selected_cases()
        numerator, denominator = self.numerator_denominator(series, selected)
        source_ids = tuple(item.id for item in selected)
        display_value = self._display_value(numerator, denominator)
        summary = f"{self.definition.title}: {self._format_value(display_value)} {self.definition.unit}"
        return GraphMetricArtifact(
            self.key,
            self.definition.title,
            self.period.sql_bounds,
            source_ids,
            numerator,
            denominator,
            self.definition.unit,
            self.definition.time_basis,
            summary,
            series,
            self.chart_kind(),
        )

    def _display_value(self, numerator, denominator):
        if numerator is None:
            return None
        if denominator is None:
            return numerator
        if not denominator:
            return None
        value = float(numerator) / float(denominator)
        return value * 100.0 if self.definition.unit.strip() == "%" else value

    @staticmethod
    def _format_value(value) -> str:
        if value is None:
            return "—"
        return f"{float(value):.2f}".rstrip("0").rstrip(".")


def build_graph_artifact(engine_class, definition, cases, period, denominator_cases=None):
    return GraphArtifactBuilder(engine_class, definition, cases, period, denominator_cases).build()
