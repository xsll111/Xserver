#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（增强版）
- 优化：Cloudflare Turnstile 验证处理顺序
- 改进：强制关闭无头模式 + 注入 anti-bot 脚本 + 增强“人类行为”模拟
- 新增：自动判断是否已续期 / 尚未到可续期日期（按 JST），避免重复续期
- 新增：Playwright 原生 proxy 参数 + 出口 IP 校验（代理未生效则中断，防止触发邮箱验证）
"""

import asyncio
import re
import datetime
from datetime import timezone, timedelta
import os
import json
import logging
from typing import Optional, Dict
from urllib.parse import urlparse, unquote

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# 尝试兼容两种 playwright-stealth 版本
try:
    from playwright_stealth import stealth_async
    STEALTH_VERSION = 'old'
except ImportError:
    STEALTH_VERSION = 'new'
    stealth_async = None


# ======================== 配置 ==========================

class Config:
    LOGIN_EMAIL = os.getenv("XSERVER_EMAIL")
    LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD")
    VPS_ID = os.getenv("XSERVER_VPS_ID", "40124478")

    # 原来的 USE_HEADLESS 在 Turnstile 下不再生效，这里保留但会强制改为 False
    USE_HEADLESS = os.getenv("USE_HEADLESS", "true").lower() == "true"
    WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30000"))

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # 仅支持带 schema 的简单代理，如 socks5://ip:port 或 http://ip:port
    PROXY_SERVER = os.getenv("PROXY_SERVER")

    # GitHub Runner 的出口 IP（用于判断代理是否生效）
    RUNNER_IP = os.getenv("RUNNER_IP")

    CAPTCHA_API_URL = os.getenv(
        "CAPTCHA_API_URL",
        "https://captcha-120546510085.asia-northeast1.run.app"
    )

    DETAIL_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/detail?id={VPS_ID}"
    EXTEND_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/freevps/extend/index?id_vps={VPS_ID}"


# ======================== 日志 ==========================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('renewal.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ======================== 通知器 ==========================

class Notifier:
    @staticmethod
    async def send_telegram(message: str):
        if not all([Config.TELEGRAM_BOT_TOKEN, Config.TELEGRAM_CHAT_ID]):
            return
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{Config.TELEGRAM_BOT_TOKEN}/sendMessage"
            data = {
                "chat_id": Config.TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data) as resp:
                    if resp.status == 200:
                        logger.info("✅ Telegram 通知发送成功")
                    else:
                        logger.error(f"❌ Telegram 返回非 200 状态码: {resp.status}")
        except Exception as e:
            logger.error(f"❌ Telegram 发送失败: {e}")

    @staticmethod
    async def notify(subject: str, message: str):
        # 目前只使用 Telegram（subject 仅预留，不使用）
        await Notifier.send_telegram(message)


# ======================== 验证码识别 ==========================

class CaptchaSolver:
    """外部 API OCR 验证码识别器"""

    def __init__(self):
        self.api_url = Config.CAPTCHA_API_URL

    def _validate_code(self, code: str) -> bool:
        """验证识别出的验证码是否合理"""
        if not code:
            return False

        if len(code) < 4 or len(code) > 6:
            logger.warning(f"⚠️ 验证码长度异常: {len(code)} 位")
            return False

        if len(set(code)) == 1:
            logger.warning(f"⚠️ 验证码可疑(所有数字相同): {code}")
            return False

        if not code.isdigit():
            logger.warning(f"⚠️ 验证码包含非数字字符: {code}")
            return False

        return True

    async def solve(self, img_data_url: str) -> Optional[str]:
        """使用外部 API 识别验证码"""
        try:
            import aiohttp

            logger.info(f"📤 发送验证码到 API: {self.api_url}")

            max_retries = 3
            retry_count = 0

            while retry_count < max_retries:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            self.api_url,
                            data=img_data_url,
                            headers={'Content-Type': 'text/plain'},
                            timeout=aiohttp.ClientTimeout(total=20)
                        ) as resp:
                            if not resp.ok:
                                raise Exception(f"API 请求失败: {resp.status}")

                            code_response = await resp.text()
                            code = code_response.strip()

                            logger.info(f"📥 API 返回验证码: {code}")

                            if code and len(code) >= 4:
                                numbers = re.findall(r'\d+', code)
                                if numbers:
                                    code = numbers[0][:6]

                                    if self._validate_code(code):
                                        logger.info(f"🎯 API 识别成功: {code}")
                                        return code

                            raise Exception('API 返回无效验证码')

                except Exception as err:
                    retry_count += 1
                    if retry_count >= max_retries:
                        logger.error(f"❌ API 识别失败(已重试 {max_retries} 次): {err}")
                        return None
                    logger.info(f"🔄 验证码识别失败,正在进行第 {retry_count} 次重试...")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ API 识别错误: {e}")

        return None


# ======================== 核心类 ==========================

class XServerVPSRenewal:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None  # 保存 playwright 实例，方便关闭

        self.renewal_status: str = "Unknown"
        self.old_expiry_time: Optional[str] = None
        self.new_expiry_time: Optional[str] = None
        self.error_message: Optional[str] = None

        self.captcha_solver = CaptchaSolver()

    # ---------- 工具：解析代理 ----------
    def _build_playwright_proxy(self, proxy_url: str) -> Dict:
        """
        将 PROXY_SERVER 转换为 Playwright proxy dict。
        支持：
          - socks5://user:pass@host:port
          - http://user:pass@host:port
          - socks5://host:port
        """
        p = urlparse(proxy_url)
        if not p.scheme or not p.hostname or not p.port:
            raise ValueError("PROXY_SERVER 格式错误，必须形如 socks5://user:pass@host:port")

        server = f"{p.scheme}://{p.hostname}:{p.port}"
        proxy: Dict[str, str] = {"server": server}

        if p.username is not None:
            proxy["username"] = unquote(p.username)
        if p.password is not None:
            proxy["password"] = unquote(p.password)

        return proxy

    # ---------- 缓存 ----------
    def load_cache(self) -> Optional[Dict]:
        if os.path.exists("cache.json"):
            try:
                with open("cache.json", "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载缓存失败: {e}")
        return None

    def save_cache(self):
        cache = {
            "last_expiry": self.old_expiry_time,
            "status": self.renewal_status,
            "last_check": datetime.datetime.now(timezone.utc).isoformat(),
            "vps_id": Config.VPS_ID
        }
        try:
            with open("cache.json", "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    # ---------- 截图 ----------
    async def shot(self, name: str):
        """安全截图,不影响主流程"""
        if not self.page:
            return
        try:
            await self.page.screenshot(path=f"{name}.png", full_page=True)
        except Exception:
            pass

    # ---------- 浏览器 ----------
    async def setup_browser(self) -> bool:
        try:
            self._pw = await async_playwright().start()
            launch_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-infobars",
                "--start-maximized",
            ]

            # 强制关闭无头模式
            if Config.USE_HEADLESS:
                logger.info("⚠️ 为了通过 Turnstile，强制使用非无头模式(headless=False)")
            else:
                logger.info("ℹ️ 已配置非无头模式(headless=False)")

            launch_kwargs = {
                "headless": False,   # ★ 关键：强制关闭 headless
                "args": launch_args
            }

            # ✅ 使用 Playwright 原生 proxy 参数（更稳，尤其带账号密码）
            if Config.PROXY_SERVER:
                try:
                    proxy = self._build_playwright_proxy(Config.PROXY_SERVER)
                    launch_kwargs["proxy"] = proxy
                    logger.info("🌐 已配置代理（PROXY_SERVER 已设置）")
                except Exception as e:
                    logger.error(f"❌ 代理配置解析失败: {e}")
                    self.error_message = f"代理配置解析失败: {e}"
                    return False
            else:
                logger.info("ℹ️ 未配置代理（PROXY_SERVER 未设置）")

            self.browser = await self._pw.chromium.launch(**launch_kwargs)

            context_options = {
                "viewport": {"width": 1920, "height": 1080},
                "locale": "ja-JP",
                "timezone_id": "Asia/Tokyo",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }

            self.context = await self.browser.new_context(**context_options)

            # Anti-bot 注入：去掉 webdriver、补全 plugins / languages / permissions
            await self.context.add_init_script("""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','ja-JP','en-US']});
Object.defineProperty(navigator, 'permissions', {
    get: () => ({
        query: ({name}) => Promise.resolve({state: 'granted'})
    })
});
""")

            self.page = await self.context.new_page()
            self.page.set_default_timeout(Config.WAIT_TIMEOUT)

            # 旧版 stealth 支持
            if STEALTH_VERSION == 'old' and stealth_async is not None:
                await stealth_async(self.page)
            else:
                logger.info("ℹ️ 使用新版 playwright_stealth 或未安装,跳过 stealth 处理")

            # === 🔍 代理生效检测：输出出口 IP，并在代理失效时中断（防邮箱验证） ===
            try:
                await self.page.goto("https://api.ipify.org", timeout=15000)
                browser_ip = (await self.page.evaluate("() => document.body.innerText")).strip()
                logger.info(f"🌐 浏览器出口 IP: {browser_ip}")

                if Config.RUNNER_IP:
                    logger.info(f"🌍 GitHub Runner 出口 IP: {Config.RUNNER_IP}")

                # 配置了代理 + 提供了 Runner IP + 两者相同 => 代理未生效
                if Config.PROXY_SERVER and Config.RUNNER_IP and browser_ip == Config.RUNNER_IP:
                    msg = (
                        "检测到代理未生效：浏览器出口 IP 与 GitHub Runner IP 相同，"
                        "为避免触发邮箱验证，已中断续期。"
                    )
                    logger.error(f"❌ {msg} (browser_ip={browser_ip}, runner_ip={Config.RUNNER_IP})")
                    self.error_message = f"{msg} (browser_ip={browser_ip}, runner_ip={Config.RUNNER_IP})"
                    return False

            except Exception as e:
                # 获取 IP 失败时不强制中断（避免 ipify 波动导致完全跑不了）
                logger.warning(f"⚠️ 无法获取出口 IP（将跳过代理强校验）: {e}")

            logger.info("✅ 浏览器初始化成功")
            return True

        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {e}")
            self.error_message = str(e)
            return False

    # ---------- 登录 ----------
    async def login(self) -> bool:
        try:
            logger.info("🌐 开始登录")
            await self.page.goto(
                "https://secure.xserver.ne.jp/xapanel/login/xvps/",
                timeout=30000
            )
            await asyncio.sleep(2)
            await self.shot("01_login")

            # 填写账号密码
            await self.page.fill("input[name='memberid']", Config.LOGIN_EMAIL)
            await self.page.fill("input[name='user_password']", Config.LOGIN_PASSWORD)
            await self.shot("02_before_submit")

            logger.info("📤 提交登录表单...")
            await self.page.click("input[type='submit']")
            await asyncio.sleep(5)
            await self.shot("03_after_submit")

            if "xvps/index" in self.page.url or "login" not in self.page.url.lower():
                logger.info("🎉 登录成功")
                return True

            logger.error("❌ 登录失败")
            self.error_message = "登录失败"
            return False
        except Exception as e:
            logger.error(f"❌ 登录错误: {e}")
            self.error_message = f"登录错误: {e}"
            return False

    # ---------- 获取到期时间 ----------
    async def get_expiry(self) -> bool:
        try:
            await self.page.goto(Config.DETAIL_URL, timeout=30000)
            await asyncio.sleep(3)
            await self.shot("04_detail")

            expiry_date = await self.page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('tr');
                    for (const row of rows) {
                        const text = row.innerText || row.textContent;
                        if (text.includes('利用期限') && !text.includes('利用開始')) {
                            const match = text.match(/(\\d{4})年(\\d{1,2})月(\\d{1,2})日/);
                            if (match) return {year: match[1], month: match[2], day: match[3]};
                        }
                    }
                    return null;
                }
            """)

            if expiry_date:
                self.old_expiry_time = (
                    f"{expiry_date['year']}-"
                    f"{expiry_date['month'].zfill(2)}-"
                    f"{expiry_date['day'].zfill(2)}"
                )
                logger.info(f"📅 利用期限: {self.old_expiry_time}")
                return True

            logger.warning("⚠️ 未能解析利用期限")
            return False
        except Exception as e:
            logger.error(f"❌ 获取到期时间失败: {e}")
            return False

    # ---------- 点击"更新する" ----------
    async def click_update(self) -> bool:
        try:
            try:
                await self.page.click("a:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(链接)")
                return True
            except Exception:
                pass

            try:
                await self.page.click("button:has-text('更新する')", timeout=3000)
                await asyncio.sleep(2)
                logger.info("✅ 点击更新按钮(按钮)")
                return True
            except Exception:
                pass

            logger.info("ℹ️ 未找到更新按钮")
            return False
        except Exception as e:
            logger.info(f"ℹ️ 点击更新按钮失败: {e}")
            return False

    # ---------- 打开续期页面 ----------
    async def open_extend(self) -> bool:
        try:
            await asyncio.sleep(2)
            await self.shot("05_before_extend")

            # 方法 1: 按钮
            try:
                logger.info("🔍 方法1: 查找续期按钮(按钮)...")
                await self.page.click(
                    "button:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(按钮点击成功)")
                return True
            except Exception as e1:
                logger.info(f"ℹ️ 方法1失败(按钮): {e1}")

            # 方法 1b: 链接
            try:
                logger.info("🔍 方法1b: 尝试链接形式...")
                await self.page.click(
                    "a:has-text('引き続き無料VPSの利用を継続する')",
                    timeout=3000
                )
                await asyncio.sleep(5)
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(链接点击成功)")
                return True
            except Exception as e1b:
                logger.info(f"ℹ️ 方法1b失败(链接): {e1b}")

            # 方法 2: 直接访问续期 URL
            try:
                logger.info("🔍 方法2: 直接访问续期URL...")
                await self.page.goto(Config.EXTEND_URL, timeout=Config.WAIT_TIMEOUT)
                await asyncio.sleep(3)
                await self.shot("05_extend_url")

                content = await self.page.content()

                if "引き続き無料VPSの利用を継続する" in content:
                    try:
                        await self.page.click(
                            "button:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-按钮)")
                        return True
                    except Exception:
                        await self.page.click(
                            "a:has-text('引き続き無料VPSの利用を継続する')",
                            timeout=5000
                        )
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-链接)")
                        return True

                if "延長期限" in content or "期限まで" in content:
                    logger.info("ℹ️ 未到续期时间窗口")
                    self.renewal_status = "Unexpired"
                    return False

            except Exception as e2:
                logger.info(f"ℹ️ 方法2失败: {e2}")

            logger.warning("⚠️ 所有打开续期页面的方法都失败")
            return False

        except Exception as e:
            logger.warning(f"⚠️ 打开续期页面异常: {e}")
            return False

    # ---------- Turnstile 高级处理 ----------
    async def complete_turnstile_verification(self, max_wait: int = 120) -> bool:
        """使用多种方法尝试完成 Cloudflare Turnstile 验证"""
        try:
            logger.info("🔐 开始 Cloudflare Turnstile 验证流程...")

            has_turnstile = await self.page.evaluate("""
                () => {
                    return document.querySelector('.cf-turnstile') !== null;
                }
            """)

            if not has_turnstile:
                logger.info("ℹ️ 未检测到 Cloudflare Turnstile,跳过验证")
                return True

            logger.info("🔍 检测到 Turnstile,尝试多种方法触发验证...")

            # 方法1: 坐标点击 iframe
            try:
                await asyncio.sleep(3)

                iframe_info = await self.page.evaluate("""
                    () => {
                        const container = document.querySelector('.cf-turnstile');
                        if (!container) return null;

                        const iframe = container.querySelector('iframe');
                        if (!iframe) return null;

                        const rect = iframe.getBoundingClientRect();
                        return {
                            x: rect.x,
                            y: rect.y,
                            width: rect.width,
                            height: rect.height,
                            visible: rect.width > 0 && rect.height > 0
                        };
                    }
                """)

                if iframe_info and iframe_info['visible']:
                    click_x = iframe_info['x'] + 35
                    click_y = iframe_info['y'] + (iframe_info['height'] / 2)

                    logger.info(f"🖱️ 方法1: 点击 iframe 坐标 ({click_x:.0f}, {click_y:.0f})")
                    await self.page.mouse.click(click_x, click_y)
                    await asyncio.sleep(2)
                    await self.shot("07_method1_clicked")
                else:
                    logger.info("⚠️ 方法1: 无法获取 iframe 位置")

            except Exception as e:
                logger.info(f"ℹ️ 方法1 失败: {e}")

            # 方法2: CDP 注入点击
            try:
                logger.info("🔧 方法2: 使用 CDP 注入到所有 frames...")

                cdp = await self.page.context.new_cdp_session(self.page)
                await cdp.send('Runtime.enable')

                frames_data = await cdp.send('Page.getFrameTree')

                def collect_frame_ids(frame_tree):
                    ids = [frame_tree['frame']['id']]
                    if 'childFrames' in frame_tree:
                        for child in frame_tree['childFrames']:
                            ids.extend(collect_frame_ids(child))
                    return ids

                frame_ids = collect_frame_ids(frames_data['frameTree'])
                logger.info(f"📋 找到 {len(frame_ids)} 个 frames")

                for frame_id in frame_ids:
                    try:
                        result = await cdp.send('Runtime.evaluate', {
                            'expression': '''
                                (() => {
                                    const checkbox = document.querySelector('input[type="checkbox"]');
                                    if (checkbox && !checkbox.checked) {
                                        checkbox.click();
                                        return 'clicked_checkbox';
                                    }

                                    const clickable = document.querySelector('[role="checkbox"]') ||
                                                     document.querySelector('label') ||
                                                     document.querySelector('span');
                                    if (clickable) {
                                        clickable.click();
                                        return 'clicked_element';
                                    }

                                    return 'no_target';
                                })()
                            ''',
                        })
                        if result.get('result', {}).get('value') in ['clicked_checkbox', 'clicked_element']:
                            logger.info("✅ 方法2: 在 frame 中成功触发点击")
                            await asyncio.sleep(2)
                            break
                    except Exception:
                        continue

                await self.shot("07_method2_injected")

            except Exception as e:
                logger.info(f"ℹ️ 方法2 失败: {e}")

            # 方法3: 模拟鼠标移动 + 点击
            try:
                logger.info("🖱️ 方法3: 模拟真实用户鼠标移动...")

                iframe_info = await self.page.evaluate("""
                    () => {
                        const container = document.querySelector('.cf-turnstile');
                        if (!container) return null;
                        const iframe = container.querySelector('iframe');
                        if (!iframe) return null;
                        const rect = iframe.getBoundingClientRect();
                        return {x: rect.x + 35, y: rect.y + rect.height/2};
                    }
                """)

                if iframe_info:
                    await self.page.mouse.move(100, 100)
                    await asyncio.sleep(0.5)

                    steps = 15
                    current_x, current_y = 100, 100
                    target_x, target_y = iframe_info['x'], iframe_info['y']

                    for i in range(steps):
                        x = current_x + (target_x - current_x) * (i + 1) / steps
                        y = current_y + (target_y - current_y) * (i + 1) / steps
                        await self.page.mouse.move(x, y)
                        await asyncio.sleep(0.06)

                    await self.page.mouse.down()
                    await asyncio.sleep(0.15)
                    await self.page.mouse.up()

                    logger.info("✅ 方法3: 已模拟真实点击")
                    await asyncio.sleep(3)
                    await self.shot("07_method3_humanlike")

            except Exception as e:
                logger.info(f"ℹ️ 方法3 失败: {e}")

            # 滚动增强“人类行为”
            try:
                await self.page.mouse.move(200, 200, steps=20)
                await asyncio.sleep(0.4)
                await self.page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.6)
                await self.page.evaluate("window.scrollBy(0, -200)")
                await asyncio.sleep(0.5)
            except Exception:
                pass

            logger.info("⏳ 等待 Turnstile 验证完成...")

            for i in range(max_wait):
                await asyncio.sleep(1)

                verification_status = await self.page.evaluate("""
                    () => {
                        const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                        const hasToken = tokenField && tokenField.value && tokenField.value.length > 0;

                        const pageText = document.body.innerText || document.body.textContent;
                        const hasSuccessText = pageText.includes('成功しました') || pageText.includes('成功');

                        const container = document.querySelector('.cf-turnstile');
                        let hasCheckmark = false;
                        if (container) {
                            hasCheckmark = container.classList.contains('success') ||
                                           container.classList.contains('verified') ||
                                           container.querySelector('[aria-checked="true"]') !== null;
                        }

                        return {
                            hasToken: hasToken,
                            hasSuccessText: hasSuccessText,
                            hasCheckmark: hasCheckmark,
                            tokenLength: hasToken ? tokenField.value.length : 0,
                            verified: hasToken || hasSuccessText || hasCheckmark
                        };
                    }
                """)

                if verification_status['verified']:
                    logger.info(
                        "✅ Cloudflare Turnstile 验证成功! "
                        f"(令牌:{verification_status['hasToken']}, "
                        f"文本:{verification_status['hasSuccessText']}, "
                        f"对勾:{verification_status['hasCheckmark']})"
                    )
                    await self.shot("07_turnstile_success")
                    return True

                if i % 20 == 10:
                    logger.info(f"🔄 重新尝试触发点击... ({i}/{max_wait}秒)")
                    try:
                        iframe_info = await self.page.evaluate("""
                            () => {
                                const container = document.querySelector('.cf-turnstile');
                                if (!container) return null;
                                const iframe = container.querySelector('iframe');
                                if (!iframe) return null;
                                const rect = iframe.getBoundingClientRect();
                                return {x: rect.x + 35, y: rect.y + rect.height/2, visible: rect.width > 0};
                            }
                        """)
                        if iframe_info and iframe_info['visible']:
                            await self.page.mouse.click(iframe_info['x'], iframe_info['y'])
                    except Exception:
                        pass

                if i % 10 == 0 and i > 0:
                    status_parts = []
                    if not verification_status['hasToken']:
                        status_parts.append("等待令牌")
                    if not verification_status['hasSuccessText']:
                        status_parts.append("等待成功标志")
                    if not verification_status['hasCheckmark']:
                        status_parts.append("等待对勾")
                    logger.info(
                        f"⏳ Turnstile 验证中... ({i}/{max_wait}秒) "
                        f"[{', '.join(status_parts) if status_parts else '检查中'}]"
                    )

            logger.warning(f"⚠️ Turnstile 验证超时({max_wait}秒)")
            await self.shot("07_turnstile_timeout")

            final_status = await self.page.evaluate("""
                () => {
                    const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                    return {
                        hasToken: tokenField && tokenField.value && tokenField.value.length > 0,
                        tokenValue: tokenField && tokenField.value
                            ? tokenField.value.substring(0, 30) + '...'
                            : 'empty'
                    };
                }
            """)

            if final_status['hasToken']:
                logger.info(f"⚠️ 超时但检测到令牌({final_status['tokenValue']}),尝试继续")
                return True

            return False

        except Exception as e:
            logger.error(f"❌ Turnstile 验证失败: {e}")
            return False

    # ---------- 提交续期表单 ----------
    async def submit_extend(self) -> bool:
        """提交续期表单 - 先完成 Turnstile, 再处理验证码并提交"""
        try:
            logger.info("📄 开始提交续期表单")
            await asyncio.sleep(3)

            logger.info("👤 在续期页面模拟用户行为以辅助 Turnstile 通过...")
            try:
                await self.page.mouse.move(50, 50, steps=25)
                await asyncio.sleep(0.7)
                await self.page.mouse.move(200, 160, steps=20)
                await asyncio.sleep(0.6)
                await self.page.evaluate("window.scrollBy(0, 300)")
                await asyncio.sleep(0.8)
                await self.page.evaluate("window.scrollBy(0, -200)")
                await asyncio.sleep(0.6)
            except Exception:
                pass

            logger.info("🔐 步骤1: 完成 Cloudflare Turnstile 验证...")
            turnstile_success = await self.complete_turnstile_verification(max_wait=90)
            if not turnstile_success:
                logger.warning("⚠️ Turnstile 验证未完全确认,但继续尝试提交...")

            await asyncio.sleep(2)

            logger.info("🔍 步骤2: 查找验证码图片...")
            img_data_url = await self.page.evaluate("""
                () => {
                    const img =
                      document.querySelector('img[src^="data:image"]') ||
                      document.querySelector('img[src^="data:"]') ||
                      document.querySelector('img[alt="画像認証"]') ||
                      document.querySelector('img');
                    if (!img || !img.src) {
                        throw new Error('未找到验证码图片');
                    }
                    return img.src;
                }
            """)

            if not img_data_url:
                logger.info("ℹ️ 无验证码,可能未到续期时间")
                self.renewal_status = "Unexpired"
                return False

            logger.info("📸 已找到验证码图片,正在发送到 API 进行识别...")
            await self.shot("08_captcha_found")

            code = await self.captcha_solver.solve(img_data_url)
            if not code:
                logger.error("❌ 验证码识别失败")
                self.renewal_status = "Failed"
                self.error_message = "验证码识别失败"
                return False

            logger.info(f"⌨️ 步骤3: 填写验证码: {code}")
            input_filled = await self.page.evaluate("""
                (code) => {
                    const input =
                      document.querySelector('[placeholder*="上の画像"]') ||
                      document.querySelector('input[type="text"]');
                    if (!input) {
                        throw new Error('未找到验证码输入框');
                    }
                    input.value = code;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """, code)

            if not input_filled:
                raise Exception("未找到验证码输入框")

            await asyncio.sleep(2)
            await self.shot("09_captcha_filled")

            try:
                await self.page.mouse.move(270, 300, steps=30)
                await asyncio.sleep(0.9)
                await self.page.mouse.move(420, 260, steps=20)
                await asyncio.sleep(0.7)
            except Exception:
                pass

            logger.info("🔍 步骤4: 最终确认 Turnstile 令牌...")
            final_check = await self.page.evaluate("""
                () => {
                    const tokenField = document.querySelector('[name="cf-turnstile-response"]');
                    const successText = document.body.innerText || document.body.textContent;
                    return {
                        hasToken: tokenField && tokenField.value && tokenField.value.length > 0,
                        tokenLength: tokenField && tokenField.value ? tokenField.value.length : 0,
                        hasSuccessText: successText.includes('成功')
                    };
                }
            """)

            if final_check['hasToken']:
                logger.info(
                    f"✅ Turnstile 令牌确认 (长度: {final_check['tokenLength']}, "
                    f"成功标志: {final_check['hasSuccessText']})"
                )
            else:
                logger.warning("⚠️ Turnstile 令牌缺失,提交可能失败")

            await asyncio.sleep(1)

            logger.info("🖱️ 步骤5: 提交表单...")
            await self.shot("10_before_submit")

            submitted = await self.page.evaluate("""
                () => {
                    if (typeof window.submit_button !== 'undefined' &&
                        window.submit_button &&
                        typeof window.submit_button.click === 'function') {
                        window.submit_button.click();
                        return true;
                    }
                    const submitBtn =
                      document.querySelector('input[type="submit"], button[type="submit"]');
                    if (submitBtn) {
                        submitBtn.click();
                        return true;
                    }
                    return false;
                }
            """)

            if not submitted:
                logger.error("❌ 无法提交表单")
                raise Exception("无法提交表单")

            logger.info("✅ 表单已提交")
            await asyncio.sleep(5)
            await self.shot("11_after_submit")

            html = await self.page.content()

            if any(err in html for err in [
                "入力された認証コードが正しくありません",
                "認証コードが正しくありません",
                "エラー",
                "間違"
            ]):
                logger.error("❌ 验证码错误或 Turnstile 验证失败")
                await self.shot("11_error")
                self.renewal_status = "Failed"
                self.error_message = "验证码错误或 Turnstile 验证失败"
                return False

            if any(success in html for success in [
                "完了",
                "継続",
                "完成",
                "更新しました"
            ]):
                logger.info("🎉 续期成功")
                self.renewal_status = "Success"
                await self.get_expiry()
                self.new_expiry_time = self.old_expiry_time
                return True

            logger.warning("⚠️ 续期提交结果未知")
            self.renewal_status = "Unknown"
            return False

        except Exception as e:
            logger.error(f"❌ 续期错误: {e}")
            self.renewal_status = "Failed"
            self.error_message = str(e)
            return False

    # ---------- README 生成 ----------
    def generate_readme(self):
        now = datetime.datetime.now(timezone(timedelta(hours=8)))  # 显示为 UTC+8
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        out = "# XServer VPS 自动续期状态\n\n"
        out += f"**运行时间**: `{ts} (UTC+8)`<br>\n"
        out += f"**VPS ID**: `{Config.VPS_ID}`<br>\n\n---\n\n"

        if self.renewal_status == "Success":
            out += (
                "## ✅ 续期成功\n\n"
                f"- 🕛 **旧到期**: `{self.old_expiry_time}`\n"
                f"- 🕡 **新到期**: `{self.new_expiry_time}`\n"
            )
        elif self.renewal_status == "Unexpired":
            out += (
                "## ℹ️ 尚未到期\n\n"
                f"- 🕛 **到期时间**: `{self.old_expiry_time}`\n"
            )
        else:
            out += (
                "## ❌ 续期失败\n\n"
                f"- 🕛 **到期**: `{self.old_expiry_time or '未知'}`\n"
                f"- ⚠️ **错误**: {self.error_message or '未知'}\n"
            )

        out += f"\n---\n\n*最后更新: {ts}*\n"

        with open("README.md", "w", encoding="utf-8") as f:
            f.write(out)

        logger.info("📄 README.md 已更新")

    # ---------- 主流程 ----------
    async def run(self):
        try:
            logger.info("=" * 60)
            logger.info("🚀 XServer VPS 自动续期开始")
            logger.info("=" * 60)

            # 1. 启动浏览器
            if not await self.setup_browser():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"浏览器初始化失败: {self.error_message}")
                return

            # 2. 登录
            if not await self.login():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", f"登录失败: {self.error_message}")
                return

            # 3. 获取当前到期时间
            await self.get_expiry()

            # 3.5 自动判断是否到可续期日（按 JST）
            try:
                if self.old_expiry_time:
                    today_jst = datetime.datetime.now(timezone(timedelta(hours=9))).date()
                    expiry_date = datetime.datetime.strptime(self.old_expiry_time, "%Y-%m-%d").date()
                    can_extend_date = expiry_date - datetime.timedelta(days=1)

                    logger.info(f"📅 今日日期(JST): {today_jst}")
                    logger.info(f"📅 到期日期: {expiry_date}")
                    logger.info(f"📅 可续期开始日: {can_extend_date}")

                    if today_jst < can_extend_date:
                        logger.info("ℹ️ 当前 VPS 尚未到可续期时间，无需续期。")
                        self.renewal_status = "Unexpired"
                        self.error_message = None

                        self.save
