"""Playwright 本地 HTML→PNG 渲染器。

设计要点：
- 复用一个长驻 browser / 多个 context，避免每次截图都重启 Chromium（启动 ~1s，截图 ~50ms）。
- 字体由模板中的 CDN 样式表加载。
- HTML 模板用 jinja2 渲染数据，截图高度按页面实际内容裁剪。
- 跨平台：Windows 用 `uv run` 启动 AstrBot 时，playwright 已装在 .venv；Linux 需额外装系统依赖。

运行时使用 Playwright Async API，避免在 AstrBot 的 asyncio 事件循环中调用 Sync API。
"""
import asyncio
import os
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Playwright,
)

from astrbot.api import logger


class Renderer:
    """单例式渲染器。插件 initialize 时 start()，terminate 时 stop()。"""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()
        self._jinja = Environment(
            loader=FileSystemLoader(os.path.dirname(os.path.abspath(__file__))),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    async def start(self) -> None:
        if self._browser is not None:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--no-sandbox", "--disable-gpu", "--force-device-scale-factor=2"]
        )
        self._context = await self._browser.new_context(
            viewport={"width": 760, "height": 800},
            device_scale_factor=2,
        )
        logger.info("Playwright 渲染器已启动 (chromium)。")

    async def stop(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"关闭 Playwright 时出错: {e}")
        finally:
            self._context = None
            self._browser = None
            self._playwright = None

    def _render_html(self, template_name: str, data: dict[str, Any]) -> str:
        """同步：jinja 渲染 HTML 字符串。"""
        tmpl = self._jinja.get_template(template_name)
        return tmpl.render(**data)

    async def _screenshot(self, html: str, output_path: str, width: int) -> str:
        """异步：set_content 后按实际内容高度截图。"""
        if self._context is None:
            raise RuntimeError("渲染器未启动，请先调用 start()")

        page = await self._context.new_page()
        try:
            await page.set_viewport_size({"width": width, "height": 800})
            # 外部字体/CDN 请求可能长期挂起；networkidle 会让整次渲染超时。
            # DOM 就绪即可开始排版，再至多等 3 秒让可用字体加载完成。
            await page.set_content(html, wait_until="domcontentloaded", timeout=10_000)
            try:
                await page.evaluate("document.fonts.ready", timeout=3_000)
            except Exception:
                logger.warning("Web 字体未在 3 秒内加载完成，使用当前可用字体出图。")
            content_height = await page.evaluate(
                """() => {
                    const elements = Array.from(document.body.children)
                        .filter((element) => {
                            return getComputedStyle(element).position !== 'fixed';
                        });
                    return Math.ceil(Math.max(
                        1,
                        ...elements.map((element) =>
                            element.getBoundingClientRect().bottom
                        )
                    ));
                }"""
            )
            await page.set_viewport_size(
                {"width": width, "height": max(1, int(content_height))}
            )
            await page.screenshot(
                path=output_path,
                full_page=False,
                clip={
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": max(1, int(content_height)),
                },
                omit_background=False,
            )
            return output_path
        finally:
            await page.close()

    async def render(
        self,
        template_name: str,
        data: dict[str, Any],
        output_path: str,
        width: int = 760,
    ) -> str:
        """异步入口：渲染模板并截图到 output_path，返回该路径。

        template_name: core/ 下的 .html 文件名（jinja 模板）
        data:          传给模板的数据
        output_path:   PNG 输出绝对路径
        width:         视口像素宽度（截图按此宽度，高度随内容自适应）
        """
        if self._browser is None:
            await self.start()

        html = self._render_html(template_name, data)
        async with self._lock:
            await self._screenshot(html, output_path, width)
        return output_path


# 全局单例：插件共享一个 browser
_renderer: Renderer | None = None


def get_renderer() -> Renderer:
    global _renderer
    if _renderer is None:
        _renderer = Renderer()
    return _renderer
