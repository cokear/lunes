#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import logging
from pathlib import Path
from datetime import datetime

from DrissionPage import ChromiumPage, ChromiumOptions

# ================== 核心配置 ==================
USERNAME = os.environ.get("USERNAME", "").strip()
PASSWORD = os.environ.get("PASSWORD", "").strip()
PROXY = os.environ.get("PROXY", "").strip()
LOGIN_URL = os.environ.get("LOGIN_URL") or "https://betadash.lunes.host/login"

OUTPUT_DIR = Path("artifacts")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("lunes_dp")

# ================== JS 常量 ==================
STEALTH_JS = """
const getParameter = WebGLRenderingContext.prototype.getParameter;
const getParameter2 = WebGL2RenderingContext.prototype.getParameter;
const spoofedVendor = 'NVIDIA Corporation';
const spoofedRenderer = 'NVIDIA GeForce GTX 1660 Ti/PCIe/SSE2';
Object.defineProperty(WebGLRenderingContext.prototype, 'getParameter', {
    configurable: true, enumerable: true, writable: true,
    value: function (parameter) {
        if (parameter === 37445) return spoofedVendor;
        if (parameter === 37446) return spoofedRenderer;
        return getParameter.apply(this, arguments);
    }
});
Object.defineProperty(WebGL2RenderingContext.prototype, 'getParameter', {
    configurable: true, enumerable: true, writable: true,
    value: function (parameter) {
        if (parameter === 37445) return spoofedVendor;
        if (parameter === 37446) return spoofedRenderer;
        return getParameter2.apply(this, arguments);
    }
});
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
Object.defineProperty(navigator, 'language', {get: () => 'ko-KR'});
if (!window.chrome) { window.chrome = { runtime: {} }; }
"""

# 注意:DrissionPage 的 run_js 把字符串当【函数体】执行,必须显式 return,否则一律返回 None。
_WININFO_JS = """
return {
    sx: window.screenX || 0,
    sy: window.screenY || 0,
    oh: window.outerHeight,
    ih: window.innerHeight
};
"""

_CF_TOKEN_JS = """
var el = document.querySelector('[id$="_response"]');
return el ? (el.value || '') : '';
"""

_CF_PRESENT_JS = """
return !!(
    document.getElementById('VXzI4') ||
    document.querySelector('h2#kxxo4') ||
    /just a moment|잠시만/i.test(document.title || '')
);
"""

# 登录表单内嵌 Turnstile 检测
_TS_PRESENT_JS = """
return !!document.querySelector('.g-recaptcha, [name="cf-turnstile-response"]');
"""

# 返回:token 字符串 / '' (有字段没解) / null (无字段)
_TS_TOKEN_JS = """
var el = document.querySelector('[name="cf-turnstile-response"]');
return el ? (el.value || '') : null;
"""

# ================== 诊断:dump 当前页面状态(默认不截图,失败点才截) ==================
def dump_state(page, tag, shot=False):
    parts = [f"[{tag}]"]
    def add(k, fn):
        try:
            parts.append(f"{k}={fn()}")
        except Exception as e:
            parts.append(f"{k}!ERR({e})")
    add("url",   lambda: page.url)
    add("title", lambda: repr(page.title))
    add("ts_present", lambda: page.run_js(
        "return !!document.querySelector('.g-recaptcha, [name=\"cf-turnstile-response\"]')"))
    add("token", lambda: (lambda t: f"{str(t)[:12]}(len={len(t) if isinstance(t,str) else 0})")(
        page.run_js("var e=document.querySelector('[name=\"cf-turnstile-response\"]');return e?(e.value||''):'<no-field>'")))
    logger.info("  |  ".join(str(p) for p in parts))
    if shot:
        screenshot(page, tag)

# ================== 工具与底层点击 ==================
def screenshot(page, name):
    try:
        path = str(OUTPUT_DIR / f"{datetime.now().strftime('%H%M%S')}-{name}.png")
        page.get_screenshot(path=path)
        logger.info(f"📸 截图已保存: {path}")
    except Exception as e:
        logger.warning(f"截图失败: {e}")

def xdotool_click(page, coords):
    try:
        wi = page.run_js(_WININFO_JS) or {"sx": 0, "sy": 0, "oh": 1080, "ih": 900}
        bar = wi.get("oh", 1080) - wi.get("ih", 900)
        if bar < 0 or bar > 200: bar = 85
        ax = int(coords["cx"] + wi.get("sx", 0))
        ay = int(coords["cy"] + wi.get("sy", 0) + bar)
        print(f"[INFO]   ---> xdotool 物理点击: screen=({ax}, {ay})")
        os.system(f"xdotool mousemove --sync {ax} {ay}")
        time.sleep(0.3)
        os.system("xdotool click 1")
        return True
    except Exception as e:
        print(f"[WARN]   xdotool_click 异常: {e}")
        return False

def cdp_click(page, vx, vy):
    try:
        page.run_cdp("Input.dispatchMouseEvent", type="mouseMoved",
                     x=vx, y=vy, button="none", clickCount=0)
        time.sleep(0.05)
        page.run_cdp("Input.dispatchMouseEvent", type="mousePressed",
                     x=vx, y=vy, button="left", clickCount=1)
        time.sleep(0.05)
        page.run_cdp("Input.dispatchMouseEvent", type="mouseReleased",
                     x=vx, y=vy, button="left", clickCount=1)
        print(f"[INFO]   ---> CDP 鼠标点击: viewport=({vx}, {vy})")
        return True
    except Exception as e:
        print(f"[WARN]   cdp_click 异常: {e}")
        return False

def _get_cf_iframe_rect(page):
    try:
        search = page.run_cdp(
            "DOM.performSearch",
            query="iframe[src*='challenges.cloudflare.com']",
            includeUserAgentShadowDOM=True
        )
        sid = search.get("searchId")
        cnt = search.get("resultCount", 0)

        if cnt > 0 and sid:
            results = page.run_cdp("DOM.getSearchResults", searchId=sid, fromIndex=0, toIndex=cnt)
            for nid in results.get("nodeIds", []):
                try:
                    obj = page.run_cdp("DOM.resolveNode", nodeId=nid)
                    oid = obj["object"]["objectId"]
                    rr = page.run_cdp(
                        "Runtime.callFunctionOn",
                        objectId=oid,
                        functionDeclaration="function() { var r = this.getBoundingClientRect(); return { left: r.left, top: r.top, width: r.width, height: r.height }; }",
                        returnByValue=True
                    )
                    rv = rr.get("result", {}).get("value", {})
                    if rv.get("width", 0) > 20 and rv.get("height", 0) > 20:
                        try: page.run_cdp("DOM.discardSearchResults", searchId=sid)
                        except: pass
                        return rv
                except:
                    continue
            try: page.run_cdp("DOM.discardSearchResults", searchId=sid)
            except: pass
    except Exception as e:
        print(f"  [WARN] _get_cf_iframe_rect 失败: {e}")
    return None

def _get_cf_click_coords_from_rect(rect):
    cx = int(rect["left"] + 28)
    cy = int(rect["top"] + rect["height"] / 2)
    return cx, cy

def _get_cf_click_coords_inline(page):
    rect = _get_cf_iframe_rect(page)
    if rect:
        cx, cy = _get_cf_click_coords_from_rect(rect)
        print(f"  [INFO] CDP pierce 发现CF坐标: ({cx}, {cy})")
        return cx, cy

    try:
        iframe = page.ele('tag:iframe@@src:challenges.cloudflare.com', timeout=1)
        if iframe:
            loc = iframe.rect.viewport_location
            size = iframe.rect.size
            if size[0] > 20 and size[1] > 20:
                cx, cy = int(loc[0] + 28), int(loc[1] + size[1] / 2)
                print(f"  [INFO] DrissionPage 发现CF坐标: ({cx}, {cy})")
                return cx, cy
    except: pass

    try:
        vh = int(page.run_js("return window.innerHeight;") or 600)
        cx = 30
        cy = vh - 33
        print(f"  [INFO] 视口推算 发现CF坐标: ({cx}, {cy})")
        return cx, cy
    except: pass

    return None, None

def _is_cf_page(page):
    title = page.title or ""
    if "Just a moment" in title or "잠시만" in title:
        return True
    try:
        return bool(page.run_js(_CF_PRESENT_JS))
    except:
        return False

def _cf_passed(page):
    try:
        token = page.run_js(_CF_TOKEN_JS) or ""
        if token: return True
    except: pass
    title = page.title or ""
    if "Just a moment" not in title and "잠시만" not in title:
        try:
            if not page.run_js(_CF_PRESENT_JS):
                return True
        except:
            return True
    return False

# ================== CF 整页盾处理(首页可能出现) ==================
def wait_for_cloudflare(page, timeout=60):
    start = time.time()
    click_count = 0

    while time.time() - start < timeout:
        if _cf_passed(page):
            print("[INFO]   ✅ CF 盾已通过或无盾")
            return True

        html = page.html
        cf_verifying = '>Verifying<' in html or 'Verifying...' in html
        cf_inline = ('challenges.cloudflare.com' in html or 'cf-turnstile' in html)

        if cf_verifying:
            print(f"  [INFO] 🛡️  出现内嵌 CF（Verifying...），等待自动完成...")
            time.sleep(2)
            continue

        if cf_inline or _is_cf_page(page):
            cx, cy = _get_cf_click_coords_inline(page)
            if cx and cy:
                print(f"  [INFO] 🛡️  点击 CF 坐标: ({cx}, {cy})...")
                ok = cdp_click(page, cx, cy)
                if not ok:
                    xdotool_click(page, {"cx": cx, "cy": cy})
                click_count += 1
                time.sleep(3)
            else:
                time.sleep(1)
        else:
            print("[INFO]   ✅ 当前页面无 CF 盾")
            return True

    print("[ERROR]   ❌ CF 处理超时")
    screenshot(page, "cf-timeout")
    return False

# ================== 登录表单内嵌 Turnstile 处理(带逐轮诊断) ==================
def solve_login_turnstile(page, appear_timeout=25, solve_timeout=60):
    # 1) 等组件渲染出来
    t0 = time.time()
    present = False
    while time.time() - t0 < appear_timeout:
        try:
            if page.run_js(_TS_PRESENT_JS):
                present = True
                logger.info("检测到登录 Turnstile 组件")
                break
        except: pass
        time.sleep(2)

    if not present and _get_cf_iframe_rect(page):
        logger.info("明 DOM 未标记,但 CDP pierce 发现 CF iframe,继续处理")
        present = True

    if not present:
        logger.info("确认无盾,直接提交")
        return True

    time.sleep(2)  # 等 iframe 内部渲染

    # 2) 先给 managed 模式几秒自动过的机会;仍不出 token 再点复选框(交互模式)
    t1 = time.time()
    clicked = False
    last_click = 0
    while time.time() - t1 < solve_timeout:
        elapsed = time.time() - t1
        try:
            tok = page.run_js(_TS_TOKEN_JS)
        except:
            tok = None
        if tok:  # 非空字符串 = 已解出 token
            logger.info(f"✅ Turnstile 已解,token len={len(tok)}")
            return True
        now = time.time()
        # 前 6 秒不点,等 managed 自动过;之后点一次,再每 12 秒重试点击
        should_click = (elapsed >= 6 and not clicked) or (clicked and now - last_click > 12)
        if should_click:
            cx, cy = _get_cf_click_coords_inline(page)
            if cx and cy:
                logger.info(f"🛡️ 点击 Turnstile 复选框 ({cx},{cy})")
                if not cdp_click(page, cx, cy):
                    xdotool_click(page, {"cx": cx, "cy": cy})
                clicked = True
                last_click = now
        time.sleep(2)

    logger.error("❌ Turnstile 未在超时内解出")
    dump_state(page, "turnstile-timeout", shot=True)
    return False

# ================== 主线业务逻辑 ==================
def main():
    if not USERNAME or not PASSWORD:
        logger.error("环境变量 USERNAME 或 PASSWORD 为空！")
        sys.exit(1)

    logger.info("初始化 DrissionPage...")
    co = ChromiumOptions()
    co.set_argument('--no-sandbox')
    co.set_argument('--disable-gpu')
    co.set_argument('--disable-dev-shm-usage')
    co.set_argument('--disable-blink-features=AutomationControlled')
    co.set_argument('--disable-features=IsolateOrigins,site-per-process')
    co.set_argument('--window-size=1280,900')
    # 每次用独立端口 + 独立用户目录,避免与残留 Chromium 冲突(端口 9222 连不上的根因)
    co.auto_port()
    if PROXY:
        co.set_argument(f'--proxy-server={PROXY}')
        logger.info(f"已配置底层代理: {PROXY}")

    # 启动重试:CI 环境浏览器偶发起不来
    page = None
    for attempt in range(3):
        try:
            page = ChromiumPage(co)
            break
        except Exception as e:
            logger.warning(f"浏览器启动失败(第{attempt+1}次): {e}")
            os.system("pkill -9 -f chrome 2>/dev/null; pkill -9 -f chromium 2>/dev/null")
            time.sleep(3)
    if page is None:
        logger.error("浏览器连续 3 次启动失败,退出")
        sys.exit(1)
    page.add_init_js(STEALTH_JS)

    try:
        logger.info(f"正在访问登录页: {LOGIN_URL}")
        page.get(LOGIN_URL)
        time.sleep(3)

        logger.info("检查首页是否有整页 CF 拦截...")
        if not wait_for_cloudflare(page, timeout=45):
            dump_state(page, "cf-block-fail", shot=True)
            sys.exit(1)

        # ====== 登录阶段 ======
        logger.info("尝试填写账号密码...")
        email_input = page.ele('css:input#email', timeout=10)
        pwd_input = page.ele('css:input#password', timeout=2)

        if not email_input or not pwd_input:
            logger.error("未找到登录表单,可能被死盾卡住")
            dump_state(page, "02-no-form", shot=True)
            sys.exit(1)

        email_input.clear()
        email_input.input(USERNAME)
        pwd_input.clear()
        pwd_input.input(PASSWORD)
        logger.info(f"账号 {USERNAME[:3]}*** 已输入")

        # ====== 关键:提交前解登录表单 Turnstile ======
        if not solve_login_turnstile(page):
            logger.error("登录 Turnstile 未通过,放弃提交")
            dump_state(page, "03b-turnstile-failed", shot=True)
            sys.exit(1)

        logger.info("正在提交登录...")
        submit_btn = page.ele('css:button.submit-btn') or page.ele('css:button[type="submit"]')
        if submit_btn:
            submit_btn.click()
        else:
            page.run_js('document.querySelector("form").submit()')

        time.sleep(5)
        logger.info("检查登录过程中是否触发 CF 拦截...")
        wait_for_cloudflare(page, timeout=30)

        if "/login" in page.url:
            logger.error("登录失败,依然停留在 Login 页面")
            dump_state(page, "04-login-failed", shot=True)
            sys.exit(1)

        logger.info("✅ 登录成功！")

        # ====== 续期阶段 ======
        logger.info("寻找服务器卡片...")
        server_card = page.ele('css:a.server-card', timeout=10) or page.ele('css:.server-card', timeout=3)
        if not server_card:
            logger.error("未找到任何服务器卡片,账号下可能没有机器")
            dump_state(page, "06-no-server", shot=True)
            sys.exit(1)

        logger.info("进入服务器详情页...")
        server_card.click()
        time.sleep(5)

        logger.info("寻找续期按钮...")
        renew_btn = page.ele('text:Renew', timeout=5) or page.ele('text:Extend', timeout=2) or page.ele('css:.btn-primary:contains("Renew")', timeout=2)
        if not renew_btn:
            logger.info("未找到显式的续期按钮 (可能已达续期上限或页面结构变了)")
            sys.exit(0)

        logger.info("点击续期按钮...")
        renew_btn.click()
        time.sleep(3)

        logger.info("检查续期后是否触发 CF 验证...")
        wait_for_cloudflare(page, timeout=30)
        screenshot(page, "08-renew-result")  # 留一张成功凭证

        logger.info("✅ 整个续期流程顺利完成！")
        sys.exit(0)

    except Exception as e:
        logger.error(f"发生不可预知的脚本崩溃: {e}")
        dump_state(page, "error-crash", shot=True)
        sys.exit(1)
    finally:
        page.quit()

if __name__ == "__main__":
    main()
