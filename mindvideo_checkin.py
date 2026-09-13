#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MindVideo.ai 每日自动签到 (Playwright 浏览器自动化)
-------------------------------------------------
原理：MindVideo 的接口有 Cloudflare + 动态签名(i-sign) 防护，直接发 HTTP 请求基本会被拦。
所以这里用真实浏览器登录账号后，自动寻找并点击「签到」按钮，让浏览器自己处理防护。

登录态恢复（按优先级）：
  1. MV_COOKIE   —— 浏览器导出的 cookie 字符串（推荐，最安全）
  2. MV_EMAIL + MV_PASSWORD —— 若 cookie 失效，自动走 UI 登录兜底

通知（可选）：配置 MV_TG_TOKEN + MV_TG_CHAT 后，用 Telegram 推送结果。

诊断：失败时自动截图 diag_*.png 并 dump 页面文本，方便定位选择器问题。
"""

import os
import re
import sys
import time

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("[FATAL] playwright 未安装，请先 pip install playwright 并 playwright install chromium")
    sys.exit(2)

BASE = "https://www.mindvideo.ai"
SIGNIN_URL = BASE + "/zh/auth/signin/"

# 签到按钮可能出现的文案（中英文都覆盖）
CHECKIN_KEYWORDS = [
    "签到", "每日签到", "签到领", "立即签到", "去签到",
    "Daily Check", "Check in", "Check-in", "Claim", "Claim daily", "Sign in daily",
]
# 已签到 / 成功 的判定文案
SUCCESS_KEYWORDS = [
    "签到成功", "已签到", "今日已签", "已经签到", "已领取", "领取成功",
    "签到完成", "success", "already checked", "claimed",
]


def log(msg):
    print(f"[checkin] {msg}", flush=True)


def send_telegram(text):
    token = os.environ.get("MV_TG_TOKEN")
    chat = os.environ.get("MV_TG_CHAT")
    if not token or not chat:
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"Telegram 通知失败: {e}")


def parse_cookies(cookie_str):
    cookies = []
    if not cookie_str:
        return cookies
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".mindvideo.ai",
            "path": "/",
        })
    return cookies


def is_logged_in(page):
    """是否已登录：只有明确看到「登录后才有」的元素才判定已登录，否则一律当作未登录去走登录流程。"""
    try:
        txt = page.inner_text("body")
    except Exception:
        return False
    if "免费登录" in txt or "Sign in" in txt:
        return False
    if "签到" in txt or "退出" in txt or "积分" in txt:
        return True
    # 页面文本里没有明确的登录特征（多半是没渲染出来 / 被拦）→ 保守当作未登录，强制走登录兜底
    return False


def get_token(context):
    """登录成功后 MindVideo 会写入名为 token 的 cookie，用它判断登录是否真的成功。"""
    try:
        for c in context.cookies():
            if c.get("name") == "token" and c.get("value"):
                return c["value"]
    except Exception:
        pass
    return None


def try_ui_login(page, context, email, password):
    """直连登录页 → 填表 → 提交。以 token cookie 是否出现作为成功判据。"""
    if not email or not password:
        log("缺少 MV_EMAIL / MV_PASSWORD，无法 UI 登录")
        return False
    log(f"开始 UI 登录（登录前 token: {'有' if get_token(context) else '无'}）")
    # 直接打开登录页（首页上的入口文案可能是「登录」而不是「免费登录」，找文案容易失败）
    for url in (SIGNIN_URL, BASE):
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            log(f"已打开 {page.url}")
            break
        except Exception as e:
            log(f"打开 {url} 异常: {e}")
    time.sleep(4)
    # 若页面上没有密码框，再尝试点登录入口
    try:
        if page.locator('input[type="password"]').count() == 0:
            for label in ("免费登录", "登录", "Sign in"):
                loc = page.get_by_text(label, exact=True).first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=5000)
                        log(f"已点击「{label}」入口")
                        time.sleep(3)
                        break
                except Exception:
                    continue
    except Exception as e:
        log(f"查找登录入口异常: {e}")
    # 填表
    try:
        page.locator(
            'input[placeholder*="邮箱"], input[type="email"], input[autocomplete="email"], '
            'input[name*="email" i], input[placeholder*="mail" i]'
        ).first.fill(email, timeout=12000)
        page.locator('input[type="password"]').first.fill(password, timeout=12000)
        log("已填写账号密码")
    except Exception as e:
        log(f"填表失败: {e}")
        return False
    # 提交：精确匹配「登录」文案，避免误点到「使用 Google 登录」
    submitted = False
    try:
        submitted = bool(page.evaluate(
            "() => { const norm = s => (s||'').replace(/\\s+/g,'');"
            "const b = [...document.querySelectorAll('button')]"
            ".find(x => ['登录','Signin','Login','Log in'].includes(norm(x.textContent)));"
            "if (b) { b.click(); return true; } return false; }"
        ))
    except Exception as e:
        log(f"精确匹配登录按钮异常: {e}")
    if submitted:
        log("已点击登录按钮（精确匹配）")
    else:
        try:
            page.locator('button[type="submit"]').first.click(timeout=8000)
            log("已点击登录按钮（submit 兜底）")
        except Exception as e:
            log(f"点登录按钮失败，改用回车: {e}")
            try:
                page.keyboard.press("Enter")
            except Exception:
                return False
    # 等待 token 出现
    for i in range(6):
        time.sleep(3)
        if get_token(context):
            log(f"登录成功，已获取 token（第 {i + 1} 次检查）")
            return True
    log(f"登录后仍未拿到 token，当前 URL={page.url}")
    return False


def detect_cloudflare(page):
    try:
        t = page.inner_text("body")
        if "Just a moment" in t or "Checking your browser" in t or "cf-chl" in (page.content() or ""):
            return True
    except Exception:
        pass
    return False


def dump_checkin_texts(page, tag):
    """列出页面上所有含「签到」相关字样的元素（含是否可见），用于定位真正的签到按钮。"""
    try:
        vals = page.evaluate(
            "() => { const ks=['签到','领取','已签']; const out=[];"
            "document.querySelectorAll('*').forEach(e=>{"
            "const t=(e.textContent||'').trim().replace(/\\s+/g,' ');"
            "if(t && t.length<40 && ks.some(k=>t.includes(k))){"
            "const r=e.getBoundingClientRect();"
            "out.push({t:t, vis:(r.width>0&&r.height>0), tag:e.tagName,"
            "cls:(e.className||'').toString().slice(0,50), onclk:!!e.getAttribute('onclick')});} });"
            "return out.slice(0,25); }"
        )
        log(f"{tag} 含量: {vals}")
    except Exception as e:
        log(f"{tag} 取文案失败: {e}")


def dump_clickables(page, tag):
    """列出页面上所有可见的可点击元素文案，用于找签到入口。"""
    try:
        vals = page.evaluate(
            "() => { const out=[];"
            "document.querySelectorAll('button,a,[onclick],[role=button],[class*=cursor-pointer]').forEach(e=>{"
            "const t=(e.innerText||e.textContent||'').trim().replace(/\\s+/g,' ');"
            "const r=e.getBoundingClientRect();"
            "if(t && t.length<30 && r.width>0 && r.height>0) out.push(t); });"
            "return [...new Set(out)].slice(0,60); }"
        )
        log(f"{tag} 可见可点击: {vals}")
    except Exception as e:
        log(f"{tag} dump_clickables 失败: {e}")


def get_credits(page):
    """读取导航栏上的积分数字（形如「22Free」）。"""
    try:
        m = re.search(r"(\d+)\s*Free", page.inner_text("body")[:400])
        return int(m.group(1)) if m else None
    except Exception:
        return None


PANEL_BTN_JS = (
    "() => { const norm = s => (s||'').replace(/\\s+/g,'');"
    "return [...document.querySelectorAll('button, [class*=cursor-pointer]')]"
    ".filter(b => norm(b.textContent) === '签到' && b.getBoundingClientRect().width > 0).length; }"
)


def find_and_click_checkin(page):
    """签到两步走：① 点导航栏「签到并领取」打开签到面板 ② 点面板内的「签到」按钮。
    返回形如 "success|说明" / "already|说明" / "failed|说明"，找不到入口返回 None。
    """
    before = get_credits(page)
    log(f"签到前积分: {before}")

    # ① 打开签到面板
    opened = False
    for kw in ("签到并领取", "每日签到", "立即签到"):
        try:
            loc = page.locator(f"text={kw}").first
            if loc.count() == 0:
                continue
            handle = loc.element_handle(timeout=3000)
            if not handle:
                continue
            page.evaluate("el => el.click()", handle)
            log(f"已点击「{kw}」，尝试打开签到面板")
            opened = True
            break
        except Exception:
            continue
    if not opened:
        log("未找到签到入口")
        return None

    # ② 点面板内的「签到」按钮
    clicked_panel = False
    for i in range(8):
        time.sleep(2)
        try:
            r = page.evaluate(
                "() => { const norm = s => (s||'').replace(/\\s+/g,'');"
                "const bs = [...document.querySelectorAll('button, [class*=cursor-pointer]')]"
                ".filter(b => norm(b.textContent) === '签到' && b.getBoundingClientRect().width > 0);"
                "if (bs.length) { bs[0].click(); return 'clicked'; } return 'none'; }"
            )
        except Exception as e:
            r = f"error:{e}"
        if r == "clicked":
            log(f"已点击签到面板内的「签到」按钮（第 {i + 1} 次尝试）")
            clicked_panel = True
            break
        log(f"面板内暂未出现可点的「签到」按钮（{r}）")

    time.sleep(5)
    after = get_credits(page)
    log(f"签到后积分: {after}")
    dump_checkin_texts(page, "签到后")

    if before is not None and after is not None and after > before:
        return f"success|签到成功，积分 {before} → {after}（+{after - before}）"

    try:
        still = page.evaluate(PANEL_BTN_JS)
    except Exception:
        still = -1
    if clicked_panel and still == 0:
        return f"already|今日已签到（积分 {before} 未变化）"
    if not clicked_panel and still == 0:
        return f"already|今日已签到（未出现可点的签到按钮）"
    return f"failed|签到未生效（积分 {before} → {after}，仍存在可点签到按钮）"


def verify_success(page):
    try:
        txt = page.inner_text("body")
    except Exception:
        return False
    return any(k.lower() in txt.lower() for k in SUCCESS_KEYWORDS)


def diagnose(page, tag):
    try:
        page.screenshot(path=f"diag_{tag}.png", full_page=False)
        with open(f"diag_{tag}.txt", "w", encoding="utf-8") as f:
            f.write(page.content()[:20000])
        log(f"已保存诊断文件 diag_{tag}.png / diag_{tag}.txt")
    except Exception as e:
        log(f"诊断保存失败: {e}")


def main():
    cookie_str = os.environ.get("MV_COOKIE", "")
    email = os.environ.get("MV_EMAIL", "")
    password = os.environ.get("MV_PASSWORD", "")

    result = "❓ 未知"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = context.new_page()

        api_hits = []

        def _on_resp(resp):
            try:
                u = resp.url.lower()
                if any(k in u for k in ("checkin", "claim", "daily", "point", "credit")):
                    api_hits.append(f"{resp.status} {resp.url[:120]}")
            except Exception:
                pass

        page.on("response", _on_resp)

        # 固定中文界面：英文界面下按钮/输入框文案不同，会导致登录步骤失配
        try:
            context.add_cookies([{
                "name": "i18next", "value": "zh",
                "domain": ".mindvideo.ai", "path": "/",
            }])
        except Exception as e:
            log(f"设置语言 cookie 失败: {e}")

        # 1) 默认不注入持久 cookie：
        #    实测注入的旧 token 会与新登录态冲突，导致页面仍显示「登录」；
        #    而账号密码 UI 登录在云端稳定可用，因此直接走登录。
        #    如确需用 cookie，设置环境变量 MV_USE_COOKIE=1 即可。
        if cookie_str and os.environ.get("MV_USE_COOKIE") == "1":
            log("注入 cookie 恢复登录态（MV_USE_COOKIE=1）")
            try:
                context.add_cookies(parse_cookies(cookie_str))
            except Exception as e:
                log(f"cookie 注入异常: {e}")

        # 2) 打开首页
        try:
            page.goto(BASE, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"首页加载异常: {e}")

        # 等待 Cloudflare（若有）
        for _ in range(10):
            if detect_cloudflare(page):
                log("检测到 Cloudflare 校验，等待放行…")
                time.sleep(3)
            else:
                break
        time.sleep(2)

        # 3) 判断登录态
        try:
            _head = page.inner_text("body")[:600].replace("\n", " | ")
        except Exception as e:
            _head = f"<取文本失败: {e}>"
        log(f"页面诊断 URL={page.url} 文本={_head}")
        logged_in = is_logged_in(page)
        log(f"登录态判断: {'已登录' if logged_in else '未登录'}")
        if not logged_in:
            if try_ui_login(page, context, email, password):
                log("UI 登录成功")
            else:
                log("UI 登录未确认成功，继续尝试找签到按钮（避免因页面判断偏差直接放弃）")

        # 4) 找到带签到入口的页面并完成签到
        outcome = None
        routes = ["", "/user", "/member", "/points", "/checkin", "/daily", "/account", "/vip"]
        for r in routes:
            try:
                page.goto(BASE + r, timeout=20000, wait_until="domcontentloaded")
            except Exception:
                pass
            # 云端 React 渲染较慢：轮询等待「签到并领取」入口出现（最多 ~30s）
            found = False
            body = ""
            rounds = 0
            for rounds in range(1, 16):
                time.sleep(2)
                try:
                    body = page.inner_text("body")
                except Exception:
                    body = ""
                if "签到并领取" in body:
                    found = True
                    break
            log(f"=== 路由 {page.url} 含签到入口={found}（等待 {rounds} 轮）===")
            if not found:
                log(f"路由 {r or '/'} 页面文本: {body[:200].replace(chr(10), ' | ')}")
                continue
            outcome = find_and_click_checkin(page)
            if outcome:
                break

        if not outcome:
            diagnose(page, "no_button")
            result = "❌ 没找到签到入口（页面未渲染出「签到并领取」按钮）"
            log(result)
            send_telegram(f"MindVideo 签到失败：{result}")
            browser.close()
            print(result)
            sys.exit(1)

        log(f"捕获到的相关接口请求: {api_hits[-12:]}")
        status, _, msg = outcome.partition("|")
        if status in ("success", "already"):
            result = f"✅ {msg}"
        else:
            result = f"⚠️ {msg}"
            diagnose(page, "after_click")
        log(result)

        browser.close()

    print(result)
    send_telegram(f"MindVideo 自动签到：{result}")


if __name__ == "__main__":
    main()
