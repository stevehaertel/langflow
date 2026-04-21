from typing import Annotated

from pydantic import BaseModel, Discriminator, Field, Tag, field_serializer, field_validator
from typing_extensions import TypedDict

from .content_types import CodeContent, ErrorContent, JSONContent, MediaContent, TextContent, ToolContent


def _get_type(d: dict | BaseModel) -> str | None:
    if isinstance(d, dict):
        return d.get("type")
    return getattr(d, "type", None)


# Create a union type of all content types
ContentType = Annotated[
    Annotated[ToolContent, Tag("tool_use")]
    | Annotated[ErrorContent, Tag("error")]
    | Annotated[TextContent, Tag("text")]
    | Annotated[MediaContent, Tag("media")]
    | Annotated[CodeContent, Tag("code")]
    | Annotated[JSONContent, Tag("json")],
    Discriminator(_get_type),
]


class ContentBlock(BaseModel):
    """A block of content that can contain different types of content and nested blocks."""

    title: str
    contents: list[ContentType]
    allow_markdown: bool = Field(default=True)
    media_url: list[str] | None = None

    # NEW FIELDS for nested structure support
    nested_blocks: list['ContentBlock'] = Field(
        default_factory=list,
        description="Nested content blocks for hierarchical display"
    )
    block_type: str = Field(
        default="default",
        description="Semantic type: 'tool_call', 'child_progress', 'child_tool', 'child_result'"
    )
    is_expandable: bool = Field(
        default=True,
        description="UI hint for expandability"
    )
    is_expanded: bool = Field(
        default=False,
        description="Default expansion state (computed by smart logic)"
    )
    nesting_depth: int = Field(
        default=0,
        description="Current nesting level (0 = top level, max 3)"
    )

    def __init__(self, **data) -> None:
        super().__init__(**data)
        schema_dict = self.__pydantic_core_schema__["schema"]
        fields = None
        if "fields" in schema_dict:
            fields = schema_dict["fields"]
        elif "schema" in schema_dict:
            fields = schema_dict["schema"]["fields"]

        # Only update model_fields_set if fields were found
        # IMPORTANT: Don't add nested_blocks to model_fields_set unless it was explicitly provided
        # This ensures the field_serializer is called when the field has content
        if fields:
            fields_with_default = (
                f for f, d in fields.items()
                if "default" in d["schema"] and f != "nested_blocks"
            )
            self.model_fields_set.update(fields_with_default)

        # If nested_blocks was actually provided in data, add it to model_fields_set
        if "nested_blocks" in data:
            self.model_fields_set.add("nested_blocks")

    @field_validator("nested_blocks", mode="before")
    @classmethod
    def validate_nested_blocks(cls, v) -> list['ContentBlock']:
        """Validate and convert nested_blocks."""
        if not v:
            return []
        if isinstance(v, list):
            return [
                cls.model_validate(block) if isinstance(block, dict)
                else block for block in v
            ]
        return []

    @field_validator("nesting_depth", mode="before")
    @classmethod
    def validate_nesting_depth(cls, v) -> int:
        """Validate and cap nesting depth at maximum of 3."""
        MAX_DEPTH = 3
        if isinstance(v, int) and v > MAX_DEPTH:
            from lfx.log.logger import logger
            logger.warning(f"Nesting depth {v} exceeds maximum {MAX_DEPTH}, capping")
            return MAX_DEPTH
        return v if isinstance(v, int) else 0

    @field_serializer("nested_blocks")
    def serialize_nested_blocks(self, nested_blocks: list['ContentBlock'], _info) -> list[dict]:
        """Ensure nested_blocks are properly serialized to dict format."""
        from lfx.log.logger import logger
        result = [block.model_dump(mode='json') for block in nested_blocks]
        if nested_blocks:
            logger.debug(
                f"[CONTENT_BLOCK SERIALIZER] Serializing nested_blocks: "
                f"count={len(nested_blocks)}, "
                f"parent_title={self.title}, "
                f"block_types={[b.block_type for b in nested_blocks]}"
            )
        return result

    @field_validator("contents", mode="before")
    @classmethod
    def validate_contents(cls, v) -> list[ContentType]:
        if isinstance(v, dict):
            msg = "Contents must be a list of ContentTypes"
            raise TypeError(msg)
        return [v] if isinstance(v, BaseModel) else v

    @field_serializer("contents")
    def serialize_contents(self, value) -> list[dict]:
        return [v.model_dump() for v in value]


class ContentBlockDict(TypedDict):
    title: str
    contents: list[dict]
    allow_markdown: bool
    media_url: list[str] | None
    nested_blocks: list[dict]
    block_type: str
    is_expandable: bool
    is_expanded: bool
    nesting_depth: int


# Rebuild model to handle forward references for nested_blocks
ContentBlock.model_rebuild()
