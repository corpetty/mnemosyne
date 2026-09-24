"""Base class for models that appear in API responses."""

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    # Fields with defaults are always present in responses; publish them as required
    # so the generated TypeScript types are not needlessly optional.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
