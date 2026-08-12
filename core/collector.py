"""本机系统状态采集器。基于 astrbot_plugin_status 的 collector 扩展：
- 增加 CPU 单核占用 (coresLoad) 与每行格数 (perRow)，用于渲染 CPU 核心方阵
- 增加系统负载 load (1/5/15 分钟，Linux/Windows 均尽力采集)
- 增加磁盘黑白名单过滤
"""
import datetime
import os
import platform

import psutil


def get_cpu_name() -> str:
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            )
            processor_name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
            winreg.CloseKey(key)
            return processor_name.strip()
        except Exception:
            pass
    elif platform.system() == "Linux":
        try:
            with open("/proc/cpuinfo", "r", encoding="utf-8") as file:
                for line in file:
                    if "model name" in line:
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or "Unknown CPU"


def get_cpu_temp() -> float | None:
    try:
        temperatures = getattr(psutil, "sensors_temperatures", lambda: {})()
        for name in ("coretemp", "cpu_thermal", "k10temp", "zenpower"):
            if name not in temperatures:
                continue
            entries = temperatures[name]
            for entry in entries:
                if "Package" in (entry.label or ""):
                    return entry.current
            if entries:
                return entries[0].current
    except Exception:
        pass
    return None


def get_cpu_freq() -> float | None:
    """当前主频 (GHz)，取不到返回 None"""
    try:
        freq = psutil.cpu_freq()
        if freq and freq.current:
            return round(freq.current / 1000, 2)
    except Exception:
        pass
    return None


def get_loadavg() -> list[float]:
    """系统负载 1/5/15。psutil.getloadavg 在 Windows 上从 5.9 起也可用(返回 0,0,0 时容错)"""
    try:
        load = os.getloadavg()
        if load and any(load):
            return [round(x, 2) for x in load]
    except (OSError, AttributeError):
        pass
    try:
        load = psutil.getloadavg()
        if load and any(load):
            return [round(x, 2) for x in load]
    except Exception:
        pass
    return [0.0, 0.0, 0.0]


def bytes_to_gb(value: int) -> float:
    return round(value / (1024**3), 2)


def bytes_to_human(value: int) -> str:
    """人类可读内存，如 18.5 GB / 480 MB"""
    if value is None:
        return "--"
    k = 1024
    sizes = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(value)
    while v >= k and i < len(sizes) - 1:
        v /= k
        i += 1
    return f"{round(v, 1)} {sizes[i]}"


def format_uptime(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分")
    return "".join(parts)


def _match_any(text: str, keywords: list[str]) -> bool:
    if not keywords:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _filter_disks(
    disks: list[dict], show_only: list[str], exclude: list[str]
) -> list[dict]:
    result = []
    for disk in disks:
        hay = f"{disk['device']} {disk['mountpoint']}"
        if show_only:
            if not _match_any(hay, show_only):
                continue
        else:
            if _match_any(hay, exclude):
                continue
        result.append(disk)
    return result


def get_status_data(
    disk_show_only: list[str] | None = None,
    disk_exclude: list[str] | None = None,
    cpu_cores: str | None = None,
    memory_info: str | None = None,
) -> dict:
    memory = psutil.virtual_memory()
    boot_ts = psutil.boot_time()
    boot_time = datetime.datetime.fromtimestamp(boot_ts)
    logical_cpus = psutil.cpu_count(logical=True) or 0
    physical_cpus = psutil.cpu_count(logical=False) or logical_cpus

    # 先取一次 percent，预热(否则首次返回 0)
    psutil.cpu_percent(interval=None)
    # 单核占用: percpu=True
    cores_load: list[float] = []
    try:
        cores_load = [
            round(x, 1) for x in psutil.cpu_percent(interval=1, percpu=True)
        ]
    except Exception:
        pass
    overall_percent = round(psutil.cpu_percent(interval=None), 1)

    disks = []
    for partition in psutil.disk_partitions():
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append(
                {
                    "device": partition.device or partition.mountpoint,
                    "mountpoint": partition.mountpoint,
                    "fstype": partition.fstype or "unknown",
                    "used": bytes_to_gb(usage.used),
                    "total": bytes_to_gb(usage.total),
                    "percent": round(usage.percent, 1),
                }
            )
        except (PermissionError, OSError):
            continue

    disks = _filter_disks(
        disks,
        show_only=disk_show_only or [],
        # None 使用默认黑名单，[] 则明确表示不过滤。
        exclude=(
            ["boot", "efi", "swap"]
            if disk_exclude is None
            else disk_exclude
        ),
    )

    cpu_temp = get_cpu_temp()
    cpu_freq = get_cpu_freq()
    uptime_seconds = max(0, datetime.datetime.now().timestamp() - boot_ts)
    load = get_loadavg()

    # 与 status-agent/Worker 保持一致：CPU 网格固定分成两行。
    per_row = max(1, (logical_cpus + 1) // 2)

    # CPU 大小核文本: 配置了 cpu_cores (如 "8:12") 显示 8P+12E, 否则显示 8C/16T
    has_hybrid = False
    perf_cores = 0
    eff_cores = 0
    if cpu_cores:
        try:
            p_str, e_str = cpu_cores.split(":", 1)
            perf_cores = int(p_str)
            eff_cores = int(e_str)
            if perf_cores > 0 and eff_cores > 0:
                has_hybrid = True
        except (ValueError, AttributeError):
            pass
    cores_text = (
        f"{perf_cores}P+{eff_cores}E"
        if has_hybrid
        else f"{physical_cpus}C/{logical_cpus}T"
    )

    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "system": {
            "name": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "boot_time": boot_time.strftime("%Y-%m-%d %H:%M:%S"),
            "uptime": format_uptime(uptime_seconds),
            "uptime_seconds": int(uptime_seconds),
        },
        "cpu": {
            "name": get_cpu_name(),
            "percent": overall_percent,
            "cores_text": cores_text,
            "cores_load": cores_load,
            "per_row": per_row,
            "frequency": cpu_freq,
            "temperature": round(cpu_temp, 1) if cpu_temp is not None else None,
        },
        "memory": {
            "used": bytes_to_gb(memory.used),
            "total": bytes_to_gb(memory.total),
            "used_human": bytes_to_human(memory.used),
            "total_human": bytes_to_human(memory.total),
            "percent": round(memory.percent, 1),
            "info": memory_info or "",
        },
        "load": load,
        "disks": disks,
    }


def format_status_text(data: dict) -> str:
    """渲染失败时回退的纯文本版本"""
    system = data["system"]
    cpu = data["cpu"]
    memory = data["memory"]
    temp = (
        f" | 温度: {cpu['temperature']}°C" if cpu["temperature"] is not None else ""
    )
    freq = f" @ {cpu['frequency']}GHz" if cpu["frequency"] else ""
    load = data.get("load", [0, 0, 0])
    disks = "".join(
        f"  {disk['device']} [{disk['fstype']}] "
        f"{disk['used']}/{disk['total']} GB ({disk['percent']}%)\n"
        for disk in data["disks"]
    ) or "  无可用磁盘信息\n"

    return (
        "系统概览\n"
        f"  系统: {system['name']} {system['release']} {system['machine']}\n"
        f"  启动: {system['boot_time']}  已运行: {system['uptime']}\n\n"
        "资源监控\n"
        f"  CPU: {cpu['name']}{freq} ({cpu['cores_text']})\n"
        f"  使用: {cpu['percent']}%{temp}\n"
        f"  负载: {load[0]} / {load[1]} / {load[2]}\n"
        f"  内存: {memory['used_human']} / {memory['total_human']} ({memory['percent']}%)\n\n"
        "存储空间\n" + disks
    )
