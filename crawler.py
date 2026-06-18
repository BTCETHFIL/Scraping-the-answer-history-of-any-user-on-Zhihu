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

from playwright.sync_api import Page, BrowserContext

from config import config
from converter import html_to_md, extract_answer_meta
from storage import (
    save_answer, save_progress, load_progress,
    save_index, download_images, embed_images_base64,
    check_missing_files
)
from utils import (
    random_delay, extract_user_id,
    sanitize_filename, format_datetime,
    get_output_path, extract_date_from_html
)


def collect_answer_links(page: Page, user_id: str, max_answers: int = 0) -> list:
    """
    在用户回答页面滚动加载，收集回答链接
    返回 [{title, url, answer_id, preview_html, upvotes, date}, ...]
    """
    answers_url = f"https://www.zhihu.com/people/{user_id}/answers"
    print(f"\n📋 正在加载回答列表: {answers_url}")

    page.goto(answers_url, wait_until="domcontentloaded")
    time.sleep(3)

    collected = []
    seen_ids = set()
    scroll_attempts = 0
    max_scroll_attempts = 500  # 安全上限
    no_new_count = 0            # 连续无新内容的滚动次数

    while scroll_attempts < max_scroll_attempts:
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
                    comment_el = card.query_selector(
                        '[class*="CommentButton"], '
                        'button:has-text("评论")'
                    )
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

        print(f"  📜 已收集 {len(collected)} 条回答" + (f" (本轮 +{new_found})" if new_found else ""))

        if max_answers > 0 and len(collected) >= max_answers:
            break

        # 无新内容计数
        if new_found == 0:
            no_new_count += 1
            if no_new_count >= 3:
                print("  ℹ 连续 3 次未发现新内容，已到达列表末尾")
                break
        else:
            no_new_count = 0

        # 滚动加载更多
        random_delay(
            config.scroll_delay_min,
            config.scroll_delay_max,
            f"滚动加载 (第{scroll_attempts + 1}次)"
        )

        # 滚动到底部
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1)

        # 也可以尝试点击"加载更多"按钮
        try:
            load_more = page.query_selector(
                'button:has-text("加载更多"), '
                '[class*="PaginationButton"], '
                '.List-footer button'
            )
            if load_more and load_more.is_visible():
                load_more.click()
                time.sleep(1)
        except Exception:
            pass

        scroll_attempts += 1

    print(f"✅ 共收集 {len(collected)} 条回答链接")
    return collected


def crawl_answer(page: Page, item: dict) -> dict:
    """
    爬取单条回答的完整内容
    返回 {meta, md_text, html_text}
    """
    answer_id = item['answer_id']
    url = item['url']
    title = item.get('title', '')

    print(f"  📖 正在获取: {title[:50]}...")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        # 等待正文加载
        try:
            page.wait_for_selector(
                '.RichContent-inner, .RichContent, '
                '[class*="answer-content"], [class*="AnswerItem"]',
                timeout=10000
            )
        except Exception:
            pass

        # 展开折叠内容（阅读全文）
        try:
            expand_btn = page.query_selector(
                'button:has-text("阅读全文"), '
                '.RichContent-uncollapse, '
                '[class*="expand"], [class*="unfold"]'
            )
            if expand_btn and expand_btn.is_visible():
                expand_btn.click()
                time.sleep(1)
        except Exception:
            pass

        # 获取完整页面 HTML
        html_text = page.content()

        # 提取元数据
        meta = extract_answer_meta(html_text)
        meta['answer_id'] = answer_id
        meta['title'] = title or meta.get('title', '')
        meta['answer_url'] = url
        meta['crawl_time'] = time.strftime('%Y-%m-%d %H:%M:%S')

        # 合并页面中收集到的元数据
        if not meta.get('upvotes') and item.get('upvotes'):
            meta['upvotes'] = item['upvotes']
        if not meta.get('date') and item.get('date'):
            meta['date'] = item['date']
        if not meta.get('comment_count') and item.get('comment_count'):
            meta['comment_count'] = item['comment_count']

        # 转换为 Markdown
        md_text = html_to_md(html_text, answer_meta=meta)

        return {'meta': meta, 'md_text': md_text, 'html_text': html_text}

    except Exception as e:
        print(f"  ✗ 获取失败: {e}")
        # 返回部分数据
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
        return {'meta': meta, 'md_text': f"# {title}\n\n> 获取失败: {e}", 'html_text': ''}


def crawl_answer_screenshot(page: Page, item: dict) -> dict:
    """
    截图模式：对回答页进行整页截图，嵌入 Markdown
    返回 {meta, md_text, screenshot_base64}
    """
    answer_id = item['answer_id']
    url = item['url']
    title = item.get('title', '')

    print(f"  📸 正在截图: {title[:50]}...")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)  # 等页面渲染

        # 等待正文加载
        try:
            page.wait_for_selector(
                '.RichContent-inner, .RichContent, '
                '[class*="answer-content"], [class*="AnswerItem"]',
                timeout=10000
            )
        except Exception:
            pass

        # 展开折叠内容
        try:
            expand_btn = page.query_selector(
                'button:has-text("阅读全文"), '
                '.RichContent-uncollapse, '
                '[class*="expand"], [class*="unfold"]'
            )
            if expand_btn and expand_btn.is_visible():
                expand_btn.click()
                time.sleep(1.5)
        except Exception:
            pass

        # 等待图片加载
        try:
            page.wait_for_load_state('networkidle', timeout=10000)
        except Exception:
            pass
        time.sleep(1)

        # 全页截图
        screenshot_bytes = page.screenshot(full_page=True, type='png')
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('ascii')

        # 构建 Markdown（元数据 + 嵌入截图）
        meta = extract_answer_meta(page.content())
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

        # 构建文档
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
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append(f"![整页截图](data:image/png;base64,{screenshot_b64})")

        md_text = '\n'.join(lines)

        return {
            'meta': meta,
            'md_text': md_text,
            'screenshot_base64': screenshot_b64,
        }

    except Exception as e:
        print(f"  ✗ 截图失败: {e}")
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
            'md_text': f"# {title}\n\n> 🕒 证据采集时间: {meta['crawl_time']}\n\n> 截图失败: {e}",
            'screenshot_base64': '',
        }


def crawl_answer_combined(page: Page, item: dict) -> dict:
    """
    混合模式：一次页面访问，同时获取截图 + 文字（含内嵌图片）
    上半部分 = 整页截图（保留原始排版），下半部分 = 可搜索/复制的文字
    返回 {meta, md_text, screenshot_base64, html_text}
    """
    answer_id = item['answer_id']
    url = item['url']
    title = item.get('title', '')

    print(f"  🎯 混合模式: {title[:50]}...")

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
            expand_btn = page.query_selector(
                'button:has-text("阅读全文"), '
                '.RichContent-uncollapse, '
                '[class*="expand"], [class*="unfold"]'
            )
            if expand_btn and expand_btn.is_visible():
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

        # ── 4. 全页截图 ──
        screenshot_bytes = page.screenshot(full_page=True, type='png')
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode('ascii')

        # ── 5. 提取文字内容 ──
        html_text = page.content()
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
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## 📸 原页面截图")
        lines.append("")
        lines.append(f"![整页截图](data:image/png;base64,{screenshot_b64})")
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
            'screenshot_base64': screenshot_b64,
            'html_text': html_text,
        }

    except Exception as e:
        print(f"  ✗ 混合模式失败: {e}")
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
            'screenshot_base64': '',
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
                       force_recrawl_ids: set = None) -> dict:
    """
    爬取指定用户的所有回答

    参数:
        page: Playwright Page 对象
        user_id: 用户 ID（纯字符串）
        force_recrawl_ids: 强制重新爬取的回答 ID 集合
                           （这些 ID 即使有进度记录也会重新爬取）

    返回统计信息
    """
    # 预检查文件状态
    output_dir = get_output_path(config.output_dir, user_id)
    existing, missing, _ = check_missing_files(output_dir)
    completed_set = load_progress(output_dir)

    # 强制重新爬取：把 force_recrawl_ids 从 completed_set 中移除
    if force_recrawl_ids:
        removed = completed_set & force_recrawl_ids
        completed_set -= force_recrawl_ids
        if removed:
            print(f"🔄 强制重新爬取 {len(removed)} 条（本地文件缺失）")

    print(f"\n{'='*60}")
    print(f"🐾 开始爬取用户: {user_id}")
    print(f"{'='*60}")

    # 测试模式：强制限制为 5 条
    max_answers = config.max_answers
    if config.test_mode:
        max_answers = 5
        print("🧪 测试模式：只爬取前 5 条回答")
        print()

    print("🎯 混合模式：截图(保留原文排版) + 文字(可搜索/复制) + base64图片嵌入")
    print()

    print(f"📁 输出目录: {output_dir}")
    print(f"📋 已完成: {len(completed_set)} 条"
          + (f" (其中 {len(missing)} 条本地文件缺失)" if missing else ""))

    # 收集回答链接
    items = collect_answer_links(page, user_id, max_answers)

    # 过滤已完成的（force_recrawl_ids 已从 completed_set 中移除）
    new_items = [it for it in items if it['answer_id'] not in completed_set]
    skipped = len(items) - len(new_items)
    if skipped > 0:
        print(f"⏭ 跳过 {skipped} 条已完成")
    print(f"📝 待爬取: {len(new_items)} 条\n")

    # 逐条爬取（统一使用混合模式：截图 + 文字 + base64图片嵌入）
    all_meta = []
    file_map = {}           # answer_id → filename（当前批次）
    success = 0
    failed = 0

    for i, item in enumerate(new_items):
        print(f"[{i + 1}/{len(new_items)}] ", end="")

        result = crawl_answer_combined(page, item)
        if result and result['md_text']:
            fpath = save_answer(
                output_dir,
                result['meta'],
                result['md_text'],
                result.get('html_text', '')
            )
            all_meta.append(result['meta'])
            completed_set.add(item['answer_id'])
            file_map[item['answer_id']] = fpath.name
            success += 1
            print(f"    ✓ 已保存(混合): {fpath.name}")

            # 每 10 条保存一次进度
            if success % 10 == 0:
                save_progress(output_dir, completed_set, file_map=file_map)
                file_map.clear()
        else:
            failed += 1
            print(f"    ✗ 混合模式失败")

        # 延迟
        if i < len(new_items) - 1:
            random_delay(
                config.page_delay_min,
                config.page_delay_max,
                "防止请求过快"
            )

    # 最终保存（file_map 合并写入 progress.json，历史记录不会丢失）
    save_progress(output_dir, completed_set, file_map=file_map)
    save_index(output_dir, all_meta)

    # 法务模式/默认：生成证据采集报告（扫描全量文件）
    if config.forensic_mode or config.save_html:
        from storage import save_evidence_report, load_progress_with_files
        _, full_file_map = load_progress_with_files(output_dir)
        stats = {'total': len(completed_set), 'success': success,
                 'failed': failed, 'skipped': skipped}
        save_evidence_report(output_dir, user_id, all_meta, full_file_map, stats)
        print(f"🔒 证据采集报告已生成: EVIDENCE_REPORT.md")

    print(f"\n{'='*60}")
    print(f"✅ {user_id} 爬取完成")
    print(f"   新增: {success} 条 | 失败: {failed} 条 | 跳过: {skipped} 条")
    print(f"   总计: {len(completed_set)} 条回答")
    print(f"   输出: {output_dir.resolve()}")
    print(f"{'='*60}\n")

    return {
        'user_id': user_id,
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'total': len(completed_set),
    }
