from __future__ import annotations


def build_procedure_create_button_style(object_name: str = "ProcedureCreateButton") -> str:
    return f"""
        QPushButton#{object_name} {{
            background: #eef3f8;
            color: #172033;
            border: 1px solid #aebccd;
            border-radius: 6px;
            padding: 6px 12px;
            min-height: 34px;
            font-weight: 700;
        }}
        QPushButton#{object_name}:hover {{
            background: #e2ebf5;
            border-color: #7aa6d8;
        }}
        QPushButton#{object_name}:pressed {{
            background: #d5e2ef;
            padding-top: 7px;
            padding-bottom: 5px;
        }}
    """
