"""
知乎登录认证模块
支持三种方式：
  1. 复用已保存的 Cookie (storage_state)
  2. 扫码登录（需有桌面环境显示浏览器窗口）
  3. 手动粘贴 Cookie 字符串
"""

import json
import time
from pathlib import Path
from playwright.sync_api import Page


def get_login_state_path(browser_data_dir: str) -> Path:
    """获取登录状态文件路径"""
    return Path(browser_data_dir) / "zhihu_state.json"


def get_cookie_path(browser_data_dir: str) -> Path:
    """获取手动 Cookie 文件路径"""
    return Path(browser_data_dir) / "cookies.json"


def is_logged_in(state_path: Path) -> bool:
    """检查是否有有效的登录状态"""
    if not state_path.exists():
        return False
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        has_key = any(c.get("name") == "z_c0" for c in cookies)
        return has_key
    except Exception:
        return False


def load_manual_cookies(cookie_path: Path) -> list:
    """从 cookies.json 加载手动 Cookie 列表"""
    if not cookie_path.exists():
        return []
    try:
        data = json.loads(cookie_path.read_text(encoding="utf-8"))
        # 支持两种格式：直接是 cookie 数组，或 {cookies: [...]}
        if isinstance(data, list):
            return data
        return data.get("cookies", [])
    except Exception:
        return []


def inject_manual_cookies(page: Page, cookies: list):
    """将 Cookie 列表注入到浏览器"""
    for c in cookies:
        # Playwright 要求 domain 开头带 .
        if 'domain' in c and not c['domain'].startswith('.'):
            c['domain'] = '.' + c['domain']
        try:
            page.context.add_cookies([c])
        except Exception:
            pass
    page.reload()
    time.sleep(2)


def check_login_status(page: Page) -> bool:
    """检查当前是否已登录"""
    try:
        # 访问首页，检查是否有登录用户信息
        page.goto("https://www.zhihu.com", wait_until="domcontentloaded", timeout=15000)
        time.sleep(2)
        # 检查是否存在用户头像/导航栏等登录特征
        is_login = page.evaluate("""() => {
            return document.querySelector('.AppHeader-profile') !== null ||
                   document.querySelector('.SignFlowHomepage') === null;
        }""")
        return bool(is_login)
    except Exception:
        return False


def login_qrcode(page: Page, state_path: Path):
    """通过扫码登录知乎，保存登录态"""
    print("📱 正在打开知乎登录页...")
    page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded")
    time.sleep(3)

    # 尝试切换到扫码登录
    try:
        # 知乎新版登录页可能默认显示二维码
        # 尝试点击"扫码登录"tab
        qr_tab = page.query_selector('text=扫码登录')
        if qr_tab:
            qr_tab.click()
            time.sleep(1)
    except Exception:
        pass

    print("📱 请使用知乎 APP 扫描浏览器窗口中的二维码...")
    print("   ⏳ 等待扫码（最长 120 秒）...")

    # 等待登录成功
    try:
        page.wait_for_url(
            lambda url: "zhihu.com" in url and "signin" not in url,
            timeout=120000
        )
        time.sleep(2)
        print("✅ 登录成功！")
    except Exception:
        print("⚠ 登录超时或失败")
        # 再检查一次，可能已经登录了
        if check_login_status(page):
            print("✅ 检测到已登录状态")
        else:
            raise RuntimeError("扫码登录失败，请重试或使用手动 Cookie 方式")

    # 保存登录状态
    state_path.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(state_path))
    print(f"💾 登录态已保存至 {state_path}")


def ensure_login(page: Page, config) -> bool:
    """确保已登录：优先复用已有状态 → 手动Cookie → 扫码"""
    state_path = get_login_state_path(config.browser_data_dir)
    cookie_path = get_cookie_path(config.browser_data_dir)

    # 1. 尝试复用 storage_state
    if is_logged_in(state_path):
        print("🔑 检测到已保存的登录状态，尝试复用...")
        if check_login_status(page):
            print("✅ 登录状态有效")
            return True
        else:
            print("⚠ 登录状态已过期，重新认证...")

    # 2. 尝试手动 Cookie
    manual_cookies = load_manual_cookies(cookie_path)
    if manual_cookies:
        print("🍪 检测到手动 Cookie，尝试注入...")
        inject_manual_cookies(page, manual_cookies)
        if check_login_status(page):
            print("✅ Cookie 注入成功，已登录")
            # 保存为 storage_state 方便下次复用
            state_path.parent.mkdir(parents=True, exist_ok=True)
            page.context.storage_state(path=str(state_path))
            return True
        else:
            print("⚠ Cookie 已过期，需要重新获取")

    # 3. 扫码登录
    print("🔑 需要登录认证")
    print("   ───────────────────────────────")
    print("   方式一：手动提供 Cookie（推荐，无需桌面环境）")
    print("     1. 用 Chrome 登录 zhihu.com")
    print("     2. F12 → Application → Cookies → zhihu.com")
    print("     3. 复制所有 Cookie 到 browser_data/cookies.json")
    print("     格式: [{\"name\":\"z_c0\",\"value\":\"...\",\"domain\":\"zhihu.com\"},...]")
    print("   ───────────────────────────────")
    print("   方式二：扫码登录（需要桌面环境显示浏览器）")
    print("   ───────────────────────────────")

    # 尝试扫码
    login_qrcode(page, state_path)
    return True

