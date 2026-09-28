from __future__ import annotations

# Совместимый публичный вход. Реализация разделена по доменам в services.operblock.
from rem_card.services.operblock.common import (
    OPERBLOCK_BLOOD_GROUP_OPTIONS,
    OPERBLOCK_BLOOD_RH_OPTIONS,
    OPERBLOCK_TABLES,
    OPERBLOCK_TRANSFER_DEPARTMENT_OPTIONS,
    OperBlockConflictError,
    OperBlockPatientInput,
    OperBlockSourceMovementChangedError,
    is_complete_operblock_mkb_code,
    normalize_operblock_blood_group,
    normalize_operblock_blood_rh,
    normalize_operblock_history_number,
    normalize_operblock_mkb_code,
    normalize_operblock_transfer_department,
)
from rem_card.services.operblock.core import OperBlockCoreMixin
from rem_card.services.operblock.archive_sources import OperBlockArchiveSourcesMixin
from rem_card.services.operblock.archive import OperBlockArchiveLifecycleMixin
from rem_card.services.operblock.archive_edit import OperBlockArchiveEditMixin
from rem_card.services.operblock.board import OperBlockBoardMixin
from rem_card.services.operblock.snapshots import OperBlockSnapshotsMixin
from rem_card.services.operblock.reporting import OperBlockReportingMixin
from rem_card.services.operblock.vitals import OperBlockVitalsMixin
from rem_card.services.operblock.case_context import OperBlockCaseContextMixin
from rem_card.services.operblock.case_lifecycle import OperBlockCaseLifecycleMixin
from rem_card.services.operblock.stages import OperBlockStagesMixin
from rem_card.services.operblock.protocol import OperBlockProtocolMixin
from rem_card.services.operblock.handoff import OperBlockHandoffMixin
from rem_card.services.operblock.orders import OperBlockOrdersMixin
from rem_card.services.operblock.infusions import OperBlockInfusionsMixin
from rem_card.services.operblock.medications import OperBlockMedicationHistoryMixin
from rem_card.services.operblock.helpers import OperBlockHelpersMixin


class OperBlockService(
    OperBlockCoreMixin,
    OperBlockArchiveSourcesMixin,
    OperBlockArchiveLifecycleMixin,
    OperBlockArchiveEditMixin,
    OperBlockBoardMixin,
    OperBlockSnapshotsMixin,
    OperBlockReportingMixin,
    OperBlockVitalsMixin,
    OperBlockCaseContextMixin,
    OperBlockCaseLifecycleMixin,
    OperBlockStagesMixin,
    OperBlockProtocolMixin,
    OperBlockHandoffMixin,
    OperBlockOrdersMixin,
    OperBlockInfusionsMixin,
    OperBlockMedicationHistoryMixin,
    OperBlockHelpersMixin,
):
    """Координатор доменных операций операционного блока."""


__all__ = (
    "OperBlockService",
    "OPERBLOCK_BLOOD_GROUP_OPTIONS",
    "OPERBLOCK_BLOOD_RH_OPTIONS",
    "OPERBLOCK_TABLES",
    "OPERBLOCK_TRANSFER_DEPARTMENT_OPTIONS",
    "OperBlockConflictError",
    "OperBlockPatientInput",
    "OperBlockSourceMovementChangedError",
    "is_complete_operblock_mkb_code",
    "normalize_operblock_blood_group",
    "normalize_operblock_blood_rh",
    "normalize_operblock_history_number",
    "normalize_operblock_mkb_code",
    "normalize_operblock_transfer_department",
)
