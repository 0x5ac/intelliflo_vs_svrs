#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml", "websockets>=13"]
# ///
"""Add the example pump dashboard to a running Home Assistant.

The integration makes its own entities, so there is nothing to add to the
database: this finds them and builds a dashboard on them, all through the
websocket API, as the UI would.

dashboard.yaml, beside this script, names the entities by their default ids
(switch.pond_pump_power and so on). Home Assistant hands out another
id when the default is taken, and you may have renamed them, so each one
is looked up by its unique id and the dashboard rewritten to match.

    HA_TOKEN=<long-lived token> uv run examples/add_dashboard.py \\
        --url http://homeassistant.local:8123

Make the token on your user profile's Security tab. --dry-run prints the
dashboard and changes nothing. An existing dashboard is replaced only with
--force.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
import yaml

DOMAIN = "intelliflo"
EXAMPLE = Path(__file__).resolve().parent / "dashboard.yaml"
# A default entity id in the example, <platform>.pond_pump_<key>; the
# entity's unique id is <entry id>_<key>.
DEFAULT_ID = re.compile(r"\b(?:switch|number|sensor)\.pond_pump_([a-z]+)\b")


class HAError(Exception):
    pass


class Connection:
    """An authenticated websocket: send a command, wait for its result."""

    def __init__(self, ws: ClientConnection) -> None:
        self._ws = ws
        self._id = 0

    async def call(self, msg_type: str, **fields: Any) -> Any:
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "type": msg_type, **fields}))
        while True:
            reply = json.loads(await self._ws.recv())
            if reply.get("id") == self._id and reply["type"] == "result":
                if not reply["success"]:
                    raise HAError(f"{msg_type}: {reply['error']['message']}")
                return reply["result"]


async def entity_ids(conn: Connection, which: str | None) -> dict[str, str]:
    """The integration's entity ids by key: {"rpm": "sensor.pond_pump_rpm", ...}."""
    entries = [
        e
        for e in await conn.call("config_entries/get", domain=DOMAIN)
        if not e["disabled_by"] and which in (None, e["entry_id"], e["title"])
    ]
    if not entries:
        sys.exit(
            f"no enabled {DOMAIN} entry{f' called {which!r}' if which else ''}: "
            "add the integration under Settings > Devices & services first"
        )
    if len(entries) > 1:
        titles = ", ".join(repr(e["title"]) for e in entries)
        sys.exit(f"{len(entries)} {DOMAIN} entries ({titles}): pick one with --entry")
    entry_id = entries[0]["entry_id"]
    return {
        e["unique_id"].removeprefix(f"{entry_id}_"): e["entity_id"]
        for e in await conn.call("config/entity_registry/list")
        if e["platform"] == DOMAIN and e["config_entry_id"] == entry_id
    }


def dashboard(ids: dict[str, str]) -> dict[str, Any]:
    """The example, its default entity ids swapped for the real ones."""
    text = EXAMPLE.read_text()
    used = {m[0]: m[1] for m in DEFAULT_ID.finditer(text)}  # default id -> key
    missing = sorted(set(used.values()) - ids.keys())
    if missing:
        sys.exit(f"the integration has no entity for {', '.join(missing)}")
    for default, key in sorted(used.items()):
        if ids[key] != default:
            print(f"{default} is {ids[key]} here", file=sys.stderr)
    return yaml.safe_load(DEFAULT_ID.sub(lambda m: ids[m[1]], text))


async def install(
    conn: Connection, url_path: str, title: str, config: dict[str, Any], force: bool
) -> None:
    existing = {d["url_path"] for d in await conn.call("lovelace/dashboards/list")}
    if url_path not in existing:
        await conn.call(
            "lovelace/dashboards/create",
            url_path=url_path,
            title=title,
            icon="mdi:pump",
            show_in_sidebar=True,
        )
    elif not force:
        sys.exit(f"dashboard {url_path!r} already exists: --force replaces it")
    await conn.call("lovelace/config/save", url_path=url_path, config=config)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add the example pump dashboard to a running Home Assistant."
    )
    parser.add_argument("--url", default="http://homeassistant.local:8123")
    parser.add_argument(
        "--entry", help="config entry title or id, when there is more than one"
    )
    parser.add_argument(
        "--dashboard",
        default="intelliflo-pump",
        help="the dashboard's url path; Home Assistant wants a hyphen in it",
    )
    parser.add_argument("--title", default="pond pump", help="sidebar title")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = os.environ.get("HA_TOKEN") or sys.exit(
        "set HA_TOKEN to a long-lived access token"
    )
    ws_url = re.sub(r"^http", "ws", args.url.rstrip("/")) + "/api/websocket"
    # The entity registry list runs to megabytes on a big install.
    async with connect(ws_url, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        if json.loads(await ws.recv())["type"] != "auth_ok":
            sys.exit("Home Assistant refused the token")
        conn = Connection(ws)
        try:
            config = dashboard(await entity_ids(conn, args.entry))
            if args.dry_run:
                print(yaml.safe_dump(config, sort_keys=False), end="")
                return
            await install(conn, args.dashboard, args.title, config, args.force)
        except HAError as e:
            sys.exit(str(e))
    print(f"added {args.url.rstrip('/')}/{args.dashboard}")


if __name__ == "__main__":
    asyncio.run(main())
