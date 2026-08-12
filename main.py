"""AstrBot 全状态聚合插件 - 本机状态监控。

命令：
    /status   → 渲染本机状态卡片图片

设计：
- 采集走 psutil (core/collector.py)
- 渲染走本地 playwright (core/render.py)，字体由模板中的 CDN 加载
- 渲染失败自动回退纯文本
"""
import os

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.collector import get_status_data, format_status_text
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

    async def initialize(self):
        """插件加载时启动 playwright browser（长驻，避免每次截图重启）。"""
        try:
            await self._renderer.start()
            logger.info("AllStatus 渲染器已就绪。")
        except Exception as e:
            logger.error(f"AllStatus 渲染器启动失败，将回退纯文本: {e}")

    async def terminate(self):
        """插件卸载时关闭 browser。"""
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
