"""
UTFCN — Use The F***ing Core Nodes.

A ComfyUI companion that nudges workflows back toward core nodes:

  1. Suggests a core (or otherwise-available) equivalent when you add a custom
     node that re-implements something ComfyUI already ships.
  2. Adds a "Replace custom nodes with core / available…" command + menu entry
     that scans the open graph and, after a preview, swaps in equivalents.
  3. Adds a right-click "Replace with core / available" item on individual nodes.

All of that is frontend behaviour (see web/utfcn.js).  This backend only serves
the *analysis*: it has the live node registry, so it computes — accurately, from
real INPUT_TYPES / RETURN_TYPES — which custom nodes have safe equivalents.

  GET  /utfcn/scan[?refresh=1]   -> { sources, candidates, stats }
  POST /utfcn/match  {nodes:[{type,inputs,outputs,output_names}]} -> { candidates }
                                   (for UNINSTALLED / missing nodes in a workflow)

Curated overrides live in mappings.json (shipped) and user_mappings.json (yours).
"""

import os

from aiohttp import web
from server import PromptServer

from . import utfcn_core

VERSION = "1.0.0"
_DIR = os.path.dirname(os.path.realpath(__file__))

# Snapshotting the registry is the expensive part, so we cache the context and
# the derived scan index, rebuilding only on ?refresh=1.
_CTX_CACHE = None
_INDEX_CACHE = None


def _get_ctx(refresh=False):
    global _CTX_CACHE
    if refresh or _CTX_CACHE is None:
        _CTX_CACHE = utfcn_core.build_context(utfcn_core.load_rules(_DIR))
    return _CTX_CACHE


def _get_index(refresh=False):
    global _INDEX_CACHE
    if refresh or _INDEX_CACHE is None:
        _INDEX_CACHE = utfcn_core.build_index(_get_ctx(refresh))
    return _INDEX_CACHE


routes = PromptServer.instance.routes


@routes.get("/utfcn/scan")
async def utfcn_scan(request):
    refresh = request.query.get("refresh") in ("1", "true", "yes")
    try:
        return web.json_response(_get_index(refresh))
    except Exception as e:
        # never let a scan failure break the editor — the frontend degrades gracefully
        print(f"[UTFCN] scan failed: {e}")
        return web.json_response({"sources": {}, "candidates": {}, "stats": {}, "error": str(e)}, status=500)


@routes.post("/utfcn/match")
async def utfcn_match(request):
    """Match uninstalled/missing nodes by their serialized signature."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"candidates": {}, "error": "invalid json"}, status=400)
    try:
        return web.json_response({"candidates": utfcn_core.match(_get_ctx(), data.get("nodes") or [])})
    except Exception as e:
        print(f"[UTFCN] match failed: {e}")
        return web.json_response({"candidates": {}, "error": str(e)}, status=500)


WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print(f"[UTFCN] loaded (v{VERSION}) — Use The F***ing Core Nodes")
