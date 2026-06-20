# coding: utf-8
"""Reorganize flat MD files in zhihu output/manual/unknown/ into per-author subdirs.
Uses Playwright with stored browser cookies to fetch answer pages and extract author_id.
"""
import os, re, shutil, sys, io

# Workaround for Windows GBK encoding issues
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent.parent
MANUAL = BASE / "output" / "manual"
UNKNOWN_DIR = MANUAL / "unknown"
STATE_PATH = BASE / "browser_data" / "zhihu_state.json"

if not UNKNOWN_DIR.exists():
    print("No unknown dir, nothing to do.")
    sys.exit(0)

files = sorted(UNKNOWN_DIR.glob("*.md"))
print(f"Found {len(files)} files in unknown/\n")

# ── Extract answer URL from MD first line ──
answer_urls = {}
for fp in files:
    try:
        first_line = fp.read_text(encoding="utf-8").split("\n", 1)[0]
        m = re.search(r'https?://www\.zhihu\.com/question/\d+/answer/\d+', first_line)
        if m:
            answer_urls[fp.name] = m.group(0)
        else:
            print(f"  [SKIP] {fp.name}: no answer URL found in first line")
    except Exception as e:
        print(f"  [SKIP] {fp.name}: read error {e}")

print(f"{len(answer_urls)} files have extractable answer URLs\n")

# ── Launch Playwright with stored cookies ──
print("Launching browser...")
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-dev-shm-usage',
        ]
    )

    context_kwargs = {
        'viewport': {'width': 1920, 'height': 1080},
        'user_agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        'locale': 'zh-CN',
    }

    if STATE_PATH.exists():
        try:
            context = browser.new_context(storage_state=str(STATE_PATH), **context_kwargs)
            print("Using stored browser state.")
        except Exception as e:
            print(f"State load failed ({e}), using fresh context.")
            context = browser.new_context(**context_kwargs)
    else:
        print("No stored state found, using fresh context (may fail auth).")
        context = browser.new_context(**context_kwargs)

    page = context.new_page()

    moved = 0
    failed = 0

    for i, (fname, url) in enumerate(answer_urls.items(), 1):
        src = UNKNOWN_DIR / fname
        print(f"[{i}/{len(answer_urls)}] {fname[:55]}...")

        author_id = ""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Extract author_id from author element href
            try:
                author_el = page.locator(
                    '.AuthorInfo-name, .UserLink-link, '
                    '[class*="author"] a[class*="name"], '
                    '.ContentItem-authorInfo a'
                ).first
                if author_el.count() > 0:
                    href = author_el.get_attribute('href') or ''
                    m_aid = re.search(r'/people/([^/?]+)', href)
                    if m_aid:
                        author_id = m_aid.group(1)
            except Exception:
                pass

            # Fallback: any /people/ link on page
            if not author_id:
                try:
                    any_link = page.locator('a[href*="/people/"]').first
                    if any_link.count() > 0:
                        href = any_link.get_attribute('href') or ''
                        m_aid = re.search(r'/people/([^/?]+)', href)
                        if m_aid:
                            author_id = m_aid.group(1)
                except Exception:
                    pass

        except Exception as e:
            print(f"  [FAIL] page load error: {e}")

        if author_id:
            dst_dir = MANUAL / author_id
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst_dir / fname))
            print(f"  -> manual/{author_id}/")
            moved += 1
        else:
            print(f"  -> kept in manual/unknown/")
            failed += 1

    browser.close()

print(f"\nDone: {moved} moved, {failed} kept in unknown/")
