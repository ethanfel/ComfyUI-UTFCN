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

  GET /utfcn/scan[?refresh=1]   -> { sources, candidates, stats }

Curated overrides live in mappings.json (shipped) and user_mappings.json (yours).
"""

import os

from aiohttp import web
from server import PromptServer

from . import utfcn_core

VERSION = "1.0.0"
_DIR = os.path.dirname(os.path.realpath(__file__))

# The scan walks the whole registry, so we cache it and only rebuild on demand.
_INDEX_CACHE = None


def _get_index(refresh=False):
    global _INDEX_CACHE
    if refresh or _INDEX_CACHE is None:
        rules = utfcn_core.load_rules(_DIR)
        _INDEX_CACHE = utfcn_core.build_index(rules)
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


WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

print(f"[UTFCN] loaded (v{VERSION}) — Use The F***ing Core Nodes")
