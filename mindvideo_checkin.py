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
    """是否已登录：等页面渲染出登录入口或账户入口再判断，避免 React 未渲染导致的误判。"""
    try:
        page.wait_for_function(
            "() => { const t = document.body ? document.body.innerText : ''; "
            "return t.includes('免费登录') || t.includes('Sign in') || t.includes('退出') "
            "|| t.includes('我的') || t.includes('签到'); }",
            timeout=8000,
        )
    except Exception:
        pass
    try:
        txt = page.inner_text("body")
    except Exception:
        return False
    if "免费登录" in txt or "Sign in" in txt:
        return False
    # 出现「签到」(登录后才有) 或「退出」等账户元素 → 视为已登录
    return True


def try_ui_login(page, email, password):
    if not email or not password:
        return False
    log("尝试用账号密码 UI 登录")
    for _ in range(2):
        try:
            page.goto(BASE, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=12000)
            break
        except Exception as e:
            log(f"首页加载异常（重试）: {e}")
            time.sleep(2)
    time.sleep(2)
    # 进入登录页：点「免费登录」
    try:
        page.get_by_text("免费登录", exact=False).first.click(timeout=8000)
        log("已点击「免费登录」")
    except Exception as e:
        log(f"点击免费登录失败（可能已在登录页）: {e}")
    time.sleep(3)
    # 填表并提交（实测：邮箱框 accessible name=邮箱地址，密码框=密码，提交按钮文案「登 录」）
    try:
        email_box = page.get_by_label("邮箱地址", exact=False)
        if email_box.count() == 0:
            email_box = page.locator('input[type="email"], input[name*="email" i], input[placeholder*="邮箱" i], input[autocomplete="email"]')
        email_box.first.fill(email, timeout=8000)
        pwd_box = page.get_by_label("密码", exact=False)
        if pwd_box.count() == 0:
            pwd_box = page.locator('input[type="password"]')
        pwd_box.first.fill(password, timeout=8000)
        page.locator('button:has-text("登")').first.click(timeout=8000)
        log("已提交登录表单")
    except Exception as e:
        log(f"填表/提交失败: {e}")
        return False
    time.sleep(5)
    return is_logged_in(page)


def detect_cloudflare(page):
    try:
        t = page.inner_text("body")
        if "Just a moment" in t or "Checking your browser" in t or "cf-chl" in (page.content() or ""):
            return True
    except Exception:
        pass
    return False


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
                log(f"找到疑似签到元素，文案含「{kw}」，尝试点击")
                ok = False
                try:
                    target.click(timeout=6000, force=True)
                    ok = True
                except Exception:
                    if js_click(target):
                        ok = True
                if ok:
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

        # 1) 先注入 cookie
        if cookie_str:
            log("注入 cookie 恢复登录态")
            try:
                context.add_cookies(parse_cookies(cookie_str))
            except Exception as e:
                log(f"cookie 注入异常: {e}")

        # 2) 打开首页
        try:
            page.goto(BASE, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=10000)
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
        logged_in = is_logged_in(page)
        log(f"登录态判断: {'已登录' if logged_in else '未登录'}")
        if not logged_in:
            if try_ui_login(page, email, password):
                logged_in = True
                log("UI 登录成功")
            else:
                diagnose(page, "not_logged_in")
                result = "❌ 未登录，且无可用的账号密码兜底。请检查 MV_COOKIE 是否有效，或补充 MV_EMAIL/MV_PASSWORD。"
                send_telegram(f"MindVideo 签到失败：{result}")
                browser.close()
                print(result)
                sys.exit(1)

        # 4) 在首页 + 常见路由里找签到按钮
        clicked = None
        routes = ["", "/user", "/member", "/points", "/checkin", "/daily", "/account", "/vip"]
        for r in routes:
            try:
                page.goto(BASE + r, timeout=20000)
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            time.sleep(2)
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
