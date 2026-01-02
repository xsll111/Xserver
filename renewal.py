#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（方案 B：自动收 Outlook 邮箱验证码）
- Playwright 原生 proxy 参数（支持 socks5://user:pass@host:port）
- Turnstile：强制使用 headless=False（配合 GitHub Actions 用 xvfb-run）
- 登录如遇“新环境登录验证”，自动点发送验证码 → IMAP 拉取邮件 → 自动回填验证码
- 代理校验：获取“浏览器出口 IP”，如果 == RUNNER_IP（说明没走代理），立刻中断续期并输出 IP
"""

import asyncio
import datetime
from datetime import timezone, timedelta
import json
import logging
import os
import re
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse

from playwright.async_api import async_playwright


# ======================== 配置 ==========================

class Config:
    # XServer
    LOGIN_EMAIL = os.getenv("XSERVER_EMAIL")
    LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD")
    VPS_ID = os.getenv("XSERVER_VPS_ID", "40124478")

    # 运行参数
    USE_HEADLESS = os.getenv("USE_HEADLESS", "false").lower() == "true"
    WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30000"))

    # 代理
    PROXY_SERVER = os.getenv("PROXY_SERVER")  # e.g. socks5://user:pass@ip:port
    RUNNER_IP = os.getenv("RUNNER_IP")        # workflow 里写入的 runner 直连出口 IP（可空）

    # 邮箱验证码（Outlook IMAP）
    MAIL_IMAP_HOST = os.getenv("MAIL_IMAP_HOST")            # imap-mail.outlook.com / outlook.office365.com
    MAIL_IMAP_USER = os.getenv("MAIL_IMAP_USER")            # 你的邮箱地址
    MAIL_IMAP_PASS = os.getenv("MAIL_IMAP_PASS")            # App Password（推荐）
    MAIL_FROM_FILTER = os.getenv("MAIL_FROM_FILTER", "").strip()        # support@xserver.ne.jp
    MAIL_SUBJECT_FILTER = os.getenv("MAIL_SUBJECT_FILTER", "").strip()  # ログイン用認証コード

    # 验证码 OCR（续期页图片验证码，可留空走默认）
    CAPTCHA_API_URL = os.getenv(
        "CAPTCHA_API_URL",
        "https://captcha-120546510085.asia-northeast1.run.app"
    )

    # Telegram（可选）
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

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
        # subject 预留（当前只发 message）
        await Notifier.send_telegram(message)


# ======================== 续期页图片验证码识别 ==========================

class CaptchaSolver:
    """外部 API OCR 验证码识别器（续期页面的图片验证码）"""

    def __init__(self):
        self.api_url = Config.CAPTCHA_API_URL

    def _validate_code(self, code: str) -> bool:
        if not code:
            return False
        if len(code) < 4 or len(code) > 6:
            return False
        if not code.isdigit():
            return False
        if len(set(code)) == 1:
            return False
        return True

    async def solve(self, img_data_url: str) -> Optional[str]:
        try:
            import aiohttp
            logger.info(f"📤 发送验证码到 API: {self.api_url}")

            max_retries = 3
            for i in range(max_retries):
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

                            numbers = re.findall(r'\d+', code)
                            if numbers:
                                candidate = numbers[0][:6]
                                if self._validate_code(candidate):
                                    logger.info(f"🎯 API 识别成功: {candidate}")
                                    return candidate

                            raise Exception("API 返回无效验证码")
                except Exception as err:
                    if i == max_retries - 1:
                        logger.error(f"❌ API 识别失败(已重试 {max_retries} 次): {err}")
                        return None
                    logger.info(f"🔄 验证码识别失败，准备重试({i+1}/{max_retries-1})...")
                    await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"❌ API 识别错误: {e}")
            return None


# ======================== 邮箱验证码（Outlook IMAP） ==========================

class EmailCodeFetcher:
    """
    通过 IMAP 拉取邮箱验证码（用于“新环境登录验证”）
    - 优先提取 5~6 位（XServer 常见 5 位：如 79933）
    - 再兜底提取 4~8 位
    """

    def __init__(self):
        self.host = Config.MAIL_IMAP_HOST
        self.user = Config.MAIL_IMAP_USER
        self.password = Config.MAIL_IMAP_PASS
        self.from_filter = Config.MAIL_FROM_FILTER
        self.subject_filter = Config.MAIL_SUBJECT_FILTER

    def _extract_code(self, text: str) -> Optional[str]:
        if not text:
            return None
        m = re.search(r"\b(\d{5,6})\b", text)
        if m:
            return m.group(1)
        m = re.search(r"\b(\d{4,8})\b", text)
        return m.group(1) if m else None

    def _decode_email_payload(self, msg) -> str:
        from email.header import decode_header

        def decode_header_value(v):
            if not v:
                return ""
            parts = decode_header(v)
            out = []
            for s, enc in parts:
                if isinstance(s, bytes):
                    out.append(s.decode(enc or "utf-8", errors="ignore"))
                else:
                    out.append(s)
            return "".join(out)

        subject = decode_header_value(msg.get("Subject"))
        from_ = decode_header_value(msg.get("From"))

        body_texts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                    payload = part.get_payload(decode=True) or b""
                    charset = part.get_content_charset() or "utf-8"
                    body_texts.append(payload.decode(charset, errors="ignore"))
        else:
            payload = msg.get_payload(decode=True) or b""
            charset = msg.get_content_charset() or "utf-8"
            body_texts.append(payload.decode(charset, errors="ignore"))

        combined = "\n".join(body_texts)
        return f"SUBJECT:\n{subject}\n\nFROM:\n{from_}\n\nBODY:\n{combined}"

    def fetch_latest_code(self, timeout_sec: int = 120, poll_interval: int = 5) -> Optional[str]:
        if not all([self.host, self.user, self.password]):
            logger.warning("⚠️ 未配置 MAIL_IMAP_*，无法自动收取邮箱验证码")
            return None

        import imaplib
        import email
        import time
        from datetime import datetime, timezone

        end_time = datetime.now(timezone.utc).timestamp() + timeout_sec

        while datetime.now(timezone.utc).timestamp() < end_time:
            try:
                mail = imaplib.IMAP4_SSL(self.host)
                mail.login(self.user, self.password)
                mail.select("INBOX")

                criteria = ["UNSEEN"]
                if self.from_filter:
                    criteria += ["FROM", f"\"{self.from_filter}\""]
                if self.subject_filter:
                    criteria += ["SUBJECT", f"\"{self.subject_filter}\""]

                typ, data = mail.search(None, *criteria)
                if typ != "OK":
                    mail.logout()
                    raise Exception(f"IMAP search failed: {typ}")

                ids = data[0].split()
                if not ids:
                    mail.logout()
                    logger.info("📭 暂无新验证码邮件，继续等待...")
                    time.sleep(poll_interval)
                    continue

                latest_id = ids[-1]
                typ, msg_data = mail.fetch(latest_id, "(RFC822)")
                if typ != "OK":
                    mail.logout()
                    raise Exception(f"IMAP fetch failed: {typ}")

                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                content = self._decode_email_payload(msg)

                code = self._extract_code(content)
                if code:
                    mail.store(latest_id, "+FLAGS", "\\Seen")
                    mail.logout()
                    logger.info(f"✅ 邮箱验证码获取成功: {code}")
                    return code

                mail.logout()
                logger.info("📩 收到新邮件但未提取到验证码，继续等待...")
                time.sleep(poll_interval)

            except Exception as e:
                logger.warning(f"⚠️ 拉取邮箱验证码失败，将重试: {e}")
                time.sleep(poll_interval)

        logger.error("❌ 等待邮箱验证码超时")
        return None


# ======================== 核心类 ==========================

class XServerVPSRenewal:
    def __init__(self):
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None

        self.renewal_status: str = "Unknown"
        self.old_expiry_time: Optional[str] = None
        self.new_expiry_time: Optional[str] = None
        self.error_message: Optional[str] = None

        self.browser_exit_ip: Optional[str] = None

        self.captcha_solver = CaptchaSolver()
        self.email_fetcher = EmailCodeFetcher()

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
            "vps_id": Config.VPS_ID,
            "browser_exit_ip": self.browser_exit_ip,
            "runner_ip": Config.RUNNER_IP
        }
        try:
            with open("cache.json", "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存缓存失败: {e}")

    # ---------- 截图 ----------
    async def shot(self, name: str):
        if not self.page:
            return
        try:
            await self.page.screenshot(path=f"{name}.png", full_page=True)
        except Exception:
            pass

    # ---------- 代理解析 ----------
    def _parse_proxy(self, proxy_url: str) -> Dict:
        """
        支持 socks5://user:pass@host:port 或 http://host:port
        Playwright proxy 需要拆成 server/username/password
        """
        p = urlparse(proxy_url)
        if not p.scheme or not p.hostname or not p.port:
            raise ValueError("PROXY_SERVER 格式不正确，应为 socks5://user:pass@host:port 或 http://host:port")

        server = f"{p.scheme}://{p.hostname}:{p.port}"
        out = {"server": server}
        if p.username:
            out["username"] = p.username
        if p.password:
            out["password"] = p.password
        return out

    # ---------- 获取浏览器出口 IP ----------
    async def _get_browser_exit_ip(self) -> Optional[str]:
        try:
            tmp = await self.context.new_page()
            tmp.set_default_timeout(15000)
            await tmp.goto("https://api.ipify.org", wait_until="domcontentloaded")
            text = (await tmp.text_content("body")) or ""
            ip = text.strip()
            await tmp.close()
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                return ip
            return None
        except Exception:
            return None

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

            # Turnstile：强制 headless=False（在 Actions 里用 xvfb-run）
            if Config.USE_HEADLESS:
                logger.info("⚠️ 检测到 USE_HEADLESS=true，但为通过 Turnstile 强制 headless=False")
            else:
                logger.info("ℹ️ 已配置非无头模式(headless=False)")

            launch_kwargs = {
                "headless": False,
                "args": launch_args
            }

            # Playwright 原生 proxy 参数
            if Config.PROXY_SERVER:
                proxy_conf = self._parse_proxy(Config.PROXY_SERVER)
                launch_kwargs["proxy"] = proxy_conf
                logger.info("🌐 已配置代理（PROXY_SERVER 已设置）")
            else:
                logger.warning("⚠️ 未配置 PROXY_SERVER（将直连运行，可能触发邮箱验证）")

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

            # 基础 anti-bot
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

            # 代理生效校验：浏览器出口 IP
            self.browser_exit_ip = await self._get_browser_exit_ip()
            if self.browser_exit_ip:
                logger.info(f"🌐 浏览器出口 IP: {self.browser_exit_ip}")
            else:
                logger.warning("⚠️ 未能获取浏览器出口 IP（跳过代理校验）")

            if Config.RUNNER_IP:
                logger.info(f"🌍 GitHub Runner 出口 IP: {Config.RUNNER_IP}")

            # 如果浏览器出口 IP == RUNNER_IP，说明没走代理 → 立刻中断（防触发邮箱验证）
            if self.browser_exit_ip and Config.RUNNER_IP and self.browser_exit_ip == Config.RUNNER_IP:
                self.renewal_status = "Aborted"
                self.error_message = f"代理疑似未生效：browser_exit_ip == runner_ip == {self.browser_exit_ip}，已中断续期"
                logger.error(f"🛑 {self.error_message}")
                return False

            logger.info("✅ 浏览器初始化成功")
            return True

        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {e}")
            self.error_message = str(e)
            return False

    # ---------- 登录（含方案B：自动邮箱验证码） ----------
    async def login(self) -> bool:
        try:
            logger.info("🌐 开始登录")
            await self.page.goto("https://secure.xserver.ne.jp/xapanel/login/xvps/", timeout=30000)
            await asyncio.sleep(2)
            await self.shot("01_login")

            await self.page.fill("input[name='memberid']", Config.LOGIN_EMAIL or "")
            await self.page.fill("input[name='user_password']", Config.LOGIN_PASSWORD or "")
            await self.shot("02_before_submit")

            logger.info("📤 提交登录表单...")
            await self.page.click("input[type='submit']")
            await asyncio.sleep(5)
            await self.shot("03_after_submit")

            current_url = self.page.url

            # 登录成功判定
            if "xvps/index" in current_url or ("login" not in current_url.lower()):
                logger.info("🎉 登录成功")
                return True

            # 检测是否进入“新环境登录验证”页面
            page_text = ""
            try:
                page_text = await self.page.evaluate("() => (document.body.innerText || document.body.textContent || '')")
            except Exception:
                page_text = ""

            need_env_verify = (
                ("新しい環境からのログイン" in page_text) or
                ("ログイン用認証コード" in page_text) or
                ("認証コードを送信" in page_text) or
                ("認証コード" in page_text and "送信" in page_text)
            )

            if not need_env_verify:
                self.error_message = f"登录失败（未检测到邮箱验证页）：url={current_url}"
                logger.error(f"❌ {self.error_message}")
                return False

            logger.warning("🔐 检测到“新环境登录验证”，开始方案B：自动收取 Outlook 邮箱验证码")
            await self.shot("03b_need_email_verify")

            # 1) 点击“发送验证码”
            sent = False
            try:
                btn = self.page.locator("button:has-text('認証コードを送信')").first
                if await btn.count() > 0:
                    await btn.click()
                    sent = True
            except Exception:
                sent = False

            if not sent:
                try:
                    btn = self.page.locator("input[type='submit'][value*='送信'], button[type='submit']").first
                    if await btn.count() > 0:
                        await btn.click()
                        sent = True
                except Exception:
                    sent = False

            await asyncio.sleep(2)
            await self.shot("03c_after_send_code")

            if not sent:
                self.error_message = "需要新环境验证，但未能点击“发送验证码”按钮"
                logger.error(f"❌ {self.error_message}")
                return False

            # 2) 拉取邮箱验证码（最长 120 秒）
            logger.info("📧 等待邮箱验证码（IMAP 轮询）...")
            code = None
            try:
                code = await asyncio.to_thread(self.email_fetcher.fetch_latest_code, 120, 5)
            except Exception as e:
                logger.error(f"❌ 邮箱取码异常: {e}")

            if not code:
                self.renewal_status = "NeedVerify"
                self.error_message = "新环境验证：未在超时内获取到邮箱验证码（请检查 IMAP/应用密码/过滤条件）"
                logger.error(f"❌ {self.error_message}")
                return False

            # 3) 回填验证码并提交
            logger.info(f"⌨️ 回填邮箱验证码: {code}")

            filled = False
            try:
                inp = self.page.locator("input[type='text'], input[type='tel'], input[name*='code'], input[name*='auth']").first
                if await inp.count() > 0:
                    await inp.fill(code)
                    filled = True
            except Exception:
                filled = False

            if not filled:
                try:
                    filled = await self.page.evaluate("""
                        (code) => {
                            const inputs = Array.from(document.querySelectorAll('input'));
                            const target = inputs.find(i => {
                                const n = (i.name || '').toLowerCase();
                                const p = (i.placeholder || '').toLowerCase();
                                return i.type === 'text' || i.type === 'tel' || n.includes('code') || n.includes('auth') || p.includes('認証');
                            });
                            if (!target) return false;
                            target.value = code;
                            target.dispatchEvent(new Event('input', { bubbles: true }));
                            target.dispatchEvent(new Event('change', { bubbles: true }));
                            return true;
                        }
                    """, code)
                except Exception:
                    filled = False

            await self.shot("03d_code_filled")

            if not filled:
                self.renewal_status = "NeedVerify"
                self.error_message = "新环境验证：未找到验证码输入框"
                logger.error(f"❌ {self.error_message}")
                return False

            submitted = False
            try:
                btn2 = self.page.locator("button:has-text('認証'), button:has-text('確認'), input[type='submit'], button[type='submit']").first
                if await btn2.count() > 0:
                    await btn2.click()
                    submitted = True
            except Exception:
                submitted = False

            await asyncio.sleep(6)
            await self.shot("03e_after_verify_submit")

            current_url = self.page.url
            if "xvps/index" in current_url or ("login" not in current_url.lower()):
                logger.info("🎉 邮箱验证通过，登录成功")
                return True

            # 失败时输出页面片段帮助排查
            hint = ""
            try:
                hint = await self.page.evaluate("""
                    () => {
                        const t = (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
                        return t.slice(0, 350);
                    }
                """)
            except Exception:
                hint = ""

            self.renewal_status = "NeedVerify"
            self.error_message = f"邮箱验证提交后仍未登录成功: url={current_url}, hint={hint or '无'}"
            logger.error(f"❌ {self.error_message}")
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
                await self.page.click("button:has-text('引き続き無料VPSの利用を継続する')", timeout=3000)
                await asyncio.sleep(5)
                await self.shot("06_extend_page")
                logger.info("✅ 打开续期页面(按钮点击成功)")
                return True
            except Exception as e1:
                logger.info(f"ℹ️ 方法1失败(按钮): {e1}")

            # 方法 1b: 链接
            try:
                logger.info("🔍 方法1b: 尝试链接形式...")
                await self.page.click("a:has-text('引き続き無料VPSの利用を継続する')", timeout=3000)
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
                        await self.page.click("button:has-text('引き続き無料VPSの利用を継続する')", timeout=5000)
                        await asyncio.sleep(5)
                        await self.shot("06_extend_page")
                        logger.info("✅ 打开续期页面(方法2-按钮)")
                        return True
                    except Exception:
                        await self.page.click("a:has-text('引き続き無料VPSの利用を継続する')", timeout=5000)
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

    # ---------- Turnstile 检测/尝试点击（简化版，保留你原本思路） ----------
    async def complete_turnstile_verification(self, max_wait: int = 90) -> bool:
        try:
            has_turnstile = await self.page.evaluate("() => document.querySelector('.cf-turnstile') !== null")
            if not has_turnstile:
                logger.info("ℹ️ 未检测到 Turnstile，跳过")
                return True

            logger.info("🔐 检测到 Turnstile，尝试点击触发验证...")
            await asyncio.sleep(2)

            # 尝试点击 iframe 中心附近
            try:
                info = await self.page.evaluate("""
                    () => {
                        const c = document.querySelector('.cf-turnstile');
                        if (!c) return null;
                        const f = c.querySelector('iframe');
                        if (!f) return null;
                        const r = f.getBoundingClientRect();
                        return {x: r.x + 35, y: r.y + r.height / 2, visible: r.width > 0 && r.height > 0};
                    }
                """)
                if info and info["visible"]:
                    await self.page.mouse.move(100, 100)
                    await asyncio.sleep(0.2)
                    await self.page.mouse.click(info["x"], info["y"])
                    await asyncio.sleep(2)
            except Exception:
                pass

            # 等待 token 出现
            for i in range(max_wait):
                await asyncio.sleep(1)
                ok = await self.page.evaluate("""
                    () => {
                        const token = document.querySelector('[name="cf-turnstile-response"]');
                        return !!(token && token.value && token.value.length > 0);
                    }
                """)
                if ok:
                    logger.info("✅ Turnstile token 已出现")
                    return True

            logger.warning("⚠️ Turnstile 等待超时（继续尝试后续提交）")
            return False

        except Exception as e:
            logger.warning(f"⚠️ Turnstile 流程异常: {e}")
            return False

    # ---------- 提交续期表单 ----------
    async def submit_extend(self) -> bool:
        try:
            logger.info("📄 开始提交续期表单")
            await asyncio.sleep(2)

            # Turnstile
            await self.complete_turnstile_verification(max_wait=90)
            await asyncio.sleep(1)

            # 找验证码图片
            logger.info("🔍 查找续期验证码图片...")
            img_data_url = await self.page.evaluate("""
                () => {
                    const img =
                      document.querySelector('img[src^="data:image"]') ||
                      document.querySelector('img[src^="data:"]') ||
                      document.querySelector('img[alt="画像認証"]') ||
                      document.querySelector('img');
                    if (!img || !img.src) return null;
                    return img.src;
                }
            """)

            if not img_data_url:
                logger.info("ℹ️ 未找到验证码图片（可能未到续期窗口）")
                self.renewal_status = "Unexpired"
                return False

            await self.shot("08_captcha_found")

            # OCR
            code = await self.captcha_solver.solve(img_data_url)
            if not code:
                self.renewal_status = "Failed"
                self.error_message = "续期验证码识别失败"
                logger.error(f"❌ {self.error_message}")
                return False

            logger.info(f"⌨️ 填写续期验证码: {code}")
            filled = await self.page.evaluate("""
                (code) => {
                    const input =
                      document.querySelector('[placeholder*="上の画像"]') ||
                      document.querySelector('input[type="text"]');
                    if (!input) return false;
                    input.value = code;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }
            """, code)

            if not filled:
                self.renewal_status = "Failed"
                self.error_message = "未找到续期验证码输入框"
                logger.error(f"❌ {self.error_message}")
                return False

            await asyncio.sleep(1)
            await self.shot("09_captcha_filled")

            # 提交
            logger.info("🖱️ 提交续期表单...")
            await self.shot("10_before_submit")
            submitted = await self.page.evaluate("""
                () => {
                    if (typeof window.submit_button !== 'undefined' &&
                        window.submit_button &&
                        typeof window.submit_button.click === 'function') {
                        window.submit_button.click();
                        return true;
                    }
                    const submitBtn = document.querySelector('input[type="submit"], button[type="submit"]');
                    if (submitBtn) { submitBtn.click(); return true; }
                    return false;
                }
            """)

            if not submitted:
                self.renewal_status = "Failed"
                self.error_message = "无法提交续期表单"
                logger.error(f"❌ {self.error_message}")
                return False

            await asyncio.sleep(5)
            await self.shot("11_after_submit")

            html = await self.page.content()

            # 错误
            if any(err in html for err in [
                "入力された認証コードが正しくありません",
                "認証コードが正しくありません",
                "エラー",
                "間違"
            ]):
                self.renewal_status = "Failed"
                self.error_message = "续期验证码错误或 Turnstile 验证失败"
                logger.error(f"❌ {self.error_message}")
                await self.shot("11_error")
                return False

            # 成功
            if any(success in html for success in ["完了", "継続", "完成", "更新しました"]):
                logger.info("🎉 续期成功")
                self.renewal_status = "Success"
                await self.get_expiry()
                self.new_expiry_time = self.old_expiry_time
                return True

            self.renewal_status = "Unknown"
            logger.warning("⚠️ 续期提交结果未知（页面未匹配成功/失败关键字）")
            return False

        except Exception as e:
            self.renewal_status = "Failed"
            self.error_message = str(e)
            logger.error(f"❌ 续期错误: {e}")
            return False

    # ---------- README 生成 ----------
    def generate_readme(self):
        now = datetime.datetime.now(timezone(timedelta(hours=8)))
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        out = "# XServer VPS 自动续期状态\n\n"
        out += f"**运行时间**: `{ts} (UTC+8)`<br>\n"
        out += f"**VPS ID**: `{Config.VPS_ID}`<br>\n"
        out += f"**Runner IP**: `{Config.RUNNER_IP or '未知'}`<br>\n"
        out += f"**浏览器出口 IP**: `{self.browser_exit_ip or '未知'}`<br>\n\n---\n\n"

        if self.renewal_status == "Success":
            out += (
                "## ✅ 续期成功\n\n"
                f"- 🕛 **到期时间**: `{self.old_expiry_time}`\n"
            )
        elif self.renewal_status == "Unexpired":
            out += (
                "## ℹ️ 尚未到续期窗口\n\n"
                f"- 🕛 **到期时间**: `{self.old_expiry_time}`\n"
            )
        elif self.renewal_status == "Aborted":
            out += (
                "## 🛑 已中断（代理疑似未生效）\n\n"
                f"- ⚠️ **原因**: {self.error_message or '未知'}\n"
            )
        elif self.renewal_status == "NeedVerify":
            out += (
                "## 🔐 需要邮箱验证/收码失败\n\n"
                f"- ⚠️ **原因**: {self.error_message or '未知'}\n"
                "- ✅ 建议检查：Outlook 是否开启 IMAP、是否使用 App Password、是否填写正确的 IMAP Host\n"
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

            # 1) 启动浏览器（包含代理校验/可能中断）
            ok = await self.setup_browser()
            if not ok:
                if self.renewal_status != "Aborted":
                    self.renewal_status = self.renewal_status if self.renewal_status != "Unknown" else "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败/中断", self.error_message or "浏览器初始化失败")
                return

            # 2) 登录（含邮箱自动验证）
            if not await self.login():
                if self.renewal_status == "Unknown":
                    self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 登录失败", self.error_message or "登录失败")
                return

            # 3) 获取到期时间
            await self.get_expiry()

            # 3.5 自动判断是否到可续期日（JST：到期前 1 天开始可续）
            try:
                if self.old_expiry_time:
                    today_jst = datetime.datetime.now(timezone(timedelta(hours=9))).date()
                    expiry_date = datetime.datetime.strptime(self.old_expiry_time, "%Y-%m-%d").date()
                    can_extend_date = expiry_date - datetime.timedelta(days=1)

                    logger.info(f"📅 今日日期(JST): {today_jst}")
                    logger.info(f"📅 到期日期: {expiry_date}")
                    logger.info(f"📅 可续期开始日: {can_extend_date}")

                    if today_jst < can_extend_date:
                        logger.info("ℹ️ 尚未到可续期时间，无需续期")
                        self.renewal_status = "Unexpired"
                        self.error_message = None
                        self.save_cache()
                        self.generate_readme()
                        await Notifier.notify("ℹ️ 尚未到续期日", f"到期: {self.old_expiry_time}\n可续期开始: {can_extend_date}")
                        return
            except Exception as e:
                logger.warning(f"⚠️ 自动判断续期窗口失败（继续执行）: {e}")

            # 4) 进入详情页，尝试点击“更新する”
            await self.page.goto(Config.DETAIL_URL, timeout=Config.WAIT_TIMEOUT)
            await asyncio.sleep(2)
            await self.click_update()
            await asyncio.sleep(3)

            # 5) 打开续期页面
            opened = await self.open_extend()
            if not opened and self.renewal_status == "Unexpired":
                self.generate_readme()
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
                return
            elif not opened:
                self.renewal_status = "Failed"
                self.error_message = "无法打开续期页面"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", self.error_message)
                return

            # 6) 提交续期
            await self.submit_extend()

            # 7) 保存缓存 & README & 通知
            self.save_cache()
            self.generate_readme()

            if self.renewal_status == "Success":
                await Notifier.notify("✅ 续期成功", f"续期成功，新到期时间: {self.new_expiry_time or self.old_expiry_time}")
            elif self.renewal_status == "Unexpired":
                await Notifier.notify("ℹ️ 尚未到期", f"当前到期时间: {self.old_expiry_time}")
            elif self.renewal_status == "NeedVerify":
                await Notifier.notify("🔐 邮箱验证异常", self.error_message or "邮箱验证异常")
            elif self.renewal_status == "Aborted":
                await Notifier.notify("🛑 已中断", self.error_message or "已中断")
            else:
                await Notifier.notify("❌ 续期失败", f"错误信息: {self.error_message or '未知错误'}")

        finally:
            logger.info("=" * 60)
            logger.info(f"✅ 流程完成 - 状态: {self.renewal_status}")
            logger.info("=" * 60)

            try:
                if self.page:
                    await self.page.close()
                if self.context:
                    await self.context.close()
                if self.browser:
                    await self.browser.close()
                if self._pw:
                    await self._pw.stop()
                logger.info("🧹 浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭浏览器时出错: {e}")


async def main():
    runner = XServerVPSRenewal()
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
