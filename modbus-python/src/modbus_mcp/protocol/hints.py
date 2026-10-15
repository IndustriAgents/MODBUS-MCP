from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True)
DEVICE_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
