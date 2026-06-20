"""
知乎回答爬虫 — GUI 界面
基于 Tkinter，提供完整配置和实时日志
"""
import sys
import os
import io
import json
import re
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
from pathlib import Path

# 工作目录切到项目根
os.chdir(Path(__file__).parent)

# 初始化日志（文件滚动覆盖，2MB×3）
from log_setup import init_logging
init_logging()

# 延迟导入爬虫模块
from config import config, Config
from utils import extract_user_id
from auth import get_cookie_path, get_login_state_path, load_manual_cookies
from id_manager import get_id_manager


# ══════════════════════════════════════════════════════════
# 日志重定向：将 print → GUI 日志区
# ══════════════════════════════════════════════════════════

class LogRedirector(io.StringIO):
    """捕获 stdout 输出并转发到 GUI 日志区"""
    def __init__(self, log_callback):
        super().__init__()
        self.log_callback = log_callback

    def write(self, s):
        if s and s.strip():
            self.log_callback(s.rstrip())

    def flush(self):
        pass


# ══════════════════════════════════════════════════════════
# 主 GUI 类
# ══════════════════════════════════════════════════════════

def _split_large_md(filepath, max_mb=10, log=None):
    """将过大的 .md 文件按 H2 标题边界分割，每个分块 < max_mb MB。"""
    fp = Path(filepath) if not isinstance(filepath, Path) else filepath
    if not fp.exists():
        return False, 0
    raw = fp.read_bytes()
    if len(raw) <= max_mb * 1024 * 1024:
        return False, 0
    text = raw.decode('utf-8', errors='replace')
    lines = text.split('\n')
    h2_idx = [0]
    for i, line in enumerate(lines):
        if re.match(r'^##\s', line):
            h2_idx.append(i)
    h2_idx.append(len(lines))
    sections = []
    for j in range(len(h2_idx) - 1):
        s, e = h2_idx[j], h2_idx[j + 1]
        sections.append((s, e, '\n'.join(lines[s:e])))
    max_bytes = max_mb * 1024 * 1024
    chunks = []
    cur_text = ''
    cur_start = 0
    for s, e, sec_text in sections:
        sec_b = len(sec_text.encode('utf-8'))
        if sec_b > max_bytes:
            if cur_text:
                chunks.append((cur_start, cur_text))
                cur_text = ''
            chunk_lines = []
            cstart = s
            for k in range(s, e):
                lb = len((lines[k] + '\n').encode('utf-8'))
                if chunk_lines and len('\n'.join(chunk_lines + [lines[k]]).encode('utf-8')) > max_bytes:
                    chunks.append((cstart, '\n'.join(chunk_lines)))
                    chunk_lines = [lines[k]]
                    cstart = k
                else:
                    chunk_lines.append(lines[k])
            if chunk_lines:
                chunks.append((cstart, '\n'.join(chunk_lines)))
            cur_start = e
            continue
        test = cur_text + '\n' + sec_text if cur_text else sec_text
        if len(test.encode('utf-8')) > max_bytes and cur_text:
            chunks.append((cur_start, cur_text))
            cur_text = sec_text
            cur_start = s
        else:
            cur_text = test
            if cur_start == 0:
                cur_start = s
    if cur_text:
        chunks.append((cur_start, cur_text))
    if len(chunks) <= 1:
        return False, 0
    stem, ext = fp.stem, fp.suffix
    for ci, (_, chunk_text) in enumerate(chunks, 1):
        new_path = fp.parent / f'{stem}({ci}){ext}'
        new_path.write_text(chunk_text + '\n' if not chunk_text.endswith('\n') else chunk_text, encoding='utf-8')
        if log:
            cmb = len(chunk_text.encode('utf-8')) / (1024 * 1024)
            log(f"    → {new_path.name} ({cmb:.1f} MB)", 'info')
    fp.unlink()
    if log:
        log(f"  ✂ 分割完成: {fp.name} → {len(chunks)} 个文件", 'info')
    return True, len(chunks)


class ZhihuCrawlerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("知乎回答爬虫 v1.0")
        self.root.geometry("880x820")
        self.root.minsize(780, 620)
        self._running = False
        self._stop_flag = False
        self._stop_event = threading.Event()
        self._crawler_thread = None
        self._raw_z_c0 = ""  # 完整cookie值（Entry显示掩码）

        # 加载配置
        self._cfg = Config.from_file()

        self._build_ui()
        self._load_config_to_ui()

        # 初始日志
        self._log("知乎回答爬虫 GUI 已就绪", 'info')
        self._log(f"Chrome 路径: {self._cfg.chrome_exe or '(使用内置 Chromium)'}", 'info')
        self._check_cookie()

    # ── UI 构建 ──────────────────────────────────────────

    def _build_ui(self):
        # 主容器
        main_frame = ttk.Frame(self.root, padding=8)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 顶部: 标题
        title_frame = ttk.Frame(main_frame)
        title_frame.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(title_frame, text="知乎回答爬虫",
                  font=("Microsoft YaHei", 16, "bold")).pack(side=tk.LEFT)
        self._login_status = ttk.Label(title_frame, text="🔍 检查登录状态...",
                                        foreground="gray")
        self._login_status.pack(side=tk.RIGHT, padx=10)

        # 左侧面板
        left_frame = ttk.Frame(main_frame, width=400)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        # ── 目标用户 ──
        user_frame = ttk.LabelFrame(left_frame, text="目标用户", padding=6)
        user_frame.pack(fill=tk.X, pady=(0, 6))

        # 快速添加行
        add_row = ttk.Frame(user_frame)
        add_row.pack(fill=tk.X, pady=(0, 4))
        self._quick_add_entry = ttk.Entry(add_row, width=30)
        self._quick_add_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._quick_add_entry.insert(0, "输入用户主页URL或用户ID...")
        self._quick_add_entry.bind('<FocusIn>', self._on_quick_add_focus_in)
        self._quick_add_entry.bind('<FocusOut>', self._on_quick_add_focus_out)
        self._quick_add_entry.bind('<Return>', self._add_user_from_entry)
        ttk.Button(add_row, text="➕ 添加到列表",
                   command=self._add_user_from_entry, width=12).pack(side=tk.LEFT, padx=(4, 0))

        # ID 列表
        list_frame = ttk.Frame(user_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self._user_listbox = tk.Listbox(list_frame, height=5, selectmode=tk.EXTENDED,
                                         font=("Consolas", 9),
                                         exportselection=False)
        self._user_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                   command=self._user_listbox.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._user_listbox.config(yscrollcommand=scrollbar.set)
        self._user_listbox.bind('<Double-1>', self._edit_nickname)

        # 列表操作按钮
        btn_row2 = ttk.Frame(user_frame)
        btn_row2.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row2, text="全选", command=self._select_all_users, width=6).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row2, text="全不选", command=self._deselect_all_users, width=6).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row2, text="✏ 编辑昵称", command=self._edit_nickname, width=10).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row2, text="📋 复制链接", command=self._copy_user_link, width=10).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row2, text="🗑 删除", command=self._delete_selected_users, width=8).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_row2, text="🔄 刷新列表", command=self._refresh_user_list, width=10).pack(side=tk.LEFT)
        self._list_status = ttk.Label(btn_row2, text="", foreground="gray")
        self._list_status.pack(side=tk.RIGHT)

        # 初始化 ID 列表
        self._id_mgr = None
        self._refresh_user_list()

        # ── Cookie ──
        cookie_frame = ttk.LabelFrame(left_frame, text="登录 Cookie", padding=6)
        cookie_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(cookie_frame, text="z_c0:  (知乎关键登录 Cookie)").pack(anchor=tk.W)
        self._z_c0_entry = ttk.Entry(cookie_frame, width=50)
        self._z_c0_entry.pack(fill=tk.X, pady=(2, 0))
        self._z_c0_entry.bind('<FocusIn>', self._on_cookie_focus_in)
        self._z_c0_entry.bind('<FocusOut>', self._on_cookie_focus_out)

        btn_row = ttk.Frame(cookie_frame)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(btn_row, text="保存 Cookie", command=self._save_cookies).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(btn_row, text="检测登录状态", command=self._check_cookie).pack(side=tk.LEFT)
        ttk.Button(btn_row, text="清除 Cookie", command=self._clear_cookies).pack(side=tk.LEFT, padx=(6, 0))

        # ── 爬取设置 ──
        crawl_frame = ttk.LabelFrame(left_frame, text="爬取设置", padding=6)
        crawl_frame.pack(fill=tk.X, pady=(0, 6))

        row1 = ttk.Frame(crawl_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="最多爬取:", width=10).pack(side=tk.LEFT)
        self._max_entry = ttk.Entry(row1, width=8)
        self._max_entry.insert(0, "0")
        self._max_entry.pack(side=tk.LEFT)
        ttk.Label(row1, text="（0=全部）", foreground="gray").pack(side=tk.LEFT, padx=4)
        # 关键词搜索
        ttk.Label(row1, text="🔍 关键词:", foreground="#4ec9b0").pack(side=tk.LEFT, padx=(16, 0))
        self._keyword_entry = ttk.Entry(row1, width=20)
        self._keyword_entry.pack(side=tk.LEFT, padx=2)
        ttk.Label(row1, text="（标题或回答内容含关键词即抓取，自动去重）", foreground="gray",
                  font=("", 8)).pack(side=tk.LEFT)

        row2 = ttk.Frame(crawl_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="输出目录:", width=10).pack(side=tk.LEFT)
        self._output_entry = ttk.Entry(row2)
        self._output_entry.insert(0, "output")
        self._output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        row3 = ttk.Frame(crawl_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Chrome 路径:", width=10).pack(side=tk.LEFT)
        self._chrome_entry = ttk.Entry(row3)
        self._chrome_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        row4 = ttk.Frame(crawl_frame)
        row4.pack(fill=tk.X, pady=2)
        self._headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="无头模式（后台运行）", variable=self._headless_var).pack(side=tk.LEFT)
        self._test_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="🧪 测试模式（只爬3条）", variable=self._test_var,
                        command=self._on_test_toggle).pack(side=tk.LEFT, padx=15)

        row6 = ttk.Frame(crawl_frame)
        row6.pack(fill=tk.X, pady=2)
        self._forensic_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row6, text="🔒 法务证据模式（保存HTML + 生成证据报告 + SHA256哈希）",
                        variable=self._forensic_var,
                        command=self._on_forensic_toggle).pack(side=tk.LEFT)
        self._save_html_var = tk.BooleanVar(value=False)
        self._save_html_cb = ttk.Checkbutton(row6, text="保存原始 HTML（法务模式默认开启）",
                        variable=self._save_html_var)
        self._save_html_cb.pack(side=tk.LEFT, padx=15)

        # ── 延迟设置 ──
        delay_frame = ttk.LabelFrame(left_frame, text="延迟策略（秒）", padding=6)
        delay_frame.pack(fill=tk.X, pady=(0, 6))
        row_d1 = ttk.Frame(delay_frame)
        row_d1.pack(fill=tk.X, pady=2)

        # 滚动间隔
        ttk.Label(row_d1, text="滚动:").pack(side=tk.LEFT)
        self._scroll_min = ttk.Entry(row_d1, width=4)
        self._scroll_min.insert(0, "2")
        self._scroll_min.pack(side=tk.LEFT)
        ttk.Label(row_d1, text="-").pack(side=tk.LEFT)
        self._scroll_max = ttk.Entry(row_d1, width=4)
        self._scroll_max.insert(0, "5")
        self._scroll_max.pack(side=tk.LEFT)

        # 翻页间隔
        ttk.Label(row_d1, text="  翻页:").pack(side=tk.LEFT)
        self._page_min = ttk.Entry(row_d1, width=4)
        self._page_min.insert(0, "3")
        self._page_min.pack(side=tk.LEFT)
        ttk.Label(row_d1, text="-").pack(side=tk.LEFT)
        self._page_max = ttk.Entry(row_d1, width=4)
        self._page_max.insert(0, "8")
        self._page_max.pack(side=tk.LEFT)

        # 缓存有效期
        ttk.Label(row_d1, text="  缓存(分):").pack(side=tk.LEFT)
        self._cache_ttl = ttk.Entry(row_d1, width=4)
        self._cache_ttl.insert(0, "30")
        self._cache_ttl.pack(side=tk.LEFT)
        ttk.Label(row_d1, text="(0=禁用)", foreground="gray").pack(side=tk.LEFT, padx=2)

        row_d4 = ttk.Frame(delay_frame)
        row_d4.pack(fill=tk.X, pady=(4, 2))
        self._force_no_cache_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row_d4, text="🔄 强制忽略缓存（测试用：跳过所有缓存+进度，从头重爬）",
                        variable=self._force_no_cache_var).pack(side=tk.LEFT)

        # ── 操作控制 ──
        action_frame = ttk.LabelFrame(left_frame, text="操作", padding=6)
        action_frame.pack(fill=tk.X, pady=(8, 0))

        self._progress = ttk.Progressbar(action_frame, mode='determinate')
        self._progress.pack(fill=tk.X, pady=(0, 4))

        ctrl_row = ttk.Frame(action_frame)
        ctrl_row.pack(fill=tk.X)
        self._progress_label = ttk.Label(ctrl_row, text="就绪", foreground="gray")
        self._progress_label.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(ctrl_row, text="■ 停止",
                                     command=self._stop_crawl, state=tk.DISABLED, width=10)
        self._stop_btn.pack(side=tk.RIGHT, padx=(4, 0))
        self._start_btn = ttk.Button(ctrl_row, text="▶ 开始爬取",
                                      command=self._start_crawl, width=14)
        self._start_btn.pack(side=tk.RIGHT)

        # ── 手动链接保存（独立功能区）──
        ttk.Separator(left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 2))
        ttk.Label(left_frame, text="📋 独立功能：手动粘贴链接保存",
                  font=("", 9, "bold"), foreground="#888").pack(anchor=tk.W, pady=(0, 2))

        manual_frame = ttk.LabelFrame(left_frame, text="🔗 手动链接保存为 MD（无需预配置用户ID · 每行一条 · 上限20条）", padding=6)
        manual_frame.pack(fill=tk.X, pady=(0, 6))

        self._manual_text = tk.Text(manual_frame, height=4, font=("Consolas", 9),
                                     wrap=tk.WORD, fg="gray")
        self._manual_text.insert("1.0", "粘贴知乎回答链接（支持App复制格式：文字+链接）...")
        self._manual_text.bind('<FocusIn>', self._on_manual_focus_in)
        self._manual_text.bind('<FocusOut>', self._on_manual_focus_out)
        self._manual_text.pack(fill=tk.X)

        manual_btn_row = ttk.Frame(manual_frame)
        manual_btn_row.pack(fill=tk.X, pady=(4, 0))
        self._manual_btn = ttk.Button(manual_btn_row, text="📝 批量保存为 MD",
                                      command=self._save_manual_urls, width=16)
        self._manual_btn.pack(side=tk.LEFT)
        ttk.Label(manual_btn_row, text="支持: https://www.zhihu.com/question/xxx/answer/xxx",
                  foreground="gray", font=("", 8)).pack(side=tk.LEFT, padx=8)

        # 分割大MD工具（左侧独立功能区）
        ttk.Separator(left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 2))
        split_row = ttk.Frame(left_frame)
        split_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(split_row, text="🔧 工具",
                  font=("", 9, "bold"), foreground="#888").pack(side=tk.LEFT)
        ttk.Button(split_row, text="✂ 分割大MD（>10MB按标题拆分）",
                   command=self._split_large_md_file, width=28).pack(side=tk.LEFT, padx=(8, 0))

        # 右侧面板: 日志
        right_frame = ttk.Frame(main_frame, width=420)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(4, 0))

        log_frame = ttk.LabelFrame(right_frame, text="运行日志", padding=4)
        log_frame.pack(fill=tk.BOTH, expand=True)

        self._log_area = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white"
        )
        self._log_area.pack(fill=tk.BOTH, expand=True)

        # 日志颜色标签
        for tg, cl in [('success', '#4ec9b0'), ('error', '#f44747'),
                        ('info', '#569cd6'), ('warn', '#e5b73c'), ('dim', '#888')]:
            self._log_area.tag_configure(tg, foreground=cl)

        # 日志操作
        log_btn_row = ttk.Frame(right_frame)
        log_btn_row.pack(fill=tk.X, pady=(4, 0))
        ttk.Button(log_btn_row, text="🗑 清空日志区", command=self._clear_log, width=12).pack(side=tk.RIGHT)

        # 关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 日志方法 ────────────────────────────────────────

    def _log(self, msg: str, tag: str = 'info'):
        self._log_area.configure(state=tk.NORMAL)
        self._log_area.insert(tk.END, msg + '\n', tag)
        self._log_area.see(tk.END)
        self._log_area.configure(state=tk.DISABLED)

    def _clear_log(self):
        self._log_area.configure(state=tk.NORMAL)
        self._log_area.delete(1.0, tk.END)
        self._log_area.configure(state=tk.DISABLED)

    # ── ID 列表管理 ─────────────────────────────────────

    def _get_id_manager(self):
        """获取 ID 管理器单例"""
        if self._id_mgr is None:
            self._id_mgr = get_id_manager()
        return self._id_mgr

    def _refresh_user_list(self):
        """刷新用户列表显示"""
        self._user_listbox.delete(0, tk.END)
        mgr = self._get_id_manager()
        for nickname, user_id, url in mgr.get_display_list():
            history = mgr.get_crawl_history(user_id)
            hist_info = ""
            if history:
                last = history[-1]
                hist_info = f" | 最近: {last['date'][:16]}, {last['answers_scraped']}条"
            self._user_listbox.insert(tk.END, f"{nickname}    ({user_id})    🔗 {url}{hist_info}")
        total = len(mgr.users)
        self._list_status.config(
            text=f"共 {total} 个用户"
        )

    def _add_user_from_entry(self, event=None):
        """从快速添加输入框添加用户"""
        raw = self._quick_add_entry.get().strip()
        if not raw or raw == "输入用户主页URL或用户ID...":
            return

        try:
            user_id = extract_user_id(raw)
        except ValueError:
            messagebox.showwarning("格式错误",
                f"无法从输入中提取用户 ID:\n{raw}\n\n"
                "支持格式: user_id / https://www.zhihu.com/people/xxx")
            return

        mgr = self._get_id_manager()
        url = f"https://www.zhihu.com/people/{user_id}"
        if mgr.add(user_id, nickname=user_id, url=url):
            self._quick_add_entry.delete(0, tk.END)
            self._refresh_user_list()
            # 自动选中新添加的
            for i in range(self._user_listbox.size()):
                if user_id in self._user_listbox.get(i):
                    self._user_listbox.selection_set(i)
                    self._user_listbox.see(i)
                    break
            self._log(f"✅ 已添加用户: {user_id}", 'success')
        else:
            messagebox.showinfo("已存在", f"用户 {user_id} 已在列表中")

    def _on_quick_add_focus_in(self, event):
        if self._quick_add_entry.get() == "输入用户主页URL或用户ID...":
            self._quick_add_entry.delete(0, tk.END)
            self._quick_add_entry.config(foreground="black")

    def _on_quick_add_focus_out(self, event):
        if not self._quick_add_entry.get().strip():
            self._quick_add_entry.insert(0, "输入用户主页URL或用户ID...")
            self._quick_add_entry.config(foreground="gray")

    def _on_manual_focus_in(self, event):
        text = self._manual_text.get("1.0", tk.END).strip()
        if text == "粘贴知乎回答链接（支持App复制格式：文字+链接）...":
            self._manual_text.delete("1.0", tk.END)
            self._manual_text.config(fg="black")

    def _on_manual_focus_out(self, event):
        if not self._manual_text.get("1.0", tk.END).strip():
            self._manual_text.insert("1.0", "粘贴知乎回答链接（支持App复制格式：文字+链接）...")
            self._manual_text.config(fg="gray")

    MAX_MANUAL_URLS = 20

    def _save_manual_urls(self, event=None):
        """批量手动链接 → 保存为 MD"""
        raw = self._manual_text.get("1.0", tk.END).strip()
        if not raw or raw == "粘贴知乎回答链接（支持App复制格式：文字+链接）...":
            messagebox.showwarning("提示", "请先粘贴知乎回答链接")
            return

        # 用正则从全部文本中提取知乎链接（兼容App复制的带文字格式）
        urls = re.findall(r'https?://[^\s]*zhihu\.com[^\s]*', raw)
        # 去重保持顺序
        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            messagebox.showwarning("提示", "请粘贴知乎回答链接（含 zhihu.com）")
            return

        if len(urls) > self.MAX_MANUAL_URLS:
            urls = urls[:self.MAX_MANUAL_URLS]
            self._manual_text.delete("1.0", tk.END)
            self._manual_text.insert("1.0", "\n".join(urls))

        if self._running:
            messagebox.showwarning("提示", "已有爬取任务在运行中，请等待完成")
            return

        self._running = True
        self._start_btn.config(state=tk.DISABLED)
        self._manual_btn.config(state=tk.DISABLED)
        self._progress_label.config(text=f"正在批量保存 ({len(urls)}条)...")

        # 读取关键词
        keyword = self._keyword_entry.get().strip()

        threading.Thread(
            target=self._manual_urls_thread,
            args=(urls, keyword),
            daemon=True
        ).start()

    def _manual_urls_thread(self, urls: list, keyword: str = ""):
        """后台线程：批量保存多条链接为 MD"""
        import time as _time
        from playwright.sync_api import sync_playwright
        from auth import ensure_login, get_login_state_path
        from crawler import crawl_single_url

        original_stdout = sys.stdout
        sys.stdout = LogRedirector(lambda m: self.root.after(0, self._log, m, 'info'))

        success_count = 0
        fail_count = 0
        skip_count = 0

        try:
            total = len(urls)
            self._log(f"\n🔗 批量保存 {total} 条链接", 'info')
            if keyword:
                self._log(f"🔍 关键词过滤: 「{keyword}」", 'info')

            with sync_playwright() as p:
                launch_kwargs = {
                    'headless': self._cfg.headless,
                    'args': [
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                    ]
                }
                if self._cfg.chrome_exe:
                    launch_kwargs['executable_path'] = self._cfg.chrome_exe

                browser = p.chromium.launch(**launch_kwargs)

                state_path = get_login_state_path(self._cfg.browser_data_dir)
                context_kwargs = {
                    'viewport': {'width': 1920, 'height': 1080},
                    'device_scale_factor': 2,
                    'user_agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    ),
                    'locale': 'zh-CN',
                }

                if state_path.exists():
                    try:
                        context = browser.new_context(storage_state=str(state_path), **context_kwargs)
                    except Exception:
                        context = browser.new_context(**context_kwargs)
                else:
                    context = browser.new_context(**context_kwargs)

                page = context.new_page()

                # 确保登录（仅一次）
                try:
                    ensure_login(page, self._cfg)
                except Exception as e:
                    self._log(f"登录失败: {e}", 'error')
                    browser.close()
                    return

                for i, url in enumerate(urls, 1):
                    self._log(f"  [{i}/{total}] {url[:80]}...", 'info')
                    result = crawl_single_url(page, url, output_dir=self._cfg.output_dir,
                                              keyword=keyword)
                    if result['success']:
                        self._log(f"    ✅ {result['md_path']}", 'success')
                        success_count += 1
                    elif '不含关键词' in result.get('error', ''):
                        self._log(f"    ⊘ 跳过（不含关键词）", 'dim')
                        skip_count += 1
                    else:
                        self._log(f"    ❌ {result['error']}", 'error')
                        fail_count += 1

                browser.close()

        except Exception as e:
            self._log(f"致命错误: {e}", 'error')
            import traceback
            self._log(traceback.format_exc(), 'error')
        finally:
            sys.stdout = original_stdout
            self._running = False
            summary = f"✅ {success_count} 成功"
            if skip_count:
                summary += f" / ⊘ {skip_count} 跳过（不含关键词）"
            if fail_count:
                summary += f" / ❌ {fail_count} 失败"
            self._log(f"\n📊 批量保存完成: {summary}", 'info')
            self.root.after(0, lambda: messagebox.showinfo("批量保存完成",
                f"批量保存 {len(urls)} 条完成:\n{summary}"))
            self.root.after(0, self._on_manual_done)

    def _on_manual_done(self):
        """手动保存完成后恢复 UI"""
        self._start_btn.config(state=tk.NORMAL)
        self._manual_btn.config(state=tk.NORMAL)
        self._progress_label.config(text="就绪")

    def _split_large_md_file(self):
        """选择目录，扫描并分割 >10MB 的 .md 文件"""
        d = filedialog.askdirectory(title="选择要扫描的 MD 文件目录",
            initialdir=self._cfg.output_dir or os.path.join(os.path.dirname(__file__), "output"))
        if not d:
            return
        big_files = []
        for mf in sorted(Path(d).rglob('*.md')):
            if not mf.is_file():
                continue
            sz = mf.stat().st_size
            if sz > 10 * 1024 * 1024:
                big_files.append((str(mf), sz))
        if not big_files:
            messagebox.showinfo("无需分割", "该目录下没有超过 10 MB 的 .md 文件。")
            return
        names = '\n'.join(f'  · {Path(f).name} ({s / 1024 / 1024:.1f} MB)' for f, s in big_files[:20])
        if len(big_files) > 20:
            names += f'\n  ... 共 {len(big_files)} 个'
        ok = messagebox.askyesno(
            "分割大文件",
            f"检测到 {len(big_files)} 个 .md 文件超过 10 MB：\n\n"
            f"{names}\n\n"
            f"是否按 H2 标题自动分割为 <10 MB 的小文件？\n"
            f"分割后编号保留，仅加 (1)(2)(3) 后缀。"
        )
        if not ok:
            return
        self._log("\n✂ 开始分割大文件...", 'info')

        def worker():
            count = 0
            for fp, sz in big_files:
                try:
                    did, n = _split_large_md(fp, 10, lambda m, t='info': self.root.after(0, lambda: self._log(m, t)))
                    if did:
                        count += 1
                except Exception as e:
                    self.root.after(0, lambda e=e, fp=fp: self._log(f"  ✗ 分割失败 {Path(fp).name}: {e}", 'error'))
            self.root.after(0, lambda: self._log(f"✂ 分割完成: {len(big_files)} 个大文件 → {count} 个已分割", 'info'))
            self.root.after(0, lambda c=count: messagebox.showinfo("分割完成", f"已处理 {len(big_files)} 个大文件，\n{count} 个文件已被分割。"))

        threading.Thread(target=worker, daemon=True).start()

    def _select_all_users(self):
        self._user_listbox.selection_set(0, tk.END)

    def _deselect_all_users(self):
        self._user_listbox.selection_clear(0, tk.END)

    def _delete_selected_users(self):
        selected = self._user_listbox.curselection()
        if not selected:
            return
        if not messagebox.askyesno("确认", f"确定删除选中的 {len(selected)} 个用户吗？"):
            return
        mgr = self._get_id_manager()
        for i in reversed(selected):
            item_text = self._user_listbox.get(i)
            # 提取 user_id: "昵称    (user_id)"
            import re as _re
            m = _re.search(r'\(([^)]+)\)', item_text)
            if m:
                mgr.remove(m.group(1))
        self._refresh_user_list()
        self._log(f"🗑 已删除 {len(selected)} 个用户", 'info')

    def _copy_user_link(self):
        """复制选中用户的页面链接到剪贴板"""
        selected = self._user_listbox.curselection()
        if not selected:
            messagebox.showinfo("提示", "请先在列表中选中一个用户")
            return
        idx = selected[0]
        item_text = self._user_listbox.get(idx)
        import re as _re
        m = _re.search(r'\(([^)]+)\)', item_text)
        if not m:
            return
        user_id = m.group(1)
        mgr = self._get_id_manager()
        user = mgr.find(user_id)
        if not user:
            return
        url = user.url or f"https://www.zhihu.com/people/{user_id}"
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._list_status.config(text=f"已复制: {url}")
        self._log(f"📋 已复制到剪贴板: {url}", 'info')

    def _edit_nickname(self, event=None):
        """编辑选中用户的昵称"""
        selected = self._user_listbox.curselection()
        if not selected:
            return
        idx = selected[0]
        item_text = self._user_listbox.get(idx)
        import re as _re
        m = _re.search(r'\(([^)]+)\)', item_text)
        if not m:
            return
        user_id = m.group(1)
        mgr = self._get_id_manager()
        user = mgr.find(user_id)
        if not user:
            return

        from tkinter import simpledialog
        new_nick = simpledialog.askstring(
            "编辑昵称",
            f"用户 ID: {user_id}\n输入新昵称:",
            initialvalue=user.nickname
        )
        if new_nick and new_nick.strip():
            mgr.update_nickname(user_id, new_nick.strip())
            self._refresh_user_list()
            self._log(f"✏ {user_id} → {new_nick.strip()}", 'info')

    # ── Cookie 管理 ─────────────────────────────────────

    def _check_cookie(self):
        """检测是否有有效 Cookie"""
        cp = get_cookie_path(self._cfg.browser_data_dir)
        sp = get_login_state_path(self._cfg.browser_data_dir)
        if sp.exists():
            self._login_status.config(text="🟢 已登录 (storage_state)", foreground="#4ec9b0")
        elif cp.exists():
            cookies = load_manual_cookies(cp)
            if cookies:
                self._login_status.config(text="🟡 有手动 Cookie (待验证)", foreground="#e5b73c")
                # 回填到输入框（只取 z_c0，显示掩码）
                for c in cookies:
                    if c.get('name') == 'z_c0':
                        raw = c.get('value', '')
                        self._raw_z_c0 = raw
                        self._z_c0_entry.delete(0, tk.END)
                        self._z_c0_entry.insert(0, self._mask_cookie(raw))
                        break
            else:
                self._login_status.config(text="🔴 未登录", foreground="#f44747")
        else:
            self._login_status.config(text="🔴 未登录", foreground="#f44747")

    def _mask_cookie(self, val: str) -> str:
        """前6位可见 + **** + 后4位可见"""
        if not val or len(val) < 8:
            return val
        return f"{val[:6]}****{val[-4:]}"

    def _on_cookie_focus_in(self, event=None):
        """点击输入框 → 显示完整cookie"""
        if self._raw_z_c0:
            self._z_c0_entry.delete(0, tk.END)
            self._z_c0_entry.insert(0, self._raw_z_c0)

    def _on_cookie_focus_out(self, event=None):
        """离开输入框 → 恢复掩码显示"""
        val = self._z_c0_entry.get().strip()
        if val:
            self._raw_z_c0 = val  # 用户可能修改了
            self._z_c0_entry.delete(0, tk.END)
            self._z_c0_entry.insert(0, self._mask_cookie(val))

    def _get_raw_cookie(self) -> str:
        """获取完整cookie值（优先从raw取，否则从entry取）"""
        entry_val = self._z_c0_entry.get().strip()
        if '*' in entry_val:
            return self._raw_z_c0
        return entry_val

    def _save_cookies(self):
        """保存 Cookie 到文件"""
        zc0 = self._get_raw_cookie()
        if not zc0:
            messagebox.showwarning("提示", "请先填入 z_c0 Cookie 值")
            return
        self._raw_z_c0 = zc0
        cookies = [
            {"name": "z_c0", "value": zc0, "domain": ".zhihu.com", "path": "/"},
            {"name": "_zap", "value": "0", "domain": ".zhihu.com", "path": "/"},
            {"name": "d_c0", "value": "0", "domain": ".zhihu.com", "path": "/"},
        ]
        cp = get_cookie_path(self._cfg.browser_data_dir)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding='utf-8')
        self._login_status.config(text="🟡 Cookie 已保存 (待验证)", foreground="#e5b73c")
        self._log("💾 Cookie 已保存到 browser_data/cookies.json", 'info')
        # 保存后显示掩码
        self._z_c0_entry.delete(0, tk.END)
        self._z_c0_entry.insert(0, self._mask_cookie(zc0))
        messagebox.showinfo("保存成功",
            "Cookie 已保存。\n\n"
            "如果你还知道 _zap 和 d_c0 的值，可以手动编辑\n"
            "browser_data/cookies.json 补充，提升登录成功率。")

    def _clear_cookies(self):
        """清除所有登录状态"""
        if not messagebox.askyesno("确认", "确定要清除所有登录状态吗？\n\n将删除 browser_data 下的 Cookie 文件。"):
            return
        for f in [get_cookie_path(self._cfg.browser_data_dir),
                   get_login_state_path(self._cfg.browser_data_dir)]:
            if f.exists():
                f.unlink()
        self._raw_z_c0 = ""
        self._z_c0_entry.delete(0, tk.END)
        self._login_status.config(text="🔴 未登录", foreground="#f44747")
        self._log("🗑 登录状态已清除", 'info')

    # ── 配置管理 ────────────────────────────────────────

    def _load_config_to_ui(self):
        """将当前配置反映到 UI"""
        # targets 现在由 ID 列表管理，不再从文本区加载
        self._max_entry.delete(0, tk.END)
        self._max_entry.insert(0, str(self._cfg.max_answers))
        self._output_entry.delete(0, tk.END)
        self._output_entry.insert(0, self._cfg.output_dir)
        self._chrome_entry.delete(0, tk.END)
        self._chrome_entry.insert(0, self._cfg.chrome_exe)
        self._headless_var.set(self._cfg.headless)
        self._test_var.set(self._cfg.test_mode)
        self._forensic_var.set(self._cfg.forensic_mode)
        self._save_html_var.set(self._cfg.save_html)
        self._on_forensic_toggle()  # 初始同步 save_html 状态
        self._scroll_min.delete(0, tk.END)
        self._scroll_min.insert(0, str(int(self._cfg.scroll_delay_min)))
        self._scroll_max.delete(0, tk.END)
        self._scroll_max.insert(0, str(int(self._cfg.scroll_delay_max)))
        self._page_min.delete(0, tk.END)
        self._page_min.insert(0, str(int(self._cfg.page_delay_min)))
        self._page_max.delete(0, tk.END)
        self._page_max.insert(0, str(int(self._cfg.page_delay_max)))
        self._cache_ttl.delete(0, tk.END)
        self._cache_ttl.insert(0, str(self._cfg.cache_ttl_minutes))
        self._force_no_cache_var.set(self._cfg.force_no_cache)
        self._on_test_toggle()  # 初始同步「最多爬取」输入框的启用状态

    def _read_config_from_ui(self):
        """从 UI 读取配置，更新 Config 对象"""
        # targets 现在从 ID 列表读取，不再从文本区读取

        # 数字字段
        for attr, widget in [
            ('max_answers', self._max_entry),
            ('scroll_delay_min', self._scroll_min),
            ('scroll_delay_max', self._scroll_max),
            ('page_delay_min', self._page_min),
            ('page_delay_max', self._page_max),
            ('cache_ttl_minutes', self._cache_ttl),
        ]:
            try:
                v = float(widget.get().strip() or 0)
                setattr(self._cfg, attr,
                        int(v) if attr in ('max_answers', 'cache_ttl_minutes') else v)
            except ValueError:
                pass

        self._cfg.output_dir = self._output_entry.get().strip() or "output"
        self._cfg.chrome_exe = self._chrome_entry.get().strip()
        self._cfg.headless = self._headless_var.get()
        # 固定为混合模式：截图 + 文字 + base64图片嵌入
        self._cfg.download_images = True
        self._cfg.embed_images = True
        self._cfg.screenshot_mode = True
        self._cfg.test_mode = self._test_var.get()
        self._cfg.forensic_mode = self._forensic_var.get()
        self._cfg.save_html = self._save_html_var.get()
        self._cfg.force_no_cache = self._force_no_cache_var.get()
        # 法务模式自动开启 save_html
        if self._cfg.forensic_mode:
            self._cfg.save_html = True
            self._save_html_var.set(True)  # 同步 UI 复选框

        # 保存到文件
        self._cfg.save("config.json")

        # 同步到模块级 config 单例（爬虫线程读取此对象）
        from dataclasses import fields
        for f in fields(self._cfg):
            setattr(config, f.name, getattr(self._cfg, f.name))

    # ── 爬取控制 ────────────────────────────────────────

    def _on_test_toggle(self):
        """测试模式切换时联动「最多爬取」输入框"""
        if self._test_var.get():
            self._max_entry.config(state=tk.DISABLED)
        else:
            self._max_entry.config(state=tk.NORMAL)

    def _on_forensic_toggle(self):
        """法务模式切换时联动 save_html 复选框"""
        if self._forensic_var.get():
            self._save_html_var.set(True)
            self._save_html_cb.config(state=tk.DISABLED)
        else:
            self._save_html_cb.config(state=tk.NORMAL)

    def _start_crawl(self):
        if self._running:
            messagebox.showwarning("提示", "已有爬取任务在运行中")
            return

        self._read_config_from_ui()

        # 从 ID 列表获取选中的用户
        selected_indices = self._user_listbox.curselection()
        if not selected_indices:
            messagebox.showwarning("提示",
                "请先在「目标用户」列表中选中要爬取的用户\n\n"
                "如果列表为空，请在上方输入 URL 后点击「添加到列表」")
            return

        # 提取选中的 user_id
        import re as _re
        user_ids = []
        for idx in selected_indices:
            item_text = self._user_listbox.get(idx)
            m = _re.search(r'\(([^)]+)\)', item_text)
            if m:
                user_ids.append(m.group(1))

        if not user_ids:
            messagebox.showwarning("提示", "无法解析选中的用户 ID")
            return

        # ── 预检查：检测本地文件缺失 ──
        force_recrawl_map = {}  # user_id → set of answer_ids to re-crawl
        from crawler import get_crawl_status

        for uid in user_ids:
            try:
                status = get_crawl_status(uid)
            except Exception:
                continue

            if status['missing_count'] > 0:
                # 有爬取记录但本地文件缺失
                missing_list = []
                for aid, detail in status['missing_details'].items():
                    fname = detail.get('filename', '?')
                    missing_list.append(f"  • [{aid[:8]}...] {fname}")

                msg = (
                    f"⚠ 用户 {uid} 的爬取记录中有 {status['missing_count']} 条回答"
                    f"的本地文件缺失（共 {status['total_progress']} 条记录）：\n\n"
                    + '\n'.join(missing_list[:10])
                )
                if len(missing_list) > 10:
                    msg += f"\n  ... 等共 {len(missing_list)} 条"

                msg += "\n\n如何处理这些缺失的回答？"

                choice = messagebox.askquestion(
                    f"文件缺失 — {uid}",
                    msg + "\n\n「是」= 重新爬取缺失的\n「否」= 跳过",
                    icon='warning'
                )

                if choice == 'yes':
                    force_recrawl_map[uid] = status['missing_ids']
                    self._log(f"🔄 {uid}: 将重新爬取 {len(status['missing_ids'])} 条缺失文件", 'warn')
                else:
                    self._log(f"⏭ {uid}: 跳过 {len(status['missing_ids'])} 条缺失文件", 'info')

        # ── 启动爬取 ──
        self._log(f"\n{'='*55}", 'dim')
        self._log(f"🎯 目标用户: {', '.join(user_ids)}", 'info')
        if self._cfg.test_mode:
            self._log(f"🧪 测试模式: 开启（只爬3条）", 'info')
        else:
            self._log(f"📊 最多爬取: {'全部' if self._cfg.max_answers == 0 else self._cfg.max_answers} 条/人", 'info')
        self._log(f"🎯 输出模式: 混合模式（截图 + 文字 + base64图片嵌入）", 'info')
        self._log(f"👁 无头模式: {'是' if self._cfg.headless else '否'}", 'info')
        self._log(f"🔒 法务证据模式: {'是（HTML + 证据报告 + SHA256）' if self._cfg.forensic_mode else '否'}", 'info')
        if self._cfg.force_no_cache:
            self._log(f"🔄 强制忽略缓存: 开启（跳过所有缓存+进度）", 'info')
        self._log(f"{'='*55}\n", 'dim')

        # 读取关键词
        keyword = self._keyword_entry.get().strip()
        if keyword:
            self._log(f"🔍 关键词过滤: 「{keyword}」", 'info')

        # 检查 Chrome
        if self._cfg.chrome_exe and not Path(self._cfg.chrome_exe).exists():
            self._log(f"⚠ Chrome 路径不存在: {self._cfg.chrome_exe}", 'warn')

        # 状态切换
        self._running = True
        self._stop_flag = False
        self._stop_event.clear()
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._progress['value'] = 0
        self._progress_label.config(text="正在启动浏览器...")

        # 后台线程
        self._crawler_thread = threading.Thread(
            target=self._crawl_thread,
            args=(user_ids, force_recrawl_map, keyword),
            daemon=True
        )
        self._crawler_thread.start()

    def _stop_crawl(self):
        if not self._running:
            return
        self._stop_flag = True
        self._stop_event.set()
        self._log("\n⚠ 正在停止...（请等待当前页面处理完成）", 'warn')
        self._stop_btn.config(state=tk.DISABLED)

    def _crawl_thread(self, user_ids: list, force_recrawl_map: dict = None,
                      keyword: str = ""):
        """后台爬取线程"""
        import time as _time
        from playwright.sync_api import sync_playwright
        from auth import ensure_login, get_login_state_path
        from crawler import crawl_user_answers

        # 重定向 stdout
        original_stdout = sys.stdout
        sys.stdout = LogRedirector(lambda m: self.root.after(0, self._log, m, 'info'))

        try:
            with sync_playwright() as p:
                launch_kwargs = {
                    'headless': self._cfg.headless,
                    'args': [
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--start-maximized',
                    ]
                }
                if self._cfg.chrome_exe:
                    launch_kwargs['executable_path'] = self._cfg.chrome_exe

                browser = p.chromium.launch(**launch_kwargs)

                state_path = get_login_state_path(self._cfg.browser_data_dir)
                context_kwargs = {
                    'viewport': {'width': 1920, 'height': 1080},
                    'device_scale_factor': 2,
                    'user_agent': (
                        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/120.0.0.0 Safari/537.36'
                    ),
                    'locale': 'zh-CN',
                }

                if state_path.exists():
                    try:
                        context = browser.new_context(storage_state=str(state_path), **context_kwargs)
                    except Exception:
                        context = browser.new_context(**context_kwargs)
                else:
                    context = browser.new_context(**context_kwargs)

                page = context.new_page()

                # 登录
                self.root.after(0, lambda: self._progress_label.config(text="正在登录..."))
                try:
                    logged_in = ensure_login(page, self._cfg)
                    if not logged_in:
                        raise RuntimeError("登录失败")
                    self.root.after(0, lambda: self._login_status.config(
                        text="🟢 已登录", foreground="#4ec9b0"))
                except Exception as e:
                    self.root.after(0, self._log, f"登录失败: {e}", 'error')
                    browser.close()
                    return

                # 爬取
                results = []
                force_recrawl_map = force_recrawl_map or {}
                for i, uid in enumerate(user_ids):
                    if self._stop_flag:
                        self._log("⚠ 用户手动停止", 'warn')
                        break
                    self.root.after(0, lambda u=uid: self._progress_label.config(
                        text=f"正在爬取: {u}"))
                    try:
                        force_ids = force_recrawl_map.get(uid, set())
                        r = crawl_user_answers(page, uid, force_recrawl_ids=force_ids,
                                              stop_event=self._stop_event,
                                              keyword=keyword)
                        results.append(r)
                        # 记录爬取历史
                        from id_manager import get_id_manager
                        mgr = get_id_manager()
                        mgr.add_crawl_record(
                            uid,
                            r.get('success', 0),
                            r.get('output_dir', str(Path(self._cfg.output_dir) / uid))
                        )
                    except Exception as e:
                        self.root.after(0, self._log, f"爬取 {uid} 失败: {e}", 'error')
                        import traceback
                        traceback.print_exc()
                    # 更新进度条
                    pct = (i + 1) / len(user_ids) * 100
                    self.root.after(0, lambda v=pct: self._progress.configure(value=v))

                # 汇总
                total_success = sum(r.get('success', 0) for r in results)
                total_failed = sum(r.get('failed', 0) for r in results)
                total_skip = sum(r.get('skipped', 0) for r in results)
                self._log(f"\n{'='*55}", 'dim')
                self._log("全部完成！汇总：", 'success')
                self._log(f"   爬取成功: {total_success} 条", 'success')
                self._log(f"   爬取失败: {total_failed} 条", 'error')
                self._log(f"   跳过重复: {total_skip} 条", 'info')
                self._log(f"   输出目录: {Path(self._cfg.output_dir).resolve()}", 'info')

                # 刷新 ID 列表的爬取历史显示
                self.root.after(0, self._refresh_user_list)

                browser.close()

        except Exception as e:
            self.root.after(0, self._log, f"\n致命错误: {e}", 'error')
            import traceback
            self.root.after(0, self._log, traceback.format_exc(), 'error')
        finally:
            sys.stdout = original_stdout
            self._running = False
            self.root.after(0, self._on_crawl_done)

    def _on_crawl_done(self):
        """爬取完成后的 UI 恢复"""
        self._start_btn.config(state=tk.NORMAL)
        self._stop_btn.config(state=tk.DISABLED)
        self._progress['value'] = 100
        self._progress_label.config(text="完成")
        self._running = False

    def _on_close(self):
        if self._running:
            if not messagebox.askokcancel("退出", "爬取正在进行中，确定退出吗？"):
                return
            self._stop_flag = True
            self._stop_event.set()
        self.root.destroy()


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    root = tk.Tk()
    app = ZhihuCrawlerGUI(root)
    root.mainloop()
