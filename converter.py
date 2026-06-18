"""
知乎回答 HTML → Markdown 转换模块
"""

import re
from bs4 import BeautifulSoup
import html2text

# 初始化 html2text 转换器
_md = html2text.HTML2Text()
_md.body_width = 0          # 不自动换行
_md.ignore_links = False    # 保留链接
_md.ignore_images = False   # 保留图片
_md.ignore_emphasis = False # 保留加粗斜体
_md.bypass_tables = False   # 保留表格
_md.single_line_break = False
_md.unicode_snob = True     # 使用 Unicode 字符
_md.protect_links = True    # 保护链接
_md.mark_code = True        # 标记代码块


def clean_html(html: str) -> str:
    """清理知乎回答的 HTML，去除干扰元素"""
    soup = BeautifulSoup(html, 'lxml')

    # 移除无用的标签
    for tag in soup.select(
        'img[src*="data:image/svg"], '        # 占位 SVG
        '.MemberLinkInfo, '                     # 会员信息
        '.ContentItem-actions, '                 # 操作栏（点赞/评论）
        '.Catalog, '                             # 目录
        '.Reward, '                              # 打赏
        '.CornerButtons, '                       # 浮动按钮
        '.VagueImage, '                          # 模糊图片
        '.ContentItem-time, '                    # 时间戳（会在元数据中记录）
        'noscript, '
        'script, '
        'style'
    ):
        tag.decompose()

    # 移除广告卡片
    for tag in soup.select('[class*="Advert"], [class*="advert"], [class*="EcomCard"]'):
        tag.decompose()

    # 移除空的 div/span
    for tag in soup.find_all(['div', 'span']):
        if not tag.get_text(strip=True) and not tag.find('img'):
            tag.decompose()

    return str(soup)


def extract_answer_meta(html: str) -> dict:
    """从 HTML 中提取回答元数据（含评论数、精确时间）"""
    soup = BeautifulSoup(html, 'lxml')
    meta = {
        'title': '',
        'author': '',
        'upvotes': 0,
        'comment_count': 0,
        'date': '',
        'answer_url': '',
    }

    # 问题标题
    title_el = soup.select_one('h1.QuestionHeader-title, .QuestionHeader-title, '
                                '[class*="question-title"], [class*="QuestionTitle"]')
    if title_el:
        meta['title'] = title_el.get_text(strip=True)

    # 作者
    author_el = soup.select_one('.AuthorInfo-name, .UserLink-link, '
                                 '[class*="AuthorInfo"], [itemprop="author"]')
    if author_el:
        author = author_el.get_text(strip=True)
        # 清除被嵌套「关注」按钮带入的文字（如"祈祈关注"→"祈祈"）
        author = re.sub(r'关注\s*$', '', author).strip()
        meta['author'] = author

    # 赞同数
    vote_el = soup.select_one('.VoteButton--up, [class*="VoteButton--up"], '
                               '[class*="votecount"]')
    if vote_el:
        vote_text = vote_el.get_text(strip=True)
        m = re.search(r'[\d,.万]+', vote_text)
        if m:
            vs = m.group().replace(',', '')
            if '万' in vs:
                meta['upvotes'] = int(float(vs.replace('万', '')) * 10000)
            else:
                meta['upvotes'] = int(vs)

    # 评论数（多策略提取）
    comment_el = None
    for selector in [
        '[class*="CommentButton"]',
        '[class*="comments-count"]',
        '[aria-label*="评论"]',
    ]:
        comment_el = soup.select_one(selector)
        if comment_el:
            break
    # fallback: BS4 不支持 :has-text，用 find 搜索含"评论"的按钮
    if not comment_el:
        comment_el = soup.find('button', string=re.compile(r'\d.*评论|评论.*\d'))
        if not comment_el:
            comment_el = soup.find('button', text=re.compile(r'评论'))
    if comment_el:
        ct = comment_el.get_text(strip=True)
        # 匹配数字（可能含"条评论"或"评论"前缀）
        m = re.search(r'([\d,.万]+)\s*(?:条)?评论?', ct)
        if not m:
            # 纯数字
            m = re.search(r'[\d,.万]+', ct)
        if m:
            v = m.group(1) if m.lastindex else m.group()
            v = v.replace(',', '')
            if '万' in v:
                meta['comment_count'] = int(float(v.replace('万', '')) * 10000)
            else:
                meta['comment_count'] = int(float(v))

    # 日期（多策略提取，支持完整时间）
    from utils import extract_date_from_html
    meta['date'] = extract_date_from_html(html)

    return meta


def find_answer_content(html: str) -> str:
    """定位回答正文区域"""
    soup = BeautifulSoup(html, 'lxml')

    # 知乎回答内容的常见容器
    content = soup.select_one(
        '.RichContent-inner, '
        '.RichContent, '
        '.AnswerCard-content, '
        '[class*="answer-content"], '
        '[class*="AnswerItem-content"]'
    )
    if content:
        return str(content)
    return html


def html_to_md(html: str, answer_meta: dict = None) -> str:
    """将知乎回答 HTML 转换为 Markdown"""
    # 定位正文
    content_html = find_answer_content(html)

    # 清理
    content_html = clean_html(content_html)

    # 转换为 Markdown
    md_body = _md.handle(content_html)

    # 后处理
    md_body = post_process(md_body)

    # 构建完整 Markdown
    meta = answer_meta or {}
    lines = []

    # 标题
    title = meta.get('title', '未命名回答')
    answer_url = meta.get('answer_url', '')
    if answer_url:
        lines.append(f"# [{title}]({answer_url})")
    else:
        lines.append(f"# {title}")

    lines.append("")

    # 作者、时间、点赞、评论
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

    # 采集时间（法务证据必要信息）
    if crawl_time:
        lines.append(f"> 🕒 证据采集时间: {crawl_time}")
    # 原文链接
    if answer_url:
        lines.append(f"> 🔗 原文链接: [{answer_url}]({answer_url})")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(md_body)

    return '\n'.join(lines)


def post_process(md_text: str) -> str:
    """Markdown 后处理：修复常见问题"""
    # 去除过多的空行（连续 3 个以上空行→2 个）
    md_text = re.sub(r'\n{4,}', '\n\n\n', md_text)

    # 修复知乎的 LaTeX 公式
    md_text = re.sub(r'<img[^>]*alt="([^"]*)"[^>]*src="[^"]*?equation[^"]*"[^>]*>',
                     r'$\1$', md_text)

    # 去除零宽字符
    md_text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\ufeff]', '', md_text)

    # 修复图片链接（去掉知乎的图片代理前缀）
    md_text = re.sub(r'https?://picx?\.zhimg\.com/', 'https://pic.zhimg.com/', md_text)

    return md_text.strip()
