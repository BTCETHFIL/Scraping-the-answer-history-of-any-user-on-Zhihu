"""
工具函数模块
"""

import re
import random
import time
from pathlib import Path


def random_delay(min_s: float, max_s: float, reason: str = ""):
    """随机延迟，模拟人类浏览行为"""
    delay = random.uniform(min_s, max_s)
    if reason:
        print(f"  ⏳ {reason}，等待 {delay:.1f}s...")
    else:
        print(f"  ⏳ 等待 {delay:.1f}s...")
    time.sleep(delay)


def extract_user_id(target: str) -> str:
    """从各种格式中提取用户 ID
    支持: 完整URL、用户ID、/people/xxx/answers
    """
    target = target.strip()
    # 从 URL 中提取
    m = re.search(r'zhihu\.com/people/([^/?]+)', target)
    if m:
        return m.group(1)
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
    from bs4 import BeautifulSoup, element

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
            import json
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


def get_output_path(output_dir: str, user_id: str) -> Path:
    """获取用户的输出目录"""
    p = Path(output_dir) / user_id
    p.mkdir(parents=True, exist_ok=True)
    return p
