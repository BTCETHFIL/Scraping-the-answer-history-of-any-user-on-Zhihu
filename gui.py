"""
知乎回答爬虫 — GUI 界面
基于 Tkinter，提供完整配置和实时日志
"""
import sys
import os
import io
import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
from pathlib import Path

# 工作目录切到项目根
os.chdir(Path(__file__).parent)

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

class ZhihuCrawlerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("知乎回答爬虫 v1.0")
        self.root.geometry("880x700")
        self.root.minsize(780, 580)
        self._running = False
        self._stop_flag = False
        self._crawler_thread = None

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
        self._quick_add_entry.insert(0, "输入URL或用户ID...")
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
        self._z_c0_entry = ttk.Entry(cookie_frame, show="*", width=50)
        self._z_c0_entry.pack(fill=tk.X, pady=(2, 0))
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
        ttk.Checkbutton(row4, text="🧪 测试模式（只爬5条）", variable=self._test_var).pack(side=tk.LEFT, padx=15)

        row5 = ttk.Frame(crawl_frame)
        row5.pack(fill=tk.X, pady=2)
        ttk.Label(row5, text="🎯 输出模式：混合模式（截图 + 文字 + base64图片嵌入）",
                  foreground="#4ec9b0", font=("", 9)).pack(side=tk.LEFT)

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
        ttk.Label(row_d1, text="滚动间隔:", width=10).pack(side=tk.LEFT)
        self._scroll_min = ttk.Entry(row_d1, width=5)
        self._scroll_min.insert(0, "2")
        self._scroll_min.pack(side=tk.LEFT)
        ttk.Label(row_d1, text="-").pack(side=tk.LEFT)
        self._scroll_max = ttk.Entry(row_d1, width=5)
        self._scroll_max.insert(0, "5")
        self._scroll_max.pack(side=tk.LEFT)

        row_d2 = ttk.Frame(delay_frame)
        row_d2.pack(fill=tk.X, pady=2)
        ttk.Label(row_d2, text="翻页间隔:", width=10).pack(side=tk.LEFT)
        self._page_min = ttk.Entry(row_d2, width=5)
        self._page_min.insert(0, "3")
        self._page_min.pack(side=tk.LEFT)
        ttk.Label(row_d2, text="-").pack(side=tk.LEFT)
        self._page_max = ttk.Entry(row_d2, width=5)
        self._page_max.insert(0, "8")
        self._page_max.pack(side=tk.LEFT)

        row_d3 = ttk.Frame(delay_frame)
        row_d3.pack(fill=tk.X, pady=2)
        ttk.Label(row_d3, text="缓存有效期(分):", width=12).pack(side=tk.LEFT)
        self._cache_ttl = ttk.Entry(row_d3, width=5)
        self._cache_ttl.insert(0, "30")
        self._cache_ttl.pack(side=tk.LEFT)
        ttk.Label(row_d3, text="0=禁用", foreground="gray").pack(side=tk.LEFT, padx=4)

        row_d4 = ttk.Frame(delay_frame)
        row_d4.pack(fill=tk.X, pady=2)
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
        ttk.Button(log_btn_row, text="清空日志", command=self._clear_log, width=10).pack(side=tk.RIGHT)

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
        for nickname, user_id in mgr.get_display_list():
            history = mgr.get_crawl_history(user_id)
            hist_info = ""
            if history:
                last = history[-1]
                hist_info = f" | 最近: {last['date'][:16]}, {last['answers_scraped']}条"
            self._user_listbox.insert(tk.END, f"{nickname}    ({user_id}){hist_info}")
        total = len(mgr.users)
        self._list_status.config(
            text=f"共 {total} 个用户"
        )

    def _add_user_from_entry(self, event=None):
        """从快速添加输入框添加用户"""
        raw = self._quick_add_entry.get().strip()
        if not raw or raw == "输入URL或用户ID...":
            return

        try:
            user_id = extract_user_id(raw)
        except ValueError:
            messagebox.showwarning("格式错误",
                f"无法从输入中提取用户 ID:\n{raw}\n\n"
                "支持格式: user_id / https://www.zhihu.com/people/xxx")
            return

        mgr = self._get_id_manager()
        if mgr.add(user_id, nickname=user_id):
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
        if self._quick_add_entry.get() == "输入URL或用户ID...":
            self._quick_add_entry.delete(0, tk.END)
            self._quick_add_entry.config(foreground="black")

    def _on_quick_add_focus_out(self, event):
        if not self._quick_add_entry.get().strip():
            self._quick_add_entry.insert(0, "输入URL或用户ID...")
            self._quick_add_entry.config(foreground="gray")

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
                # 回填到输入框（只取 z_c0）
                for c in cookies:
                    if c.get('name') == 'z_c0':
                        self._z_c0_entry.delete(0, tk.END)
                        self._z_c0_entry.insert(0, c.get('value', ''))
                        break
            else:
                self._login_status.config(text="🔴 未登录", foreground="#f44747")
        else:
            self._login_status.config(text="🔴 未登录", foreground="#f44747")

    def _save_cookies(self):
        """保存 Cookie 到文件"""
        zc0 = self._z_c0_entry.get().strip()
        if not zc0:
            messagebox.showwarning("提示", "请先填入 z_c0 Cookie 值")
            return
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
        for f in self._cfg.__dataclass_fields__:
            setattr(config, f, getattr(self._cfg, f))

    # ── 爬取控制 ────────────────────────────────────────

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
        self._log(f"📊 最多爬取: {'全部' if self._cfg.max_answers == 0 else self._cfg.max_answers} 条/人", 'info')
        self._log(f"🎯 输出模式: 混合模式（截图 + 文字 + base64图片嵌入）", 'info')
        self._log(f"👁 无头模式: {'是' if self._cfg.headless else '否'}", 'info')
        self._log(f"🔒 法务证据模式: {'是（HTML + 证据报告 + SHA256）' if self._cfg.forensic_mode else '否'}", 'info')
        if self._cfg.test_mode:
            self._log(f"🧪 测试模式: 开启（最多5条）", 'info')
        if self._cfg.force_no_cache:
            self._log(f"🔄 强制忽略缓存: 开启（跳过所有缓存+进度）", 'info')
        self._log(f"{'='*55}\n", 'dim')

        # 检查 Chrome
        if self._cfg.chrome_exe and not Path(self._cfg.chrome_exe).exists():
            self._log(f"⚠ Chrome 路径不存在: {self._cfg.chrome_exe}", 'warn')

        # 状态切换
        self._running = True
        self._stop_flag = False
        self._start_btn.config(state=tk.DISABLED)
        self._stop_btn.config(state=tk.NORMAL)
        self._progress['value'] = 0
        self._progress_label.config(text="正在启动浏览器...")

        # 后台线程
        self._crawler_thread = threading.Thread(
            target=self._crawl_thread,
            args=(user_ids, force_recrawl_map),
            daemon=True
        )
        self._crawler_thread.start()

    def _stop_crawl(self):
        if not self._running:
            return
        self._stop_flag = True
        self._log("\n⚠ 正在停止...（请等待当前页面处理完成）", 'warn')
        self._stop_btn.config(state=tk.DISABLED)

    def _crawl_thread(self, user_ids: list, force_recrawl_map: dict = None):
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
                    ]
                }
                if self._cfg.chrome_exe:
                    launch_kwargs['executable_path'] = self._cfg.chrome_exe

                browser = p.chromium.launch(**launch_kwargs)

                state_path = get_login_state_path(self._cfg.browser_data_dir)
                context_kwargs = {
                    'viewport': {'width': 1280, 'height': 800},
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
                        r = crawl_user_answers(page, uid, force_recrawl_ids=force_ids)
                        results.append(r)
                        # 记录爬取历史
                        from id_manager import get_id_manager
                        mgr = get_id_manager()
                        mgr.add_crawl_record(
                            uid,
                            r.get('success', 0),
                            str(Path(self._cfg.output_dir) / uid)
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
        self.root.destroy()


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════

if __name__ == '__main__':
    root = tk.Tk()
    app = ZhihuCrawlerGUI(root)
    root.mainloop()
