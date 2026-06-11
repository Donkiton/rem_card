from __future__ import annotations


# Official department capacity used only for statistical calculations.
STATISTICAL_BED_COUNT = 4
STATISTICAL_HIGH_LOAD_THRESHOLD = STATISTICAL_BED_COUNT

# Additional patient-flow items for recovery-bed analytics in graphical reports.
RECOVERY_FLOW_TABLE_KEY = "recovery_flow_table"
RECOVERY_FLOW_MONTHS_KEY = "recovery_flow_months"
RECOVERY_FLOW_DURATION_KEY = "recovery_flow_duration"
RECOVERY_FLOW_OUTCOMES_KEY = "recovery_flow_outcomes"
RECOVERY_FLOW_GRAPH_KEYS = frozenset(
    {
        RECOVERY_FLOW_TABLE_KEY,
        RECOVERY_FLOW_MONTHS_KEY,
        RECOVERY_FLOW_DURATION_KEY,
        RECOVERY_FLOW_OUTCOMES_KEY,
    }
)
