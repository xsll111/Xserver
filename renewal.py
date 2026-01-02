#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
XServer VPS 自动续期脚本（方案 C：清理旧未读邮件 + 自动收 Outlook 邮箱验证码）
- 登录如遇“新环境登录验证”，自动点发送验证码 → IMAP 拉取邮件 → 自动回填验证码
- 方案C关键：点击“发送验证码”前，先把旧的“验证码相关未读邮件”全部标为已读（Seen），避免读到旧验证码
- 解决 Outlook IMAP 搜索 ascii 报错：如果 SUBJECT 过滤包含非 ASCII（如日文），自动跳过该过滤
- 续期地址已改为你提供的：https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/input
- 续期两次确认：
    1) 点击「確認画面に進む」
    2) 点击「期限を延長する」
"""

import asyncio
import datetime
from datetime import timezone, timedelta
import json
import logging
import os
import re
from typing import Optional, Dict, List, Tuple
from urllib.parse import urlparse

from playwright.async_api import async_playwright


# ======================== stealth（可选） ==========================

try:
    # 老版本
    from playwright_stealth import stealth_async  # type: ignore
    STEALTH_VERSION = "old"
except Exception:
    stealth_async = None
    STEALTH_VERSION = "none"


# ======================== 配置 ==========================

class Config:
    # XServer
    LOGIN_EMAIL = os.getenv("XSERVER_EMAIL")
    LOGIN_PASSWORD = os.getenv("XSERVER_PASSWORD")
    VPS_ID = os.getenv("XSERVER_VPS_ID", "40124478")

    # 运行参数
    USE_HEADLESS = os.getenv("USE_HEADLESS", "false").lower() == "true"
    WAIT_TIMEOUT = int(os.getenv("WAIT_TIMEOUT", "30000"))

    # 代理（你现在不需要全程代理，所以这里仅保留变量/提示，不用于 launch）
    PROXY_SERVER = os.getenv("PROXY_SERVER")
    RUNNER_IP = os.getenv("RUNNER_IP")

    # 邮箱验证码（Outlook IMAP）
    MAIL_IMAP_HOST = os.getenv("MAIL_IMAP_HOST")            # imap-mail.outlook.com / outlook.office365.com
    MAIL_IMAP_USER = os.getenv("MAIL_IMAP_USER")            # 你的邮箱地址
    MAIL_IMAP_PASS = os.getenv("MAIL_IMAP_PASS")            # App Password（推荐）或 OAuth（此脚本不做）
    MAIL_FROM_FILTER = os.getenv("MAIL_FROM_FILTER", "").strip()        # support@xserver.ne.jp
    MAIL_SUBJECT_FILTER = os.getenv("MAIL_SUBJECT_FILTER", "").strip()  # 建议留空（日文会触发 ascii 报错）

    # Telegram（可选）
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

    # 登录页
    LOGIN_URL = "https://secure.xserver.ne.jp/xapanel/login/xvps/"

    # 你确认的续期地址（xmgame）
    EXTEND_URL = "https://secure.xserver.ne.jp/xmgame/game/freeplan/extend/input"

    # 旧版 xvps 到期详情（保留，用于读取到期日）
    DETAIL_URL = f"https://secure.xserver.ne.jp/xapanel/xvps/server/detail?id={VPS_ID}"


# ======================== 日志 ==========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("renewal.log", encoding="utf-8"),
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
            data = {"chat_id": Config.TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
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
        # subject 预留
        await Notifier.send_telegram(message)


# ======================== 邮箱验证码（Outlook IMAP） ==========================

class EmailCodeFetcher:
    """
    通过 IMAP 拉取邮箱验证码（用于“新环境登录验证”）
    - 方案C：在点击“发送验证码”之前，先把匹配条件的旧 UNSEEN 全部标记为 Seen，避免读到旧码
    - 注意：Outlook 个人账号 IMAP 对 Basic Auth 可能会限制；你现在已经能拿到码，说明可用
    """

    def __init__(self):
        self.host = Config.MAIL_IMAP_HOST
        self.user = Config.MAIL_IMAP_USER
        self.password = Config.MAIL_IMAP_PASS
        self.from_filter = Config.MAIL_FROM_FILTER
        self.subject_filter = Config.MAIL_SUBJECT_FILTER

    @staticmethod
    def _is_ascii(s: str) -> bool:
        try:
            s.encode("ascii")
            return True
        except Exception:
            return False

    def _extract_code(self, text: str) -> Optional[str]:
        if not text:
            return None
        # 先抓 5~6 位（你截图/日志里是 5 位）
        m = re.search(r"\b(\d{5,6})\b", text)
        if m:
            return m.group(1)
        # 兜底
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

    def _build_search_criteria(self) -> List[str]:
        """
        IMAP SEARCH 的参数必须是 ASCII，否则会报 'ascii' codec can't encode...
        - FROM 一般是 ASCII：OK
        - SUBJECT 如果你填了日文，会触发报错：这里自动跳过并提示
        """
        criteria: List[str] = ["UNSEEN"]

        if self.from_filter:
            if self._is_ascii(self.from_filter):
                criteria += ["FROM", f"\"{self.from_filter}\""]
            else:
                logger.warning("⚠️ MAIL_FROM_FILTER 含非 ASCII，已跳过该过滤（避免 IMAP ascii 报错）")

        if self.subject_filter:
            if self._is_ascii(self.subject_filter):
                criteria += ["SUBJECT", f"\"{self.subject_filter}\""]
            else:
                logger.warning("⚠️ MAIL_SUBJECT_FILTER 含非 ASCII（日文等），已跳过该过滤（避免 IMAP ascii 报错）")

        return criteria

    def mark_old_unseen_as_seen(self) -> None:
        """
        方案C核心：清掉旧的未读验证码邮件，避免拿到旧验证码
        """
        if not all([self.host, self.user, self.password]):
            logger.warning("⚠️ 未配置 MAIL_IMAP_*，无法清理旧未读验证码邮件")
            return

        import imaplib

        try:
            mail = imaplib.IMAP4_SSL(self.host)
            mail.login(self.user, self.password)
            mail.select("INBOX")

            criteria = self._build_search_criteria()
            typ, data = mail.search(None, *criteria)
            if typ != "OK":
                mail.logout()
                logger.warning(f"⚠️ IMAP search 失败(清理阶段): {typ}")
                return

            ids = data[0].split()
            if not ids:
                mail.logout()
                logger.info("🧹 清理阶段：没有旧的未读验证码邮件")
                return

            # 全部标为已读
            for mid in ids:
                try:
                    mail.store(mid, "+FLAGS", "\\Seen")
                except Exception:
                    pass

            mail.logout()
            logger.info(f"🧹 清理阶段：已将 {len(ids)} 封旧未读验证码邮件标记为已读（避免旧验证码干扰）")

        except Exception as e:
            logger.warning(f"⚠️ 清理旧未读验证码邮件失败（将继续尝试正常收码）: {e}")

    def fetch_latest_code(self, timeout_sec: int = 120, poll_interval: int = 5) -> Optional[str]:
        """
        轮询获取“新来的未读验证码邮件”
        """
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

                criteria = self._build_search_criteria()
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

                # 取最新一封
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

                # 没提取到就也标记已读，避免一直卡住同一封
                mail.store(latest_id, "+FLAGS", "\\Seen")
                mail.logout()
                logger.info("📩 收到新邮件但未提取到验证码，已标记已读，继续等待...")
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

        self.email_fetcher = EmailCodeFetcher()

    # ---------- 缓存 ----------
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

            if Config.PROXY_SERVER:
                logger.info("ℹ️ 已配置 PROXY_SERVER，但当前策略不启用全程代理（避免 socks5 认证导致 launch 失败）")

            if Config.USE_HEADLESS:
                logger.info("⚠️ 为了通过风控/验证码，强制使用非无头模式(headless=False)")
            else:
                logger.info("ℹ️ 已配置非无头模式(headless=False)")

            self.browser = await self._pw.chromium.launch(
                headless=False,
                args=launch_args,
            )

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

            if STEALTH_VERSION == "old" and stealth_async is not None:
                await stealth_async(self.page)
                logger.info("✅ 已启用 playwright-stealth(old)")
            else:
                logger.info("ℹ️ 未启用 stealth（未安装或非 old 版本）")

            self.browser_exit_ip = await self._get_browser_exit_ip()
            if self.browser_exit_ip:
                logger.info(f"🌐 浏览器出口 IP: {self.browser_exit_ip}")
            else:
                logger.warning("⚠️ 未能获取浏览器出口 IP")

            if Config.RUNNER_IP:
                logger.info(f"🌍 GitHub Runner 出口 IP: {Config.RUNNER_IP}")

            if self.browser_exit_ip and Config.RUNNER_IP and self.browser_exit_ip == Config.RUNNER_IP:
                logger.warning(f"⚠️ browser_exit_ip == runner_ip == {self.browser_exit_ip}（当前策略允许直连，继续执行）")

            logger.info("✅ 浏览器初始化成功")
            return True

        except Exception as e:
            logger.error(f"❌ 浏览器初始化失败: {e}")
            self.error_message = str(e)
            return False

    # ---------- 登录（自动邮箱验证码 + 方案C清理旧未读） ----------
    async def login(self) -> bool:
        try:
            logger.info("🌐 开始登录")
            await self.page.goto(Config.LOGIN_URL, timeout=30000)
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

            # 是否进入“新环境登录验证/邮箱验证码”页
            page_text = ""
            try:
                page_text = await self.page.evaluate("() => (document.body.innerText || document.body.textContent || '')")
            except Exception:
                page_text = ""

            need_env_verify = (
                ("新しい環境からのログイン" in page_text) or
                ("ログイン用認証コード" in page_text) or
                ("認証コードを送信" in page_text) or
                (("認証コード" in page_text) and ("送信" in page_text))
            )

            if not need_env_verify:
                self.error_message = f"登录失败（未检测到邮箱验证页）：url={current_url}"
                logger.error(f"❌ {self.error_message}")
                return False

            logger.warning("🔐 检测到“新环境登录验证/邮箱验证码”页面，尝试自动发送验证码并收码...")

            # ===== 方案C：先清理旧未读验证码邮件（关键）=====
            # 注意：清理必须发生在“发送验证码”之前
            self.email_fetcher.mark_old_unseen_as_seen()

            # 1) 点击“发送验证码”
            sent = False
            try:
                btn = self.page.locator(
                    "input[type='submit'][value*='送信'], button:has-text('送信'), button[type='submit'], input[type='submit']"
                ).first
                if await btn.count() > 0:
                    await btn.click()
                    sent = True
            except Exception:
                sent = False

            await asyncio.sleep(2)
            await self.shot("03c_after_send_code")

            if not sent:
                self.renewal_status = "NeedVerify"
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
                inp = self.page.locator(
                    "input[type='text'], input[type='tel'], input[name*='code'], input[name*='auth']"
                ).first
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
                                return i.type === 'text' || i.type === 'tel' ||
                                       n.includes('code') || n.includes('auth') ||
                                       p.includes('認証') || p.includes('code');
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
                btn2 = self.page.locator(
                    "button:has-text('認証'), button:has-text('確認'), input[type='submit'], button[type='submit']"
                ).first
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

    # ---------- 获取到期时间（旧 xvps 页面读取） ----------
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

    # ---------- 续期：两次确认（xmgame/freeplan） ----------
    async def extend_two_step_confirm(self) -> bool:
        """
        你截图的流程：
          1) 点「確認画面に進む」
          2) 再点「期限を延長する」
        """
        try:
            logger.info(f"🌐 打开续期页面: {Config.EXTEND_URL}")
            await self.page.goto(Config.EXTEND_URL, timeout=Config.WAIT_TIMEOUT)
            await asyncio.sleep(2)
            await self.shot("05_extend_input")

            # 第一步：找到「確認画面に進む」
            step1 = self.page.locator(
                "button:has-text('確認画面に進む'), a:has-text('確認画面に進む'), input[type='submit'][value*='確認']"
            ).first

            # 有些页面会显示 “+72時間延長” 等提示按钮
            # 当前策略：不主动点击，直接进入后续确认流程
            try:
                has_plus72 = await self.page.locator(
                    "button:has-text('+72'), a:has-text('+72'), button:has-text('72'), a:has-text('72')"
                ).count()
                if has_plus72 > 0:
                    logger.info("ℹ️ 检测到 '+72時間延長' 提示，但当前策略不点击，直接继续")
                    await self.shot("05a_plus72_detected_but_skipped")
            except Exception:
                pass

            if await step1.count() == 0:
                # 再兜底：页面上所有按钮里找“確認”
                step1 = self.page.locator("button:has-text('確認'), a:has-text('確認')").first

            if await step1.count() == 0:
                self.error_message = "续期失败：未找到「確認画面に進む/確認」按钮"
                logger.error(f"❌ {self.error_message}")
                await self.shot("05b_no_confirm_button")
                return False

            logger.info("🖱️ 续期第1步：点击「確認画面に進む」")
            await step1.click()
            await asyncio.sleep(2)
            await self.shot("06_extend_confirm")

            # 第二步：点击「期限を延長する」
            step2 = self.page.locator(
                "button:has-text('期限を延長する'), a:has-text('期限を延長する'), input[type='submit'][value*='延長']"
            ).first

            if await step2.count() == 0:
                # 再兜底：找“延長”
                step2 = self.page.locator("button:has-text('延長'), a:has-text('延長')").first

            if await step2.count() == 0:
                self.error_message = "续期失败：未找到「期限を延長する/延長」按钮"
                logger.error(f"❌ {self.error_message}")
                await self.shot("06b_no_extend_button")
                return False

            logger.info("🖱️ 续期第2步：点击「期限を延長する」")
            await step2.click()
            await asyncio.sleep(3)
            await self.shot("07_extend_done")

            # 简单成功判定：页面包含“完了/延長/成功”等
            page_text = ""
            try:
                page_text = await self.page.evaluate("() => (document.body.innerText || document.body.textContent || '')")
            except Exception:
                page_text = ""

            if any(k in page_text for k in ["完了", "延長", "成功", "更新"]):
                logger.info("🎉 续期操作已提交（页面出现成功/完成提示）")
                self.renewal_status = "Success"
                return True

            # 如果没有明显关键字，也当作 Unknown，但不直接失败
            logger.warning("⚠️ 未检测到明确成功关键字，但已完成两次点击（请看截图确认）")
            self.renewal_status = "Unknown"
            return True

        except Exception as e:
            self.error_message = f"续期两次确认流程异常: {e}"
            logger.error(f"❌ {self.error_message}")
            return False

    # ---------- README ----------
    def generate_readme(self):
        now = datetime.datetime.now(timezone(timedelta(hours=8)))
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        out = "# XServer VPS 自动续期状态\n\n"
        out += f"**运行时间**: `{ts} (UTC+8)`<br>\n"
        out += f"**VPS ID**: `{Config.VPS_ID}`<br>\n"
        out += f"**Runner IP**: `{Config.RUNNER_IP or '未知'}`<br>\n"
        out += f"**浏览器出口 IP**: `{self.browser_exit_ip or '未知'}`<br>\n\n---\n\n"

        if self.renewal_status == "Success":
            out += "## ✅ 续期成功\n\n"
            if self.old_expiry_time:
                out += f"- 🕛 **到期时间（旧面板读取）**: `{self.old_expiry_time}`\n"
        elif self.renewal_status == "Unexpired":
            out += "## ℹ️ 尚未到续期窗口\n\n"
            out += f"- 🕛 **到期时间**: `{self.old_expiry_time}`\n"
        elif self.renewal_status == "NeedVerify":
            out += "## 🔐 需要邮箱验证/收码失败\n\n"
            out += f"- ⚠️ **原因**: {self.error_message or '未知'}\n"
        else:
            out += "## ❌ 续期失败/未知\n\n"
            out += f"- ⚠️ **错误**: {self.error_message or '未知'}\n"

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

            # 1) 浏览器
            if not await self.setup_browser():
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 失败", self.error_message or "浏览器初始化失败")
                return

            # 2) 登录（含邮箱验证）
            if not await self.login():
                if self.renewal_status == "Unknown":
                    self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 登录失败", self.error_message or "登录失败")
                return

            # 3) 读取到期日（旧面板）
            await self.get_expiry()

            # 4) 续期（两次确认）
            ok = await self.extend_two_step_confirm()
            if not ok:
                self.renewal_status = "Failed"
                self.generate_readme()
                await Notifier.notify("❌ 续期失败", self.error_message or "续期失败")
                return

            # 5) 输出
            self.save_cache()
            self.generate_readme()

            if self.renewal_status == "Success":
                await Notifier.notify("✅ 续期成功", "已完成续期两次确认流程（建议查看截图确认页面提示）")
            else:
                await Notifier.notify("⚠️ 续期完成但状态不确定", "已完成两次点击，但未匹配到明确成功关键字，请看截图。")

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
