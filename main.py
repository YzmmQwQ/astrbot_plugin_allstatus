"""AstrBot 全状态聚合插件 - 纯文字状态查询。"""
import asyncio
import datetime
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PlatformAdapterType
from astrbot.api.star import Context, Star, register

from .core.collector import get_status_data, format_status_text
from .core.mcstatus import format_server_status, query_server

@register(
    "astrbot_plugin_allstatus",
    "rishu",
    "本机和 Minecraft 服务器状态查询，纯文字输出",
    "0.1.0",
)
class AllStatusPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

        self._minecraft_monitor_task: asyncio.Task | None = None
        self._minecraft_monitor_state: dict[tuple[str, str], bool] = {}
        self._minecraft_monitor_failures: dict[tuple[str, str], int] = {}
        self._minecraft_history: dict[tuple[str, str], list[dict[str, int]]] = {}

    async def initialize(self):
        if self._minecraft_monitor_enabled():
            self._minecraft_monitor_task = asyncio.create_task(
                self._minecraft_monitor_loop()
            )
            logger.info("Minecraft 服务器监控已启动。")

    async def terminate(self):
        if self._minecraft_monitor_task:
            self._minecraft_monitor_task.cancel()
            try:
                await self._minecraft_monitor_task
            except asyncio.CancelledError:
                pass
            self._minecraft_monitor_task = None

    def _monitor_cfg(self) -> dict:
        """读取 monitor 配置段，容错缺字段。"""
        cfg = self.config.get("monitor", {}) if isinstance(self.config, dict) else {}
        if not isinstance(cfg, dict):
            return {}
        return cfg

    def _group_default_server(self, group_id: str | None) -> str:
        """从 JSON 配置中读取当前群的默认 Minecraft 服务器地址。"""
        if not group_id or not isinstance(self.config, dict):
            return ""
        minecraft = self.config.get("minecraft", {})
        if not isinstance(minecraft, dict):
            return ""
        raw_mapping = minecraft.get("group_default_servers", "{}")
        if isinstance(raw_mapping, dict):
            mapping = raw_mapping
        elif isinstance(raw_mapping, str):
            try:
                mapping = json.loads(raw_mapping)
            except json.JSONDecodeError:
                logger.warning("Minecraft 群默认服务器映射不是有效 JSON。")
                return ""
        else:
            return ""
        if not isinstance(mapping, dict):
            logger.warning("Minecraft 群默认服务器映射必须是 JSON 对象。")
            return ""
        address = mapping.get(str(group_id), "")
        return address.strip() if isinstance(address, str) else ""

    def _minecraft_cfg(self) -> dict:
        cfg = self.config.get("minecraft", {}) if isinstance(self.config, dict) else {}
        return cfg if isinstance(cfg, dict) else {}

    def _group_default_servers(self) -> dict[str, str]:
        """返回经过校验的群号到服务器地址映射。"""
        raw_mapping = self._minecraft_cfg().get("group_default_servers", "{}")
        if isinstance(raw_mapping, str):
            try:
                mapping = json.loads(raw_mapping)
            except json.JSONDecodeError:
                logger.warning("Minecraft 群默认服务器映射不是有效 JSON。")
                return {}
        elif isinstance(raw_mapping, dict):
            mapping = raw_mapping
        else:
            return {}
        if not isinstance(mapping, dict):
            logger.warning("Minecraft 群默认服务器映射必须是 JSON 对象。")
            return {}
        return {
            str(group_id): address.strip()
            for group_id, address in mapping.items()
            if str(group_id).isdigit() and isinstance(address, str) and address.strip()
        }

    def _minecraft_monitor_enabled(self) -> bool:
        return bool(self._minecraft_cfg().get("enable_monitor", False))

    def _minecraft_monitor_interval(self) -> int:
        interval = self._minecraft_cfg().get("monitor_interval_seconds", 60)
        try:
            return max(30, int(interval))
        except (TypeError, ValueError):
            return 60

    async def _send_monitor_message(self, group_id: str, message: str) -> None:
        platform = self.context.get_platform(PlatformAdapterType.AIOCQHTTP)
        if platform is None:
            logger.error("未找到 AIOCQHTTP 平台适配器，无法发送 Minecraft 监控通知。")
            return
        try:
            await platform.get_client().api.call_action(
                "send_group_msg",
                group_id=int(group_id),
                message=message,
            )
        except Exception as e:
            logger.error(
                f"发送 Minecraft 监控通知到群 {group_id} 失败: "
                f"{type(e).__name__}: {e!r}"
            )

    async def _minecraft_monitor_loop(self) -> None:
        """监控各群默认服务器，仅在离线或恢复时推送。"""
        while True:
            try:
                servers = self._group_default_servers()
                active_keys = {(group_id, address) for group_id, address in servers.items()}
                self._minecraft_monitor_state = {
                    key: value
                    for key, value in self._minecraft_monitor_state.items()
                    if key in active_keys
                }
                self._minecraft_monitor_failures = {
                    key: value
                    for key, value in self._minecraft_monitor_failures.items()
                    if key in active_keys
                }
                self._minecraft_history = {
                    key: value
                    for key, value in self._minecraft_history.items()
                    if key in active_keys
                }

                for group_id, address in servers.items():
                    key = (group_id, address)
                    try:
                        data = await query_server(address)
                    except Exception as e:
                        failures = self._minecraft_monitor_failures.get(key, 0) + 1
                        self._minecraft_monitor_failures[key] = failures
                        if failures < 2:
                            logger.warning(
                                f"Minecraft 监控查询失败 ({address})，"
                                f"将在下次确认: {type(e).__name__}: {e!r}"
                            )
                            continue
                        online = False
                    else:
                        self._minecraft_monitor_failures[key] = 0
                        online = True
                        self._record_minecraft_sample(key, data)

                    previous = self._minecraft_monitor_state.get(key)
                    self._minecraft_monitor_state[key] = online
                    if previous is None or previous == online:
                        continue
                    if online:
                        message = (
                            f"Minecraft 服务器已恢复在线\n"
                            f"地址: {address}\n"
                            f"玩家: {data['online']} / {data['maximum']}"
                        )
                    else:
                        message = f"Minecraft 服务器已离线\n地址: {address}"
                    await self._send_monitor_message(group_id, message)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"Minecraft 服务器监控循环异常: {type(e).__name__}: {e!r}"
                )
            await asyncio.sleep(self._minecraft_monitor_interval())

    def _record_minecraft_sample(
        self, key: tuple[str, str], data: dict
    ) -> None:
        """保留最近 30 个成功查询的人数和延迟样本。"""
        samples = self._minecraft_history.setdefault(key, [])
        samples.append(
            {
                "players": int(data["online"]),
                "latency": int(data["latency"]),
                "time": datetime.datetime.now().strftime("%H:%M"),
            }
        )
        del samples[:-30]

    def _minecraft_charts(self, key: tuple[str, str]) -> dict[str, str | bool]:
        samples = self._minecraft_history.get(key, [])
        player_values = [sample["players"] for sample in samples]
        latency_values = [sample["latency"] for sample in samples]
        return {
            "has_history": len(samples) >= 2,
            "latest_players": str(samples[-1]["players"]) if samples else "--",
            "latest_latency": str(samples[-1]["latency"]) if samples else "--",
            "players_max": str(max(player_values)) if player_values else "--",
            "players_min": str(min(player_values)) if player_values else "--",
            "latency_max": str(max(latency_values)) if latency_values else "--",
            "latency_min": str(min(latency_values)) if latency_values else "--",
            "start_time": str(samples[0].get("time", "--")) if samples else "--",
            "end_time": str(samples[-1].get("time", "--")) if samples else "--",
        }

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        """获取本机系统状态。"""
        event.stop_event()

        cfg = self._monitor_cfg()
        disk_show_only = cfg.get("disk_show_only", []) or []
        # None 表示使用默认过滤；显式传入 [] 表示关闭过滤。
        disk_exclude = cfg.get("disk_filter", ["boot", "efi", "swap"])
        cpu_cores = cfg.get("cpu_cores", "") or ""

        # 采集（psutil 的 cpu_percent(interval=1) 是阻塞调用，丢线程池）
        import asyncio
        data = await asyncio.to_thread(
            get_status_data,
            disk_show_only=disk_show_only,
            disk_exclude=disk_exclude,
            cpu_cores=cpu_cores,
        )

        yield event.plain_result(format_status_text(data))

    @filter.command("mcs")
    async def mcs(self, event: AstrMessageEvent, server_addr: str = ""):
        """查询 Minecraft Java 服务器状态。"""
        event.stop_event()
        if not server_addr.strip():
            server_addr = self._group_default_server(event.get_group_id())
            if not server_addr:
                yield event.plain_result(
                    "当前群未配置 Minecraft 服务器。\n"
                    "用法：/mcs [服务器地址]"
                )
                return
        try:
            data = await query_server(server_addr)
        except Exception as e:
            logger.warning(
                f"Minecraft 服务器查询失败 ({server_addr.strip()}): "
                f"{type(e).__name__}: {e!r}"
            )
            data = {
                "address": server_addr.strip(),
                "motd": "Minecraft Server",
                "online_state": False,
                "error": "无法连接服务器，请检查地址、端口和服务器是否在线。",
            }
        else:
            data["online_state"] = True

        if data["online_state"]:
            yield event.plain_result(format_server_status(data))
        else:
            yield event.plain_result(
                f"无法连接 Minecraft 服务器：{data['address']}\n{data['error']}"
            )

    @filter.command("mcsc")
    async def mcsc(self, event: AstrMessageEvent, command_text: str = ""):
        """显示本群已绑定 Minecraft 服务器的监控历史。"""
        event.stop_event()
        if command_text.strip():
            yield event.plain_result("/mcsc 不接受参数，只能查询本群已配置的服务器。")
            return
        group_id = event.get_group_id()
        server_addr = self._group_default_server(group_id)
        if not server_addr:
            yield event.plain_result("当前群未配置 Minecraft 服务器。")
            return
        charts = self._minecraft_charts((str(group_id), server_addr))
        if not charts["has_history"]:
            yield event.plain_result(
                f"Minecraft 服务器监控\n地址: {server_addr}\n"
                "有效样本不足，至少需要两次监控数据。"
            )
            return
        yield event.plain_result(
            f"Minecraft 服务器监控\n地址: {server_addr}\n"
            f"采样时间: {charts['start_time']} - {charts['end_time']}\n"
            f"在线人数: 当前 {charts['latest_players']}，范围 {charts['players_min']} - {charts['players_max']}\n"
            f"延迟: 当前 {charts['latest_latency']} ms，范围 {charts['latency_min']} - {charts['latency_max']} ms"
        )
