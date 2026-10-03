"""Transactional editing of an existing IVL event and its derived timeline."""
from datetime import timedelta

from rem_card.data.dto.remcard_dto import VentilationEventType, VentilationStartType


def edit_event_in_transaction(service, cursor, *, event_id, admission_id, event_time,
                              event_type, mode, parameters, indications, o2_flow,
                              expected_event_revision, expected_case_revision):
    dao = service.dao
    cursor.execute("SELECT * FROM clinical_events WHERE id=? AND admission_id=? AND ivl_episode_id IS NOT NULL", (event_id, admission_id))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Событие ИВЛ удалено или относится к другому пациенту. Обновите журнал.")
    event = dao._map_event(row)
    dao.assert_event_revision(cursor, event_id, expected_event_revision)
    cursor.execute("SELECT * FROM ivl_episodes WHERE id=? AND admission_id=?", (event.ivl_episode_id, admission_id))
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Случай ИВЛ не найден.")
    case = dao._map_case(row)
    dao.assert_case_revision(cursor, case.id, expected_case_revision)
    new_type = service._to_event_type(event_type) if event_type else event.event_type
    interchangeable = {VentilationEventType.MODE_CHANGE, VentilationEventType.TRACHEOSTOMY}
    if new_type != event.event_type and not {new_type, event.event_type}.issubset(interchangeable):
        raise ValueError("Можно менять тип только между сменой режима и трахеостомией.")
    mode_enum = service._to_mode(mode) if mode else None
    normalized = service._normalize_parameters(mode_enum, parameters or {})
    service._validate_event_payload(new_type, mode_enum, normalized, extubation_reason=indications, o2_flow=o2_flow)
    original_events = _validate_timeline(service, cursor, case, event, event_time)
    serialized = service._dump_parameters(normalized)
    # Keep IDs, author and creation time: this is an edit, not an undo/add.
    cursor.execute(
        """UPDATE clinical_events SET timestamp=?, mode=?, parameters_json=?, data=?,
           event_type=?, extubation_reason=?, o2_flow=?, revision=COALESCE(revision,0)+1 WHERE id=?""",
        (event_time.isoformat(), mode_enum.value if mode_enum else None, serialized, serialized, new_type.value, indications, o2_flow, event_id),
    )
    _update_support(dao, cursor, case, event_id, event_time, mode_enum, normalized, serialized)
    _update_case_and_tubes(dao, cursor, case, event, event_time, new_type, original_events)
    cursor.execute("SELECT * FROM clinical_events WHERE id=?", (event_id,))
    return dao._map_event(cursor.fetchone())


def _validate_timeline(service, cursor, case, event, event_time):
    cursor.execute("SELECT admission_datetime FROM admissions WHERE id=?", (case.admission_id,))
    admission_time = service.dao._parse_dt(cursor.fetchone()[0])
    if admission_time and event_time < admission_time:
        raise ValueError("Событие ИВЛ не может быть раньше поступления пациента.")
    cursor.execute("SELECT * FROM clinical_events WHERE ivl_episode_id=? ORDER BY DATETIME(timestamp), id", (case.id,))
    events = [service.dao._map_event(row) for row in cursor.fetchall()]
    index = next(i for i, item in enumerate(events) if item.id == event.id)
    if index and event_time < events[index - 1].timestamp:
        raise ValueError("Время не может быть раньше предыдущего события ИВЛ.")
    if index + 1 < len(events) and event_time > events[index + 1].timestamp:
        raise ValueError("Время не может быть позже следующего события ИВЛ.")
    if event.event_type == VentilationEventType.START_VENT:
        if case.start_type == VentilationStartType.ADMISSION and admission_time and event_time != admission_time:
            raise ValueError("Начало ИВЛ 'С поступления' должно совпадать со временем поступления.")
    elif event.event_type != VentilationEventType.EXTUBATION:
        service._validate_event_time(case, event_time)
    if event.event_type == VentilationEventType.EXTUBATION and event_time < case.start_time:
        raise ValueError("Нельзя экстубировать раньше начала ИВЛ.")
    if event.author == service.OUTCOME_SYNC_AUTHOR and event_time != event.timestamp:
        raise ValueError("Время автоматического завершения ИВЛ задаётся исходом пациента. Измените время исхода.")
    cursor.execute("SELECT * FROM ivl_episodes WHERE admission_id=? AND id<>? ORDER BY episode_number", (case.admission_id, case.id))
    for row in cursor.fetchall():
        other = service.dao._map_case(row)
        if event.event_type == VentilationEventType.START_VENT and other.episode_number < case.episode_number and other.end_time:
            if event_time < other.end_time + timedelta(minutes=1):
                raise ValueError("Начало случая должно быть минимум через минуту после предыдущего случая ИВЛ.")
        if event.event_type == VentilationEventType.EXTUBATION and other.episode_number > case.episode_number:
            if event_time + timedelta(minutes=1) > other.start_time:
                raise ValueError("Завершение случая должно быть минимум за минуту до следующего случая ИВЛ.")
    return events


def _update_support(dao, cursor, case, event_id, event_time, mode, parameters, serialized):
    cursor.execute("SELECT id FROM respiratory_support WHERE event_id=?", (event_id,))
    exists = cursor.fetchone() is not None
    if mode is None:
        dao.delete_respiratory_support_by_event(cursor, event_id)
    elif exists:
        cursor.execute(
            """UPDATE respiratory_support SET datetime=?, mode=?, fio2=?, peep=?, tv=?, rr=?, parameters_json=?
               WHERE event_id=?""",
            (event_time.isoformat(), mode.value, parameters.get("FiO2"), parameters.get("PEEP"), parameters.get("TV"), parameters.get("RR"), serialized, event_id),
        )
    else:
        dao.insert_respiratory_support(cursor, admission_id=case.admission_id, case_id=case.id,
                                       event_id=event_id, event_time=event_time, mode=mode,
                                       parameters=parameters, parameters_json=serialized)


def _update_case_and_tubes(dao, cursor, case, event, event_time, new_type, original_events):
    structural = (VentilationEventType.START_VENT, VentilationEventType.TRACHEOSTOMY, VentilationEventType.TUBE_REPLACEMENT)
    if new_type != event.event_type or (event.event_type in structural and event_time != event.timestamp):
        _reconcile_tubes(dao, cursor, case, original_events)
    if event.event_type == VentilationEventType.START_VENT:
        cursor.execute("UPDATE ivl_episodes SET start_time=?, revision=COALESCE(revision,0)+1 WHERE id=?", (event_time.isoformat(), case.id))
    elif event.event_type == VentilationEventType.EXTUBATION:
        dao.set_case_end_time(cursor, case.id, event_time)
        if event_time != event.timestamp:
            # Rebuild each interval, including coincident tube/end events.
            # Moving every closure at the old timestamp would also move an
            # earlier tube closed by a replacement at that same timestamp.
            case.end_time = event_time
            _reconcile_tubes(dao, cursor, case, original_events)
    else:
        dao.bump_case_revision(cursor, case.id)


def _reconcile_tubes(dao, cursor, case, original_events):
    structural = {VentilationEventType.START_VENT, VentilationEventType.TRACHEOSTOMY, VentilationEventType.TUBE_REPLACEMENT}
    old_events = [item for item in original_events if item.event_type in structural]
    cursor.execute("SELECT * FROM devices WHERE ivl_episode_id=? AND device_type IN ('ENDOTRACHEAL_TUBE','TRACHEOSTOMY_TUBE') ORDER BY DATETIME(insertion_date),id", (case.id,))
    old_tubes = cursor.fetchall()
    if len(old_tubes) != len(old_events):
        raise ValueError("История трубок не соответствует событиям ИВЛ. Изменение не сохранено.")
    by_event = {item.id: tube["id"] for item, tube in zip(old_events, old_tubes)}
    cursor.execute("SELECT * FROM clinical_events WHERE ivl_episode_id=? ORDER BY DATETIME(timestamp),id", (case.id,))
    events = [dao._map_event(row) for row in cursor.fetchall()]
    tube_events = [item for item in events if item.event_type in structural]
    retained = {item.id for item in tube_events}
    for event_id, tube_id in by_event.items():
        if event_id not in retained:
            dao.delete_tube(cursor, tube_id)
    for index, item in enumerate(tube_events):
        tube_id = by_event.get(item.id)
        if tube_id is None:
            tube_id = dao.insert_tube(cursor, admission_id=case.admission_id, case_id=case.id,
                                      insertion_time=item.timestamp, device_type="TRACHEOSTOMY_TUBE")
        removal = tube_events[index + 1].timestamp if index + 1 < len(tube_events) else case.end_time
        cursor.execute("UPDATE devices SET insertion_date=?, removal_date=?, replacement_time=? WHERE id=?",
                       (item.timestamp.isoformat(), removal.isoformat() if removal else None,
                        removal.isoformat() if removal else None, tube_id))
