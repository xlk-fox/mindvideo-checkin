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
    # 提交
    try:
        page.locator('button[type="submit"], button:has-text("登")').first.click(timeout=8000)
        log("已点击登录按钮")
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


def find_and_click_checkin(page):
    def js_click(locator):
        # 用真实 DOM click 绕过遮挡层/动画导致的 Playwright 可见性拦截
        try:
            h = locator.element_handle(timeout=3000)
            if h:
                page.evaluate("el => el.click()", h)
                return True
        except Exception:
            pass
        return False

    # 轮询最多 ~40s，给页面/动画留出渲染时间
    deadline = time.time() + 40
    while time.time() < deadline:
        for kw in CHECKIN_KEYWORDS:
            try:
                loc = page.locator(f"text={kw}").first
                if loc.count() == 0:
                    continue
                # 找到最近的可点击祖先：button / a / 带 onclick / cursor-pointer / role=button
                clickable = loc.locator(
                    "xpath=ancestor-or-self::*[self::button or self::a or @onclick "
                    "or contains(@class,'cursor-pointer') or @role='button']"
                ).first
                target = clickable if clickable.count() else loc
                try:
                    target.scroll_into_view_if_needed()
                except Exception:
                    pass
                box = target.bounding_box(timeout=3000)
                if not box:
                    continue
                try:
                    tgt_txt = (target.inner_text(timeout=2000) or "").strip()
                except Exception:
                    tgt_txt = ""
                try:
                    outer = target.evaluate("el => el.outerHTML.slice(0, 240)")
                except Exception:
                    outer = ""
                log(f"找到疑似签到元素，文案含「{kw}」，元素全文=「{tgt_txt}」，outerHTML={outer}")
                if not tgt_txt:
                    log("元素文本为空（多半是隐藏元素/模板文本），跳过")
                    continue
                ok = False
                try:
                    target.click(timeout=6000, force=True)
                    ok = True
                except Exception:
                    if js_click(target):
                        ok = True
                if ok:
                    time.sleep(2)
                    dump_checkin_texts(page, "点击后+2s")
                    return kw
            except Exception:
                continue
        time.sleep(1)
    return None


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

        # 1) 先注入 cookie
        if cookie_str:
            log("注入 cookie 恢复登录态")
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

        # 4) 在首页 + 常见路由里找签到按钮
        clicked = None
        routes = ["", "/user", "/member", "/points", "/checkin", "/daily", "/account", "/vip"]
        for r in routes:
            try:
                page.goto(BASE + r, timeout=20000, wait_until="domcontentloaded")
            except Exception:
                pass
            time.sleep(3)
            log(f"=== 当前路由 {page.url} ===")
            dump_checkin_texts(page, f"路由{r or '/'}")
            clicked = find_and_click_checkin(page)
            if clicked:
                break

        if not clicked:
            diagnose(page, "no_button")
            result = "❌ 没找到「签到」按钮。已保存页面截图/文本，请据此调整选择器（或把页面文本发我）。"
            send_telegram(f"MindVideo 签到失败：{result}")
            browser.close()
            print(result)
            sys.exit(1)

        log(f"已点击签到元素（文案含「{clicked}」），等待结果…")
        time.sleep(5)
        dump_checkin_texts(page, "点击后+5s")
        log(f"捕获到的相关接口请求: {api_hits[-12:]}")

        if verify_success(page):
            result = "✅ 签到成功（检测到成功提示）"
            log(result)
        else:
            # 可能是「今日已签」或按钮文案未变，截个图确认
            diagnose(page, "after_click")
            result = "⚠️ 已点击签到，但未识别到明确的成功提示。已保存截图，请人工确认是否签到成功（可能本来就已签过）。"
            log(result)

        browser.close()

    print(result)
    send_telegram(f"MindVideo 自动签到：{result}")


if __name__ == "__main__":
    main()
