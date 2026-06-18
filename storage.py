"""
文件存储模块
负责 Markdown 文件写入、图片下载、进度管理
"""

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from config import config
from utils import sanitize_filename, format_datetime, format_datetime_full


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


def download_images(md_text: str, output_dir: Path, answer_id: str) -> str:
    """下载 Markdown 中的图片到本地，替换链接"""
    if not config.download_images:
        return md_text

    img_dir = output_dir / "images" / answer_id
    # 正则匹配 Markdown 图片: ![alt](url)
    pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

    def replacer(m):
        alt = m.group(1)
        url = m.group(2)

        # 跳过已经是本地路径的
        if url.startswith(('images/', './', '../', 'file://')):
            return m.group(0)

        try:
            img_dir.mkdir(parents=True, exist_ok=True)

            # 生成图片文件名
            ext = Path(urlparse(url).path).suffix or '.jpg'
            img_name = f"{int(time.time() * 1000)}_{hash(url) & 0xffff}{ext}"
            img_path = img_dir / img_name

            # 下载
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://www.zhihu.com/',
            }
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                img_path.write_bytes(resp.content)
                rel_path = f"images/{answer_id}/{img_name}"
                return f"![{alt}]({rel_path})"
        except Exception:
            pass

        return m.group(0)  # 下载失败保留原链接

    return pattern.sub(replacer, md_text)


def embed_images_base64(md_text: str) -> str:
    """下载 Markdown 中的图片并以 base64 data URI 嵌入，生成自包含 MD"""
    # 正则匹配 Markdown 图片: ![alt](url)
    pattern = re.compile(r'!\[([^\]]*)\]\(([^)]+)\)')

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
            resp = requests.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
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


def _load_progress_data(output_dir: Path) -> dict:
    """读取 progress.json 原始数据"""
    progress_file = output_dir / "progress.json"
    if progress_file.exists():
        try:
            return json.loads(progress_file.read_text(encoding='utf-8'))
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
                  file_map: dict = None, total_scraped: int = 0):
    """
    保存爬取进度
    file_map: {answer_id: filename} 可选的文件名映射
    """
    progress_file = output_dir / "progress.json"
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


def save_index(output_dir: Path, answers_meta: list):
    """生成回答索引文件（增量合并：保留历史条目 + 新条目）"""
    index_path = output_dir / "INDEX.md"

    # 解析已有索引条目（按文件名去重）
    existing_fnames = set()
    existing_lines = []
    idx_pattern = re.compile(
        r'^-\s*\[([^\]]*)\]\s*\[([^\]]*)\]\(([^)]+)\)\s*—\s*👍\s*(\d+)(.*)$'
    )

    if index_path.exists():
        for line in index_path.read_text(encoding='utf-8').split('\n'):
            m = idx_pattern.match(line.strip())
            if m:
                fname = m.group(3)
                existing_fnames.add(fname)
                existing_lines.append(line.strip())

    # 生成新条目（跳过已存在的文件名）
    new_lines = []
    for meta in answers_meta:
        fname = _filename(meta)
        if fname in existing_fnames:
            continue
        existing_fnames.add(fname)
        date = format_datetime(meta.get('date', ''))
        title = meta.get('title', '未命名')
        upvotes = meta.get('upvotes', 0)
        comment_count = meta.get('comment_count', 0)

        idx_line = f"- [{date}] [{title}]({fname}) — 👍 {upvotes}"
        if comment_count:
            idx_line += f" / 💬 {comment_count}"
        new_lines.append(idx_line)

    all_lines = existing_lines + new_lines
    total = len(all_lines)

    header = [f"# 回答索引\n\n共 {total} 条回答\n"]
    index_path.write_text('\n'.join(header + all_lines), encoding='utf-8')


def _sha256_file(filepath: Path) -> str:
    """计算文件的 SHA-256 哈希（16进制小写）"""
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def save_evidence_report(output_dir: Path, user_id: str,
                         all_meta: list, file_map: dict,
                         stats: dict):
    """
    生成电子证据采集报告 (EVIDENCE_REPORT.md)
    - 采集时间、工具、模式
    - 完整文件清单（扫描全部 MD 文件）+ SHA-256 哈希
    - 统计摘要
    """
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    col_mode = "混合模式（截图+文字+base64图片嵌入）"

    # 收集所有 MD 文件（不仅是本次爬取的）
    all_md_files = []
    if output_dir.exists():
        for f in sorted(output_dir.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.name in ("INDEX.md", "EVIDENCE_REPORT.md"):
                continue
            all_md_files.append(f)

    # 构建文件名→元数据映射（从 all_meta 和 file_map）
    fname_to_meta = {}
    for meta in all_meta:
        fname = _filename(meta)
        fname_to_meta[fname] = meta
    for aid, fname in file_map.items():
        if fname and fname not in fname_to_meta:
            fname_to_meta[fname] = {
                'answer_id': aid, 'title': '', 'upvotes': 0, 'comment_count': 0
            }

    lines = []
    lines.append("# 🔒 电子证据采集报告")
    lines.append("")
    lines.append("## 采集信息")
    lines.append("")
    lines.append(f"| 项目 | 内容 |")
    lines.append(f"|------|------|")
    lines.append(f"| 采集时间 | {now} (UTC+8) |")
    lines.append(f"| 采集工具 | zhihu-crawler + Playwright (Chromium) |")
    lines.append(f"| 目标用户 | `{user_id}` |")
    lines.append(f"| 用户主页 | https://www.zhihu.com/people/{user_id} |")
    lines.append(f"| 采集模式 | {col_mode} |")
    lines.append(f"| 输出目录 | `{output_dir.resolve()}` |")
    lines.append("")
    lines.append("## 文件清单")
    lines.append("")
    lines.append("| # | 文件名 | 回答ID | 赞同 | 评论 | SHA-256 |")
    lines.append("|---|--------|--------|------|------|---------|")

    for i, fpath in enumerate(all_md_files, 1):
        fname = fpath.name
        meta = fname_to_meta.get(fname, {})
        aid = meta.get('answer_id', '?')
        upvotes = meta.get('upvotes', 0)
        comment_count = meta.get('comment_count', 0)

        # 计算 SHA-256
        sha = _sha256_file(fpath) if fpath.exists() else '(文件缺失)'
        sha_short = sha[:16] + '...' if len(sha) > 16 else sha

        aid_display = str(aid)[:12] + '...' if aid and aid != '?' else '?'
        lines.append(
            f"| {i} | [{fname}]({fname}) | {aid_display} | "
            f"👍 {upvotes} | 💬 {comment_count} | `{sha_short}` |"
        )

    lines.append("")
    lines.append("## 统计摘要")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 回答总数 | {stats.get('total', 0)} |")
    lines.append(f"| 新增采集 | {stats.get('success', 0)} |")
    lines.append(f"| 采集失败 | {stats.get('failed', 0)} |")
    lines.append(f"| 跳过（已有） | {stats.get('skipped', 0)} |")
    lines.append("")
    lines.append("## 文件完整性验证")
    lines.append("")
    lines.append("每个 `.md` 文件的 SHA-256 哈希值已列入上方文件清单。")
    lines.append("可通过以下命令验证文件完整性：")
    lines.append("")
    lines.append("```bash")
    lines.append("# Windows PowerShell:")
    lines.append(f"Get-FileHash -Algorithm SHA256 *.md | Format-Table -AutoSize")
    lines.append("")
    lines.append("# Linux/macOS:")
    lines.append(f"sha256sum *.md")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("*本报告由 zhihu-crawler 自动生成，可作为电子证据固定记录。*")
    lines.append(f"*报告生成时间: {now}*")

    report_path = output_dir / "EVIDENCE_REPORT.md"
    report_path.write_text('\n'.join(lines), encoding='utf-8')
