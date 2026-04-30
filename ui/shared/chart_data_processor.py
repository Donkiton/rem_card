import os
import warnings
from datetime import datetime, timedelta
from typing import List, Tuple

import numpy as np

MAX_INTERPOLATED_POINTS_PER_CHUNK = max(
    128,
    int(os.environ.get("REMCARD_CHART_MAX_INTERPOLATED_POINTS", "2048")),
)
CHART_VISIBLE_HOURS = 24.0
CHART_EDGE_CONTEXT_POINTS = max(0, int(os.environ.get("REMCARD_CHART_EDGE_CONTEXT_POINTS", "2")))
MAX_RAW_POINTS_FOR_PROCESSING = max(512, int(os.environ.get("REMCARD_CHART_MAX_RAW_POINTS", "6000")))


class ChartDataProcessor:
    @staticmethod
    def _is_sorted_by_timestamp(vitals) -> bool:
        if len(vitals) < 2:
            return True
        prev_ts = vitals[0].timestamp
        for vital in vitals[1:]:
            if vital.timestamp < prev_ts:
                return False
            prev_ts = vital.timestamp
        return True

    @staticmethod
    def _clip_to_visible_window(sorted_vitals, start_time: datetime):
        """
        Ограничивает массив виталов видимым окном графика (24ч)
        + небольшим контекстом до/после окна для непрерывности линий.
        """
        if not sorted_vitals:
            return []

        end_time = start_time + timedelta(hours=CHART_VISIBLE_HOURS)

        before = []
        inside = []
        after = []
        for vital in sorted_vitals:
            ts = vital.timestamp
            if ts < start_time:
                before.append(vital)
            elif ts > end_time:
                after.append(vital)
            else:
                inside.append(vital)

        selected = []
        if CHART_EDGE_CONTEXT_POINTS > 0 and before:
            selected.extend(before[-CHART_EDGE_CONTEXT_POINTS:])
        selected.extend(inside)
        if CHART_EDGE_CONTEXT_POINTS > 0 and after:
            selected.extend(after[:CHART_EDGE_CONTEXT_POINTS])

        if not selected:
            if CHART_EDGE_CONTEXT_POINTS > 0 and before:
                selected.extend(before[-CHART_EDGE_CONTEXT_POINTS:])
            if CHART_EDGE_CONTEXT_POINTS > 0 and after:
                selected.extend(after[:CHART_EDGE_CONTEXT_POINTS])

        if MAX_RAW_POINTS_FOR_PROCESSING > 0 and len(selected) > MAX_RAW_POINTS_FOR_PROCESSING:
            # Равномерная децимация для защиты UI от подвисаний на архивных массивах.
            idx = np.linspace(0, len(selected) - 1, num=MAX_RAW_POINTS_FOR_PROCESSING, dtype=int)
            selected = [selected[int(i)] for i in idx]

        return selected

    @staticmethod
    def _pchip_interpolate_numpy(x, y, x_new):
        """Монотонная кубическая эрмитова интерполяция на numpy."""
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        x_new = np.asarray(x_new, dtype=float)

        h = np.diff(x)
        delta = np.diff(y) / h

        d = np.zeros_like(y)

        for i in range(1, len(y) - 1):
            if delta[i - 1] * delta[i] <= 0:
                d[i] = 0.0
            else:
                w1 = 2 * h[i] + h[i - 1]
                w2 = h[i] + 2 * h[i - 1]
                d[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])

        d[0] = delta[0]
        d[-1] = delta[-1]

        idx = np.searchsorted(x, x_new, side="right") - 1
        idx = np.clip(idx, 0, len(x) - 2)

        h_idx = h[idx]
        t = (x_new - x[idx]) / h_idx
        t2 = t * t
        t3 = t2 * t

        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2

        return (
            h00 * y[idx]
            + h10 * h_idx * d[idx]
            + h01 * y[idx + 1]
            + h11 * h_idx * d[idx + 1]
        )

    @staticmethod
    def _interpolate_chunk(x, y, points_per_hour=60):
        x = np.array(x)
        y = np.array(y)

        _, unique_indices = np.unique(x, return_index=True)
        x = x[unique_indices]
        y = y[unique_indices]

        if len(x) < 2:
            return x.tolist(), y.tolist()

        duration = x[-1] - x[0]
        num_points = max(len(x), int(duration * points_per_hour))
        num_points = min(num_points, MAX_INTERPOLATED_POINTS_PER_CHUNK)

        if num_points <= len(x):
            return x.tolist(), y.tolist()

        new_x = np.linspace(x[0], x[-1], num_points)
        try:
            new_y = ChartDataProcessor._pchip_interpolate_numpy(x, y, new_x)
            return new_x.tolist(), new_y.tolist()
        except Exception:
            return x.tolist(), y.tolist()

    @staticmethod
    def _merge_adjacent_intervals(intervals, threshold_minutes=1):
        if not intervals:
            return []

        sorted_ints = sorted(intervals, key=lambda item: item[0])
        merged = []
        curr_start, curr_end = sorted_ints[0]

        for next_start, next_end in sorted_ints[1:]:
            if (next_start - curr_end).total_seconds() / 60.0 <= threshold_minutes:
                curr_end = max(curr_end, next_end)
            else:
                merged.append((curr_start, curr_end))
                curr_start, curr_end = next_start, next_end

        merged.append((curr_start, curr_end))
        return merged

    @staticmethod
    def _attach_interval_indexes(vitals, merged_intervals):
        """
        Возвращает пары (vital, interval_idx) для точек, которые попали в активные интервалы.
        Сложность O(N + M) для отсортированных точек и интервалов.
        """
        if not vitals:
            return []
        if not merged_intervals:
            return [(vital, None) for vital in vitals]

        indexed = []
        interval_idx = 0
        last_interval_idx = len(merged_intervals) - 1

        for vital in vitals:
            v_ts_min = vital.timestamp.replace(second=0, microsecond=0)

            while interval_idx <= last_interval_idx and v_ts_min > merged_intervals[interval_idx][1]:
                interval_idx += 1

            if interval_idx > last_interval_idx:
                break

            int_start, int_end = merged_intervals[interval_idx]
            if int_start <= v_ts_min <= int_end:
                indexed.append((vital, interval_idx))

        return indexed

    @staticmethod
    def _densify_array(x_arr, y_arr):
        if len(x_arr) == 0:
            return x_arr, y_arr

        new_x = []
        new_y = []

        nan_mask = np.isnan(y_arr)
        chunk_x = []
        chunk_y = []

        for i in range(len(x_arr)):
            if nan_mask[i]:
                if chunk_x:
                    nx, ny = ChartDataProcessor._interpolate_chunk(chunk_x, chunk_y)
                    new_x.extend(nx)
                    new_y.extend(ny)
                    chunk_x = []
                    chunk_y = []
                new_x.append(x_arr[i])
                new_y.append(np.nan)
            else:
                chunk_x.append(x_arr[i])
                chunk_y.append(y_arr[i])

        if chunk_x:
            nx, ny = ChartDataProcessor._interpolate_chunk(chunk_x, chunk_y)
            new_x.extend(nx)
            new_y.extend(ny)

        return np.array(new_x), np.array(new_y)

    @staticmethod
    def process_vitals(vitals, start_time: datetime, active_intervals: List[Tuple[datetime, datetime]] = None):
        """
        Преобразует виталы в X/Y для графика, добавляя разрывы на неактивных интервалах.
        """
        data = {
            "sys_x": [],
            "sys_y": [],
            "dia_x": [],
            "dia_y": [],
            "pulse_x": [],
            "pulse_y": [],
            "spo2_x": [],
            "spo2_y": [],
            "temp_x": [],
            "temp_y": [],
            "rr_x": [],
            "rr_y": [],
            "cvp_x": [],
            "cvp_y": [],
        }

        if not vitals:
            return data

        merged_intervals = ChartDataProcessor._merge_adjacent_intervals(active_intervals) if active_intervals else None

        sorted_all_vitals = list(vitals)
        if len(sorted_all_vitals) > 1 and not ChartDataProcessor._is_sorted_by_timestamp(sorted_all_vitals):
            sorted_all_vitals.sort(key=lambda v: v.timestamp)
        scoped_vitals = ChartDataProcessor._clip_to_visible_window(sorted_all_vitals, start_time)

        vitals_with_intervals = ChartDataProcessor._attach_interval_indexes(scoped_vitals, merged_intervals)
        sorted_vitals = [vital for vital, _ in vitals_with_intervals]

        def clean_val(value):
            if value is None:
                return np.nan
            try:
                return float(value)
            except Exception:
                return np.nan

        last_interval_idx = None
        for vital, interval_idx in vitals_with_intervals:
            delta = vital.timestamp - start_time
            exact_hour = delta.total_seconds() / 3600.0

            if (
                merged_intervals
                and interval_idx is not None
                and last_interval_idx is not None
                and interval_idx != last_interval_idx
            ):
                prev_interval_end = merged_intervals[last_interval_idx][1]
                gap_x = (prev_interval_end - start_time).total_seconds() / 3600.0 + 0.001
                for key in ["sys", "dia", "pulse", "spo2", "temp", "rr", "cvp"]:
                    data[f"{key}_x"].append(gap_x)
                    data[f"{key}_y"].append(np.nan)

            v_sys = clean_val(vital.sys)
            v_dia = clean_val(vital.dia)
            if not np.isnan(v_sys) or not np.isnan(v_dia):
                data["sys_x"].append(exact_hour)
                data["sys_y"].append(v_sys)
                data["dia_x"].append(exact_hour)
                data["dia_y"].append(v_dia)

            v_pulse = clean_val(vital.pulse)
            if not np.isnan(v_pulse):
                data["pulse_x"].append(exact_hour)
                data["pulse_y"].append(v_pulse)

            v_spo2 = clean_val(vital.spo2)
            if not np.isnan(v_spo2):
                data["spo2_x"].append(exact_hour)
                data["spo2_y"].append(v_spo2)

            v_temp = clean_val(vital.temp)
            if not np.isnan(v_temp):
                data["temp_x"].append(exact_hour)
                data["temp_y"].append(v_temp)

            v_rr = clean_val(getattr(vital, "rr", None))
            if not np.isnan(v_rr):
                data["rr_x"].append(exact_hour)
                data["rr_y"].append(v_rr)

            v_cvp = clean_val(getattr(vital, "cvp", None))
            if not np.isnan(v_cvp):
                data["cvp_x"].append(exact_hour)
                data["cvp_y"].append(0.0 if v_cvp == -1 else v_cvp)

            last_interval_idx = interval_idx

        for key in data:
            data[key] = np.array(data[key], dtype=float)

        if len(data["sys_x"]) > 1:
            common_x = data["sys_x"]
            data["sys_x"], data["sys_y"] = ChartDataProcessor._densify_array(common_x, data["sys_y"])
            _, data["dia_y"] = ChartDataProcessor._densify_array(common_x, data["dia_y"])
            data["dia_x"] = data["sys_x"]

        for key in ["pulse", "spo2", "temp", "rr", "cvp"]:
            data[f"{key}_x"], data[f"{key}_y"] = ChartDataProcessor._densify_array(
                data[f"{key}_x"], data[f"{key}_y"]
            )

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            pass

        return {
            "densified_data": data,
            "original_vitals": sorted_vitals,
        }
