"""
文件存储模块
负责 Markdown 文件写入、图片下载、进度管理
"""

import base64
import json
import re
import shutil
import time
from pathlib import Path

import requests

from config import config
from utils import sanitize_filename, format_datetime


def _filename(meta: dict) -> str:
    """根据模板生成文件名"""
    date = format_datetime(meta.get('date', ''))
    title = sanitize_filename(meta.get('title', '未命名'))
    upvotes = str(meta.get('upvotes', 0))
    answer_id = meta.get('answer_id', '')

    tmpl = config.filename_template
    name = tmpl.format(
        date=date,
        title=title,
        upvotes=upvotes,
        answer_id=answer_id,
    )
    return sanitize_filename(name, 120)


def save_answer(output_dir: Path, meta: dict, md_text: str, html_text: str = ""):
    """保存回答为 Markdown 文件，可选保存 HTML"""
    fname = _filename(meta)
    fpath = output_dir / fname

    # 处理重名
    counter = 1
    stem, ext = fpath.stem, fpath.suffix
    while fpath.exists():
        fpath = output_dir / f"{stem}_{counter}{ext}"
        counter += 1

    fpath.write_text(md_text, encoding='utf-8')

    if config.save_html and html_text:
        # HTML 文件名与最终 MD 文件名保持一致（含 _N 重名后缀）
        html_path = output_dir / f"{fpath.stem}.html"
        html_path.write_text(html_text, encoding='utf-8')

    return fpath





def embed_images_base64(md_text: str, cookies: dict = None) -> str:
    """下载 Markdown 中的图片并以 base64 data URI 嵌入，生成自包含 MD"""
    import re as _re_module
    # 正则匹配 Markdown 图片: ![alt](url)
    pattern = _re_module.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    cookie_str = ''
    if cookies:
        cookie_str = '; '.join(f'{k}={v}' for k, v in cookies.items())

    def replacer(m):
        alt = m.group(1)
        url = m.group(2)

        # 跳过已经是 data: URI 的
        if url.startswith('data:'):
            return m.group(0)

        # 跳过已经是本地路径的
        if url.startswith(('images/', './', '../', 'file://')):
            return m.group(0)

        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.zhihu.com/',
            }
            if cookie_str:
                headers['Cookie'] = cookie_str
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                # 跳过过大图片（>5MB），避免 MD 膨胀
                if len(resp.content) > 5 * 1024 * 1024:
                    return m.group(0)
                img_data = base64.b64encode(resp.content).decode('ascii')
                content_type = resp.headers.get('Content-Type', 'image/jpeg')
                data_uri = f"data:{content_type};base64,{img_data}"
                return f"![{alt}]({data_uri})"
        except Exception:
            pass

        return m.group(0)  # 下载失败保留原链接

    return pattern.sub(replacer, md_text)


# ── 进度管理 ──

def load_progress(output_dir: Path) -> set:
    """加载已爬取的回答 ID 集合"""
    _data = _load_progress_data(output_dir)
    completed = _data.get('completed', [])
    # 兼容旧格式（列表）和新格式（字典）
    if isinstance(completed, list):
        return set(completed)
    elif isinstance(completed, dict):
        return set(completed.keys())
    return set()


def load_progress_with_files(output_dir: Path) -> tuple[set, dict]:
    """加载已爬取的回答 ID 集合 + 文件名映射 {answer_id: filename}"""
    _data = _load_progress_data(output_dir)
    completed = _data.get('completed', [])
    if isinstance(completed, list):
        # 旧格式：没有文件名映射，需从本地文件反向查找
        files = _discover_files_from_disk(output_dir, set(completed))
        return set(completed), files
    elif isinstance(completed, dict):
        return set(completed.keys()), dict(completed)
    return set(), {}


def _progress_file(output_dir: Path) -> Path:
    """progress.json 路径（存于 .cache/ 子目录）"""
    return _cache_dir(output_dir) / "progress.json"


def _load_progress_data(output_dir: Path) -> dict:
    """读取 progress.json 原始数据，兼容旧位置的迁移"""
    # 新位置优先
    new_file = _progress_file(output_dir)
    old_file = output_dir / "progress.json"
    # 迁移：旧位置存在但新位置不存在时，移动到新位置
    if old_file.exists() and not new_file.exists():
        try:
            shutil.move(str(old_file), str(new_file))
        except Exception:
            pass
    if new_file.exists():
        try:
            return json.loads(new_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _discover_files_from_disk(output_dir: Path, answer_ids: set) -> dict:
    """旧格式兼容：从磁盘扫描 MD 文件，通过文件名中的 answer_id 匹配"""
    files = {}
    if not output_dir.exists():
        return files
    for aid in answer_ids:
        for f in output_dir.glob("*.md"):
            if aid in f.stem:
                files[aid] = f.name
                break
    return files


def save_progress(output_dir: Path, completed: set,
                  file_map: dict = None):
    """
    保存爬取进度（存于 .cache/progress.json）
    file_map: {answer_id: filename} 可选的文件名映射
    """
    progress_file = _progress_file(output_dir)
    # 合并已有的 files 映射
    existing_data = _load_progress_data(output_dir)
    existing_files = {}
    old_completed = existing_data.get('completed', [])
    if isinstance(old_completed, dict):
        existing_files = dict(old_completed)

    if file_map:
        existing_files.update(file_map)

    # 构建新的 completed 字典：{answer_id: filename}
    new_completed = {}
    for aid in sorted(completed):
        new_completed[aid] = existing_files.get(aid, '')

    data = {
        'completed': new_completed,
        'total': len(completed),
        'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    progress_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def check_missing_files(output_dir: Path) -> tuple[set, set, dict]:
    """
    检查已爬取回答的本地文件是否存在

    返回 (existing_ids, missing_ids, missing_details)
    missing_details: {answer_id: {'filename': str, 'title': str, ...}}
    """
    completed_set, file_map = load_progress_with_files(output_dir)
    existing = set()
    missing = set()
    missing_details = {}

    for aid in completed_set:
        fname = file_map.get(aid, '')
        if fname:
            fpath = output_dir / fname
            if fpath.exists():
                existing.add(aid)
            else:
                missing.add(aid)
                missing_details[aid] = {'filename': fname}
        else:
            # 没有文件名映射，尝试模糊匹配
            found = False
            if output_dir.exists():
                for f in output_dir.glob("*.md"):
                    if aid in f.stem:
                        existing.add(aid)
                        found = True
                        break
            if not found:
                missing.add(aid)
                missing_details[aid] = {'filename': f'*{aid}*.md (未找到)'}

    return existing, missing, missing_details






# ── 短时缓存：避免短时间内重复网络请求 ──

def _cache_dir(output_dir: Path) -> Path:
    d = output_dir / ".cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _links_cache_key(output_dir: Path, user_id: str, keyword_hash: str = "") -> Path:
    """获取链接缓存文件路径，keyword_hash 用于区分不同关键词配置的缓存"""
    d = _cache_dir(output_dir)
    if keyword_hash:
        return d / f"links_{keyword_hash}.json"
    return d / "links.json"


def load_links_cache(output_dir: Path, user_id: str, keyword_hash: str = "") -> list | None:
    """加载缓存的回答链接列表，过期返回 None"""
    if config.force_no_cache or not config.cache_ttl_minutes:
        return None
    cache_file = _links_cache_key(output_dir, user_id, keyword_hash)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        age = time.time() - data.get('fetched_at', 0)
        ttl = config.cache_ttl_minutes * 60
        if age < ttl:
            print(f"  📦 使用缓存的链接列表（{age:.0f}秒前，TTL={config.cache_ttl_minutes}分钟）")
            return data.get('items', [])
        else:
            print(f"  ⏰ 链接缓存已过期（{age:.0f}秒 > {ttl}秒）")
    except Exception:
        pass
    return None


def save_links_cache(output_dir: Path, user_id: str, items: list, keyword_hash: str = ""):
    """缓存回答链接列表"""
    if not config.cache_ttl_minutes:
        return
    cache_file = _links_cache_key(output_dir, user_id, keyword_hash)
    data = {
        'user_id': user_id,
        'fetched_at': time.time(),
        'ttl_minutes': config.cache_ttl_minutes,
        'count': len(items),
        'items': items,
    }
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def load_answer_cache(output_dir: Path, answer_id: str) -> dict | None:
    """加载缓存的回答内容，过期返回 None"""
    if config.force_no_cache or not config.cache_ttl_minutes:
        return None
    cache_file = _cache_dir(output_dir) / f"{answer_id}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        age = time.time() - data.get('fetched_at', 0)
        ttl = config.cache_ttl_minutes * 60
        if age < ttl:
            return data.get('result')
        else:
            # 不打印单条过期日志，太吵
            pass
    except Exception:
        pass
    return None


def save_answer_cache(output_dir: Path, answer_id: str, result: dict):
    """缓存回答内容"""
    if not config.cache_ttl_minutes:
        return
    cache_file = _cache_dir(output_dir) / f"{answer_id}.json"
    data = {
        'answer_id': answer_id,
        'fetched_at': time.time(),
        'ttl_minutes': config.cache_ttl_minutes,
        'result': result,
    }
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')



