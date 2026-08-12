"""Minecraft Java 服务器状态查询。"""

from __future__ import annotations

import re
from typing import Any

from mcstatus import JavaServer


def _clean_motd(value: Any) -> str:
    """移除 Minecraft MOTD 中的样式控制码。"""
    text = value if isinstance(value, str) else str(value)
    return re.sub(r"§[0-9a-fk-orx]", "", text, flags=re.IGNORECASE)


async def query_server(address: str, timeout: float = 3) -> dict[str, Any]:
    """查询服务器状态，失败时抛出异常交给命令入口处理。"""
    address = address.strip()
    if not address:
        raise ValueError("服务器地址不能为空")

    server = await JavaServer.async_lookup(address, timeout=timeout)
    status = await server.async_status()
    description = getattr(status, "description", "未知 MOTD")
    motd = _clean_motd(description)
    players = getattr(status.players, "sample", None) or []

    return {
        "address": address,
        "motd": motd.replace("\n", " ").strip() or "未知 MOTD",
        "version": getattr(status.version, "name", "未知"),
        "protocol": getattr(status.version, "protocol", "未知"),
        "online": status.players.online,
        "maximum": status.players.max,
        "latency": round(status.latency),
        "players": [getattr(player, "name", str(player)) for player in players],
    }


def format_server_status(data: dict[str, Any]) -> str:
    players = data["players"]
    player_text = ", ".join(players) if players else "暂无玩家在线"
    return (
        f"Minecraft 服务器状态\n"
        f"地址: {data['address']}\n"
        f"状态: 在线\n"
        f"MOTD: {data['motd']}\n"
        f"版本: {data['version']} (协议 {data['protocol']})\n"
        f"延迟: {data['latency']} ms\n"
        f"玩家: {data['online']} / {data['maximum']}\n"
        f"在线列表: {player_text}"
    )
