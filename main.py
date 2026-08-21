"""AstrBot 全状态聚合插件 - 本机状态监控。

命令：
    /status   → 渲染本机状态卡片图片

设计：
- 采集走 psutil (core/collector.py)
- 渲染走本地 playwright (core/render.py)，字体由模板中的 CDN 加载
- 渲染失败自动回退纯文本
"""
import asyncio
import datetime
import json
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PlatformAdapterType
from astrbot.api.star import Context, Star, register

from .core.collector import get_status_data, format_status_text
from .core.mcstatus import format_server_status, query_server
from .core.render import get_renderer

try:
    from astrbot.api.star import StarTools
    _HAS_STAR_TOOLS = True
except ImportError:
    _HAS_STAR_TOOLS = False


def _uptime_units(seconds: int) -> list[dict]:
    """把秒数拆成 3 个 (值, 单位) 用于 UPTIME 网格，自动选最大三档。"""
    total = max(0, int(seconds))
    months = total // (86400 * 30)
    days = (total % (86400 * 30)) // 86400
    hours = (total % 86400) // 3600
    minutes = (total % 3600) // 60
    units = [
        {"value": months, "label": "MO"},
        {"value": days, "label": "D"},
        {"value": hours, "label": "H"},
        {"value": minutes, "label": "M"},
    ]
    start = 0
    for i, u in enumerate(units):
        if u["value"] > 0:
            start = i
            break
    if start + 3 > len(units):
        start = len(units) - 3
    return units[start:start + 3]


@register(
    "astrbot_plugin_allstatus",
    "rishu",
    "本机状态监控：CPU/内存/磁盘/温度，playwright 本地渲染图片",
    "0.1.0",
)
class AllStatusPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}

        # 插件数据目录（存渲染出的临时图片）
        self._data_dir = None
        if _HAS_STAR_TOOLS:
            try:
                self._data_dir = str(StarTools.get_data_dir(self.name))
                os.makedirs(self._data_dir, exist_ok=True)
            except Exception as e:
                logger.warning(f"获取插件数据目录失败，将使用临时目录: {e}")

        self._renderer = get_renderer()
        render_cfg = self.config.get("render", {})
        if not isinstance(render_cfg, dict):
            render_cfg = {}
        try:
            self._renderer.configure(
                render_cfg.get("page_timeout_seconds", 10),
                render_cfg.get("font_timeout_seconds", 3),
                render_cfg.get("font_css_url", ""),
            )
        except (TypeError, ValueError):
            logger.warning("渲染超时配置无效，使用默认值 10 秒 / 3 秒。")
            self._renderer.configure()
        self._minecraft_monitor_task: asyncio.Task | None = None
        self._minecraft_monitor_state: dict[tuple[str, str], bool] = {}
        self._minecraft_monitor_failures: dict[tuple[str, str], int] = {}
        self._minecraft_history: dict[tuple[str, str], list[dict[str, int]]] = {}

    async def initialize(self):
        """插件加载时启动 playwright browser（长驻，避免每次截图重启）。"""
        try:
            await self._renderer.start()
            logger.info("AllStatus 渲染器已就绪。")
        except Exception as e:
            logger.error(f"AllStatus 渲染器启动失败，将回退纯文本: {e}")

        if self._minecraft_monitor_enabled():
            self._minecraft_monitor_task = asyncio.create_task(
                self._minecraft_monitor_loop()
            )
            logger.info("Minecraft 服务器监控已启动。")

    async def terminate(self):
        """插件卸载时关闭 browser。"""
        if self._minecraft_monitor_task:
            self._minecraft_monitor_task.cancel()
            try:
                await self._minecraft_monitor_task
            except asyncio.CancelledError:
                pass
            self._minecraft_monitor_task = None
        try:
            await self._renderer.stop()
        except Exception as e:
            logger.warning(f"关闭渲染器时出错: {e}")

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

    @staticmethod
    def _chart_points(samples: list[dict[str, int]], field: str) -> str:
        """将样本归一化为 SVG 折线坐标。"""
        values = [sample[field] for sample in samples]
        if len(values) < 2:
            return ""
        low, high = min(values), max(values)
        span = high - low or 1
        count = len(values) - 1
        return " ".join(
            f"{index / count * 100:.1f},{90 - (value - low) / span * 80:.1f}"
            for index, value in enumerate(values)
        )

    def _minecraft_charts(self, key: tuple[str, str]) -> dict[str, str | bool]:
        samples = self._minecraft_history.get(key, [])
        player_values = [sample["players"] for sample in samples]
        latency_values = [sample["latency"] for sample in samples]
        return {
            "has_history": len(samples) >= 2,
            "player_points": self._chart_points(samples, "players"),
            "latency_points": self._chart_points(samples, "latency"),
            "latest_players": str(samples[-1]["players"]) if samples else "--",
            "latest_latency": str(samples[-1]["latency"]) if samples else "--",
            "players_max": str(max(player_values)) if player_values else "--",
            "players_min": str(min(player_values)) if player_values else "--",
            "latency_max": str(max(latency_values)) if latency_values else "--",
            "latency_min": str(min(latency_values)) if latency_values else "--",
            "start_time": str(samples[0].get("time", "--")) if samples else "--",
            "end_time": str(samples[-1].get("time", "--")) if samples else "--",
        }

    def _output_path(self) -> str:
        import tempfile
        d = self._data_dir or tempfile.gettempdir()
        return os.path.join(d, "allstatus_status.png")

    @filter.command("status")
    async def status(self, event: AstrMessageEvent):
        """获取本机系统状态（CPU/内存/磁盘/温度），渲染成图片发送。"""
        event.stop_event()

        cfg = self._monitor_cfg()
        disk_show_only = cfg.get("disk_show_only", []) or []
        # None 表示使用默认过滤；显式传入 [] 表示关闭过滤。
        disk_exclude = cfg.get("disk_filter", ["boot", "efi", "swap"])
        cpu_cores = cfg.get("cpu_cores", "") or ""
        memory_info = cfg.get("memory_info", "") or ""

        # 采集（psutil 的 cpu_percent(interval=1) 是阻塞调用，丢线程池）
        import asyncio
        data = await asyncio.to_thread(
            get_status_data,
            disk_show_only=disk_show_only,
            disk_exclude=disk_exclude,
            cpu_cores=cpu_cores,
            memory_info=memory_info,
        )
        data["uptime_units"] = _uptime_units(data["system"]["uptime_seconds"])

        # 尝试图片渲染
        try:
            out = self._output_path()
            await self._renderer.render(
                template_name="status_template.html",
                data=data,
                output_path=out,
                width=760,
            )
            if os.path.exists(out):
                yield event.image_result(out)
                return
            # 渲染返回但文件不存在，走回退
            logger.warning("渲染未生成图片文件，回退纯文本。")
        except Exception as e:
            logger.error(f"状态图片渲染失败，回退纯文本: {e}")

        # 回退：纯文本
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
            data["player_percent"] = min(
                100,
                round(data["online"] / max(data["maximum"], 1) * 100, 1),
            )
            data["player_text"] = ", ".join(data["players"]) or "暂无玩家在线"
        render_data = {
            "server": data,
            "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            out = self._output_path().replace("allstatus_status.png", "allstatus_mcs.png")
            await self._renderer.render(
                template_name="mcstatus_template.html",
                data=render_data,
                output_path=out,
                width=760,
            )
            if os.path.exists(out):
                yield event.image_result(out)
                return
        except Exception as e:
            logger.error(f"Minecraft 状态图片渲染失败，回退纯文本: {e}")

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
        try:
            out = self._output_path().replace(
                "allstatus_status.png", "allstatus_mcsc.png"
            )
            await self._renderer.render(
                template_name="mcstatus_chart_template.html",
                data={
                    "server": {"name": "Minecraft Server", "address": server_addr},
                    "charts": self._minecraft_charts((str(group_id), server_addr)),
                    "generated_at": datetime.datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                },
                output_path=out,
                width=760,
            )
            if os.path.exists(out):
                yield event.image_result(out)
                return
        except Exception as e:
            logger.error(f"Minecraft 监控图表渲染失败: {e}")
        yield event.plain_result("监控图表渲染失败。")
