"""Serialization mixin shared by response models and the error envelope."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import GetJsonSchemaHandler, model_serializer

if TYPE_CHECKING:
    from pydantic.json_schema import JsonSchemaValue
    from pydantic_core import core_schema as _cs


class CompactDumpMixin:
    """Drop None-valued fields at serialization.

    Pydantic re-adds declared Optional fields with None defaults after
    `model_validate`, so pruning dicts before validation is not enough — the
    strip has to happen at dump time. Fields where null is semantically
    meaningful opt out via `_keep_null`.

    Schema transparency: Pydantic's mode="serialization" JSON-schema path
    normally collapses a model that carries a `model_serializer` into
    `{additionalProperties: true}` because it derives the schema from the
    serializer's declared return type (`dict[str, Any]`). We override
    `__get_pydantic_json_schema__` to strip the serialization annotation from
    the core-schema before the handler inspects it, so FastMCP (which calls
    `TypeAdapter.json_schema(mode="serialization")`) still sees the full field
    schema. The actual null-stripping still happens at call time via the
    `model_serializer` hook on `pydantic_core.to_jsonable_python`.
    """

    _keep_null: ClassVar[frozenset[str]] = frozenset()

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        """Return the field-based schema regardless of serialization mode.

        Removes the 'serialization' key from the core schema before delegating
        to the standard handler so Pydantic derives the JSON schema from the
        model's declared fields instead of from our serializer's return type.
        """
        if "serialization" in core_schema:
            # Strip the custom serializer annotation so that Pydantic derives
            # the JSON schema from the model's declared fields rather than from
            # the serializer's dict[str, Any] return type.  We cast back to the
            # expected CoreSchemaOrField type to satisfy the type checker.
            stripped: dict[str, Any] = {
                k: v for k, v in core_schema.items() if k != "serialization"
            }
            core_schema = cast("_cs.ModelSchema", stripped)
        return handler(core_schema)

    @model_serializer(mode="wrap")
    def _drop_nulls(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        keep = type(self)._keep_null
        return {k: v for k, v in data.items() if v is not None or k in keep}
