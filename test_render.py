"""独立测试脚本：脱离 AstrBot 验证采集 + 渲染出图。

在 AstrBot venv 下运行：
    cd F:/rishu.cfd/plugins/astrbot_plugin_allstatus
    uv run --project F:/rishu.cfd/AstrBot python test_render.py
"""
import asyncio
import os
import sys
import tempfile

# 让脚本能 import core.* 且不依赖 astrbot 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# render.py 里 `from astrbot.api import logger` 会失败，做个假 logger
import types
astrbot_mod = types.ModuleType("astrbot")
astrbot_api_mod = types.ModuleType("astrbot.api")
class _Logger:
    def info(self, *a, **k): print("[INFO]", *a)
    def warning(self, *a, **k): print("[WARN]", *a)
    def error(self, *a, **k): print("[ERR]", *a)
astrbot_api_mod.logger = _Logger()
astrbot_mod.api = astrbot_api_mod
sys.modules["astrbot"] = astrbot_mod
sys.modules["astrbot.api"] = astrbot_api_mod

from core.collector import get_status_data
from core.render import get_renderer


def _uptime_units(seconds: int) -> list[dict]:
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


async def main():
    print("采集系统状态...")
    data = get_status_data(
        cpu_cores="8:12",          # 测试大小核: 8P+12E
        memory_info="DDR5 32GBx2通道 5600MT/s",
    )
    data["uptime_units"] = _uptime_units(data["system"]["uptime_seconds"])
    print(f"  CPU: {data['cpu']['percent']}% ({data['cpu']['cores_text']})")
    print(f"  内存: {data['memory']['used_human']} / {data['memory']['total_human']} (info={data['memory']['info']!r})")
    print(f"  磁盘: {len(data['disks'])} 个")
    print(f"  负载: {data['load']}")

    print("\n启动渲染器并截图...")
    r = get_renderer()
    await r.start()
    try:
        out = os.path.join(tempfile.gettempdir(), "allstatus_test.png")
        await r.render(
            template_name="status_template.html",
            data=data,
            output_path=out,
            width=760,
        )
        print(f"\n[OK] 截图已保存: {out}")
        print(f"     大小: {os.path.getsize(out) / 1024:.1f} KB")
        # Windows 下自动打开
        try:
            os.startfile(out)
        except Exception:
            pass
    finally:
        await r.stop()


if __name__ == "__main__":
    asyncio.run(main())
