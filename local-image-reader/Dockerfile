FROM python:3.12-slim

# Reads a local image file and returns it as base64 ImageContent so a
# vision-capable client model can look at it. Single tool: read_image
# (PNG / JPEG / GIF / WebP / BMP / SVG).
#
# This is the containerised form of the `mcp-local-image-reader` host MCP
# that AiderDesk used to launch with uvx. The project trees are bind-mounted
# read-only at their original paths (see catalog/custom.yaml) so a path the
# agent already knows resolves unchanged inside the container.
#
# Pinned: the upstream package is a thin, single-tool server; pin to keep the
# image reproducible.
#
# `mcp<2` is required: server.py uses the low-level Server.list_tools()
# decorator, which the MCP Python SDK dropped in 2.x (the package only declares
# mcp>=1.0.0, so pip would otherwise resolve the incompatible 2.x). Same guard
# as the FlipperAgent MCP.
RUN pip install --no-cache-dir "mcp-local-image-reader==0.1.1" "mcp<2"

ENV PYTHONUNBUFFERED=1

# Speaks MCP over stdin/stdout; the gateway owns the process lifecycle.
ENTRYPOINT ["mcp-local-image-reader"]
