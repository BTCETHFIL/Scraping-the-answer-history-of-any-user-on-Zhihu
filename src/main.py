"""
知乎回答爬虫 — 主入口
用法:
  python main.py                          # 从 config.json 读取配置运行
  python main.py <用户ID>                 # 爬取指定用户
  python main.py <用户ID1> <用户ID2> ...  # 批量爬取
  python main.py --url <回答链接>          # 手动保存单条回答为 MD（无需配置用户ID）
  python main.py --config                 # 生成默认 config.json
  python main.py --help                   # 查看帮助
"""

import sys
import os
from pathlib import Path

# Windows 控制台 UTF-8 编码（支持 emoji）
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 确保工作目录在项目根目录
os.chdir(Path(__file__).parent.parent)

# 初始化日志（文件滚动覆盖，2MB×3）
from log_setup import init_logging
init_logging()

# 惰性导入：非爬取模式的命令（--config / --help）无需加载爬虫模块
from config import config
from utils import extract_user_id


def print_banner():
    print("""
╔══════════════════════════════════════════╗
║        📚 知乎回答爬虫 v1.0              ║
║  抓取指定用户的回答历史，导出为 MD 文件    ║
╚══════════════════════════════════════════╝
""")


def print_help():
    print_banner()
    print("用法:")
    print("  python main.py                       从 config.json 读取配置运行")
    print("  python main.py <用户ID>              爬取指定用户")
    print("  python main.py <ID1> <ID2> ...       批量爬取")
    print("  python main.py --url <回答链接>       手动保存单条回答为MD（无需配置用户ID）")
    print("  python main.py --config              生成默认 config.json")
    print("  python main.py --help                查看帮助\n")
    print("支持的用户标识格式:")
    print("  · 纯用户ID:         zhang-jia-wei")
    print("  · 完整主页链接:      https://www.zhihu.com/people/zhang-jia-wei")
    print("  · 回答页链接:        https://www.zhihu.com/people/zhang-jia-wei/answers\n")
    print("手动链接保存 (--url):")
    print("  python main.py --url https://www.zhihu.com/question/12345/answer/67890")
    print("  支持任何公开的知乎回答链接，无需预配置用户ID，直接保存为MD\n")
    print("配置文件说明:")
    print("  config.json 中的 targets 字段填写要爬取的用户列表")
    print("  max_answers: 0 表示爬取全部，设为数字则限制条数")
    print("  headless: true 可后台运行（首次登录需设为 false）\n")


def main():
    print_banner()

    # 解析命令行参数
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]

    if '--help' in flags:
        print_help()
        return

    if '--config' in flags:
        config.save("config.json")
        print("✅ 已生成默认配置文件 config.json")
        print("   请编辑该文件，填写 targets 字段后运行 python main.py")
        return

    # ── 手动链接模式: --url <回答链接> ──
    if '--url' in sys.argv:
        url_idx = sys.argv.index('--url')
        if url_idx + 1 >= len(sys.argv):
            print("❌ 请提供知乎回答链接: python main.py --url <链接>")
            return
        url = sys.argv[url_idx + 1]
        if 'zhihu.com' not in url:
            print("❌ 请提供有效的知乎回答链接（zhihu.com）")
            return

        from playwright.sync_api import sync_playwright
        from auth import ensure_login, get_login_state_path
        from crawler import crawl_single_url

        print(f"🔗 手动链接模式: {url}")
        with sync_playwright() as p:
            launch_kwargs = {
                'headless': config.headless,
                'args': [
                    '--disable-blink-features=AutomationControlled',
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                ]
            }
            if config.chrome_exe:
                launch_kwargs['executable_path'] = config.chrome_exe

            browser = p.chromium.launch(**launch_kwargs)
            state_path = get_login_state_path(config.browser_data_dir)
            context_kwargs = {
                'viewport': {'width': 1920, 'height': 1080},
                'device_scale_factor': 2,
                'user_agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
                'locale': 'zh-CN',
            }

            if state_path.exists():
                try:
                    context = browser.new_context(storage_state=str(state_path), **context_kwargs)
                except Exception:
                    context = browser.new_context(**context_kwargs)
            else:
                context = browser.new_context(**context_kwargs)

            page = context.new_page()

            try:
                ensure_login(page, config)
            except Exception as e:
                print(f"❌ 登录失败: {e}")
                browser.close()
                return

            result = crawl_single_url(page, url, output_dir=config.output_dir)
            if result['success']:
                print(f"✅ 已保存: {result['md_path']}")
            else:
                print(f"❌ 保存失败: {result['error']}")

            browser.close()
        return

    # 确定目标用户列表
    if args:
        targets = args
    elif config.targets:
        targets = config.targets
    else:
        print("❌ 未指定目标用户。请通过以下方式指定：")
        print("   1. 命令行: python main.py <用户ID>")
        print("   2. 编辑 config.json 的 targets 字段")
        print("   3. 运行 python main.py --config 生成配置文件")
        return

    # 解析用户 ID
    user_ids = []
    for t in targets:
        try:
            uid = extract_user_id(t)
            user_ids.append(uid)
        except ValueError as e:
            print(f"⚠ 跳过无效目标 '{t}': {e}")

    if not user_ids:
        print("❌ 没有有效的目标用户")
        return

    print(f"🎯 目标用户: {', '.join(user_ids)}")
    print(f"📊 最多爬取: {'全部' if config.max_answers == 0 else config.max_answers} 条/人")
    print(f"🖼 下载图片: {'是' if config.download_images else '否'}")
    print()

    # 运行时导入（避免 --config / --help 时加载重依赖）
    from playwright.sync_api import sync_playwright
    from auth import ensure_login, get_login_state_path
    from crawler import crawl_user_answers

    # 启动浏览器
    with sync_playwright() as p:
        # 优先使用用户指定的 Chrome，否则用 Playwright 内置 Chromium
        launch_kwargs = {
            'headless': config.headless,
            'args': [
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-dev-shm-usage',
                '--start-maximized',
            ]
        }
        if config.chrome_exe:
            launch_kwargs['executable_path'] = config.chrome_exe

        browser = p.chromium.launch(**launch_kwargs)

        # 尝试复用登录状态
        state_path = get_login_state_path(config.browser_data_dir)
        context_kwargs = {
            'viewport': {'width': 1920, 'height': 1080},
            'device_scale_factor': 2,
            'user_agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/120.0.0.0 Safari/537.36'
            ),
            'locale': 'zh-CN',
        }

        if state_path.exists():
            try:
                context = browser.new_context(
                    storage_state=str(state_path),
                    **context_kwargs
                )
                print("🔑 已加载登录状态")
            except Exception:
                context = browser.new_context(**context_kwargs)
        else:
            context = browser.new_context(**context_kwargs)

        page = context.new_page()

        # 确保登录
        try:
            ensure_login(page, config)
        except Exception as e:
            print(f"❌ 登录失败: {e}")
            browser.close()
            return

        # 爬取所有用户
        results = []
        for uid in user_ids:
            try:
                r = crawl_user_answers(page, uid)
                results.append(r)
            except Exception as e:
                print(f"❌ 爬取 {uid} 失败: {e}")
                import traceback
                traceback.print_exc()
                import traceback
                traceback.print_exc()

        # 汇总
        print("\n" + "=" * 60)
        print("📊 全部完成！汇总：")
        print("=" * 60)
        total_success = sum(r['success'] for r in results)
        total_failed = sum(r['failed'] for r in results)
        total_skip = sum(r['skipped'] for r in results)
        print(f"   爬取成功: {total_success} 条")
        print(f"   爬取失败: {total_failed} 条")
        print(f"   跳过重复: {total_skip} 条")
        print(f"   输出目录: {Path(config.output_dir).resolve()}")

        # 记录爬取历史到 id_manager
        from id_manager import get_id_manager
        mgr = get_id_manager()
        for r in results:
            mgr.add_crawl_record(
                r['user_id'],
                r.get('success', 0),
                r.get('output_dir', str(Path(config.output_dir) / r['user_id']))
            )

        browser.close()


if __name__ == '__main__':
    main()
