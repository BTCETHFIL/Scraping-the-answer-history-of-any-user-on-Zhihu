"""
知乎爬虫配置
可通过 config.json 覆盖默认配置
"""

import json
from pathlib import Path
from dataclasses import dataclass, field, fields


@dataclass
class Config:
    # ── 目标用户 ──
    # 支持格式: 用户ID、完整主页链接、/people/xxx/answers
    targets: list = field(default_factory=list)

    # ── 爬取设置 ──
    max_answers: int = 0           # 每个用户最多爬取回答数，0=全部
    test_mode: bool = False        # 测试模式：只爬3条用于快速验证
    output_dir: str = "output"     # 输出根目录

    # ── 延迟策略（秒）──
    scroll_delay_min: float = 2.0  # 滚动加载最小间隔
    scroll_delay_max: float = 5.0  # 滚动加载最大间隔
    page_delay_min: float = 3.0    # 打开回答页最小间隔
    page_delay_max: float = 8.0    # 打开回答页最大间隔

    # ── 浏览器 ──
    browser_data_dir: str = "browser_data"  # 浏览器持久化目录（保持登录态）
    chrome_exe: str = ""           # Chrome 路径，留空则用 Playwright 内置 Chromium

    # ── 内容设置（固定混合模式：截图+文字+图片嵌入）──
    download_images: bool = True   # 始终处理图片（base64嵌入）
    embed_images: bool = True      # 始终以 base64 嵌入图片（自包含）
    screenshot_mode: bool = True   # 始终截图整页（保留原始排版）
    save_html: bool = False        # 同时保存原始 HTML（法务模式自动开启）

    # ── 法务证据模式 ──
    forensic_mode: bool = True     # 法务模式：默认保存HTML、生成证据报告、文件SHA256哈希

    # ── 缓存设置 ──
    cache_dir: str = "cache_data"  # 缓存目录（独立于 output，防止用户清理 output 时误删）
    cache_ttl_minutes: int = 0     # 链接/回答内容缓存有效期（0=永久有效，-1=禁用缓存）
    force_no_cache: bool = False   # 强制忽略所有缓存+进度，从头重新爬取
    links_cache_max_age_hours: int = 24  # 链接缓存新鲜度阈值（小时）。换关键词时，若缓存距上次收集未超过此值，
                                         # 则跳过滚屏，直接内存重过滤（0=始终滚屏增量检查）

    # ── 输出文件名模板 ──
    # 可用变量: {date}, {title}, {upvotes}, {answer_id}
    filename_template: str = "{date}_{title}.md"

    @classmethod
    def from_file(cls, path: str = "config.json") -> "Config":
        """从 JSON 文件加载配置，与默认值合并"""
        cfg = cls()
        cfg_path = Path(path)
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
        return cfg

    def save(self, path: str = "config.json"):
        """保存当前配置到 JSON 文件"""
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


# 全局配置实例
config = Config.from_file()
