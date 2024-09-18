from __future__ import annotations

__all__ = [
    "BinaryFieldTypeError",
    "BinaryFieldValueError",
]

import dataclasses


class BinaryFieldTypeError(Exception):
    """Exception raised at the class level due to an invalid or incorrectly specified field."""

    def __init__(self, field: dataclasses.Field, cls_name: str, error_msg: str):
        name = f"`{cls_name}.{field.name}`" if cls_name else f"`{field.name}`"
        super().__init__(f"Field {name}: {error_msg}")


class BinaryFieldValueError(Exception):
    """Exception raised at the instance level due to an invalid field value."""
    pass
