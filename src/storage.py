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

# progress.json 新格式 (v2):
# {
#   "completed": {
#     "12345678": {"filename": "xxx.md", "groups": ["hash1", "hash2"]},
#     ...
#   },
#   "groups": {
#     "hash1": {"keywords": "蔚来",  "count": 20, "last_run": "2026-06-20 20:30:00"},
#     "hash2": {"keywords": "比亚迪", "count": 15, "last_run": "2026-06-20 20:35:00"}
#   },
#   "last_updated": "2026-06-20 20:35:00"
# }
# 兼容旧格式:
#   v0: "completed": ["id1", "id2"]              → 迁移到 v2（无 filename, groups=[]）
#   v1: "completed": {"id1": "f1.md", "id2": ""}  → 迁移到 v2（groups=[]）


def _normalize_entry(entry_value) -> dict:
    """将旧格式的 entry 值（str 或 dict）标准化为 v2 格式"""
    if isinstance(entry_value, str):
        return {'filename': entry_value, 'groups': []}
    elif isinstance(entry_value, dict):
        if 'groups' not in entry_value:
            entry_value['groups'] = []
        if 'filename' not in entry_value:
            entry_value['filename'] = ''
        return entry_value
    return {'filename': '', 'groups': []}


def load_progress(output_dir: Path) -> set:
    """加载所有已爬取的回答 ID 集合（跨关键词汇总）"""
    _data = _load_progress_data(output_dir)
    completed = _data.get('completed', [])
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
        files = _discover_files_from_disk(output_dir, set(completed))
        return set(completed), files
    elif isinstance(completed, dict):
        files = {}
        for aid, entry in completed.items():
            entry = _normalize_entry(entry)
            fname = entry.get('filename', '')
            if fname:
                files[aid] = fname
        return set(completed.keys()), files
    return set(), {}


def load_progress_full(output_dir: Path) -> tuple[set, dict, dict]:
    """
    加载完整进度信息（v2 格式）
    返回 (completed_set, file_map, groups_info)
    groups_info: {keyword_hash: {keywords, count, last_run}, ...}
    """
    _data = _load_progress_data(output_dir)
    completed_raw = _data.get('completed', [])

    completed_set = set()
    file_map = {}

    if isinstance(completed_raw, list):
        completed_set = set(completed_raw)
        file_map = _discover_files_from_disk(output_dir, completed_set)
    elif isinstance(completed_raw, dict):
        for aid, entry in completed_raw.items():
            completed_set.add(aid)
            entry = _normalize_entry(entry)
            fname = entry.get('filename', '')
            if fname:
                file_map[aid] = fname

    groups_info = _data.get('groups', {})
    return completed_set, file_map, groups_info


def _progress_file(output_dir: Path) -> Path:
    """progress.json 路径（存于 .cache/ 子目录）"""
    return _cache_dir(output_dir) / "progress.json"


def _load_progress_data(output_dir: Path) -> dict:
    """读取 progress.json 原始数据，兼容旧位置的迁移"""
    new_file = _progress_file(output_dir)
    old_file = output_dir / "progress.json"
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
                  file_map: dict = None,
                  keyword_hash: str = "",
                  keyword_str: str = "",
                  new_ids: set = None):
    """
    保存爬取进度（存于 .cache/progress.json）

    参数:
        completed: 所有已完成的 answer_id 集合（含之前关键词的）
        file_map: {answer_id: filename} 本批次新增的文件名映射
        keyword_hash: 当前关键词组哈希（8位MD5前缀），空字符串表示无关键词
        keyword_str: 当前关键词原始字符串（用于显示）
        new_ids: 本批次新增完成的 answer_id 集合（仅这些会被标记当前关键词）
    """
    progress_file = _progress_file(output_dir)
    existing_data = _load_progress_data(output_dir)

    # ── 加载已有 completed 数据（含 groups 信息）──
    old_completed_raw = existing_data.get('completed', [])
    old_entries = {}  # {answer_id: {filename, groups}}

    if isinstance(old_completed_raw, list):
        for aid in old_completed_raw:
            old_entries[aid] = {'filename': '', 'groups': []}
    elif isinstance(old_completed_raw, dict):
        for aid, entry in old_completed_raw.items():
            old_entries[aid] = _normalize_entry(entry)

    # 合并文件映射
    if file_map:
        for aid, fname in file_map.items():
            if aid in old_entries:
                old_entries[aid]['filename'] = fname or old_entries[aid]['filename']
            else:
                old_entries[aid] = {'filename': fname, 'groups': []}

    # ── 加载已有 groups 信息 ──
    groups_info = existing_data.get('groups', {})

    # ── 本批次新增的 ID 集合 ──
    if new_ids is None:
        new_ids = set()
    # 确保 new_ids 是 set 类型
    new_ids = set(new_ids)

    # ── 构建新的 completed 字典（v2 格式）──
    new_completed = {}
    for aid in sorted(completed):
        entry = old_entries.get(aid, {'filename': '', 'groups': []})
        # 仅为本批次新增的回答打上当前关键词标签（去重）
        if keyword_hash and aid in new_ids and keyword_hash not in entry['groups']:
            entry['groups'].append(keyword_hash)
        new_completed[aid] = {
            'filename': entry.get('filename', ''),
            'groups': entry.get('groups', []),
        }

    # ── 更新 groups 统计（仅当有关键词时）──
    if keyword_hash:
        # 统计带当前关键词标签的回答数量
        kw_count = sum(
            1 for entry in new_completed.values()
            if keyword_hash in entry.get('groups', [])
        )
        # 合并已有的 count（避免 force_no_cache 时归零）
        old_count = groups_info.get(keyword_hash, {}).get('count', 0)
        groups_info[keyword_hash] = {
            'keywords': keyword_str,
            'count': max(kw_count, old_count),
            'last_run': time.strftime('%Y-%m-%d %H:%M:%S'),
        }

    data = {
        'completed': new_completed,
        'total': len(completed),
        'groups': groups_info,
        'last_updated': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    progress_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding='utf-8'
    )


def get_group_summary(output_dir: Path) -> str:
    """
    返回人类可读的关键词组统计摘要
    格式:
      🔑 蔚来: 20条 (2026-06-20 20:30)
      🔑 比亚迪: 15条 (2026-06-20 20:35)
    """
    _, _, groups_info = load_progress_full(output_dir)
    if not groups_info:
        return ""
    lines = []
    for khash, info in sorted(groups_info.items(),
                               key=lambda x: x[1].get('last_run', '')):
        kws = info.get('keywords', '无关键词')
        cnt = info.get('count', 0)
        last = info.get('last_run', '未知')
        lines.append(f"  🔑 {kws}: {cnt}条 ({last})")
    return '\n'.join(lines)


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






# ── 缓存目录（独立于 output，防用户误删）──

def _get_cache_root() -> Path:
    """缓存根目录（cache_data/，与 output/ 平级，不会被用户误操作影响）"""
    root = Path(config.cache_dir)
    if not root.is_absolute():
        root = Path.cwd() / root
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_dir(output_dir: Path) -> Path:
    """
    返回用户缓存目录（cache_data/{dir_name}/.cache/）

    规则：
    1. 收集 cache_data/ 下所有匹配此用户的目录（同名 或 同 uid 后缀）
    2. 如果找到多个 → 合并缓存文件到主目录，避免数据分散
    3. 都不存在则新建
    4. 自动从旧位置 output/{user_dir_name}/.cache/ 迁移
    """
    cache_root = _get_cache_root()

    def _has_cache(d: Path) -> bool:
        c = d / ".cache"
        return (c / "links.json").exists() or (c / "progress.json").exists()

    dir_name = output_dir.name
    uid = dir_name.rsplit('_', 1)[-1] if '_' in dir_name else ""

    # 收集所有匹配的目录
    candidates = []
    for d in cache_root.iterdir():
        if not d.is_dir() or not _has_cache(d):
            continue
        d_name = d.name
        if d_name == dir_name:
            candidates.append(d)
        elif uid and (d_name == uid or d_name.endswith(f"_{uid}")):
            candidates.append(d)

    if candidates:
        # 同名目录优先做主目录
        primary = next((d for d in candidates if d.name == dir_name), candidates[0])
        primary_cache = primary / ".cache"
        primary_cache.mkdir(parents=True, exist_ok=True)
        # 从其他候选目录合并所有缓存文件
        for other in candidates:
            if other == primary:
                continue
            other_cache = other / ".cache"
            for src in other_cache.iterdir():
                if not src.is_file():
                    continue
                dst = primary_cache / src.name
                if not dst.exists():
                    try:
                        shutil.copy2(str(src), str(dst))
                    except Exception:
                        pass
        return primary_cache

    # 不存在则新建
    new_dir = cache_root / dir_name / ".cache"
    old_dir = output_dir / ".cache"

    # 一次性迁移：旧位置有缓存但新位置没有 → 复制
    if old_dir.exists() and not new_dir.exists():
        try:
            shutil.copytree(str(old_dir), str(new_dir))
        except Exception:
            pass

    new_dir.mkdir(parents=True, exist_ok=True)
    return new_dir


def _links_cache_key(output_dir: Path, user_id: str) -> Path:
    """获取链接缓存文件路径（用户级并集，与关键词无关）"""
    return _cache_dir(output_dir) / "links.json"


def _load_existing_cache_items(cache_file: Path) -> list | None:
    """从缓存文件读取 items（内部辅助，不打印日志）"""
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        return data.get('items', [])
    except Exception:
        return None


def load_links_cache(output_dir: Path, user_id: str) -> tuple[list | None, dict]:
    """加载缓存的回答链接列表（永久有效，除非 force_no_cache）
    返回 (items, meta) — meta含collected_at/oldest_id/newest_id用于增量时间校验
    缓存为用户级并集：同一用户的所有关键词匹配回答合并存储。"""
    if config.force_no_cache or config.cache_ttl_minutes < 0:
        return None, {}
    cache_file = _links_cache_key(output_dir, user_id)
    if not cache_file.exists():
        return None, {}
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        collected_at = data.get('collected_at', data.get('fetched_at', 0))
        age = time.time() - collected_at
        items = data.get('items', [])
        meta = {
            'collected_at': collected_at,
            'collected_at_iso': data.get('collected_at_iso', ''),
            'oldest_id': data.get('oldest_id', 0),
            'newest_id': data.get('newest_id', 0),
            'count': len(items),
        }
        # 显示时间 + ID 范围（用于增量校验）
        oldest = meta['oldest_id']
        newest = meta['newest_id']
        used_kws = data.get('used_keywords', [])
        kw_info = f"，已用关键词: {', '.join(used_kws)}" if used_kws else ""
        print(f"  📦 使用缓存链接（{len(items)}条并集，{age/3600:.1f}小时前收集，ID{oldest}～{newest}{kw_info}）")
        return items, meta
    except Exception:
        pass
    return None, {}


def save_links_cache(output_dir: Path, user_id: str, items: list, merge: bool = True,
                     new_keyword: str = ""):
    """缓存回答链接列表（永久有效，含时间戳和ID范围用于增量校验）
    
    merge=True（默认）：与已有缓存做并集合并（answer_id 去重），实现跨关键词累积。
    merge=False：直接覆盖（全量重新收集时使用）。
    new_keyword：本次使用的关键词，会追加到 used_keywords 元信息中。
    """
    if config.cache_ttl_minutes < 0:
        return
    cache_file = _links_cache_key(output_dir, user_id)

    # 合并模式：加载已有缓存做并集
    if merge:
        existing = _load_existing_cache_items(cache_file)
        if existing:
            existing_map = {it['answer_id']: it for it in existing}
            for it in items:
                existing_map[it['answer_id']] = it  # 新数据覆盖旧（更新预览等）
            items = list(existing_map.values())

    # 更新 used_keywords 元信息
    used_keywords = []
    if cache_file.exists():
        try:
            old_data = json.loads(cache_file.read_text(encoding='utf-8'))
            used_keywords = old_data.get('used_keywords', [])
        except Exception:
            pass
    if new_keyword and new_keyword not in used_keywords:
        used_keywords.append(new_keyword)

    # 计算 ID 范围
    ids = [int(it['answer_id']) for it in items if it.get('answer_id', '').isdigit()]
    data = {
        'user_id': user_id,
        'collected_at': time.time(),
        'collected_at_iso': time.strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(items),
        'oldest_id': min(ids) if ids else 0,
        'newest_id': max(ids) if ids else 0,
        'used_keywords': used_keywords,
        'items': items,
    }
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    # 清理旧版 keyword_hash 分隔的缓存文件（links_*.json），迁移到统一 links.json
    cache_dir = cache_file.parent
    for old_file in cache_dir.glob("links_*.json"):
        try:
            old_file.unlink()
        except Exception:
            pass


def load_answer_cache(output_dir: Path, answer_id: str) -> dict | None:
    """加载缓存的回答内容（永久有效，除非 force_no_cache）"""
    if config.force_no_cache or config.cache_ttl_minutes < 0:
        return None
    cache_file = _cache_dir(output_dir) / f"{answer_id}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        return data.get('result')
    except Exception:
        pass
    return None


def save_answer_cache(output_dir: Path, answer_id: str, result: dict):
    """缓存回答内容（永久有效）"""
    if config.cache_ttl_minutes < 0:
        return
    cache_file = _cache_dir(output_dir) / f"{answer_id}.json"
    data = {
        'answer_id': answer_id,
        'collected_at': time.time(),
        'result': result,
    }
    cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def clear_user_cache(output_dir: Path) -> int:
    """清除指定用户的所有缓存（links.json + 回答内容缓存），保留进度文件。返回删除文件数。"""
    cache_dir = _cache_dir(output_dir)
    if not cache_dir.exists():
        return 0
    deleted = 0
    for f in cache_dir.iterdir():
        if f.name in ('progress.json',):
            continue
        try:
            f.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted


# ── 法务模式转换：从缓存生成缺失的 HTML 文件 ──

def convert_to_forensic_mode(output_dir: Path, log_cb=None) -> dict:
    """
    将已爬取的内容转换为法务证据格式（补生成 .html 文件）

    原理：爬取时无论是否开启法务模式，crawl_answer_combined() 都会
    将清洗后的 html_text 存入缓存。此函数从缓存读取 html_text，
    为缺少 .html 文件的回答生成对应的 HTML 文件。

    参数:
        output_dir: 用户输出目录
        log_cb: 可选日志回调 log_cb(msg, level)

    返回:
        {converted: N, already_had: N, no_cache_html: N, no_cache: N, total: N}
    """
    from pathlib import Path as _Path
    completed_set, file_map = load_progress_with_files(_Path(output_dir))
    if not completed_set:
        if log_cb:
            log_cb("  无已完成记录", 'dim')
        return {'converted': 0, 'already_had': 0, 'no_cache_html': 0,
                'no_cache': 0, 'total': 0}

    output_dir = _Path(output_dir)
    converted = 0
    already_had = 0
    no_cache_html = 0
    no_cache = 0

    for aid in sorted(completed_set):
        # 确定 MD 文件名
        fname = file_map.get(aid, '')
        if fname:
            md_stem = _Path(fname).stem
        else:
            # 旧格式无文件名映射：扫描目录
            md_stem = None
            for f in output_dir.glob("*.md"):
                if aid in f.stem:
                    md_stem = f.stem
                    break

        if not md_stem:
            no_cache += 1
            if log_cb:
                log_cb(f"    ⚠ {aid}: 找不到对应 MD 文件", 'warning')
            continue

        html_path = output_dir / f"{md_stem}.html"
        if html_path.exists():
            already_had += 1
            continue

        # 从缓存读取 html_text
        cached = load_answer_cache(output_dir, aid)
        if not cached:
            no_cache += 1
            if log_cb:
                log_cb(f"    ⚠ {aid}: 缓存已丢失，需重新爬取", 'warning')
            continue

        html_text = cached.get('html_text', '')
        if not html_text:
            no_cache_html += 1
            if log_cb:
                log_cb(f"    ⚠ {aid}: 缓存中无 html_text（旧版缓存），需重新爬取", 'warning')
            continue

        try:
            html_path.write_text(html_text, encoding='utf-8')
            converted += 1
            if log_cb:
                log_cb(f"    ✓ {html_path.name}", 'info')
        except Exception as e:
            if log_cb:
                log_cb(f"    ✗ {html_path.name}: 写入失败 ({e})", 'error')

    return {
        'converted': converted,
        'already_had': already_had,
        'no_cache_html': no_cache_html,
        'no_cache': no_cache,
        'total': len(completed_set),
    }






