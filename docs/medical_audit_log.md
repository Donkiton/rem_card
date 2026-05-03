# Медицинский audit log

`change_log` остается механизмом синхронизации. Для медицинского аудита добавлена отдельная таблица `medical_audit_log`.

## Что пишется

Триггеры фиксируют `insert/update/delete` для ключевых медицинских таблиц:

- `orders`;
- `administrations`;
- `vitals`;
- `fluids`;
- `admissions`;
- `beds`;
- `patient_status_events`;
- `ivl_episodes`;
- `clinical_events`;
- `diet_plan`;
- `oral_intake_events`.

## Поля

- `operation_id`;
- `table_name`;
- `row_id`;
- `admission_id`;
- `action_type`;
- `changed_at`;
- `changed_by`;
- `before_json`;
- `after_json`.

## Ограничения

Текущий audit-log отвечает на вопрос “какая строка изменилась и как выглядела до/после”. Для полноценного юридически значимого аудита следующим шагом нужно прокидывать в операции явные `operation_id`, роль и рабочее место из UI/session context.
