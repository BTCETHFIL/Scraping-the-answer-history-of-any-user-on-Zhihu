"""
工具函数模块
"""

import json
import re
import random
import time
from pathlib import Path

from bs4 import BeautifulSoup, element


def random_delay(min_s: float, max_s: float, reason: str = "",
                 stop_check=None):
    """随机延迟，模拟人类浏览行为。
    如果提供 stop_check 回调，会在延迟期间分片检查停止信号。
    检测到停止信号时抛出 StopCrawlException 让调用方中断。
    """
    delay = random.uniform(min_s, max_s)
    if reason:
        print(f"  ⏳ {reason}，等待 {delay:.1f}s...")
    else:
        print(f"  ⏳ 等待 {delay:.1f}s...")

    # 如果有停止检查，分片 sleep（每 0.5s 检查一次）
    if stop_check is not None:
        remaining = delay
        while remaining > 0:
            chunk = min(0.5, remaining)
            time.sleep(chunk)
            remaining -= chunk
            if stop_check():
                print("  ⚠ 用户手动停止（延迟中）")
                raise StopCrawlException("stop")
    else:
        time.sleep(delay)


class StopCrawlException(Exception):
    """爬取被手动停止的异常"""
    pass


def extract_user_id(target: str) -> str:
    """从各种格式中提取用户 ID
    支持: 完整URL、用户ID、/people/xxx/answers
    也支持知乎App复制的带文字链接（自动提取其中的URL）
    """
    target = target.strip()
    # 先用正则提取文本中所有知乎链接
    all_urls = re.findall(r'https?://[^\s]*zhihu\.com[^\s]*', target)
    # 优先匹配 people URL
    for url in all_urls:
        m = re.search(r'zhihu\.com/people/([^/?]+)', url)
        if m:
            return m.group(1)
    # 如果找到了回答链接但没有 people 链接
    if any('/answer/' in u for u in all_urls):
        raise ValueError(
            "检测到「回答链接」，请输入用户主页链接（https://www.zhihu.com/people/xxx）\n"
            "提示：回答链接请粘贴到下方「手动链接保存为 MD」区域"
        )
    # 如果存在其他知乎链接（如问题链接）
    if all_urls:
        raise ValueError(f"未找到用户主页链接，请粘贴 https://www.zhihu.com/people/xxx 格式")
    # 如果已经是纯 ID
    if re.match(r'^[a-zA-Z0-9_-]+$', target):
        return target
    raise ValueError(f"无法解析用户 ID: {target}")


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """清理文件名中的非法字符"""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip().strip('.')
    if len(name) > max_len:
        name = name[:max_len - 3] + '...'
    return name or 'untitled'


def format_datetime(iso_str: str) -> str:
    """将 ISO 时间格式化为 YYYY-MM-DD（用于文件名）"""
    if not iso_str:
        return "未知日期"
    m = re.match(r'(\d{4}-\d{2}-\d{2})', iso_str)
    return m.group(1) if m else iso_str[:10]


def format_datetime_full(iso_str: str) -> str:
    """将 ISO 时间格式化为 YYYY-MM-DD HH:MM:SS（用于元数据显示）"""
    if not iso_str:
        return ""
    # 尝试匹配完整时间
    m = re.match(r'(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(:\d{2})?)', iso_str)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    # 只有日期
    m = re.match(r'(\d{4}-\d{2}-\d{2})', iso_str)
    if m:
        return m.group(1)
    return iso_str[:19] if len(iso_str) >= 19 else iso_str[:10]


def extract_date_from_html(html_or_soup) -> str:
    """
    从 HTML 或 BeautifulSoup 元素中提取日期字符串。
    尝试多种策略，按优先级：
    1. <time datetime="..."> 属性
    2. <meta itemprop="dateCreated"> / <meta itemprop="datePublished">
    3. JSON-LD (application/ld+json)
    4. 文本中的日期模式 (YYYY-MM-DD / YYYY年MM月DD日 / MM-DD 等)
    返回 YYYY-MM-DD 格式的日期字符串，失败返回空字符串。
    """
    if isinstance(html_or_soup, str):
        soup = BeautifulSoup(html_or_soup, 'lxml')
    elif isinstance(html_or_soup, element.Tag):
        soup = html_or_soup
        # 如果传入的是单个Tag，把它包一层便于搜索
        if soup.name != 'html':
            soup = BeautifulSoup(str(soup), 'lxml')
    else:
        return ''

    # 策略 1: <time> 元素的 datetime 属性
    time_els = soup.select('time[datetime]')
    if not time_els:
        time_els = soup.find_all('time')
    for t in time_els:
        dt = t.get('datetime', '')
        if dt:
            m = re.match(r'(\d{4}-\d{2}-\d{2})', dt)
            if m:
                return m.group(1)
        # time 元素可能没有 datetime，取文本
        text = t.get_text(strip=True)
        m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
        if m:
            return m.group(1)

    # 策略 2: schema.org meta 标签
    for selector in [
        'meta[itemprop="dateCreated"]',
        'meta[itemprop="datePublished"]',
        'meta[itemprop="dateModified"]',
        'meta[property="article:published_time"]',
        'meta[name="publishdate"]',
    ]:
        meta = soup.select_one(selector)
        if meta:
            content = meta.get('content', '')
            m = re.match(r'(\d{4}-\d{2}-\d{2})', content)
            if m:
                return m.group(1)

    # 策略 3: JSON-LD 结构化数据
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, list):
                data = data[0] if data else {}
            for key in ('dateCreated', 'datePublished', 'dateModified'):
                val = data.get(key, '')
                if val:
                    m = re.match(r'(\d{4}-\d{2}-\d{2})', str(val))
                    if m:
                        return m.group(1)
        except Exception:
            pass

    # 策略 4: 文本中的日期模式(中文格式: 2024年06月18日)
    #          也匹配带时间的格式: 2024-06-18 15:30 / 编辑于 2024-06-18 15:30:25
    text = soup.get_text()
    for pat in [
        # 先匹配带时间的格式
        r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2}):(\d{2})',
        r'发布于\s*(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}(:\d{2})?)',
        r'编辑于\s*(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}(:\d{2})?)',
        r'发表于\s*(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}(:\d{2})?)',
        # 然后是只有日期的格式
        r'(\d{4})年(\d{1,2})月(\d{1,2})日',
        r'(\d{4})-(\d{1,2})-(\d{1,2})',
        r'(\d{4})/(\d{1,2})/(\d{1,2})',
        r'编辑于\s*(\d{4}-\d{2}-\d{2})',
        r'发布于\s*(\d{4}-\d{2}-\d{2})',
    ]:
        m = re.search(pat, text)
        if m:
            if m.lastindex and m.lastindex >= 4:
                # 年/月/日/时/分 分开: 2024年06月18日 15:30
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{m.group(5)}"
            if m.lastindex and m.lastindex >= 3:
                # 年/月/日 分开的3个捕获组
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            # 单一捕获组 (如 编辑于 YYYY-MM-DD / 发布于 YYYY-MM-DD HH:MM:SS)
            return m.group(1).strip()[:19] if m.lastindex and m.lastindex == 1 else m.group(0)[:19].strip()

    return ''


def get_output_path(output_dir: str, user_id: str, nickname: str = "") -> Path:
    """获取用户的输出目录，如有昵称则加入目录名便于识别"""
    # 构建目录名：{nickname}_{user_id} 或 {user_id}
    if nickname and nickname != user_id:
        safe_nickname = sanitize_filename(nickname, 30)
        dirname = f"{safe_nickname}_{user_id}"
    else:
        dirname = user_id

    p = Path(output_dir) / dirname

    # 迁移：如果旧目录 {user_id} 存在，将内容移入新目录
    old_p = Path(output_dir) / user_id
    if old_p.exists() and old_p.resolve() != p.resolve():
        _migrate_dir(old_p, p)

    p.mkdir(parents=True, exist_ok=True)
    return p


def _migrate_dir(src: Path, dst: Path):
    """将旧目录中的文件迁移到新目录"""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in src.iterdir():
        target = dst / item.name
        if not target.exists():
            try:
                shutil.move(str(item), str(target))
                moved += 1
            except Exception:
                pass
    if moved > 0:
        print(f"📦 已将旧目录内容迁移: {src.name} → {dst.name}")
    # 源目录为空则删除
    try:
        remaining = list(src.iterdir())
        if not remaining:
            src.rmdir()
    except Exception:
        pass
