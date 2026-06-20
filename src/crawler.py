"""
知乎回答爬虫核心模块
基于 Playwright 浏览器自动化
"""

import base64
import re
import time
import random
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import Page, BrowserContext

from config import config
from converter import html_to_md, extract_answer_meta
from storage import (
    save_answer, save_progress, load_progress,
    save_index, embed_images_base64,
    check_missing_files,
    load_links_cache, save_links_cache,
    load_answer_cache, save_answer_cache,
)
from utils import (
    random_delay, extract_user_id,
    sanitize_filename, format_datetime,
    get_output_path, extract_date_from_html,
    StopCrawlException,
)
from log_setup import log_print


def _is_stopped(stop_event) -> bool:
    """检查停止信号（兼容 None）"""
    return stop_event is not None and stop_event.is_set()


def _split_keywords(keyword_str: str) -> list:
    """按逗号/分号拆分多关键词列表，去空白去重。空输入返回空列表。"""
    if not keyword_str or not keyword_str.strip():
        return []
    parts = re.split(r'[,，;]', keyword_str)
    seen = set()
    result = []
    for p in parts:
        kw = p.strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def _any_keyword_matches(text: str, keywords: list) -> bool:
    """不区分大小写检查文本是否匹配任一关键词。keywords 为空时返回 True（全部通过）。"""
    if not keywords:
        return True
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def collect_answer_links(page: Page, user_id: str, max_answers: int = 0,
                         stop_event=None, keyword: str = "",
                         keyword_min_match: int = 0) -> list:
    """
    在用户回答页面滚动加载，收集回答链接

    参数:
        keyword: 关键词字符串（逗号/分号分隔多个），用于标题匹配提前终止
        keyword_min_match: 标题匹配至少达到此数量后停止收集（0=不提前终止）
                          测试模式+关键词时设为 3，避免收集全部链接

    返回 [{title, url, answer_id, preview_html, upvotes, date}, ...]
    """
    answers_url = f"https://www.zhihu.com/people/{user_id}/answers"
    log_print(f"\n📋 正在加载回答列表: {answers_url}")

    # 解析关键词列表
    kws = _split_keywords(keyword) if keyword and keyword_min_match > 0 else []

    # 短时缓存检查：先看是否有未过期的链接缓存
    # 注意：有关键词提前终止时跳过缓存（缓存可能包含全部链接，需要重新收集以分批处理）
    from utils import get_output_path
    if not kws:
        cached = load_links_cache(get_output_path(config.output_dir, user_id), user_id)
        if cached:
            if max_answers > 0 and len(cached) > max_answers:
                cached = cached[:max_answers]
            log_print(f"✅ 使用缓存链接: {len(cached)} 条")
            return cached

    page.goto(answers_url, wait_until="domcontentloaded")
    time.sleep(3)

    collected = []
    seen_ids = set()
    scroll_attempts = 0
    max_scroll_attempts = 500  # 安全上限
    no_new_count = 0            # 连续无新内容的滚动次数

    while scroll_attempts < max_scroll_attempts:
        # 检查停止信号
        if _is_stopped(stop_event):
            log_print("\n  ⚠ 用户手动停止（收集链接阶段）")
            break
        # 提取当前页面上的所有回答卡片
        cards = page.query_selector_all('[itemprop="zhihu:answer"], '
                                         '.List-item, '
                                         '[class*="AnswerItem"], '
                                         '.ContentItem')

        new_found = 0
        for card in cards:
            try:
                # 提取回答链接
                link_el = card.query_selector(
                    'a[data-za-detail-view-element_name="Title"], '
                    '.ContentItem-title a, '
                    '.QuestionItem-title a, '
                    'h2 a, '
                    '[class*="question_link"]'
                )

                # 如果上面都没找到，找任意指向 /question/ 的链接
                if not link_el:
                    all_links = card.query_selector_all('a[href*="/answer/"]')
                    for a in all_links:
                        href = a.get_attribute('href') or ''
                        if '/answer/' in href:
                            link_el = a
                            break

                if not link_el:
                    continue

                href = link_el.get_attribute('href') or ''
                title = link_el.inner_text().strip()

                # 提取 answer_id
                m = re.search(r'/answer/(\d+)', href)
                if not m:
                    continue
                answer_id = m.group(1)

                if answer_id in seen_ids:
                    continue

                # 提取赞同数
                upvotes = 0
                vote_el = card.query_selector(
                    '[class*="VoteButton--up"], '
                    '[class*="votecount"], '
                    'button[aria-label*="赞同"]'
                )
                if vote_el:
                    vt = vote_el.inner_text().strip() or '0'
                    vt = vt.replace(',', '')
                    m_v = re.search(r'[\d.万]+', vt)
                    if m_v:
                        vs = m_v.group()
                        if '万' in vs:
                            upvotes = int(float(vs.replace('万', '')) * 10000)
                        else:
                            upvotes = int(float(vs))

                # 提取日期（多种策略）
                date = ''
                try:
                    card_html = card.evaluate('el => el.outerHTML')
                    date = extract_date_from_html(card_html)
                except Exception:
                    pass

                # 提取评论数（列表页）
                comment_count = 0
                try:
                    comment_el = card.query_selector('[class*="CommentButton"]')
                    if not comment_el:
                        # :has-text 不被 query_selector 支持，手动搜索含"评论"的按钮
                        for btn in card.query_selector_all('button'):
                            try:
                                if btn.is_visible() and '评论' in (btn.inner_text() or ''):
                                    comment_el = btn
                                    break
                            except Exception:
                                pass
                    if comment_el:
                        ct = comment_el.inner_text().strip() or '0'
                        m_c = re.search(r'[\d,.万]+', ct)
                        if m_c:
                            v = m_c.group().replace(',', '')
                            if '万' in v:
                                comment_count = int(float(v.replace('万', '')) * 10000)
                            else:
                                comment_count = int(float(v))
                except Exception:
                    pass

                # 构建完整 URL
                full_url = urljoin(page.url, href)

                seen_ids.add(answer_id)
                collected.append({
                    'title': title,
                    'url': full_url,
                    'answer_id': answer_id,
                    'upvotes': upvotes,
                    'date': date,
                    'comment_count': comment_count,
                })
                new_found += 1

                if max_answers > 0 and len(collected) >= max_answers:
                    break
            except Exception:
                continue

        log_print(f"  📜 已收集 {len(collected)} 条回答" + (f" (本轮 +{new_found})" if new_found else ""))

        if max_answers > 0 and len(collected) >= max_answers:
            break

        # 关键词提前终止：检查已收集链接中有多少标题匹配
        if kws and keyword_min_match > 0:
            title_match_count = sum(
                1 for it in collected
                if _any_keyword_matches(it.get('title', ''), kws)
            )
            if title_match_count >= keyword_min_match:
                log_print(f"  🎯 已找到 {title_match_count} 条标题匹配（≥{keyword_min_match}），停止收集")
                break

        # 无新内容计数
        if new_found == 0:
            no_new_count += 1
            if no_new_count >= 3:
                log_print("  ℹ 连续 3 次未发现新内容，已到达列表末尾")
                break
        else:
            no_new_count = 0

        # 滚动加载更多
        if _is_stopped(stop_event):
            log_print("\n  ⚠ 用户手动停止（收集链接阶段）")
            break
        random_delay(
            config.scroll_delay_min,
            config.scroll_delay_max,
            f"滚动加载 (第{scroll_attempts + 1}次)",
            stop_check=lambda: _is_stopped(stop_event) if stop_event else False
        )

        # 滚动到底部
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        # 也可以尝试点击"加载更多"按钮
        try:
            load_more = page.locator(
                'button:has-text("加载更多"), '
                '[class*="PaginationButton"], '
                '.List-footer button'
            ).first
            if load_more.count() > 0 and load_more.is_visible():
                load_more.click()
                time.sleep(1)
        except Exception:
            pass

        scroll_attempts += 1

    log_print(f"✅ 共收集 {len(collected)} 条回答链接")
    # 缓存链接列表（关键词提前终止时不缓存——只收集了部分链接）
    if not kws:
        from utils import get_output_path as _gop
        save_links_cache(_gop(config.output_dir, user_id), user_id, collected)
    return collected


def _capture_answer_area(page: Page) -> bytes:
    """
    截取知乎回答页的内容区：问题标题 + 目标回答正文（排除右侧栏和其他用户回答）
    策略：主列容器 → 单条回答卡片 clip → 识别"下一个回答"分界线裁剪 → 全页回退
    """
    # 策略1：查找主内容列容器（验证是否只含一个回答）
    content_selectors = [
        '.Question-mainColumn',
        '.Question-main',
        '[class*="QuestionPage-main"]',
        '.SearchMain',
    ]
    for sel in content_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                # 检查容器内有多少个回答条目
                answer_cards = el.query_selector_all(
                    '.AnswerItem, .AnswerCard, [class*="AnswerItem"], '
                    '[class*="answer-item"]'
                )
                if len(answer_cards) <= 1:
                    # 只有一个回答，直接截图容器
                    return el.screenshot(type='png')
                else:
                    # 多个回答：只截第一条回答卡片
                    first_answer = answer_cards[0]
                    if first_answer.is_visible():
                        return first_answer.screenshot(type='png')
        except Exception:
            pass

    # 策略2：定位问题标题 + 第一条回答卡片的 bounding box 做 clip
    try:
        q_header = page.query_selector(
            '.QuestionHeader, .QuestionHeader-main, '
            '[class*="QuestionHeader"], [class*="question-header"]'
        )
        # 使用 locator().first 精确拿第一条回答（而非 query_selector 返回的可能包裹多个的容器）
        first_answer = page.locator(
            '.AnswerItem, .AnswerCard, [class*="AnswerItem"], '
            '[class*="answer-item"]'
        ).first
        if q_header and first_answer:
            try:
                qb = q_header.bounding_box()
                ab = first_answer.bounding_box()
                if qb and ab:
                    x = min(qb['x'], ab['x'])
                    y = qb['y']
                    w = max(qb['x'] + qb['width'], ab['x'] + ab['width']) - x
                    h = ab['y'] + ab['height'] - y
                    # 安全检查：如果截取高度异常大（>5000px），可能是整页容器，
                    # 回退到仅截图第一个回答卡片
                    if h > 5000:
                        return first_answer.screenshot(type='png')
                    return page.screenshot(
                        clip={'x': x, 'y': y, 'width': w, 'height': h},
                        type='png'
                    )
            except Exception:
                pass
    except Exception:
        pass

    # 策略3：寻找 "下一个回答" / "更多回答" / 分割线 作为裁剪下界
    try:
        answer_card = page.locator(
            '.AnswerItem, .AnswerCard, [class*="AnswerItem"], '
            '[class*="answer-item"]'
        ).first
        if answer_card:
            try:
                ab = answer_card.bounding_box()
                if ab:
                    # 查找分界线：下一个回答的标题、分隔符、"推荐阅读"等
                    dividers = page.query_selector_all(
                        'h2:has-text("推荐阅读"), '
                        '[class*="NextAnswers"], '
                        '[class*="MoreAnswers"], '
                        '[class*="QuestionAnswers-answers"], '
                        '[class*="List-header"]'
                    )
                    clip_bottom = ab['y'] + ab['height']
                    for div in dividers:
                        try:
                            db = div.bounding_box()
                            if db and db['y'] > ab['y']:
                                clip_bottom = min(clip_bottom, db['y'])
                        except Exception:
                            pass
                    return page.screenshot(
                        clip={
                            'x': ab['x'],
                            'y': ab['y'],
                            'width': ab['width'],
                            'height': clip_bottom - ab['y']
                        },
                        type='png'
                    )
            except Exception:
                pass
    except Exception:
        pass

    # 策略4：回退整页截图
    return page.screenshot(full_page=True, type='png')


def sanitize_html_for_offline(html_text: str) -> str:
    """
    将知乎原始 HTML 清洗为离线友好的自包含版本：
    1. 移除所有外部 <link> (CSS) 引用 — 避免离线时超时阻塞渲染
    2. 移除所有 <script> 标签 — 去掉 Sentry/埋点等联网 JS
    3. 移除所有 @font-face 和外部字体引用 — 避免 FOUT
    4. 移除 CSS 动画/过渡 — 消除闪烁
    5. 添加最小化的内联样式 — 保证基本可读性
    """
    soup = BeautifulSoup(html_text, 'lxml')

    # 1. 移除外部样式表和预加载
    for tag in soup.find_all('link', rel=re.compile(r'stylesheet|preload|preconnect|dns-prefetch', re.I)):
        tag.decompose()
    # 移除 canonical（避免浏览器跳转）、alternate 等
    for tag in soup.find_all('link', rel=re.compile(r'canonical|alternate', re.I)):
        tag.decompose()

    # 2. 移除所有 <script>
    for tag in soup.find_all('script'):
        tag.decompose()

    # 3. 移除 <noscript> 和 <meta> 刷新/重定向
    for tag in soup.find_all('noscript'):
        tag.decompose()
    for tag in soup.find_all('meta', attrs={'http-equiv': re.compile(r'refresh', re.I)}):
        tag.decompose()

    # 4. 移除 <style> 中的 @font-face / @import / 动画
    for style_tag in soup.find_all('style'):
        if style_tag.string:
            css = style_tag.string
            # 移除 @font-face 块
            css = re.sub(r'@font-face\s*\{[^}]*\}', '', css)
            # 移除 @import
            css = re.sub(r'@import[^;]+;', '', css)
            # 移除 keyframes 动画
            css = re.sub(r'@(?:-webkit-|-moz-|-o-)?keyframes\s+\S+\s*\{[^}]*\}', '', css)
            # 移除 transition/animation 属性 (简化处理：清除这些属性值)
            css = re.sub(r'transition\s*:\s*[^;]+;?', '', css)
            css = re.sub(r'animation\s*:\s*[^;]+;?', '', css)
            # 禁用所有 transition
            css = re.sub(r'([^{};]*transition[^{};]*)', '', css)
            style_tag.string = css

    # 5. 注入基础样式 — 保证离线可读
    base_style = """
    body {
        max-width: 800px;
        margin: 0 auto;
        padding: 20px;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        font-size: 16px;
        line-height: 1.8;
        color: #1a1a1a;
        background: #fff;
        -webkit-font-smoothing: antialiased;
    }
    img {
        max-width: 100%;
        height: auto;
    }
    pre, code {
        white-space: pre-wrap;
        word-break: break-all;
    }
    """
    style_el = soup.new_tag('style')
    style_el.string = base_style
    head = soup.find('head')
    if head:
        head.insert(0, style_el)

    # 6. 移除固定定位元素（知乎导航栏等）
    for tag in soup.find_all(style=re.compile(r'position\s*:\s*fixed')):
        tag.decompose()

    # 7. 移除懒加载占位属性，让图片直接显示
    for img in soup.find_all('img'):
        if img.get('data-src') or img.get('data-original'):
            actual_src = img.get('data-src') or img.get('data-original') or ''
            img['src'] = actual_src
        # 移除懒加载 class（避免触发淡入动画残留样式）
        classes = img.get('class', [])
        if classes:
            img['class'] = [c for c in classes if 'lazy' not in c.lower()]

    return str(soup)


def crawl_answer_combined(page: Page, item: dict,
                         user_profile: dict = None) -> dict:
    """
    混合模式：一次页面访问，同时获取截图 + 文字（含内嵌图片）
    上半部分 = 内容区截图（问题+回答，排除右侧栏），下半部分 = 可搜索/复制的文字
    截图保存为独立 PNG 文件（{answer_id}.png），MD 中用相对路径引用
    返回 {meta, md_text, screenshot_bytes, html_text}
    """
    answer_id = item['answer_id']
    url = item['url']
    title = item.get('title', '')

    log_print(f"  🎯 混合模式: {title[:50]}...")

    try:
        # ── 1. 导航 & 等待渲染 ──
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        try:
            page.wait_for_selector(
                '.RichContent-inner, .RichContent, '
                '[class*="answer-content"], [class*="AnswerItem"]',
                timeout=10000
            )
        except Exception:
            pass

        # ── 2. 展开折叠内容 ──
        try:
            expand_btn = page.locator(
                'button:has-text("阅读全文"), '
                '.RichContent-uncollapse, '
                '[class*="expand"], [class*="unfold"]'
            ).first
            if expand_btn.count() > 0 and expand_btn.is_visible():
                expand_btn.click()
                time.sleep(1.5)
        except Exception:
            pass

        # ── 3. 等待图片加载 ──
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass
        time.sleep(1)

        # ── 4. 截图：仅问题+回答内容区（排除右侧栏）──
        screenshot_bytes = _capture_answer_area(page)
        # 截图以 base64 data URI 嵌入 MD，保证 MD 文件移动到任意位置后图片仍可显示
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('ascii')

        # ── 5. 提取文字内容 ──
        raw_html = page.content()
        # 清洗 HTML：去除外部依赖，生成离线友好的自包含版本
        html_text = sanitize_html_for_offline(raw_html)
        meta = extract_answer_meta(html_text)
        meta['answer_id'] = answer_id
        meta['title'] = title or meta.get('title', '')
        meta['answer_url'] = url
        meta['crawl_time'] = time.strftime('%Y-%m-%d %H:%M:%S')
        if not meta.get('upvotes') and item.get('upvotes'):
            meta['upvotes'] = item['upvotes']
        if not meta.get('date') and item.get('date'):
            meta['date'] = item['date']
        if not meta.get('comment_count') and item.get('comment_count'):
            meta['comment_count'] = item['comment_count']

        # 生成文字版 MD 并嵌入图片
        text_md = html_to_md(html_text, answer_meta=meta)
        if config.download_images:
            text_md = embed_images_base64(text_md)

        # ── 6. 合并文档：头部 + 截图 + 分隔 + 文字 ──
        lines = []
        lines.append(f"# [{meta.get('title', '未命名回答')}]({url})")
        lines.append("")

        author = meta.get('author', '')
        date = meta.get('date', '')
        upvotes = meta.get('upvotes', 0)
        comment_count = meta.get('comment_count', 0)
        crawl_time = meta.get('crawl_time', '')
        meta_line = ""
        if author:
            meta_line += f"**{author}**"
        if date:
            meta_line += f" / {date}" if meta_line else date
        if upvotes:
            meta_line += f" / 👍 {upvotes}" if meta_line else f"👍 {upvotes}"
        if comment_count:
            meta_line += f" / 💬 {comment_count}" if meta_line else f"💬 {comment_count}"
        if meta_line:
            lines.append(meta_line)
            lines.append("")

        if crawl_time:
            lines.append(f"> 🕒 证据采集时间: {crawl_time}")
        lines.append(f"> 🔗 原文链接: [{url}]({url})")

        # 用户影响力数据
        if user_profile:
            parts = []
            up_rcv = user_profile.get('upvotes_received', 0)
            if up_rcv:
                parts.append(f"赞同 {up_rcv:,}")
            fl_ers = user_profile.get('followers', 0)
            if fl_ers:
                parts.append(f"关注者 {fl_ers:,}")
            fl_ing = user_profile.get('following', 0)
            if fl_ing:
                parts.append(f"关注了 {fl_ing:,}")
            likes = user_profile.get('likes_received', 0)
            if likes:
                parts.append(f"喜欢 {likes:,}")
            colls = user_profile.get('collections', 0)
            if colls:
                parts.append(f"收藏 {colls:,}")
            pedits = user_profile.get('public_edits', 0)
            if pedits:
                parts.append(f"公共编辑 {pedits:,}")
            answers = user_profile.get('answers_count', 0)
            if answers:
                parts.append(f"回答 {answers:,}")
            if parts:
                lines.append(f"> 📊 用户影响力: {' · '.join(parts)}")

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📸 原页面截图")
        lines.append("")
        lines.append(f"![问题与回答截图](data:image/png;base64,{screenshot_b64})")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📝 文字内容")
        lines.append("")

        # 从 text_md 中提取正文内容（去掉重复的头部 meta）
        # html_to_md 输出格式: # [title] \n\n meta \n\n > 🔗 \n\n --- \n\n body
        text_lines = text_md.split('\n')
        content_start = 0
        for j, line in enumerate(text_lines):
            if line.strip() == '---':
                # 找到分隔线后，跳过其后的空行
                content_start = j + 1
                while content_start < len(text_lines) and text_lines[content_start].strip() == '':
                    content_start += 1
                break

        if content_start > 0 and content_start < len(text_lines):
            lines.extend(text_lines[content_start:])
        else:
            # fallback: 跳过标题和元信息行
            skip = 2  # 标题 + 空行
            while skip < len(text_lines) and text_lines[skip].strip() in ('', '---'):
                skip += 1
            lines.extend(text_lines[skip:])

        md_text = '\n'.join(lines)

        return {
            'meta': meta,
            'md_text': md_text,
            'screenshot_bytes': screenshot_bytes,
            'html_text': html_text,
        }

    except Exception as e:
        log_print(f"  ✗ 混合模式失败: {e}")
        meta = {
            'title': title,
            'answer_id': answer_id,
            'answer_url': url,
            'author': '',
            'date': item.get('date', ''),
            'upvotes': item.get('upvotes', 0),
            'comment_count': item.get('comment_count', 0),
            'crawl_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        return {
            'meta': meta,
            'md_text': f"# {title}\n\n> 🕒 证据采集时间: {meta['crawl_time']}\n\n> 混合模式失败: {e}",
            'screenshot_bytes': b'',
            'html_text': '',
        }


def get_crawl_status(user_id: str) -> dict:
    """
    预检查：返回用户的爬取状态摘要
    {user_id, output_dir, total_progress, existing_count, missing_count, missing_details}
    """
    output_dir = get_output_path(config.output_dir, user_id)
    existing, missing, missing_details = check_missing_files(output_dir)
    completed_set = load_progress(output_dir)

    return {
        'user_id': user_id,
        'output_dir': output_dir,
        'total_progress': len(completed_set),
        'existing_count': len(existing),
        'missing_count': len(missing),
        'missing_ids': missing,
        'missing_details': missing_details,
    }


def crawl_user_answers(page: Page, user_id: str,
                       force_recrawl_ids: set = None,
                       stop_event=None,
                       keyword: str = "") -> dict:
    """
    爬取指定用户的所有回答

    参数:
        page: Playwright Page 对象
        user_id: 用户 ID（纯字符串）
        force_recrawl_ids: 强制重新爬取的回答 ID 集合
                          （这些 ID 即使有进度记录也会重新爬取）
        stop_event: threading.Event，用于支持 GUI 中途停止
        keyword: 关键词过滤（仅抓取标题含此关键词的回答）

    返回统计信息
    """
    # 先采集用户影响力数据（获取昵称用于目录命名）
    user_profile = scrape_user_profile(page, user_id)
    nickname = user_profile.get('nickname', '')

    # 预检查文件状态（使用含昵称的目录名；首次运行时会自动迁移旧目录）
    output_dir = get_output_path(config.output_dir, user_id, nickname)
    existing, missing, _ = check_missing_files(output_dir)
    completed_set = load_progress(output_dir)

    # 强制忽略缓存：清空进度，从头重爬
    if config.force_no_cache:
        if completed_set:
            log_print(f"🔄 强制忽略缓存：已清除 {len(completed_set)} 条进度记录，将全部重新爬取")
            completed_set = set()
            # 同时清空 force_recrawl_ids，避免双重提示
            force_recrawl_ids = None

    # 强制重新爬取：把 force_recrawl_ids 从 completed_set 中移除
    if force_recrawl_ids:
        removed = completed_set & force_recrawl_ids
        completed_set -= force_recrawl_ids
        if removed:
            log_print(f"🔄 强制重新爬取 {len(removed)} 条（本地文件缺失）")

    log_print(f"\n{'='*60}")
    log_print(f"🐾 开始爬取用户: {nickname or user_id} ({user_id})")
    log_print(f"{'='*60}")

    # 测试模式：强制限制为 3 条
    max_answers = config.max_answers
    if config.test_mode:
        if keyword:
            # 测试模式+关键词：5个5个收集，找到3条标题匹配即停止
            log_print("🧪 测试模式（含关键词过滤）：边收集边筛选 → 标题匹配≥3条即停 → 至多爬3条")
        else:
            max_answers = 3
            log_print("🧪 测试模式：只爬取前 3 条回答")
        log_print()

    log_print("🎯 混合模式：截图(保留原文排版) + 文字(可搜索/复制) + base64图片嵌入")
    log_print()

    log_print(f"📁 输出目录: {output_dir}")
    log_print(f"📋 已完成: {len(completed_set)} 条"
          + (f" (其中 {len(missing)} 条本地文件缺失)" if missing else ""))

    # 收集回答链接
    try:
        items = collect_answer_links(
            page, user_id, max_answers, stop_event=stop_event,
            keyword=keyword if config.test_mode else "",
            keyword_min_match=3 if config.test_mode else 0,
        )
    except StopCrawlException:
        log_print("\n  ⚠ 用户手动停止（收集链接阶段）")
        items = []

    # 关键词过滤（标题 OR 内容，多关键词 OR 关系，去重）
    check_content_ids = set()  # 需爬取后检查内容的 answer_id 集合
    if keyword:
        keywords = _split_keywords(keyword)
        if keywords:
            before = len(items)
            title_match = []
            title_no_match = []
            for it in items:
                if _any_keyword_matches(it.get('title', ''), keywords):
                    title_match.append(it)
                else:
                    title_no_match.append(it)
            # 去重：每个回答只在一个列表中（answer_id 已在 collect_answer_links 中去重）
            for it in title_no_match:
                check_content_ids.add(it['answer_id'])
            items = title_match + title_no_match
            kw_display = ' / '.join(keywords)
            log_print(f"🔍 关键词过滤（{len(keywords)}个 / 标题OR内容 / 任一命中即抓取）:")
            log_print(f"   「{kw_display}」")
            log_print(f"   标题匹配: {len(title_match)} 条（直接抓取）")
            log_print(f"   待查内容: {len(title_no_match)} 条（爬取后检查回答内容）")
            log_print(f"   合计 {len(items)}/{before} 条进入爬取队列")

    # 测试模式+关键词：只保留前 3 条进入爬取（collect_answer_links 已提前停止收集，此处兜底）
    if config.test_mode and keyword and len(items) > 3:
        items = items[:3]
        check_content_ids = {it['answer_id'] for it in items if it['answer_id'] in check_content_ids}
        log_print(f"🧪 测试模式：取前 {len(items)} 条匹配项进入爬取")

    # 过滤已完成的（force_recrawl_ids 已从 completed_set 中移除）
    new_items = [it for it in items if it['answer_id'] not in completed_set]
    skipped = len(items) - len(new_items)

    if _is_stopped(stop_event) and len(items) == 0:
        # 在收集链接阶段被停止且未收集到任何内容，直接保存进度返回
        log_print("⚠ 收集链接阶段被停止，未获取到回答列表")
        save_progress(output_dir, completed_set, file_map={})
        return {
            'user_id': user_id,
            'success': 0,
            'failed': 0,
            'skipped': skipped,
            'total': len(completed_set),
            'output_dir': str(output_dir),
        }

    if skipped > 0:
        log_print(f"⏭ 跳过 {skipped} 条已完成")
    log_print(f"📝 待爬取: {len(new_items)} 条\n")

    # 逐条爬取（统一使用混合模式：截图 + 文字 + base64图片嵌入）
    all_meta = []
    file_map = {}           # answer_id → filename（当前批次）
    success = 0
    failed = 0
    content_skip = 0        # 内容不含关键词的跳过数

    for i, item in enumerate(new_items):
        # 检查停止信号
        if _is_stopped(stop_event):
            log_print("\n  ⚠ 用户手动停止（爬取阶段）")
            break

        try:
            log_print(f"[{i + 1}/{len(new_items)}] ", end="")

            aid = item['answer_id']
            # 短时缓存检查
            cached = load_answer_cache(output_dir, aid)
            if cached:
                result = cached
                # 缓存中 screenshot_bytes 以 base64 存储，需还原
                if 'screenshot_bytes' in result and isinstance(result['screenshot_bytes'], str):
                    result['screenshot_bytes'] = base64.b64decode(result['screenshot_bytes'])
                log_print(f"📦 缓存命中: {item.get('title', '')[:30]}...")
            else:
                result = crawl_answer_combined(page, item, user_profile=user_profile)
                if result and result['md_text']:
                    # 缓存时 screenshot_bytes 转为 base64（JSON 兼容）
                    cache_result = dict(result)
                    if 'screenshot_bytes' in cache_result:
                        cache_result['screenshot_bytes'] = base64.b64encode(
                            cache_result['screenshot_bytes']
                        ).decode('ascii')
                    save_answer_cache(output_dir, aid, cache_result)
            if result and result['md_text']:
                # 内容关键词检查（仅对标题不匹配的项，多关键词 OR 关系）
                if keyword and item['answer_id'] in check_content_ids:
                    keywords = _split_keywords(keyword)
                    if keywords and not (
                        _any_keyword_matches(result.get('html_text', ''), keywords) or
                        _any_keyword_matches(result['md_text'], keywords)
                    ):
                        log_print(f"    ⊘ 跳过（内容不含任一关键词）")
                        content_skip += 1
                        continue

                fpath = save_answer(
                    output_dir,
                    result['meta'],
                    result['md_text'],
                    result.get('html_text', '')
                )
                # 截图已 base64 嵌入 MD 中，无需单独保存 PNG 文件
                all_meta.append(result['meta'])
                completed_set.add(item['answer_id'])
                file_map[item['answer_id']] = fpath.name
                success += 1
                log_print(f"    ✓ 已保存(混合): {fpath.name}")

                # 每 10 条保存一次进度
                if success % 10 == 0:
                    save_progress(output_dir, completed_set, file_map=file_map)
                    file_map.clear()
            else:
                failed += 1
                log_print(f"    ✗ 混合模式失败")

            # 延迟
            if i < len(new_items) - 1:
                if _is_stopped(stop_event):
                    log_print("\n  ⚠ 用户手动停止（爬取阶段）")
                    break
                random_delay(
                    config.page_delay_min,
                    config.page_delay_max,
                    "防止请求过快",
                    stop_check=lambda: _is_stopped(stop_event) if stop_event else False
                )
        except StopCrawlException:
            log_print("\n  ⚠ 用户手动停止（延迟中）")
            break

    # 最终保存（file_map 合并写入 progress.json，历史记录不会丢失）
    save_progress(output_dir, completed_set, file_map=file_map)
    save_index(output_dir, all_meta)

    # EVIDENCE_REPORT.md 暂不生成，需要时单独提出

    log_print(f"\n{'='*60}")
    log_print(f"✅ {user_id} 爬取完成")
    parts = [f"新增: {success} 条"]
    if content_skip:
        parts.append(f"内容不含关键词跳过: {content_skip} 条")
    parts.append(f"失败: {failed} 条 | 跳过(已完成): {skipped} 条")
    log_print(f"   {' | '.join(parts)}")
    log_print(f"   总计: {len(completed_set)} 条回答")
    log_print(f"   输出: {output_dir.resolve()}")
    log_print(f"{'='*60}\n")

    return {
        'user_id': user_id,
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'content_skip': content_skip,
        'total': len(completed_set),
        'output_dir': str(output_dir),
    }


def crawl_single_url(page: Page, url: str, output_dir: str = None,
                     keyword: str = "") -> dict:
    """
    手动给定链接 → 保存为 MD

    支持格式:
        - https://www.zhihu.com/question/{qid}/answer/{aid}  （回答）
        - https://www.zhihu.com/answer/{aid}                  （短链回答，浏览器会自动跳转）

    返回 {success: bool, md_path: str, meta: dict, error: str}
    """
    # 解析 URL 提取 answer_id
    answer_id = ""
    qid_from_url = ""
    m = re.search(r'zhihu\.com/question/(\d+)/answer/(\d+)', url)
    if m:
        qid_from_url = m.group(1)
        answer_id = m.group(2)
    else:
        m = re.search(r'zhihu\.com/answer/(\d+)', url)
        if m:
            answer_id = m.group(1)
        else:
            return {'success': False, 'md_path': '', 'meta': {}, 'error': f'无法从链接中提取回答ID: {url}'}

    log_print(f"\n🔗 手动保存单条回答: {url}")
    log_print(f"   answer_id: {answer_id}")

    # 基础输出目录（稍后追加作者子目录）
    if output_dir:
        base_dir = Path(output_dir)
    else:
        base_dir = Path(config.output_dir)

    try:
        # ── 导航 & 等待渲染 ──
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        try:
            page.wait_for_selector(
                '.RichContent-inner, .RichContent, '
                '[class*="answer-content"], [class*="AnswerItem"], '
                '.QuestionHeader, .ContentItem',
                timeout=10000
            )
        except Exception:
            pass

        # ── 展开折叠内容 ──
        try:
            expand_btn = page.locator(
                'button:has-text("阅读全文"), '
                '.RichContent-uncollapse, '
                '[class*="expand"], [class*="unfold"]'
            ).first
            if expand_btn.count() > 0 and expand_btn.is_visible():
                expand_btn.click()
                time.sleep(1.5)
        except Exception:
            pass

        # ── 等待图片加载 ──
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass

        # ── 提取页面内容 ──
        html = page.content()

        # 提取问题标题
        title = ""
        try:
            title_el = page.locator('.QuestionHeader-title, h1.QuestionHeader-title, '
                                    '[class*="QuestionHeader"] h1, h1[class*="title"]').first
            if title_el.count() > 0:
                title = title_el.inner_text().strip()
        except Exception:
            pass
        if not title:
            m = re.search(r'<h1[^>]*class="[^"]*title[^"]*"[^>]*>(.*?)</h1>', html)
            if m:
                title = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        if not title:
            title = "手动保存回答"

        # 关键词预判断（标题匹配 → 直接抓取；标题不匹配 → 爬取后检查内容）
        kw = keyword.strip()
        keywords = _split_keywords(kw)
        title_matches_keyword = keywords and _any_keyword_matches(title, keywords)
        need_content_check = keywords and not title_matches_keyword

        # 提取作者 & 作者 ID（用于按用户分类目录）
        author = ""
        author_id = ""
        # 第一轮：定位回答卡片的作者信息（限定在回答区域内，避免拿到推荐用户/评论者）
        author_selectors = [
            # 优先：回答作者区域内的链接
            '.AnswerItem .AuthorInfo a[href*="/people/"]',
            '.ContentItem-authorInfo a[href*="/people/"]',
            '[itemprop="author"] a[href*="/people/"]',
            '.AuthorInfo a[href*="/people/"]',
            # 备选：回答区域内的用户名
            '.AnswerItem .AuthorInfo-name',
            '.ContentItem-authorInfo',
            '[class*="answer"] [class*="author"] a[href*="/people/"]',
        ]
        for sel in author_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    author = el.inner_text().strip()
                    href = el.get_attribute('href') or ''
                    m_aid = re.search(r'/people/([^/?]+)', href)
                    if m_aid:
                        author_id = m_aid.group(1)
                        break
            except Exception:
                continue
        # 第二轮：如果没拿到 author_id，尝试从 HTML meta 标签获取
        if not author_id:
            try:
                m_meta = re.search(
                    r'<meta\s[^>]*itemprop="author"[^>]*content="([^"]*)"',
                    html
                )
                if m_meta:
                    author = m_meta.group(1).strip()
            except Exception:
                pass
            try:
                m_url = re.search(
                    r'<meta\s[^>]*itemprop="url"[^>]*content="https?://www\.zhihu\.com/people/([^"/?]+)"',
                    html
                )
                if m_url:
                    author_id = m_url.group(1)
            except Exception:
                pass
        # 第三轮：实在找不到 → 记录警告，使用 "unknown" 但附加 answer_id 前缀区分
        if not author_id:
            log_print(f"  ⚠ 无法确定作者，answer_id: {answer_id}")

        # 提取日期
        from utils import extract_date_from_html
        date = extract_date_from_html(html)

        # 清理 HTML → MD
        from converter import html_to_md
        meta = {
            'title': title,
            'answer_id': answer_id,
            'answer_url': url,
            'author': author,
            'date': date,
            'upvotes': 0,
            'comment_count': 0,
            'crawl_time': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        text_md = html_to_md(html, answer_meta=meta)

        # 内容图片 base64 嵌入
        from storage import embed_images_base64
        text_md = embed_images_base64(text_md)

        # ── 截图 ──
        screenshot_bytes = _capture_answer_area(page)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('ascii')

        # ── 合并 MD ──
        lines = []
        lines.append(f"# [{title}]({url})")
        lines.append("")
        meta_line = ""
        if author:
            meta_line += f"**{author}**"
        if date:
            meta_line += f" / {date}" if meta_line else date
        if meta_line:
            lines.append(meta_line)
            lines.append("")
        lines.append(f"> 🔗 原文链接: [{url}]({url})")
        lines.append(f"> 🕒 保存时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📸 原页面截图")
        lines.append("")
        lines.append(f"![问题与回答截图](data:image/png;base64,{screenshot_b64})")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📝 文字内容")
        lines.append("")

        # 提取 body 内容（去掉 text_md 中重复的标题/元信息）
        text_lines = text_md.split('\n')
        content_start = 0
        for j, line in enumerate(text_lines):
            if line.strip() == '---':
                content_start = j + 1
                while content_start < len(text_lines) and text_lines[content_start].strip() == '':
                    content_start += 1
                break
        if content_start > 0 and content_start < len(text_lines):
            lines.extend(text_lines[content_start:])
        else:
            lines.extend(text_lines[2:])

        md_text = '\n'.join(lines)

        # 内容关键词检查（仅对标题不匹配的项，OR 关系的第二部分）
        if need_content_check and keywords:
            if not (_any_keyword_matches(html, keywords) or _any_keyword_matches(md_text, keywords)):
                kw_display = ' / '.join(keywords)
                log_print(f"  ⊘ 跳过（标题和内容均不含任一关键词「{kw_display}」）")
                return {
                    'success': False,
                    'md_path': '',
                    'meta': meta,
                    'error': f'标题和内容均不含任一关键词',
                }

        # ── 按作者分类目录（复用 get_output_path，与自动抓取命名一致）──
        from utils import get_output_path
        if author_id:
            user_dir = get_output_path(str(base_dir), author_id, author)
        elif author:
            # 有昵称无 ID：用昵称做目录名（罕见情况）
            user_dir = base_dir / sanitize_filename(author, 40)
            user_dir.mkdir(parents=True, exist_ok=True)
        else:
            # 无法确定作者：用 answer_id 生成唯一目录，避免多人内容混入 unknown
            user_dir = base_dir / f"_unknown_{answer_id[:8]}"
            user_dir.mkdir(parents=True, exist_ok=True)

        # ── 保存 MD ──
        safe_title = sanitize_filename(title, 60)
        safe_date = format_datetime(date) if date else time.strftime('%Y-%m-%d')
        md_filename = f"{safe_date}_{safe_title}_{answer_id[:8]}.md"
        md_path = user_dir / md_filename
        md_path.write_text(md_text, encoding='utf-8')

        log_print(f"  ✓ 已保存: {md_path}")
        return {
            'success': True,
            'md_path': str(md_path),
            'meta': meta,
            'error': '',
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {
            'success': False,
            'md_path': '',
            'meta': {},
            'error': str(e),
        }


def scrape_user_profile(page: Page, user_id: str) -> dict:
    """
    抓取用户基本影响力数据（法务证据用）

    返回 dict:
        nickname, followers, following, upvotes_received,
        likes_received, collections, public_edits,
        answers_count, articles_count, profile_url, bio
    """
    profile_url = f"https://www.zhihu.com/people/{user_id}"
    result = {
        'nickname': user_id,
        'followers': 0,
        'following': 0,
        'upvotes_received': 0,
        'likes_received': 0,
        'collections': 0,
        'public_edits': 0,
        'answers_count': 0,
        'articles_count': 0,
        'profile_url': profile_url,
        'bio': '',
    }

    try:
        log_print(f"\n👤 正在采集用户影响力数据: {profile_url}")
        page.goto(profile_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)

        # 策略1: NumberBoard-item 数字板（新版知乎）
        boards = page.query_selector_all('[class*="NumberBoard-item"],'
                                         ' [class*="NumberBoard"] [class*="item"]')
        for board in boards:
            try:
                text = board.inner_text().strip()
                # 格式: "1,234\n关注者" 或 "567\n赞同"
                m = re.search(r'([\d,.万]+)\s*\n?\s*(关注者|关注了|赞同|喜欢|收藏)', text)
                if m:
                    v = _parse_zhihu_number(m.group(1))
                    label = m.group(2)
                    if label == '关注者':
                        result['followers'] = v
                    elif label == '关注了':
                        result['following'] = v
                    elif label == '赞同':
                        result['upvotes_received'] = v
                    elif label == '喜欢':
                        result['likes_received'] = v
                    elif label == '收藏':
                        result['collections'] = v
            except Exception:
                pass

        # 策略2: ProfileHeader 数字统计（旧版知乎）
        if result['followers'] == 0:
            try:
                items = page.query_selector_all('[class*="ProfileHeader"] [class*="NumberBoard-value"],'
                                                 ' [class*="Profile"] [class*="followers"] [class*="count"],'
                                                 ' [class*="Profile-lightList"] strong')
                labels = page.query_selector_all('[class*="ProfileHeader"] [class*="NumberBoard-name"],'
                                                   ' [class*="Profile"] [class*="label"]')
                for i, item in enumerate(items):
                    try:
                        v = _parse_zhihu_number(item.inner_text().strip())
                        label = labels[i].inner_text().strip() if i < len(labels) else ''
                        if '关注者' in label:
                            result['followers'] = v
                        elif '关注' in label:
                            result['following'] = v
                    except Exception:
                        pass
            except Exception:
                pass

        # 策略3: 从页面meta/结构化数据提取（旧版知乎） 
        try:
            html = page.content()
            soup = BeautifulSoup(html, 'html.parser')

            # 关注者
            follower_el = soup.select_one('[class*="Followship"],'
                                          ' strong[class*="NumberBoard-value"],'
                                          ' [class*="followers-count"]')
            if follower_el and result['followers'] == 0:
                result['followers'] = _parse_zhihu_number(follower_el.get_text(strip=True))

            # 昵称
            name_el = soup.select_one('[class*="ProfileHeader-name"],'
                                       ' .ProfileHeader-title,'
                                       ' [class*="UserLink-link"]')
            if name_el:
                result['nickname'] = name_el.get_text(strip=True)

            # 简介
            bio_el = soup.select_one('[class*="ProfileHeader-headline"],'
                                      ' [class*="bio"]')
            if bio_el:
                result['bio'] = bio_el.get_text(strip=True)

            # 回答数、文章数
            stat_items = soup.select('[class*="Profile-lightList"] [class*="item"],'
                                      ' [class*="Tabs-item"]')
            for item in stat_items:
                try:
                    text = item.get_text(strip=True)
                    m = re.search(r'回答\s*([\d,.万]+)', text)
                    if m:
                        result['answers_count'] = _parse_zhihu_number(m.group(1))
                    m = re.search(r'文章\s*([\d,.万]+)', text)
                    if m:
                        result['articles_count'] = _parse_zhihu_number(m.group(1))
                except Exception:
                    pass
        except Exception:
            pass

        # 策略4：右侧个人成就卡片（新版知乎 Profile-sidebar）
        # 包含 赞同/感谢/收藏/关注者/关注了/公共编辑 等统计
        # 始终执行 —— 侧栏卡片数据可能与顶部统计不同，且常含顶部缺失的字段
        try:
            # 4a: 右侧栏卡片内的数据行（key-value 配对）
            rows = page.query_selector_all(
                '[class*="Profile-sidebar"] [class*="NumberBoard-item"],'
                ' [class*="Profile-sidebar"] [class*="Card"] [class*="Row"],'
                ' [class*="Profile-sidebar"] [class*="Content"] [class*="Row"],'
                ' [class*="css-"][class*="-j9mia3"]'  # CSS-in-JS 样式类
            )
            if not rows:
                # 4b: 更宽泛匹配 —— 任何包含关键词的可点击数字区域
                rows = page.query_selector_all(
                    ':has-text("赞同"), :has-text("关注者"), :has-text("感谢"),'
                    ' :has-text("收藏"), :has-text("关注了"),'
                    ' :has-text("喜欢"), :has-text("公共编辑")'
                )
            for row in rows:
                try:
                    text = row.inner_text().strip()

                    # --- 匹配 "2,862 赞同" / "赞同 2,862" 等单项 ---
                    m = re.search(
                        r'([\d,.万]+)\s*(赞同|感谢|收藏\s*(?!夹)|喜欢)'
                        r'|(赞同|感谢|收藏\s*(?!夹)|喜欢)\s*([\d,.万]+)',
                        text
                    )
                    if m:
                        num_str = m.group(1) or m.group(4)
                        label = m.group(2) or m.group(3)
                        v = _parse_zhihu_number(num_str)
                        if label and v > 0:
                            if '赞同' in label:
                                result['upvotes_received'] = max(result['upvotes_received'], v)
                            elif '感谢' in label and result['likes_received'] == 0:
                                # 感谢 ≈ 喜欢 (知乎旧版叫「感谢」)
                                result['likes_received'] = v
                            elif '收藏' in label:
                                result['collections'] = max(result['collections'], v)
                            elif '喜欢' in label:
                                result['likes_received'] = max(result['likes_received'], v)

                    # --- 匹配 "获得 1,862 次喜欢，18 次收藏" 合并格式 ---
                    cm = re.search(
                        r'获得\s*([\d,.万]+)\s*次?喜欢.*?([\d,.万]+)\s*次?收藏',
                        text
                    )
                    if cm:
                        v_like = _parse_zhihu_number(cm.group(1))
                        v_coll = _parse_zhihu_number(cm.group(2))
                        if v_like > 0:
                            result['likes_received'] = max(result['likes_received'], v_like)
                        if v_coll > 0:
                            result['collections'] = max(result['collections'], v_coll)

                    # --- 匹配 "参与 33 次公共编辑" ---
                    pm = re.search(
                        r'参与\s*([\d,.万]+)\s*次.*公共编辑',
                        text
                    )
                    if pm:
                        v = _parse_zhihu_number(pm.group(1))
                        if v > 0:
                            result['public_edits'] = max(result['public_edits'], v)

                    # --- 匹配 "1,455 关注者" 或 "288 关注了" ---
                    fm = re.search(
                        r'([\d,.万]+)\s*(关注者|关注了)'
                        r'|(关注者|关注了)\s*([\d,.万]+)',
                        text
                    )
                    if fm:
                        num_str = fm.group(1) or fm.group(4)
                        label = fm.group(2) or fm.group(3)
                        v = _parse_zhihu_number(num_str)
                        if label and v > 0:
                            if '关注者' in label:
                                result['followers'] = max(result['followers'], v)
                            elif '关注了' in label:
                                result['following'] = max(result['following'], v)
                except Exception:
                    pass

            # 4c: JavaScript 评估 —— 从 React 状态提取数据（兜底）
            try:
                js_data = page.evaluate("""() => {
                    const items = document.querySelectorAll(
                        '[class*="Profile-sidebar"] [class*="NumberBoard-item"],'
                        + ' [class*="Card"] [class*="NumberBoard-item"],'
                        + ' [class*="Profile-sidebar"] [class*="Achievements"],'
                        + ' [class*="Profile-sidebar"] [class*="Card"]'
                    );
                    return Array.from(items).map(el => el.innerText);
                }""")
                for item_text in js_data:
                    # 常规 key-value
                    m = re.search(r'([\d,.万]+)\s*\n?\s*(关注者|关注了|赞同|感谢|喜欢|收藏)',
                                  item_text)
                    if m:
                        v = _parse_zhihu_number(m.group(1))
                        label = m.group(2)
                        if label == '关注者' and v > 0:
                            result['followers'] = max(result['followers'], v)
                        elif label == '关注了' and v > 0:
                            result['following'] = max(result['following'], v)
                        elif label == '赞同' and v > 0:
                            result['upvotes_received'] = max(result['upvotes_received'], v)
                        elif label == '感谢' and v > 0:
                            result['likes_received'] = max(result['likes_received'], v)
                        elif label == '喜欢' and v > 0:
                            result['likes_received'] = max(result['likes_received'], v)
                        elif label == '收藏' and v > 0:
                            result['collections'] = max(result['collections'], v)
                    # 合并格式 "获得 1,862 次喜欢，18 次收藏"
                    cm = re.search(
                        r'获得\s*([\d,.万]+)\s*次?喜欢.*?([\d,.万]+)\s*次?收藏',
                        item_text
                    )
                    if cm:
                        v_like = _parse_zhihu_number(cm.group(1))
                        v_coll = _parse_zhihu_number(cm.group(2))
                        if v_like > 0:
                            result['likes_received'] = max(result['likes_received'], v_like)
                        if v_coll > 0:
                            result['collections'] = max(result['collections'], v_coll)
                    # 公共编辑
                    pm = re.search(r'参与\s*([\d,.万]+)\s*次.*公共编辑', item_text)
                    if pm:
                        v = _parse_zhihu_number(pm.group(1))
                        if v > 0:
                            result['public_edits'] = max(result['public_edits'], v)
            except Exception:
                pass
        except Exception:
            pass

        # 汇总日志
        log_print(f"  👍 赞同: {result['upvotes_received']} | ❤️ 喜欢: {result['likes_received']}"
              f" | ⭐ 收藏: {result['collections']} | ✏️ 公共编辑: {result['public_edits']}"
              f" | 👥 关注者: {result['followers']} | 📝 回答: {result['answers_count']}")

    except Exception as e:
        log_print(f"  ⚠ 用户影响力采集失败: {e}")

    return result


def _parse_zhihu_number(s: str) -> int:
    """解析知乎风格数字: '1.2 万' → 12000, '3,456' → 3456"""
    s = s.replace(',', '').replace(' ', '').strip()
    if '万' in s:
        return int(float(s.replace('万', '')) * 10000)
    if '亿' in s:
        return int(float(s.replace('亿', '')) * 100000000)
    try:
        return int(float(s))
    except ValueError:
        return 0
