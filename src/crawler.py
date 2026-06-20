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
    load_progress_full, get_group_summary,
    embed_images_base64,
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
    """按常见分隔符（逗号/顿号/分号/空格）拆分多关键词列表，去空白去重。空输入返回空列表。"""
    if not keyword_str or not keyword_str.strip():
        return []
    parts = re.split(r'[,，;；\s、]+', keyword_str)
    seen = set()
    result = []
    for p in parts:
        kw = p.strip()
        if kw and kw not in seen:
            seen.add(kw)
            result.append(kw)
    return result


def _any_keyword_matches(text: str, keywords: list) -> bool:
    """检查文本是否精确匹配任一关键词（英文字母不区分大小写）。
    
    匹配规则：
    - 纯英文关键词（如 ES、NIO）：前后须为非字母数字或字符串边界，
      避免 "test" 误匹配 "ES"，同时兼容中文上下文（如 "蔚来ES" 中 "ES" 前为中文）。
    - 含中文/混合关键词（如 蔚来、ES6）：大小写不敏感子串匹配
    - keywords 为空时返回 True（全部通过）
    """
    if not keywords:
        return True
    import re as _re
    for kw in keywords:
        if kw.isascii() and kw.isalpha():
            # 纯英文：前后边界 = 非字母数字 或 字符串首尾
            # 不使用 \\b（Python中中文也是\\w，\\b在"蔚来ES"中不匹配），
            # 改用 (?<![a-zA-Z0-9])...(?![a-zA-Z0-9]) 实现跨语言精确边界
            escaped = _re.escape(kw)
            pattern = r'(?<![a-zA-Z0-9])' + escaped + r'(?![a-zA-Z0-9])'
            if _re.search(pattern, text, _re.IGNORECASE):
                return True
        else:
            # 含中文或混合：子串匹配
            if kw.lower() in text.lower():
                return True
    return False


def collect_answer_links(page: Page, user_id: str, max_answers: int = 0,
                         stop_event=None, keyword: str = "",
                         keyword_min_match: int = 0) -> list:
    """
    在用户回答页面滚动加载，收集回答链接

    参数:
        keyword: 关键词字符串（逗号/分号分隔多个），用于标题匹配提前终止
        keyword_min_match: 标题匹配至少达到此数量后停止收集（0=不提前终止）
                          测试模式+关键词时设为 3，避免收集全部链接

    返回 [{title, url, answer_id, preview_text, upvotes, date, comment_count}, ...]
    """
    answers_url = f"https://www.zhihu.com/people/{user_id}/answers"
    log_print(f"\n📋 正在加载回答列表: {answers_url}")

    # 永久缓存检查（用户级并集，跨关键词共享）
    from utils import get_output_path
    cached, cache_meta = load_links_cache(get_output_path(config.output_dir, user_id), user_id)

    # 缓存兼容性：有关键词时，旧缓存可能不含 preview_text，需重新收集
    cache_usable = False
    if cached:
        if keyword:
            # 检查缓存是否含 preview_text（旧缓存兼容）
            if all('preview_text' in item for item in cached):
                cache_usable = True
                log_print(f"✅ 使用缓存链接: {len(cached)} 条（并集缓存，跨关键词共享）")
            else:
                log_print(f"⚠ 旧缓存不含 preview_text，将重新收集以支持关键词过滤")
                # 删除不兼容的旧缓存文件
                try:
                    from storage import _links_cache_key as _lck
                    old_cache = _lck(get_output_path(config.output_dir, user_id), user_id)
                    if old_cache.exists():
                        old_cache.unlink()
                        log_print(f"  🗑 已清理不兼容的旧缓存: {old_cache.name}")
                except Exception:
                    pass
        else:
            cache_usable = True

    # 无关键词且缓存可用 → 尝试增量收集新回答
    if cache_usable and not keyword:
        existing_ids = set(item['answer_id'] for item in cached)
        page.goto(answers_url, wait_until="domcontentloaded")
        time.sleep(3)

        new_items = []
        seen_ids = set()
        scroll_attempts = 0
        max_scroll_attempts = 50  # 增量模式：通常只需少量滚动即可追上已缓存
        no_new_count = 0
        consecutive_cached = 0
        incremental_stop = False

        log_print(f"🔍 增量模式：已有 {len(cached)} 条缓存，查找新回答...")

        while scroll_attempts < max_scroll_attempts and not incremental_stop:
            if _is_stopped(stop_event):
                log_print("\n  ⚠ 用户手动停止（增量收集阶段）")
                break

            cards = page.query_selector_all('[itemprop="zhihu:answer"], '
                                             '.List-item, '
                                             '[class*="AnswerItem"], '
                                             '.ContentItem')

            new_found_this_round = 0
            for card in cards:
                try:
                    link_el = card.query_selector(
                        'a[data-za-detail-view-element_name="Title"], '
                        '.ContentItem-title a, '
                        '.QuestionItem-title a, '
                        'h2 a, '
                        '[class*="question_link"]'
                    )
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
                    m = re.search(r'/answer/(\d+)', href)
                    if not m:
                        continue
                    answer_id = m.group(1)

                    # 命中已缓存 ID → 连续计数
                    if answer_id in existing_ids:
                        consecutive_cached += 1
                        if consecutive_cached >= 10:
                            incremental_stop = True
                            break
                        continue
                    consecutive_cached = 0  # 遇到新 ID，重置

                    if answer_id in seen_ids:
                        continue
                    seen_ids.add(answer_id)

                    # 提取完整信息（复用原提取逻辑）
                    title = link_el.inner_text().strip()
                    full_url = urljoin(page.url, href)

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

                    date = ''
                    try:
                        card_html = card.evaluate('el => el.outerHTML')
                        date = extract_date_from_html(card_html)
                    except Exception:
                        pass

                    comment_count = 0
                    try:
                        comment_el = card.query_selector('[class*="CommentButton"]')
                        if not comment_el:
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

                    preview_text = ''
                    try:
                        content_el = (
                            card.query_selector('.RichContent-inner') or
                            card.query_selector('[itemprop="text"]') or
                            card.query_selector('[class*="RichContent"]')
                        )
                        if content_el:
                            preview_text = content_el.inner_text().strip()
                    except Exception:
                        pass

                    new_items.append({
                        'title': title,
                        'url': full_url,
                        'answer_id': answer_id,
                        'upvotes': upvotes,
                        'date': date,
                        'comment_count': comment_count,
                        'preview_text': preview_text,
                    })
                    new_found_this_round += 1
                except Exception:
                    continue

            if incremental_stop:
                break

            if new_found_this_round == 0:
                no_new_count += 1
                if no_new_count >= 3:
                    log_print("  ℹ 连续 3 次未发现新内容，已到达列表末尾")
                    break
            else:
                no_new_count = 0

            random_delay(
                config.scroll_delay_min,
                config.scroll_delay_max,
                f"增量滚动 (第{scroll_attempts + 1}次)",
                stop_check=lambda: _is_stopped(stop_event) if stop_event else False
            )
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
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

        if new_items:
            # 新回答在前，旧缓存在后（知乎按最新排序）
            merged = new_items + cached
            if max_answers > 0 and len(merged) > max_answers:
                merged = merged[:max_answers]
            log_print(f"🆕 增量 {len(new_items)} 条 + 缓存 {len(cached)} 条 → 合并 {len(merged)} 条")
            # 时间校验：上次收集时间 vs 本次
            last_at = cache_meta.get('collected_at_iso', '未知')
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            log_print(f"  ⌚ 上次完整收集: {last_at}，本次增量: {now_str}")
            # 跨日期警告
            if cache_meta.get('collected_at'):
                days_gap = (time.time() - cache_meta['collected_at']) / 86400
                if days_gap > 1:
                    log_print(f"  ⚠ 距上次收集已 {days_gap:.0f} 天，增量可能漏爬（建议定期全量收集校验）")
            save_links_cache(get_output_path(config.output_dir, user_id), user_id, merged)
            return merged
        else:
            log_print(f"✅ 无新回答，完全使用缓存: {len(cached)} 条")
            if cache_meta.get('collected_at_iso'):
                log_print(f"  ⌚ 缓存收集时间: {cache_meta['collected_at_iso']}")
            if max_answers > 0 and len(cached) > max_answers:
                cached = cached[:max_answers]
            return cached


    # 有关键词且缓存可用 → 并集缓存模式
    if cache_usable and keyword:
        keywords = _split_keywords(keyword)
        
        # ── 保存并集全量引用（用于增量重叠检测）──
        full_cached = list(cached)  # 并集缓存：所有历史关键词匹配的合集
        full_cached_ids = set(item['answer_id'] for item in full_cached)
        
        # ── 内存重过滤：从并集缓存中筛出匹配当前关键词的 ──
        filtered_cached = []
        for item in cached:
            if _any_keyword_matches(item.get('title', ''), keywords) or \
               _any_keyword_matches(item.get('preview_text', ''), keywords):
                filtered_cached.append(item)
        if len(filtered_cached) < len(cached):
            log_print(f"🔍 关键词重过滤：并集缓存 {len(cached)} → {len(filtered_cached)} 条"
                      f"（匹配「{' / '.join(keywords)}」）")

        # ── 缓存鲜度检查：新鲜缓存直接返回，跳过滚屏 ──
        cache_age_hours = 0.0
        if cache_meta.get('collected_at'):
            cache_age_hours = (time.time() - cache_meta['collected_at']) / 3600.0
        max_age = getattr(config, 'links_cache_max_age_hours', 72)
        cache_collected_str = cache_meta.get('collected_at_iso', '未知')

        if max_age > 0 and cache_age_hours <= max_age:
            # 缓存新鲜，跳过滚屏 → 纯内存操作，秒级完成
            log_print(f"⚡ 缓存新鲜（{cache_age_hours:.0f}小时前 / {cache_collected_str}），"
                      f"跳过滚屏，直接内存重过滤")
            log_print(f"   匹配: {len(filtered_cached)} 条（并集{len(full_cached)}条，阈值≤{max_age}小时）")
            if max_answers > 0 and len(filtered_cached) > max_answers:
                filtered_cached = filtered_cached[:max_answers]
            return filtered_cached
        else:
            if cache_meta.get('collected_at'):
                log_print(f"🕐 缓存已过期（{cache_age_hours:.0f}小时前 / >{max_age}小时阈值），"
                          f"将滚屏检查新回答")
            else:
                log_print(f"🕐 缓存无时间戳，将滚屏检查新回答")

        # ── 缓存过期或阈值=0 → 滚屏增量扫描 ──
        # 用并集全集的 existing_ids 做重叠检测（而非关键词过滤子集的 max_cached_id）
        existing_ids = full_cached_ids

        page.goto(answers_url, wait_until="domcontentloaded")
        time.sleep(3)

        new_items = []
        seen_ids = set()
        scroll_attempts = 0
        max_scroll_attempts = 50
        no_new_count = 0
        consecutive_cached = 0
        incremental_stop = False

        log_print(f"🔍 关键词增量模式：并集缓存 {len(full_cached)} 条，当前匹配 {len(filtered_cached)} 条")
        log_print(f"   「{' / '.join(keywords)}」")

        while scroll_attempts < max_scroll_attempts and not incremental_stop:
            if _is_stopped(stop_event):
                log_print("\n  ⚠ 用户手动停止（增量收集阶段）")
                break

            cards = page.query_selector_all('[itemprop="zhihu:answer"], '
                                             '.List-item, '
                                             '[class*="AnswerItem"], '
                                             '.ContentItem')

            new_found_this_round = 0
            for card in cards:
                try:
                    link_el = card.query_selector(
                        'a[data-za-detail-view-element_name="Title"], '
                        '.ContentItem-title a, '
                        '.QuestionItem-title a, '
                        'h2 a, '
                        '[class*="question_link"]'
                    )
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
                    m = re.search(r'/answer/(\d+)', href)
                    if not m:
                        continue
                    answer_id = m.group(1)

                    # ── 重叠检测：已在并集缓存中 → 连续计数 ──
                    if answer_id in existing_ids:
                        consecutive_cached += 1
                        if consecutive_cached >= 20:
                            incremental_stop = True
                            break
                        continue
                    consecutive_cached = 0  # 遇到未缓存 ID，重置

                    if answer_id in seen_ids:
                        continue
                    seen_ids.add(answer_id)

                    title = link_el.inner_text().strip()

                    # 关键词检查
                    kw_match = _any_keyword_matches(title, keywords)
                    if not kw_match:
                        preview = ''
                        try:
                            content_el = (
                                card.query_selector('.RichContent-inner') or
                                card.query_selector('[itemprop="text"]') or
                                card.query_selector('[class*="RichContent"]')
                            )
                            if content_el:
                                preview = content_el.inner_text().strip()
                        except Exception:
                            pass
                        kw_match = _any_keyword_matches(preview, keywords)
                    if not kw_match:
                        continue  # 新回答但不含关键词，跳过

                    # 匹配关键词的新回答 → 提取完整信息
                    full_url = urljoin(page.url, href)

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

                    date = ''
                    try:
                        card_html = card.evaluate('el => el.outerHTML')
                        date = extract_date_from_html(card_html)
                    except Exception:
                        pass

                    comment_count = 0
                    try:
                        comment_el = card.query_selector('[class*="CommentButton"]')
                        if not comment_el:
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

                    preview_text = ''
                    try:
                        content_el = (
                            card.query_selector('.RichContent-inner') or
                            card.query_selector('[itemprop="text"]') or
                            card.query_selector('[class*="RichContent"]')
                        )
                        if content_el:
                            preview_text = content_el.inner_text().strip()
                    except Exception:
                        pass

                    new_items.append({
                        'title': title,
                        'url': full_url,
                        'answer_id': answer_id,
                        'upvotes': upvotes,
                        'date': date,
                        'comment_count': comment_count,
                        'preview_text': preview_text,
                    })
                    new_found_this_round += 1
                except Exception:
                    continue

            if incremental_stop:
                break

            if new_found_this_round == 0:
                no_new_count += 1
                if no_new_count >= 3:
                    break
            else:
                no_new_count = 0

            random_delay(
                config.scroll_delay_min,
                config.scroll_delay_max,
                f"增量滚动 (第{scroll_attempts + 1}次)",
                stop_check=lambda: _is_stopped(stop_event) if stop_event else False
            )
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
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

        # ── 合并保存：new_items ∪ 并集全量 → 去重 → 保存 → 过滤当前关键词返回 ──
        if new_items:
            # 与并集全量合并（answer_id 去重，新数据覆盖旧预览）
            merge_map = {it['answer_id']: it for it in full_cached}
            for it in new_items:
                merge_map[it['answer_id']] = it
            full_merged = list(merge_map.values())

            # 保存并集缓存（merge=True 确保二次校验）
            save_links_cache(get_output_path(config.output_dir, user_id), user_id,
                           full_merged, merge=True, new_keyword=keyword)

            # 从并集中过滤出匹配当前关键词的结果
            result = []
            for item in full_merged:
                if _any_keyword_matches(item.get('title', ''), keywords) or \
                   _any_keyword_matches(item.get('preview_text', ''), keywords):
                    result.append(item)

            log_print(f"🆕 增量 {len(new_items)} 条 + 并集 {len(full_cached)} 条 "
                      f"→ 并集 {len(full_merged)} 条（当前匹配 {len(result)} 条）")
            last_at = cache_meta.get('collected_at_iso', '未知')
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')
            log_print(f"  ⌚ 上次收集: {last_at}，本次: {now_str}")
            if cache_meta.get('collected_at'):
                days_gap = (time.time() - cache_meta['collected_at']) / 86400
                if days_gap > 1:
                    log_print(f"  ⚠ 距上次收集已 {days_gap:.0f} 天，增量可能漏爬")

            if max_answers > 0 and len(result) > max_answers:
                result = result[:max_answers]
            return result
        else:
            log_print(f"✅ 无新关键词匹配回答，使用缓存过滤结果: {len(filtered_cached)} 条")
            if cache_meta.get('collected_at_iso'):
                log_print(f"  ⌚ 缓存收集时间: {cache_meta['collected_at_iso']}")
            if max_answers > 0 and len(filtered_cached) > max_answers:
                filtered_cached = filtered_cached[:max_answers]
            return filtered_cached

    # ── 以下为全量收集（无缓存或缓存不兼容）──
    page.goto(answers_url, wait_until="domcontentloaded")
    time.sleep(3)

    # 关键词列表（用于内嵌过滤）
    keywords = _split_keywords(keyword) if keyword else []

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

                # 提取预览文本（卡片上的内容摘要，用于关键词过滤）
                preview_text = ''
                try:
                    content_el = (
                        card.query_selector('.RichContent-inner') or
                        card.query_selector('[itemprop="text"]') or
                        card.query_selector('[class*="RichContent"]')
                    )
                    if content_el:
                        preview_text = content_el.inner_text().strip()
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
                    'preview_text': preview_text,
                })
                new_found += 1

                if max_answers > 0 and len(collected) >= max_answers:
                    break
            except Exception:
                continue

        log_print(f"  📜 已收集 {len(collected)} 条回答" + (f" (本轮 +{new_found})" if new_found else ""))

        if max_answers > 0 and len(collected) >= max_answers:
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
    # 永久缓存链接列表（全量滚屏 → 全量保存）
    if not _is_stopped(stop_event):
        from utils import get_output_path as _gop
        save_links_cache(_gop(config.output_dir, user_id), user_id, collected, merge=True, new_keyword=keyword)
    else:
        log_print("  ⚠ 用户手动停止，跳过缓存保存（数据可能不完整）")

    # ── 关键词过滤（纯内存操作，在缓存保存之后）──
    if keywords:
        filtered = []
        for item in collected:
            if _any_keyword_matches(item.get('title', ''), keywords) or \
               _any_keyword_matches(item.get('preview_text', ''), keywords):
                filtered.append(item)
        log_print(f"🔍 全量 {len(collected)} 条 → 关键词过滤 → {len(filtered)} 条"
                  f"（匹配「{' / '.join(keywords)}」）")
        if max_answers > 0 and len(filtered) > max_answers:
            filtered = filtered[:max_answers]
        return filtered
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
                         user_profile: dict = None,
                         output_dir: Path = None) -> dict:
    """
    混合模式：一次页面访问，同时获取截图 + 文字
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

        # ── 4. 截图：仅问题+回答内容区（排除右侧栏），保存为独立 PNG ──
        screenshot_bytes = _capture_answer_area(page)
        # 保存截图 PNG（使用相对路径 ./{aid}.png，MD 与 PNG 同目录）
        screenshot_filename = f"{answer_id}.png"
        screenshot_ref = f"./{screenshot_filename}"  # 相对路径，跨平台兼容
        if output_dir:
            try:
                png_path = Path(output_dir).resolve() / screenshot_filename
                png_path.write_bytes(screenshot_bytes)
            except Exception:
                screenshot_ref = None  # 写盘失败则跳过截图引用

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

        # 生成文字版 MD 并嵌入图片（传 cookies 解决知乎 CDN 防盗链）
        text_md = html_to_md(html_text, answer_meta=meta)
        if config.download_images:
            try:
                ctx_cookies = {c['name']: c['value'] for c in page.context.cookies()}
            except Exception:
                ctx_cookies = None
            text_md = embed_images_base64(text_md, cookies=ctx_cookies)

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
        if screenshot_ref:
            lines.append(f"![问题与回答截图]({screenshot_ref})")
        else:
            lines.append("> ⚠ 截图保存失败")
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


def get_crawl_status(user_id: str, nickname: str = "") -> dict:
    """
    预检查：返回用户的爬取状态摘要
    {user_id, output_dir, total_progress, existing_count, missing_count, missing_details, groups_info}
    """
    output_dir = get_output_path(config.output_dir, user_id, nickname=nickname)
    existing, missing, missing_details = check_missing_files(output_dir)
    completed_set, _, groups_info = load_progress_full(output_dir)

    return {
        'user_id': user_id,
        'output_dir': output_dir,
        'total_progress': len(completed_set),
        'existing_count': len(existing),
        'missing_count': len(missing),
        'missing_ids': missing,
        'missing_details': missing_details,
        'groups_info': groups_info,
    }


def crawl_user_answers(page: Page, user_id: str,
                       force_recrawl_ids: set = None,
                       stop_event=None,
                       keyword: str = "",
                       scroll_only: bool = False) -> dict:
    """
    爬取指定用户的所有回答

    参数:
        page: Playwright Page 对象
        user_id: 用户 ID（纯字符串）
        force_recrawl_ids: 强制重新爬取的回答 ID 集合
                          （这些 ID 即使有进度记录也会重新爬取）
        stop_event: threading.Event，用于支持 GUI 中途停止
        keyword: 关键词过滤（仅爬取标题含此关键词的回答）
        scroll_only: 仅滚屏收集链接+缓存，不生成 MD（用于「爬取」与「输出MD」分离）

    返回统计信息
    """
    # 先采集用户影响力数据（获取昵称用于目录命名）
    user_profile = scrape_user_profile(page, user_id)
    nickname = user_profile.get('nickname', '')

    # 更新头像URL到用户列表
    avatar_url = user_profile.get('avatar_url', '')
    if avatar_url:
        from id_manager import get_id_manager
        mgr = get_id_manager()
        mgr.update_avatar(user_id, avatar_url)
    # 更新回答总数到用户列表
    answers_count = user_profile.get('answers_count', 0)
    if answers_count > 0:
        from id_manager import get_id_manager
        mgr = get_id_manager()
        mgr.update_answers_count(user_id, answers_count)

    # 预检查文件状态（使用含昵称的目录名；首次运行时会自动迁移旧目录）
    output_dir = get_output_path(config.output_dir, user_id, nickname)
    existing, missing, _ = check_missing_files(output_dir)
    completed_set, _, groups_info = load_progress_full(output_dir)

    # 生成当前关键词哈希（用于分组追踪）
    import hashlib
    kw_hash = hashlib.md5(keyword.encode('utf-8')).hexdigest()[:8] if keyword else ""

    # 记录运行前已完成的 ID（用于区分本批次新增）
    pre_existing_ids = set(completed_set)

    # 强制忽略缓存：清空进度，从头重爬
    if config.force_no_cache:
        if completed_set:
            group_lines = get_group_summary(output_dir)
            detail = f"\n  原有关键词分组:\n{group_lines}" if group_lines else ""
            log_print(f"🔄 强制忽略缓存：已清除 {len(completed_set)} 条进度记录，将全部重新爬取{detail}")
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
    # 按关键词分组统计
    if keyword and groups_info:
        group_summary = get_group_summary(output_dir)
        if group_summary:
            log_print(f"📋 历史关键词分组:")
            log_print(group_summary)
    log_print(f"📋 已完成(汇总): {len(completed_set)} 条"
          + (f" (其中 {len(missing)} 条本地文件缺失)" if missing else ""))

    # 收集回答链接
    # 始终传递 keyword，让缓存逻辑能感知关键词（避免使用旧的无过滤全量缓存）
    # keyword_min_match=0 表示不提前终止（非测试模式），但仍传递关键词用于缓存键
    try:
        items = collect_answer_links(
            page, user_id, max_answers, stop_event=stop_event,
            keyword=keyword,
            keyword_min_match=3 if config.test_mode else 0,
        )
    except StopCrawlException:
        log_print("\n  ⚠ 用户手动停止（收集链接阶段）")
        items = []

    # 关键词过滤已在 collect_answer_links 末尾完成（纯内存操作），此处 items 已为匹配项
    if keyword:
        keywords = _split_keywords(keyword)
        kw_display = ' / '.join(keywords)
        log_print(f"🔍 关键词过滤（{len(keywords)}个 / 标题或预览匹配）:")
        log_print(f"   「{kw_display}」")
        log_print(f"   匹配: {len(items)} 条")

    # 测试模式+关键词：只保留前 3 条进入爬取
    if config.test_mode and keyword and len(items) > 3:
        items = items[:3]
        log_print(f"🧪 测试模式：取前 {len(items)} 条匹配项进入爬取")

    # 过滤已完成的（force_recrawl_ids 已从 completed_set 中移除）
    new_items = [it for it in items if it['answer_id'] not in completed_set]
    skipped = len(items) - len(new_items)

    # 增量提示：多少条因其他关键词已完成而被跳过
    if keyword and skipped > 0 and groups_info:
        other_kw_hits = sum(
            1 for it in items
            if it['answer_id'] in completed_set and it['answer_id'] not in pre_existing_ids
        )
        log_print(f"⏭ 跳过 {skipped} 条（其中 {skipped} 条已被其他关键词覆盖）")

    if _is_stopped(stop_event) and len(items) == 0:
        # 在收集链接阶段被停止且未收集到任何内容，直接保存进度返回
        log_print("⚠ 收集链接阶段被停止，未获取到回答列表")
        save_progress(output_dir, completed_set, file_map={}, 
                      keyword_hash=kw_hash, keyword_str=keyword, new_ids=set())
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

    # ── scroll_only 模式：只滚屏收集链接+缓存，不生成 MD ──
    if scroll_only:
        log_print(f"🔄 滚屏收集完成（scroll_only），共收集 {len(items)} 条链接"
                  f"，其中 {len(new_items)} 条待爬取")
        return {
            'user_id': user_id,
            'success': 0,
            'failed': 0,
            'skipped': skipped,
            'total': len(completed_set) + len(new_items),
            'collected': len(items),
            'pending': len(new_items),
            'output_dir': str(output_dir),
            'scroll_only': True,
        }

    # 逐条爬取（统一使用混合模式：截图 + 文字 + base64图片嵌入）
    all_meta = []
    file_map = {}           # answer_id → filename（当前批次）
    session_new_ids = set() # 本批次新增完成的 answer_id（用于关键词分组标记）
    success = 0
    failed = 0

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
                # 缓存命中时也确保截图 PNG 存在（兼容旧缓存含 base64 URI 格式）
                png_path = output_dir / f"{aid}.png"
                if not png_path.exists() and result.get('screenshot_bytes'):
                    try:
                        png_path.write_bytes(result['screenshot_bytes'])
                    except Exception:
                        pass
                log_print(f"📦 缓存命中: {item.get('title', '')[:30]}...")
            else:
                result = crawl_answer_combined(page, item, user_profile=user_profile, output_dir=output_dir)
                if result and result['md_text']:
                    # 缓存时 screenshot_bytes 转为 base64（JSON 兼容）
                    cache_result = dict(result)
                    if 'screenshot_bytes' in cache_result:
                        cache_result['screenshot_bytes'] = base64.b64encode(
                            cache_result['screenshot_bytes']
                        ).decode('ascii')
                    save_answer_cache(output_dir, aid, cache_result)
            if result and result['md_text']:
                # 修复旧缓存中可能残留的 base64 截图引用 → 相对 PNG 路径引用
                md_to_save = result['md_text']
                if 'data:image/png;base64,' in md_to_save:
                    # 使用 lambda 替换避免 Windows 路径中的反斜杠被 re.sub 误解析为正则转义
                    md_to_save = re.sub(
                        r'!\[问题与回答截图\]\(data:image/png;base64,[^)]+\)',
                        lambda m, _aid=aid: f'![问题与回答截图](./{_aid}.png)',
                        md_to_save
                    )
                fpath = save_answer(
                    output_dir,
                    result['meta'],
                    md_to_save,
                    result.get('html_text', '')
                )
                all_meta.append(result['meta'])
                completed_set.add(aid)
                session_new_ids.add(aid)
                file_map[aid] = fpath.name
                success += 1
                log_print(f"    ✓ 已保存(混合): {fpath.name}")

                # 每 10 条保存一次进度
                if success % 10 == 0:
                    save_progress(output_dir, completed_set, file_map=file_map,
                                  keyword_hash=kw_hash, keyword_str=keyword,
                                  new_ids=session_new_ids)
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
        except Exception as e:
            failed += 1
            log_print(f"    ✗ 异常: {e}")

    # 最终保存（file_map 合并写入 cache_data/{user}/.cache/progress.json，历史记录不会丢失）
    save_progress(output_dir, completed_set, file_map=file_map,
                  keyword_hash=kw_hash, keyword_str=keyword,
                  new_ids=session_new_ids)

    # EVIDENCE_REPORT.md 暂不生成，需要时单独提出

    # 最终分组摘要
    final_group_summary = get_group_summary(output_dir)

    log_print(f"\n{'='*60}")
    log_print(f"✅ {user_id} 爬取完成")
    parts = [f"新增: {success} 条"]
    parts.append(f"失败: {failed} 条 | 跳过(已完成): {skipped} 条")
    log_print(f"   {' | '.join(parts)}")
    log_print(f"   总计: {len(completed_set)} 条回答")
    if final_group_summary:
        log_print(f"  关键词分组:")
        log_print(final_group_summary)
    log_print(f"   输出: {output_dir.resolve()}")
    log_print(f"{'='*60}\n")

    return {
        'user_id': user_id,
        'success': success,
        'failed': failed,
        'skipped': skipped,
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

        # 内容图片 base64 嵌入（传 cookies 解决知乎 CDN 防盗链）
        from storage import embed_images_base64
        try:
            ctx_cookies = {c['name']: c['value'] for c in page.context.cookies()}
        except Exception:
            ctx_cookies = None
        text_md = embed_images_base64(text_md, cookies=ctx_cookies)

        # ── 截图（先采集字节，确定输出目录后再保存为独立 PNG）──
        screenshot_bytes = _capture_answer_area(page)

        # ── 合并 MD（截图引用使用占位符，待确定目录后替换）──
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
        lines.append("{SCREENSHOT_PLACEHOLDER}")
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

        # ── 保存截图 PNG（与 MD 同目录）──
        screenshot_filename = f"{answer_id}.png"
        png_path = user_dir / screenshot_filename
        screenshot_md = ""
        try:
            png_path.write_bytes(screenshot_bytes)
            screenshot_md = f"![问题与回答截图](./{screenshot_filename})"
        except Exception:
            screenshot_md = "> ⚠ 截图保存失败"
        # 替换占位符
        md_text = md_text.replace("{SCREENSHOT_PLACEHOLDER}", screenshot_md)

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
        'avatar_url': '',
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

        # 策略3a: 从页面直接提取头像（Playwright，新版知乎 JS 渲染）
        if not result['avatar_url']:
            try:
                avatar_el = page.query_selector(
                    '[class*="Avatar"] img,'
                    ' [class*="ProfileHeader-avatar"] img,'
                    ' img[class*="Avatar"],'
                    ' img[alt*="头像"]'
                )
                if avatar_el:
                    avatar_src = avatar_el.get_attribute('src') or ''
                    if avatar_src:
                        if avatar_src.startswith('//'):
                            avatar_src = 'https:' + avatar_src
                        result['avatar_url'] = avatar_src
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

            # 头像
            avatar_el = soup.select_one('[class*="Avatar"] img,'
                                        ' [class*="ProfileHeader-avatar"] img,'
                                        ' [class*="UserLink"] img,'
                                        ' img[class*="Avatar"],'
                                        ' img[alt*="头像"]')
            if avatar_el:
                avatar_src = avatar_el.get('src', '') or avatar_el.get('data-src', '')
                if avatar_src:
                    # 补全协议
                    if avatar_src.startswith('//'):
                        avatar_src = 'https:' + avatar_src
                    result['avatar_url'] = avatar_src

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
