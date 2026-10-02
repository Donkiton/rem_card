from __future__ import annotations

import os

ADD_PATIENT_LOCK_POLL_INTERVAL_MS = 1500
ADD_PATIENT_LOCK_KEY = "add_patient_button"
PATIENT_BED_MANAGEMENT_MODE = "patient_bed_management"
CARD_UI_PREWARM_ENABLED = os.environ.get("REMCARD_CARD_UI_PREWARM", "1") == "1"
CARD_UI_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_DELAY_MS", "900")))
CARD_UI_PREWARM_STAGGER_MS = max(0, int(os.environ.get("REMCARD_CARD_PREWARM_STAGGER_MS", "120")))
CARD_OPEN_HYDRATE_DELAY_MS = max(0, int(os.environ.get("REMCARD_CARD_OPEN_HYDRATE_DELAY_MS", "250")))
CARD_HYDRATION_FOREGROUND_IDLE_SEC = max(
    0.0,
    float(os.environ.get("REMCARD_CARD_HYDRATION_FOREGROUND_IDLE_SEC", "3")),
)
CARD_HYDRATION_MAX_DEFER_ATTEMPTS = max(
    0,
    int(os.environ.get("REMCARD_CARD_HYDRATION_MAX_DEFER_ATTEMPTS", "5")),
)
CHART_LAZY_INIT_DELAY_MS = max(0, int(os.environ.get("REMCARD_CHART_LAZY_INIT_DELAY_MS", "0")))
JOURNAL_PREWARM_DELAY_MS = max(0, int(os.environ.get("REMCARD_JOURNAL_PREWARM_DELAY_MS", "60000")))
JOURNAL_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_PREWARM", "0") == "1"
JOURNAL_WIDGET_PREWARM_ENABLED = os.environ.get("REMCARD_JOURNAL_WIDGET_PREWARM", "0") == "1"
PATIENT_PREVIEW_ICON_PREWARM_DELAY_MS = max(
    0,
    int(os.environ.get("REMCARD_PATIENT_PREVIEW_ICON_PREWARM_DELAY_MS", "1200")),
)
LOCAL_ORDER_FORCE_PREFIXES = (
    "orders_add_input:",
    "orders_add_cvp:",
    "orders_edit_input:",
    "orders_left_click:",
    "orders_middle_click:",
    "orders_right_click:",
    "doctor_order_mark:",
    "nurse_order_mark:",
    "nurse_order_panel_mark:",
    "orders_finalize:",
)
EMERGENCY_NOTICE_FORCE_PREFIX = "emergency_notice_save:"
ORDER_CHANGE_ENTITIES = {"orders", "administrations"}
LAB_ORDER_CHANGE_ENTITIES = {"lab_orders"}
VITALS_CACHE_CHANGE_ENTITIES = {
    "patients",
    "admissions",
    "beds",
    "operations",
    "vitals",
    "vital_settings",
    "patient_status_events",
    "fluids",
    "diet_plan",
    "diet_plan_versions",
    "oral_intake_events",
}
CARD_CACHE_CHANGE_ENTITIES = VITALS_CACHE_CHANGE_ENTITIES | ORDER_CHANGE_ENTITIES | {
    "diet_templates",
    "ivl_episodes",
    "transfusions",
    "clinical_events",
    "devices",
    "respiratory_support",
}
