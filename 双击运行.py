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

# 将 src/ 加入 sys.path，所有模块已移至 src/ 子目录
_src_dir = Path(__file__).parent / "src"
if str(_src_dir) not in sys.path:
    sys.path.insert(0, str(_src_dir))

# 初始化日志（文件滚动覆盖，2MB×3）
from log_setup import init_logging
init_logging()

# 延迟导入爬虫模块
from config import config, Config
from utils import extract_user_id
from auth import get_cookie_path, get_login_state_path, load_manual_cookies
from id_manager import get_id_manager
from keyword_manager import keyword_mgr
from user_manager import user_mgr


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
# ToolTip — 鼠标悬停提示
# ══════════════════════════════════════════════════════════

class ToolTip:
    """鼠标悬停时弹出提示标签（400ms 延迟）"""
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip_window = None
        self._enter_id = None
        widget.bind('<Enter>', self._schedule)
        widget.bind('<Leave>', self._hide)
        widget.bind('<Button-1>', self._hide)

    def _schedule(self, event=None):
        self._hide()
        self._enter_id = self.widget.after(400, self._show)

    def _show(self):
        self._enter_id = None
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        ttk.Label(tw, text=self.text, justify='left',
                  background="#ffffe0", relief='solid', borderwidth=1,
                  font=("", 9), padding=(5, 3), wraplength=360).pack()

    def _hide(self, event=None):
        if self._enter_id:
            self.widget.after_cancel(self._enter_id)
            self._enter_id = None
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None


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
        self._resume_state = None  # 断点续传状态: {user_ids, keyword, force_recrawl_map}
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

        # 左侧面板（Canvas + Scrollbar 支持滚动，内容多时不会挤出可视区）
        left_outer = ttk.Frame(main_frame)
        left_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))
        left_outer.grid_rowconfigure(0, weight=1)
        left_outer.grid_columnconfigure(0, weight=1)

        left_canvas = tk.Canvas(left_outer, highlightthickness=0)
        left_scroll = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=left_canvas.yview)
        left_frame = ttk.Frame(left_canvas)

        left_canvas.grid(row=0, column=0, sticky="nsew")
        left_scroll.grid(row=0, column=1, sticky="ns")

        left_frame.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_win_id = left_canvas.create_window((0, 0), window=left_frame, anchor=tk.NW)
        left_canvas.configure(yscrollcommand=left_scroll.set)

        # 同步 Canvas 宽度 → 内部 Frame 宽度
        def _on_left_canvas_resize(event):
            left_canvas.itemconfig(left_win_id, width=event.width)
        left_canvas.bind("<Configure>", _on_left_canvas_resize)

        # 鼠标滚轮：鼠标悬停在左侧面板时滚轮滚动
        def _on_left_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _bind_left_wheel(event):
            left_canvas.bind_all("<MouseWheel>", _on_left_mousewheel)
        def _unbind_left_wheel(event):
            left_canvas.unbind_all("<MouseWheel>")
        left_canvas.bind("<Enter>", _bind_left_wheel)
        left_canvas.bind("<Leave>", _unbind_left_wheel)

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
        ToolTip(self._quick_add_entry, "输入知乎用户主页链接或用户ID\n支持格式: https://www.zhihu.com/people/xxx\n或纯用户ID (如: navisli)")
        b = ttk.Button(add_row, text="➕ 添加到列表",
                   command=self._add_user_from_entry, width=12)
        b.pack(side=tk.LEFT, padx=(4, 0))
        ToolTip(b, "将上方输入的用户添加到下方列表\n输入姓名或ID后点击即可加入")

        # 用户列表（Treeview，固定列宽 + 左对齐）
        list_frame = ttk.Frame(user_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        self._user_tree = ttk.Treeview(list_frame,
                                        columns=("nickname", "user_id", "recent"),
                                        show="headings", height=6, selectmode=tk.EXTENDED)
        self._user_tree.heading("nickname", text="昵称")
        self._user_tree.heading("user_id", text="用户ID")
        self._user_tree.heading("recent", text="最近爬取")
        self._user_tree.column("nickname", width=100, anchor=tk.W, minwidth=60)
        self._user_tree.column("user_id", width=140, anchor=tk.W, minwidth=80)
        self._user_tree.column("recent", width=180, anchor=tk.W, minwidth=100)
        self._user_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                   command=self._user_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._user_tree.configure(yscrollcommand=scrollbar.set)
        self._user_tree.bind('<Double-1>', self._edit_nickname)

        # 列表操作按钮
        btn_row2 = ttk.Frame(user_frame)
        btn_row2.pack(fill=tk.X, pady=(4, 0))
        b = ttk.Button(btn_row2, text="全选", command=self._select_all_users, width=6)
        b.pack(side=tk.LEFT, padx=(0, 4))
        ToolTip(b, "选中列表中所有用户\n爬取时只会处理选中的用户")
        b = ttk.Button(btn_row2, text="全不选", command=self._deselect_all_users, width=6)
        b.pack(side=tk.LEFT, padx=(0, 4))
        ToolTip(b, "取消所有用户的选中状态")
        b = ttk.Button(btn_row2, text="✏ 编辑昵称", command=self._edit_nickname, width=10)
        b.pack(side=tk.LEFT, padx=(0, 4))
        ToolTip(b, "修改选中用户的显示昵称\n双击列表项也可编辑")
        b = ttk.Button(btn_row2, text="📋 复制链接", command=self._copy_user_link, width=10)
        b.pack(side=tk.LEFT, padx=(0, 4))
        ToolTip(b, "复制选中用户的知乎主页链接到剪贴板")
        b = ttk.Button(btn_row2, text="🗑 删除", command=self._delete_selected_users, width=8)
        b.pack(side=tk.LEFT, padx=(0, 4))
        ToolTip(b, "从列表中删除选中的用户")
        b = ttk.Button(btn_row2, text="🔄 刷新列表", command=self._refresh_user_list, width=10)
        b.pack(side=tk.LEFT)
        ToolTip(b, "刷新用户列表，显示最新的爬取历史")
        self._list_status = ttk.Label(btn_row2, text="", foreground="gray")
        self._list_status.pack(side=tk.RIGHT)

        # 初始化 ID 列表
        self._id_mgr = None
        self._refresh_user_list()

        # ── 用户分组管理 ──
        ug_row = ttk.Frame(user_frame)
        ug_row.pack(fill=tk.X, pady=4)
        ttk.Label(ug_row, text="👥 用户分组:", font=("", 8)).pack(side=tk.LEFT, padx=(0, 4))
        self._user_group_var = tk.StringVar()
        self._user_group_combo = ttk.Combobox(ug_row, textvariable=self._user_group_var,
                                              width=14, state="readonly")
        self._user_group_combo.pack(side=tk.LEFT, padx=2)
        self._user_group_combo.bind("<<ComboboxSelected>>", self._on_user_group_selected)
        ToolTip(self._user_group_combo, "选择预设的用户组，可一键批量选中\n先在下方管理分组中创建分组")
        b = ttk.Button(ug_row, text="应用分组", command=self._apply_user_group, width=8)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "将选中的用户分组应用到列表\n可选择替换或合并到已有列表")
        b = ttk.Button(ug_row, text="保存为分组…", command=self._save_as_user_group, width=10)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "将当前列表中选中的用户保存为一个分组\n方便下次一键加载")
        b = ttk.Button(ug_row, text="管理分组…", command=self._manage_user_groups, width=9)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "查看/编辑/删除已有的用户分组")
        b = ttk.Button(ug_row, text="导入文件…", command=self._import_users_file, width=9)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "从 .txt 文件批量导入用户\n每行格式: user_id 昵称（空格分隔）\n#开头表示注释")
        # 最近使用
        ttk.Label(ug_row, text="🕐", font=("", 8)).pack(side=tk.LEFT, padx=(8, 2))
        self._user_recent_var = tk.StringVar()
        self._user_recent_combo = ttk.Combobox(ug_row, textvariable=self._user_recent_var,
                                               width=26, state="readonly")
        self._user_recent_combo.pack(side=tk.LEFT, padx=2)
        self._user_recent_combo.bind("<<ComboboxSelected>>", self._on_recent_users_selected)
        ToolTip(self._user_recent_combo, "最近使用过的用户组合\n点击即可快速恢复之前抓取过的用户列表")

        # ── Cookie ──
        cookie_frame = ttk.LabelFrame(left_frame, text="登录 Cookie", padding=6)
        cookie_frame.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(cookie_frame, text="z_c0:  (知乎关键登录 Cookie)").pack(anchor=tk.W)
        self._z_c0_entry = ttk.Entry(cookie_frame, width=50)
        self._z_c0_entry.pack(fill=tk.X, pady=(2, 0))
        self._z_c0_entry.bind('<FocusIn>', self._on_cookie_focus_in)
        self._z_c0_entry.bind('<FocusOut>', self._on_cookie_focus_out)
        ToolTip(self._z_c0_entry, "填入知乎 z_c0 Cookie 值\n登录知乎后从浏览器 F12 → Application → Cookies\n复制 z_c0 的 Value 粘贴到这里\n点击输入框可查看完整值，离开自动隐藏")

        btn_row = ttk.Frame(cookie_frame)
        btn_row.pack(fill=tk.X, pady=(4, 0))
        b = ttk.Button(btn_row, text="保存 Cookie", command=self._save_cookies)
        b.pack(side=tk.LEFT, padx=(0, 6))
        ToolTip(b, "将 Cookie 保存到 browser_data/cookies.json\n下次启动自动加载")
        b = ttk.Button(btn_row, text="检测登录状态", command=self._check_cookie)
        b.pack(side=tk.LEFT)
        ToolTip(b, "检测当前是否有有效的登录状态\n🟢已登录 / 🟡有Cookie待验证 / 🔴未登录")
        b = ttk.Button(btn_row, text="清除 Cookie", command=self._clear_cookies)
        b.pack(side=tk.LEFT, padx=(6, 0))
        ToolTip(b, "清除所有登录状态\n包括 storage_state 和手动 Cookie 文件")

        # ── 爬取设置 ──
        crawl_frame = ttk.LabelFrame(left_frame, text="爬取设置", padding=6)
        crawl_frame.pack(fill=tk.X, pady=(0, 6))

        row1 = ttk.Frame(crawl_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="最多爬取:", width=10).pack(side=tk.LEFT)
        self._max_entry = ttk.Entry(row1, width=8)
        self._max_entry.insert(0, "0")
        self._max_entry.pack(side=tk.LEFT)
        ToolTip(self._max_entry, "每个用户最多爬取的回答条数\n0 = 不限制（爬取全部可见回答）\n测试模式开启时自动设为3条")
        ttk.Label(row1, text="（0=全部）", foreground="gray").pack(side=tk.LEFT, padx=4)
        # 关键词搜索
        ttk.Label(row1, text="🔍 关键词:", foreground="#4ec9b0").pack(side=tk.LEFT, padx=(16, 0))
        self._keyword_entry = ttk.Entry(row1, width=20)
        self._keyword_entry.pack(side=tk.LEFT, padx=2)
        self._keyword_entry.bind('<Return>', self._on_keyword_enter)
        ToolTip(self._keyword_entry, "多关键词用逗号、顿号、空格、分号分隔\n如: AI, 人工智能、NLP\n标题或内容含任一关键词即保存\n不区分大小写，answer_id 自动去重\n回车确认并记录到最近使用")
        ttk.Label(row1, text="（多关键词用逗号分隔，标题/内容含任一即抓取，answer_id 自动去重）", foreground="gray",
                  font=("", 8)).pack(side=tk.LEFT)

        # ── 关键词分组管理 ──
        row_kw = ttk.Frame(crawl_frame)
        row_kw.pack(fill=tk.X, pady=2)
        ttk.Label(row_kw, text="📂 分组:", foreground="#9cdcfe", font=("", 8)).pack(side=tk.LEFT, padx=(0, 4))
        self._group_var = tk.StringVar()
        self._group_combo = ttk.Combobox(row_kw, textvariable=self._group_var,
                                         width=14, state="readonly")
        self._group_combo.pack(side=tk.LEFT, padx=2)
        self._group_combo.bind("<<ComboboxSelected>>", self._on_group_selected)
        ToolTip(self._group_combo, "选择预设的关键词组，可一键填入\n先在下方管理分组中创建关键词组")
        b = ttk.Button(row_kw, text="应用分组", command=self._apply_group, width=8)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "将选中分组的关键词填入上方输入框\n如已有内容则替换")
        b = ttk.Button(row_kw, text="保存为分组…", command=self._save_as_group, width=10)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "将当前输入框的关键词保存为一个分组\n方便下次一键加载")
        b = ttk.Button(row_kw, text="管理分组…", command=self._manage_groups, width=9)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "查看/编辑/删除已有的关键词分组")
        b = ttk.Button(row_kw, text="导入文件…", command=self._import_keywords_file, width=9)
        b.pack(side=tk.LEFT, padx=2)
        ToolTip(b, "从 .txt 或 .csv 文件批量导入关键词\n支持逗号/换行分隔\n不区分大小写")

        # ── 最近使用关键词 ──
        row_recent = ttk.Frame(crawl_frame)
        row_recent.pack(fill=tk.X, pady=1)
        ttk.Label(row_recent, text="🕐 最近:", foreground="#9cdcfe", font=("", 8)).pack(side=tk.LEFT, padx=(0, 4))
        self._recent_var = tk.StringVar()
        self._recent_combo = ttk.Combobox(row_recent, textvariable=self._recent_var,
                                          width=50, state="readonly")
        self._recent_combo.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        self._recent_combo.bind("<<ComboboxSelected>>", self._on_recent_selected)
        ToolTip(self._recent_combo, "最近使用过的关键词\n点击即可快速填入输入框")

        row2 = ttk.Frame(crawl_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="输出目录:", width=10).pack(side=tk.LEFT)
        self._output_entry = ttk.Entry(row2)
        self._output_entry.insert(0, "output")
        self._output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ToolTip(self._output_entry, "爬取结果的保存目录\n默认为程序文件夹下的 output/\n每个用户会创建独立子文件夹")

        row3 = ttk.Frame(crawl_frame)
        row3.pack(fill=tk.X, pady=2)
        ttk.Label(row3, text="Chrome 路径:", width=10).pack(side=tk.LEFT)
        self._chrome_entry = ttk.Entry(row3)
        self._chrome_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ToolTip(self._chrome_entry, "Chrome 浏览器的可执行文件路径\n留空则使用 Playwright 内置的 Chromium")

        row4 = ttk.Frame(crawl_frame)
        row4.pack(fill=tk.X, pady=2)
        self._headless_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(row4, text="无头模式（后台运行）", variable=self._headless_var)
        cb.pack(side=tk.LEFT)
        ToolTip(cb, "不显示浏览器窗口，静默后台爬取\n节省资源但无法观察爬取过程\n首次使用建议关闭以确认登录正常")
        self._test_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(row4, text="🧪 测试模式（只爬3条）", variable=self._test_var,
                        command=self._on_test_toggle)
        cb.pack(side=tk.LEFT, padx=10)
        ToolTip(cb, "开启后只爬取最多3条回答\n有关键词时：5条/批筛选→标题≥3条即停\n适合测试 Cookie 和配置是否正常")
        self._forensic_var = tk.BooleanVar(value=True)
        cb = ttk.Checkbutton(row4, text="🔒 法务证据（HTML+证据报告+SHA256）", variable=self._forensic_var)
        cb.pack(side=tk.LEFT, padx=10)
        ToolTip(cb, "保存完整的 HTML 页面 + SHA256 校验文件\n作为法律取证证据\n会增加存储空间和爬取时间")

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
        ToolTip(self._scroll_min, "页面滚动间隔最小值（秒）\n程序会在最小~最大之间随机取值\n模拟人类浏览行为，防反爬")
        ttk.Label(row_d1, text="-").pack(side=tk.LEFT)
        self._scroll_max = ttk.Entry(row_d1, width=4)
        self._scroll_max.insert(0, "5")
        self._scroll_max.pack(side=tk.LEFT)
        ToolTip(self._scroll_max, "页面滚动间隔最大值（秒）")

        # 翻页间隔
        ttk.Label(row_d1, text="  翻页:").pack(side=tk.LEFT)
        self._page_min = ttk.Entry(row_d1, width=4)
        self._page_min.insert(0, "3")
        self._page_min.pack(side=tk.LEFT)
        ToolTip(self._page_min, "翻页间隔最小值（秒）\n知乎每页约显示20条\n翻页过快容易触发反爬")
        ttk.Label(row_d1, text="-").pack(side=tk.LEFT)
        self._page_max = ttk.Entry(row_d1, width=4)
        self._page_max.insert(0, "8")
        self._page_max.pack(side=tk.LEFT)
        ToolTip(self._page_max, "翻页间隔最大值（秒）")

        # 缓存有效期
        ttk.Label(row_d1, text="  缓存(分):").pack(side=tk.LEFT)
        self._cache_ttl = ttk.Entry(row_d1, width=4)
        self._cache_ttl.insert(0, "30")
        self._cache_ttl.pack(side=tk.LEFT)
        ToolTip(self._cache_ttl, "页面缓存有效期（分钟）\n有效期内不会重复请求同一页面\n0 = 禁用缓存，每次都重新请求")
        ttk.Label(row_d1, text="(0=禁用)", foreground="gray").pack(side=tk.LEFT, padx=2)

        row_d4 = ttk.Frame(delay_frame)
        row_d4.pack(fill=tk.X, pady=(4, 2))
        self._force_no_cache_var = tk.BooleanVar(value=False)
        cb = ttk.Checkbutton(row_d4, text="🔄 强制忽略缓存（测试用：跳过所有缓存+进度，从头重爬）",
                        variable=self._force_no_cache_var)
        cb.pack(side=tk.LEFT)
        ToolTip(cb, "忽略所有缓存和爬取进度\n每次从头重新爬取\n适合测试期间使用，正常使用不建议勾选")

        # ── 操作控制 ──
        action_frame = ttk.LabelFrame(left_frame, text="操作", padding=6)
        action_frame.pack(fill=tk.X, pady=(8, 0))

        self._progress = ttk.Progressbar(action_frame, mode='determinate')
        self._progress.pack(fill=tk.X, pady=(0, 4))
        ToolTip(self._progress, "爬取进度条：每个用户完成后前进一格")

        ctrl_row = ttk.Frame(action_frame)
        ctrl_row.pack(fill=tk.X)
        self._progress_label = ttk.Label(ctrl_row, text="就绪", foreground="gray")
        self._progress_label.pack(side=tk.LEFT)
        self._stop_btn = ttk.Button(ctrl_row, text="■ 停止",
                                     command=self._stop_crawl, state=tk.DISABLED, width=10)
        self._stop_btn.pack(side=tk.RIGHT, padx=(4, 0))
        ToolTip(self._stop_btn, "停止正在进行的爬取任务\n已爬取的数据不会丢失\n请等待当前页面处理完成")
        self._resume_btn = ttk.Button(ctrl_row, text="▶ 继续",
                                       command=self._resume_crawl, state=tk.DISABLED, width=8)
        self._resume_btn.pack(side=tk.RIGHT, padx=(4, 0))
        ToolTip(self._resume_btn, "从上次停止位置断点续传\n继续爬取未完成的用户")
        self._start_btn = ttk.Button(ctrl_row, text="▶ 开始爬取",
                                      command=self._start_crawl, width=14)
        self._start_btn.pack(side=tk.RIGHT)
        ToolTip(self._start_btn, "开始爬取选中用户的回答\n⚠ 请先在「目标用户」列表中选中用户\n如未选中用户会有提示")

        # ── 手动链接保存（独立功能区）──
        ttk.Separator(left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 2))
        ttk.Label(left_frame, text="📋 独立功能：手动粘贴链接保存",
                  font=("", 9, "bold"), foreground="#888").pack(anchor=tk.W, pady=(0, 2))

        manual_frame = ttk.LabelFrame(left_frame, text="🔗 手动链接保存为 MD（无需预配置用户ID · 每行一条 · 上限100条）", padding=6)
        manual_frame.pack(fill=tk.X, pady=(0, 6))

        self._manual_text = tk.Text(manual_frame, height=4, font=("Consolas", 9),
                                     wrap=tk.WORD, fg="gray")
        self._manual_text.insert("1.0", "粘贴知乎回答链接（支持App复制格式：文字+链接）...")
        self._manual_text.bind('<FocusIn>', self._on_manual_focus_in)
        self._manual_text.bind('<FocusOut>', self._on_manual_focus_out)
        self._manual_text.pack(fill=tk.X)
        ToolTip(self._manual_text, "粘贴知乎回答链接，每行一条\n支持 App 复制格式（含文字描述）\n上限 100 条，超出可用导入文件功能分批处理\n格式: https://www.zhihu.com/question/xxx/answer/xxx")

        manual_btn_row = ttk.Frame(manual_frame)
        manual_btn_row.pack(fill=tk.X, pady=(4, 0))
        self._manual_btn = ttk.Button(manual_btn_row, text="📝 批量保存为 MD",
                                      command=self._save_manual_urls, width=16)
        self._manual_btn.pack(side=tk.LEFT)
        ToolTip(self._manual_btn, "将上方粘贴的知乎回答链接逐条抓取\n保存为 Markdown 文件\n无需预配置目标用户\n最多同时处理 100 条，超量自动分批")
        self._import_btn = ttk.Button(manual_btn_row, text="📂 导入文件",
                                      command=self._import_link_file, width=12)
        self._import_btn.pack(side=tk.RIGHT)
        ToolTip(self._import_btn, "从 .txt/.csv 文件导入链接\n自动提取知乎链接并去重\n超 100 条自动分批显示")
        ttk.Label(manual_btn_row, text="支持: https://www.zhihu.com/question/xxx/answer/xxx",
                  foreground="gray", font=("", 8)).pack(side=tk.LEFT, padx=8)

        # 分割大MD工具（左侧独立功能区）
        ttk.Separator(left_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=(12, 2))
        split_row = ttk.Frame(left_frame)
        split_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(split_row, text="🔧 工具",
                  font=("", 9, "bold"), foreground="#888").pack(side=tk.LEFT)
        b = ttk.Button(split_row, text="✂ 分割大MD（>10MB按标题拆分）",
                   command=self._split_large_md_file, width=28)
        b.pack(side=tk.LEFT, padx=(8, 0))
        ToolTip(b, "扫描 output 目录中超过 10MB 的 Markdown 文件\n按二级标题（## ）自动分割为小文件\n分块命名: 原文件名(1).md / (2).md ...\n分割后原大文件会被删除")

        # 右侧面板: 日志（缩小宽度，给左侧功能区更多空间）
        right_frame = ttk.Frame(main_frame, width=320)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=False, padx=(4, 0))

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

        # 关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── 日志方法 ────────────────────────────────────────

    def _log(self, msg: str, tag: str = 'info'):
        self._log_area.configure(state=tk.NORMAL)
        self._log_area.insert(tk.END, msg + '\n', tag)
        self._log_area.see(tk.END)
        self._log_area.configure(state=tk.DISABLED)

    # ── ID 列表管理 ─────────────────────────────────────

    def _get_id_manager(self):
        """获取 ID 管理器单例"""
        if self._id_mgr is None:
            self._id_mgr = get_id_manager()
        return self._id_mgr

    def _refresh_user_list(self):
        """刷新用户列表显示（Treeview 三列：昵称 / 用户ID / 最近爬取）"""
        for item in self._user_tree.get_children():
            self._user_tree.delete(item)
        mgr = self._get_id_manager()
        for nickname, user_id, url in mgr.get_display_list():
            history = mgr.get_crawl_history(user_id)
            hist_info = ""
            if history:
                last = history[-1]
                hist_info = f"{last['date'][:16]}, {last['answers_scraped']}条"
            self._user_tree.insert("", tk.END, iid=user_id,
                                    values=(nickname, user_id, hist_info))
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
            if self._user_tree.exists(user_id):
                self._user_tree.selection_add(user_id)
                self._user_tree.see(user_id)
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

    MAX_MANUAL_URLS = 100
    BATCH_SIZE = 100

    def _import_link_file(self):
        """导入链接文件（.txt / .csv），支持超大批次自动分批"""
        fp = filedialog.askopenfilename(
            title="选择链接文件",
            filetypes=[("文本文件", "*.txt"), ("CSV 文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not fp:
            return

        try:
            with open(fp, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(fp, 'r', encoding='gbk') as f:
                content = f.read()

        # 用正则提取知乎链接
        urls = re.findall(r'https?://[^\s]*zhihu\.com[^\s]*', content)
        seen = set()
        urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            messagebox.showwarning("提示", f"文件中未找到知乎链接\n文件: {Path(fp).name}")
            return

        # 存储导入的链接（内部全量）
        self._imported_urls = urls

        batches = (len(urls) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
        if len(urls) <= self.MAX_MANUAL_URLS:
            # 少量链接：直接填入文本框
            self._manual_text.delete("1.0", tk.END)
            self._manual_text.insert("1.0", "\n".join(urls))
            self._manual_text.config(fg="black")
            msg = f"已导入 {len(urls)} 条链接"
        else:
            # 大量链接：文本框显示摘要
            self._manual_text.delete("1.0", tk.END)
            self._manual_text.insert(
                "1.0",
                f"已导入 {len(urls)} 条链接（将自动分 {batches} 批处理，每批 {self.BATCH_SIZE} 条）\n"
                f"来源: {Path(fp).name}"
            )
            self._manual_text.config(fg="black")
            msg = f"已导入 {len(urls)} 条链接（分 {batches} 批）"
        self._log(msg, 'info')

    def _save_manual_urls(self, event=None):
        """批量手动链接 → 保存为 MD（含导入文件分批支持）"""
        # 优先使用导入的链接列表
        if getattr(self, '_imported_urls', None):
            urls = self._imported_urls
        else:
            raw = self._manual_text.get("1.0", tk.END).strip()
            if not raw or raw == "粘贴知乎回答链接（支持App复制格式：文字+链接）..." or raw.startswith("已导入"):
                messagebox.showwarning("提示", "请先粘贴知乎回答链接或导入链接文件")
                return

            urls = re.findall(r'https?://[^\s]*zhihu\.com[^\s]*', raw)
            seen = set()
            urls = [u for u in urls if not (u in seen or seen.add(u))]

        if not urls:
            messagebox.showwarning("提示", "请粘贴知乎回答链接（含 zhihu.com）或导入链接文件")
            return

        # 手动粘贴时限制上限 100
        if not getattr(self, '_imported_urls', None) and len(urls) > self.MAX_MANUAL_URLS:
            urls = urls[:self.MAX_MANUAL_URLS]
            self._manual_text.delete("1.0", tk.END)
            self._manual_text.insert("1.0", "\n".join(urls))

        # 超大批次确认
        if len(urls) > self.BATCH_SIZE:
            batches = (len(urls) + self.BATCH_SIZE - 1) // self.BATCH_SIZE
            ok = messagebox.askyesno(
                "分批处理确认",
                f"共 {len(urls)} 条链接，超过单批上限（{self.BATCH_SIZE}条）。\n\n"
                f"将自动分 {batches} 批处理，是否继续？"
            )
            if not ok:
                return

        if self._running:
            messagebox.showwarning("提示", "已有爬取任务在运行中，请等待完成")
            return

        keyword = self._keyword_entry.get().strip()

        # ── 确认弹窗 ──
        from crawler import _split_keywords
        kws = _split_keywords(keyword)
        filter_info = f"🔍 关键词筛选：{len(kws)}个 → {', '.join(kws[:8])}"
        if len(kws) > 8:
            filter_info += f" …(共{len(kws)}个)"
        if not keyword:
            filter_info = "⚠ 未设置关键词，将保存全部链接（无筛选）"
        confirm_msg = (
            f"确认批量保存？\n\n"
            f"🔗 链接数: {len(urls)}条\n"
            f"{filter_info}"
        )
        if not messagebox.askyesno("确认批量保存", confirm_msg, parent=self.root):
            self._log("⚠ 用户取消批量保存", "warn")
            return

        if keyword:
            keyword_mgr.add_recent(keyword)
            self._refresh_keyword_ui()

        self._running = True
        self._start_btn.config(state=tk.DISABLED)
        self._resume_btn.config(state=tk.DISABLED)
        self._manual_btn.config(state=tk.DISABLED)
        self._import_btn.config(state=tk.DISABLED)
        self._progress_label.config(text=f"正在批量保存 ({len(urls)}条)...")

        threading.Thread(
            target=self._manual_urls_thread,
            args=(urls, keyword),
            daemon=True
        ).start()

    def _manual_urls_thread(self, urls: list, keyword: str = ""):
        """后台线程：批量保存多条链接为 MD（支持分批进度）"""
        import time as _time
        from playwright.sync_api import sync_playwright
        from auth import ensure_login, get_login_state_path
        from crawler import crawl_single_url

        original_stdout = sys.stdout
        sys.stdout = LogRedirector(lambda m: self.root.after(0, self._log, m, 'info'))

        success_count = 0
        fail_count = 0
        skip_count = 0
        total = len(urls)

        try:
            self._log(f"\n🔗 批量保存 {total} 条链接", 'info')
            if keyword:
                self._log(f"🔍 关键词过滤: 「{keyword}」", 'info')

            batch_count = (total + self.BATCH_SIZE - 1) // self.BATCH_SIZE
            if batch_count > 1:
                self._log(f"📦 自动分批: 共 {batch_count} 批，每批 {self.BATCH_SIZE} 条", 'info')

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

                for batch_idx in range(batch_count):
                    start = batch_idx * self.BATCH_SIZE
                    end = min(start + self.BATCH_SIZE, total)
                    batch_urls = urls[start:end]

                    if batch_count > 1:
                        self._log(f"\n── 📦 批次 {batch_idx + 1}/{batch_count} ({start + 1}-{end}) ──", 'info')

                    for i, url in enumerate(batch_urls):
                        idx = start + i + 1
                        self._log(f"  [{idx}/{total}] {url[:80]}...", 'info')
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
            self._imported_urls = None  # 清除导入缓存
            summary = f"✅ {success_count} 成功"
            if skip_count:
                summary += f" / ⊘ {skip_count} 跳过（不含关键词）"
            if fail_count:
                summary += f" / ❌ {fail_count} 失败"
            self._log(f"\n📊 批量保存完成: {summary}", 'info')
            self.root.after(0, lambda: messagebox.showinfo("批量保存完成",
                f"批量保存 {total} 条完成:\n{summary}"))
            self.root.after(0, self._on_manual_done)

    def _on_manual_done(self):
        """手动保存完成后恢复 UI"""
        self._start_btn.config(state=tk.NORMAL)
        self._manual_btn.config(state=tk.NORMAL)
        self._import_btn.config(state=tk.NORMAL)
        self._resume_btn.config(state=tk.DISABLED)
        self._progress_label.config(text="就绪")
        # 自动刷新已抓取用户列表
        self._refresh_crawled_users_report(silent=True)

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
        for item in self._user_tree.get_children():
            self._user_tree.selection_add(item)

    def _deselect_all_users(self):
        for item in self._user_tree.get_children():
            self._user_tree.selection_remove(item)

    def _delete_selected_users(self):
        selected = self._user_tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("确认", f"确定删除选中的 {len(selected)} 个用户吗？"):
            return
        mgr = self._get_id_manager()
        for iid in selected:
            mgr.remove(iid)  # iid 就是 user_id
        self._refresh_user_list()
        self._log(f"🗑 已删除 {len(selected)} 个用户", 'info')

    def _copy_user_link(self):
        """复制选中用户的页面链接到剪贴板"""
        selected = self._user_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先在列表中选中一个用户")
            return
        user_id = selected[0]  # iid 就是 user_id
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
        selected = self._user_tree.selection()
        if not selected:
            return
        user_id = selected[0]  # iid 就是 user_id
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
        """显示 Cookie 首尾部分便于识别"""
        if not val:
            return "(未设置)"
        if len(val) <= 12:
            return val[:4] + "****"
        return f"Cookie({len(val)}字) 前:{val[:15]} / 后:{val[-15:]}"

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
        self._refresh_keyword_ui()  # 刷新关键词分组/最近使用下拉框
        self._refresh_user_group_ui()  # 刷新用户分组下拉框

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
        self._cfg.force_no_cache = self._force_no_cache_var.get()
        # 法务模式自动开启 save_html（后台属性，不在 UI 显示）
        if self._cfg.forensic_mode:
            self._cfg.save_html = True

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

    # ── 用户分组管理 ────────────────────────────────────

    def _refresh_user_group_ui(self):
        """刷新用户分组下拉框和最近使用下拉框"""
        groups = user_mgr.group_names()
        self._user_group_combo["values"] = groups if groups else ["（暂无分组）"]
        recent = user_mgr.recent
        self._user_recent_combo["values"] = recent if recent else ["（暂无记录）"]

    def _on_user_group_selected(self, event=None):
        """选中用户分组时预览用户列表（不自动应用）"""
        name = self._user_group_var.get()
        if not name or name == "（暂无分组）":
            return
        g = user_mgr.get_group(name)
        if g:
            nicks = [u.get("nickname", u.get("user_id", "?")) for u in g.users[:5]]
            preview = ", ".join(nicks)
            if len(g.users) > 5:
                preview += f" …(共{len(g.users)}人)"
            self._log(f"👥 用户分组「{name}」: {preview}", "info")

    def _apply_user_group(self):
        """将选中分组的用户选中到目标列表（支持替换/合并）"""
        name = self._user_group_var.get()
        if not name or name == "（暂无分组）":
            messagebox.showinfo("提示", "请先选择一个用户分组")
            return
        g = user_mgr.get_group(name)
        if not g or not g.users:
            messagebox.showinfo("提示", f"分组「{name}」为空")
            return

        mgr = self._get_id_manager()

        # 询问替换或合并
        existing = len(self._user_tree.get_children()) > 0
        if existing:
            choice = messagebox.askyesnocancel(
                "应用用户分组",
                f"将分组「{name}」({len(g.users)}人)应用到目标列表：\n\n"
                f"「是」= 替换为分组用户\n"
                f"「否」= 合并（添加分组用户到列表并选中）\n"
                f"「取消」= 不操作"
            )
            if choice is None:
                return
            if choice is False:  # 合并
                # 添加缺失用户到 IDManager
                added = 0
                for u in g.users:
                    uid = u.get("user_id", "")
                    nick = u.get("nickname", uid)
                    url = f"https://www.zhihu.com/people/{uid}"
                    if mgr.add(uid, nickname=nick, url=url):
                        added += 1
                # 选中分组用户
                self._deselect_all_users()
                self._refresh_user_list()
                group_ids = {u.get("user_id", "") for u in g.users}
                for iid in self._user_tree.get_children():
                    if iid in group_ids:
                        self._user_tree.selection_add(iid)
                self._log(f"👥 合并分组「{name}」: 新增 {added} 人，共选中 {len(group_ids)} 人", "info")
                # 记录最近
                display = f"{name}: " + ", ".join(
                    u.get("nickname", uid) for u in g.users[:10])
                if len(g.users) > 10: display += f" …共{len(g.users)}人"
                user_mgr.add_recent(display); self._refresh_user_group_ui()
                return
            # else: 替换，继续下面逻辑

        # 替换模式：清空 IDManager，添加分组用户
        mgr.clear()
        for u in g.users:
            uid = u.get("user_id", "")
            nick = u.get("nickname", uid)
            url = f"https://www.zhihu.com/people/{uid}"
            mgr.add(uid, nickname=nick, url=url)
        self._refresh_user_list()
        # 全选
        self._select_all_users()
        # 记录最近
        display = f"{name}: " + ", ".join(
            u.get("nickname", u.get("user_id", "?")) for u in g.users[:10])
        if len(g.users) > 10: display += f" …共{len(g.users)}人"
        user_mgr.add_recent(display); self._refresh_user_group_ui()
        self._log(f"👥 已应用用户分组「{name}」({len(g.users)}人)", "info")

    def _save_as_user_group(self):
        """将当前选中用户保存为用户分组"""
        selected = self._user_tree.selection()
        mgr = self._get_id_manager()
        if selected:
            # 仅保存选中的
            users = []
            for uid in selected:
                user = mgr.find(uid)
                users.append({"user_id": uid, "nickname": user.nickname if user else uid})
        else:
            # 全部用户
            users = [{"user_id": u.user_id, "nickname": u.nickname}
                     for u in mgr.users]
        if not users:
            messagebox.showwarning("提示", "用户列表为空")
            return

        dialog = tk.Toplevel(self.root)  # Note: uses self.root not self._root
        dialog.title("保存用户分组")
        dialog.geometry("380x180"); dialog.resizable(False, False)
        dialog.transient(self.root); dialog.focus_set()
        ttk.Label(dialog, text="分组名称:").pack(pady=(12, 2))
        name_entry = ttk.Entry(dialog, width=40); name_entry.pack(padx=20, pady=2)
        name_entry.focus_set()
        ttk.Label(dialog, text="备注（可选）:", font=("", 8)).pack(pady=(8, 2))
        note_entry = ttk.Entry(dialog, width=40); note_entry.pack(padx=20, pady=2)

        def do_save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showwarning("提示", "请输入分组名称", parent=dialog)
                return
            is_new = user_mgr.add_group(name, users, note_entry.get().strip())
            self._refresh_user_group_ui(); self._user_group_var.set(name)
            act = "新建" if is_new else "更新（合并用户）"
            self._log(f"💾 {act}用户分组「{name}」({len(users)}人)", "info")
            dialog.destroy()

        ttk.Button(dialog, text="保存", command=do_save).pack(pady=(12, 6))
        dialog.bind("<Return>", lambda e: do_save())

    def _manage_user_groups(self):
        """管理用户分组（查看/编辑/删除）"""
        dialog = tk.Toplevel(self.root)
        dialog.title("管理用户分组"); dialog.geometry("580x460")
        dialog.resizable(True, True)
        dialog.transient(self.root); dialog.focus_set()
        list_frame = ttk.Frame(dialog); list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        ttk.Label(list_frame, text="已有分组（选中后可编辑/删除）:", font=("", 9)).pack(anchor=tk.W)
        lb = tk.Listbox(list_frame, height=8)
        lb.pack(fill=tk.BOTH, expand=True, pady=4)
        for g in user_mgr.groups:
            lb.insert(tk.END, f"{g.name}  ({len(g.users)}人)")
        ttk.Label(list_frame, text="用户列表（每行: user_id nickname）:", font=("", 9)).pack(anchor=tk.W, pady=(8, 0))
        user_text = tk.Text(list_frame, height=8); user_text.pack(fill=tk.BOTH, expand=True, pady=4)
        btn_frame = ttk.Frame(list_frame); btn_frame.pack(fill=tk.X, pady=6)

        def on_select(evt=None):
            sel = lb.curselection()
            if sel:
                g = user_mgr.groups[sel[0]]
                user_text.delete("1.0", tk.END)
                lines = [f"{u.get('user_id', '')} {u.get('nickname', '')}" for u in g.users]
                user_text.insert("1.0", "\n".join(lines))
        lb.bind("<<ListboxSelect>>", on_select)

        def do_update():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = user_mgr.groups[sel[0]]
            raw = user_text.get("1.0", tk.END).strip()
            new_users = []; seen = set()
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"): continue
                parts = line.split(None, 1)
                if parts:
                    uid = parts[0]
                    if uid in seen: continue
                    seen.add(uid)
                    nick = parts[1].strip() if len(parts) > 1 else uid
                    new_users.append({"user_id": uid, "nickname": nick})
            user_mgr.update_group_users(g.name, new_users)
            self._refresh_user_group_ui()
            self._log(f"✏ 已更新用户分组「{g.name}」({len(new_users)}人)", "info")
            messagebox.showinfo("完成", f"分组「{g.name}」已更新", parent=dialog); dialog.destroy()

        def do_delete():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog); return
            g = user_mgr.groups[sel[0]]
            if messagebox.askyesno("确认删除", f"确定要删除用户分组「{g.name}」吗？", parent=dialog):
                user_mgr.delete_group(g.name); self._refresh_user_group_ui()
                self._log(f"🗑 已删除用户分组「{g.name}」", "warn"); dialog.destroy()

        ttk.Button(btn_frame, text="💾 保存修改", command=do_update).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🗑 删除分组", command=do_delete).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)
        if lb.size() > 0: lb.selection_set(0); on_select()

    def _import_users_file(self):
        """从文本文件导入用户列表"""
        filepath = filedialog.askopenfilename(
            title="选择用户列表文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not filepath: return
        try:
            users = user_mgr.import_from_file(filepath)
            if not users:
                messagebox.showwarning("导入结果", "文件中未找到有效用户"); return
            mgr = self._get_id_manager()
            existing = len(self._user_tree.get_children()) > 0
            if existing:
                choice = messagebox.askyesnocancel(
                    "导入用户",
                    f"从 {Path(filepath).name} 导入了 {len(users)} 个用户：\n\n"
                    f"「是」= 替换当前列表\n"
                    f"「否」= 合并到当前列表\n"
                    f"「取消」= 不操作"
                )
                if choice is None: return
                if choice is False:  # 合并
                    added = 0
                    for u in users:
                        if mgr.add(u.get("user_id", ""), nickname=u.get("nickname", ""),
                                    url=f"https://www.zhihu.com/people/{u.get('user_id', '')}"):
                            added += 1
                    self._refresh_user_list()
                    self._log(f"📥 导入合并 {Path(filepath).name}: 新增 {added} 人", "info")
                    return

            # 替换模式
            mgr.clear()
            for u in users:
                mgr.add(u.get("user_id", ""), nickname=u.get("nickname", ""),
                         url=f"https://www.zhihu.com/people/{u.get('user_id', '')}")
            self._refresh_user_list()
            self._select_all_users()
            self._log(f"📥 已导入 {len(users)} 个用户: {Path(filepath).name}", "info")
        except Exception as e:
            messagebox.showerror("导入失败", f"读取文件出错:\n{e}")

    def _on_recent_users_selected(self, event=None):
        """选中最近使用的用户分组时应用"""
        val = self._user_recent_var.get()
        if not val or val == "（暂无记录）": return
        if ": " in val:
            group_name = val.split(": ", 1)[0]
            g = user_mgr.get_group(group_name)
            if g and g.users:
                mgr = self._get_id_manager()
                mgr.clear()
                for u in g.users:
                    uid = u.get("user_id", "")
                    mgr.add(uid, nickname=u.get("nickname", uid),
                             url=f"https://www.zhihu.com/people/{uid}")
                self._refresh_user_list(); self._select_all_users()
                self._user_group_var.set(group_name)
                self._log(f"🕐 已恢复用户分组「{group_name}」({len(g.users)}人)", "info")

    # ── 关键词分组管理 ────────────────────────────────────

    def _refresh_keyword_ui(self):
        """刷新关键词分组下拉框和最近使用下拉框"""
        groups = keyword_mgr.group_names()
        self._group_combo["values"] = groups if groups else ["（暂无分组）"]
        recent = keyword_mgr.recent
        self._recent_combo["values"] = recent if recent else ["（暂无记录）"]

    def _on_group_selected(self, event=None):
        """选中分组时自动填入关键词到输入框"""
        name = self._group_var.get()
        if not name or name == "（暂无分组）":
            return
        g = keyword_mgr.get_group(name)
        if g:
            kw_str = ", ".join(g.keywords)
            self._keyword_entry.delete(0, tk.END)
            self._keyword_entry.insert(0, kw_str)
            self._log(f"✅ 已应用分组「{name}」({len(g.keywords)}个关键词)", "info")

    def _on_keyword_enter(self, event=None):
        """关键词输入框回车：确认关键词并记录到最近使用"""
        kw = self._keyword_entry.get().strip()
        if kw:
            keyword_mgr.add_recent(kw)
            self._refresh_keyword_ui()
            self._log(f"🔍 关键词已确认: {kw}", "info")

    def _apply_group(self):
        """将选中分组的全部关键词填入关键词输入框"""
        name = self._group_var.get()
        if not name or name == "（暂无分组）":
            messagebox.showinfo("提示", "请先选择一个关键词分组")
            return
        g = keyword_mgr.get_group(name)
        if g:
            kw_str = ", ".join(g.keywords)
            self._keyword_entry.delete(0, tk.END)
            self._keyword_entry.insert(0, kw_str)
            self._log(f"✅ 已应用分组「{name}」({len(g.keywords)}个关键词)", "info")

    def _save_as_group(self):
        """将当前输入框的关键词保存为分组（若选中已有分组则预填名称，保存时替换而非合并）"""
        kw_str = self._keyword_entry.get().strip()
        if not kw_str:
            messagebox.showwarning("提示", "请先在关键词输入框中输入关键词")
            return

        # 弹出命名对话框
        dialog = tk.Toplevel(self.root)
        dialog.title("保存关键词分组")
        dialog.geometry("380x180")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.focus_set()

        ttk.Label(dialog, text="分组名称:").pack(pady=(12, 2))
        name_entry = ttk.Entry(dialog, width=40)
        name_entry.pack(padx=20, pady=2)
        # 预填当前选中的分组名（如果有）
        current_name = self._group_var.get()
        if current_name and current_name != "（暂无分组）":
            name_entry.insert(0, current_name)
            name_entry.selection_range(0, tk.END)
        name_entry.focus_set()

        ttk.Label(dialog, text="备注（可选）:", font=("", 8)).pack(pady=(8, 2))
        note_entry = ttk.Entry(dialog, width=40)
        note_entry.pack(padx=20, pady=2)

        def do_save():
            name = name_entry.get().strip()
            if not name:
                messagebox.showwarning("提示", "请输入分组名称", parent=dialog)
                return
            # 用 crawler 的 _split_keywords 保持拆分逻辑一致
            from crawler import _split_keywords
            kws = _split_keywords(kw_str)
            is_new = keyword_mgr.add_group(name, kws, note_entry.get().strip(), replace=True)
            self._refresh_keyword_ui()
            self._group_var.set(name)
            act = "新建" if is_new else "更新（已替换）"
            self._log(f"💾 {act}分组「{name}」({len(kws)}个关键词)", "info")
            dialog.destroy()

        ttk.Button(dialog, text="保存", command=do_save).pack(pady=(12, 6))
        dialog.bind("<Return>", lambda e: do_save())

    def _manage_groups(self):
        """管理关键词分组（查看/编辑/删除）"""
        dialog = tk.Toplevel(self.root)
        dialog.title("管理关键词分组")
        dialog.geometry("520x420")
        dialog.resizable(True, True)
        dialog.transient(self.root)
        dialog.focus_set()

        # 分组列表
        list_frame = ttk.Frame(dialog)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        ttk.Label(list_frame, text="已有分组（选中目标→粘贴关键词→保存）:", font=("", 9)).pack(anchor=tk.W)
        lb = tk.Listbox(list_frame, height=8, exportselection=False)
        lb.pack(fill=tk.BOTH, expand=True, pady=4)
        for g in keyword_mgr.groups:
            lb.insert(tk.END, f"{g.name}  ({len(g.keywords)}个关键词)")

        # 关键词编辑区
        ttk.Label(list_frame, text="关键词（逗号分隔）:", font=("", 9)).pack(anchor=tk.W, pady=(8, 0))
        kw_text = tk.Text(list_frame, height=5)
        kw_text.pack(fill=tk.BOTH, expand=True, pady=4)

        # 按钮
        btn_frame = ttk.Frame(list_frame)
        btn_frame.pack(fill=tk.X, pady=6)

        def load_selected():
            """将选中分组的关键词加载到编辑区"""
            sel = lb.curselection()
            if sel:
                g = keyword_mgr.groups[sel[0]]
                kw_text.delete("1.0", tk.END)
                kw_text.insert("1.0", ", ".join(g.keywords))
            else:
                messagebox.showwarning("提示", "请先在列表中选中一个分组", parent=dialog)

        # 显式"📥 加载"按钮才载入旧关键词，选择分组不会覆盖编辑区

        def do_update():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog)
                return
            g = keyword_mgr.groups[sel[0]]
            from crawler import _split_keywords
            new_kws = _split_keywords(kw_text.get("1.0", tk.END))
            keyword_mgr.update_group_keywords(g.name, new_kws)
            self._refresh_keyword_ui()
            # 同步更新主窗口：填入关键词 + 选中分组
            self._keyword_entry.delete(0, tk.END)
            self._keyword_entry.insert(0, ", ".join(new_kws))
            self._group_var.set(g.name)
            self._log(f"✏ 已更新分组「{g.name}」({len(new_kws)}个关键词)", "info")
            messagebox.showinfo("完成", f"分组「{g.name}」已更新（已自动填入关键词输入框）", parent=dialog)
            dialog.destroy()

        def do_delete():
            sel = lb.curselection()
            if not sel:
                messagebox.showwarning("提示", "请先选择一个分组", parent=dialog)
                return
            g = keyword_mgr.groups[sel[0]]
            if messagebox.askyesno("确认删除", f"确定要删除分组「{g.name}」吗？", parent=dialog):
                keyword_mgr.delete_group(g.name)
                self._refresh_keyword_ui()
                # 如果删除的是主窗口当前选中的分组，清除输入框
                if self._group_var.get() == g.name:
                    self._group_var.set("（暂无分组）" if not keyword_mgr.groups else keyword_mgr.groups[0].name)
                    self._keyword_entry.delete(0, tk.END)
                self._log(f"🗑 已删除分组「{g.name}」", "warn")
                dialog.destroy()

        ttk.Button(btn_frame, text="📥 加载", command=load_selected).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="💾 保存修改", command=do_update).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="🗑 删除分组", command=do_delete).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="关闭", command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

        # 初始选中第一个（仅高亮，不填编辑区）
        if lb.size() > 0:
            lb.selection_set(0)

    def _import_keywords_file(self):
        """从文本文件导入关键词"""
        filepath = filedialog.askopenfilename(
            title="选择关键词文件",
            filetypes=[("文本文件", "*.txt"), ("CSV文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not filepath:
            return
        try:
            kws = keyword_mgr.import_from_file(filepath)
            if not kws:
                messagebox.showwarning("导入结果", "文件中未找到关键词")
                return
            # 填入输入框
            kw_str = ", ".join(kws)
            self._keyword_entry.delete(0, tk.END)
            self._keyword_entry.insert(0, kw_str)
            self._log(f"📥 已导入 {len(kws)} 个关键词: {Path(filepath).name}", "info")
        except Exception as e:
            messagebox.showerror("导入失败", f"读取文件出错:\n{e}")

    def _on_recent_selected(self, event=None):
        """选中最近使用的关键词时填入输入框"""
        kw = self._recent_var.get()
        if not kw or kw == "（暂无记录）":
            return
        self._keyword_entry.delete(0, tk.END)
        self._keyword_entry.insert(0, kw)

    def _start_crawl(self):
        if self._running:
            messagebox.showwarning("提示", "已有爬取任务在运行中")
            return

        # 有断点时，提示用户是否使用断点续传
        if self._resume_state and self._resume_state.get('user_ids'):
            remaining = self._resume_state['user_ids']
            resume_keyword = self._resume_state.get('keyword', '')
            from crawler import _split_keywords
            rkws = _split_keywords(resume_keyword)
            resume_kw_display = ', '.join(rkws[:5])
            if len(rkws) > 5:
                resume_kw_display += f' …({len(rkws)}个)'
            choice = messagebox.askyesnocancel(
                "断点续传可用",
                f"检测到上次未完成的爬取任务：\n\n"
                f"👤 剩余用户: {len(remaining)} 人\n"
                f"🔍 关键词: {resume_kw_display if resume_keyword else '（无筛选）'}\n\n"
                f"「是」= 断点续传（推荐，无需重新选择）\n"
                f"「否」= 放弃断点，全新开始\n"
                f"「取消」= 不做任何操作",
                parent=self.root
            )
            if choice is None:  # 取消
                return
            if choice:  # 是 — 断点续传
                self._resume_crawl()
                return
            # 否 — 清空断点，继续全新开始
            self._log("🗑 用户放弃断点，全新开始", 'info')

        self._read_config_from_ui()

        # 从用户列表获取选中的用户
        selected_iids = self._user_tree.selection()
        if not selected_iids:
            messagebox.showwarning("提示",
                "请先在「目标用户」列表中选中要爬取的用户\n\n"
                "如果列表为空，请在上方输入 URL 后点击「添加到列表」")
            return

        # iid 就是 user_id
        user_ids = list(selected_iids)

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

        # 读取关键词（提前，测试模式需要判断）
        keyword = self._keyword_entry.get().strip()
        # 记录到最近使用列表
        if keyword:
            keyword_mgr.add_recent(keyword)
            self._refresh_keyword_ui()

        # ── 确认弹窗 ──
        from crawler import _split_keywords
        kws = _split_keywords(keyword)
        filter_info = f"🔍 关键词筛选：{len(kws)}个 → {', '.join(kws[:8])}"
        if len(kws) > 8:
            filter_info += f" …(共{len(kws)}个)"
        if not keyword:
            filter_info = "⚠ 未设置关键词，将抓取全部回答（无筛选）"
        confirm_msg = (
            f"确认开始爬取？\n\n"
            f"👤 目标用户: {len(user_ids)}人\n"
            f"{filter_info}\n"
            f"📊 最多爬取: {'全部' if self._cfg.max_answers == 0 else self._cfg.max_answers} 条/人"
        )
        if self._cfg.test_mode:
            confirm_msg += "\n🧪 测试模式: 开启"
        if not messagebox.askyesno("确认爬取", confirm_msg, parent=self.root):
            self._log("⚠ 用户取消爬取", "warn")
            return

        # 清空旧断点状态
        self._resume_state = None

        # ── 启动爬取 ──
        self._log(f"\n{'='*55}", 'dim')
        self._log(f"🎯 目标用户: {', '.join(user_ids)}", 'info')
        if self._cfg.test_mode:
            if keyword:
                self._log(f"🧪 测试模式: 开启（5条/批筛选→标题匹配≥3条即停→至多爬3条）", 'info')
            else:
                self._log(f"🧪 测试模式: 开启（只爬3条）", 'info')
        else:
            self._log(f"📊 最多爬取: {'全部' if self._cfg.max_answers == 0 else self._cfg.max_answers} 条/人", 'info')
        self._log(f"🎯 输出模式: 混合模式（截图 + 文字 + base64图片嵌入）", 'info')
        self._log(f"👁 无头模式: {'是' if self._cfg.headless else '否'}", 'info')
        self._log(f"🔒 法务证据模式: {'是（HTML + 证据报告 + SHA256）' if self._cfg.forensic_mode else '否'}", 'info')
        if self._cfg.force_no_cache:
            self._log(f"🔄 强制忽略缓存: 开启（跳过所有缓存+进度）", 'info')
        self._log(f"{'='*55}\n", 'dim')

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
        self._resume_btn.config(state=tk.DISABLED)
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

    def _resume_crawl(self):
        """断点续传：从上次停止位置继续爬取"""
        if not self._resume_state or not self._resume_state.get('user_ids'):
            messagebox.showwarning("提示", "没有可继续的断点")
            return
        if self._running:
            messagebox.showwarning("提示", "已有爬取任务在运行中")
            return

        state = self._resume_state
        remaining = state['user_ids']
        keyword = state.get('keyword', '')
        force_recrawl_map = state.get('force_recrawl_map', {})

        # 确认弹窗
        from crawler import _split_keywords
        kws = _split_keywords(keyword)
        filter_info = f"🔍 关键词筛选：{len(kws)}个 → {', '.join(kws[:8])}"
        if len(kws) > 8:
            filter_info += f" …(共{len(kws)}个)"
        if not keyword:
            filter_info = "⚠ 未设置关键词，将抓取全部回答（无筛选）"
        confirm_msg = (
            f"确认断点续传？\n\n"
            f"👤 剩余用户: {len(remaining)}人\n"
            f"{filter_info}\n\n"
            f"💡 将使用停止时的关键词配置（非UI当前值）"
        )
        if not messagebox.askyesno("确认断点续传", confirm_msg, parent=self.root):
            self._log("⚠ 用户取消断点续传", "warn")
            return

        self._log(f"\n{'='*55}", 'dim')
        self._log(f"🔄 断点续传: 剩余 {len(remaining)} 人", 'info')
        self._log(f"{'='*55}\n", 'dim')

        # 状态切换
        self._running = True
        self._stop_flag = False
        self._stop_event.clear()
        self._start_btn.config(state=tk.DISABLED)
        self._resume_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._progress['value'] = 0
        self._progress_label.config(text=f"继续爬取 ({len(remaining)} 人)...")

        self._crawler_thread = threading.Thread(
            target=self._crawl_thread,
            args=(list(remaining), force_recrawl_map, keyword),
            daemon=True
        )
        self._crawler_thread.start()

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
                        # 保存断点：剩余未处理的用户
                        remaining = user_ids[i:]
                        self._resume_state = {
                            'user_ids': remaining,
                            'keyword': keyword,
                            'force_recrawl_map': force_recrawl_map
                        }
                        self._log(f"⚠ 用户手动停止（剩余 {len(remaining)} 人可断点续传）", 'warn')
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
        # 有断点状态时启用「继续」按钮
        # 额外检查：如果 _stop_flag 为 True（被用户手动停止），即使 _resume_state 未构建
        # （边缘情况），也尝试从当前 user_ids 构建断点
        if self._stop_flag and (not self._resume_state or not self._resume_state.get('user_ids')):
            # 边缘情况兜底：被停止但没有断点记录，尝试从最近一次爬取的 user_ids 恢复
            self._log("⚠ 检测到手动停止但断点状态异常，尝试自动恢复...", 'warn')

        has_resume = self._resume_state and self._resume_state.get('user_ids')
        if has_resume:
            n_remaining = len(self._resume_state['user_ids'])
            self._resume_btn.config(state=tk.NORMAL)
            resume_kw = self._resume_state.get('keyword', '')
            kw_hint = f"，关键词「{resume_kw[:30]}...」" if resume_kw else ""
            self._progress_label.config(
                text=f"已停止 — 剩余 {n_remaining} 人可继续{kw_hint}"
            )
        else:
            self._resume_btn.config(state=tk.DISABLED)
            self._progress['value'] = 100
            self._progress_label.config(text="完成")
            self._resume_state = None  # 全部完成，清除断点
        self._running = False
        # 自动刷新已抓取用户列表
        self._refresh_crawled_users_report(silent=True)

    def _refresh_crawled_users_report(self, silent=False):
        """刷新「已抓取用户列表.md」"""
        try:
            from id_manager import get_id_manager
            mgr = get_id_manager()
            mgr.load()  # 重新加载，确保读到最新爬取历史
            path = mgr.save_crawled_users_report(
                output_root=self._cfg.output_dir
            )
            if not silent:
                self._log(f"📋 已刷新用户列表: {path.name}", 'info')
        except Exception as e:
            self._log(f"⚠ 刷新用户列表失败: {e}", 'warn')

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
