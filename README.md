# 知乎回答爬虫 (zhihu-crawler)

抓取指定知乎用户的回答历史，导出为结构化 Markdown 文件。每篇回答同时保存整页截图 + 可搜索文字 + Base64 内嵌图片。

支持 **GUI 图形界面** 和 **命令行** 两种方式。

## 🆕 核心设计：滚屏与输出分离

GUI 将「收集链接」与「生成 MD」拆为两个独立步骤：

| 阶段 | 操作 | 说明 |
|------|------|------|
| **一、滚屏** | 点「开始滚屏」 | 浏览器自动滚动，全量收集所有回答链接到缓存（24h 有效），**不生成 MD 文件** |
| **二、输出** | 点「从缓存生成 MD」 | 从缓存读取链接，可加关键词筛选，逐条抓取回答并生成 Markdown |

这样设计的好处：一次滚屏可反复用不同关键词生成多份 MD，避免重复滚屏请求。

## 功能特点

- **图形界面** — 直观的 Tkinter GUI，一站式配置、监控和 ID 管理
- **滚屏收集** — 自动滚动加载用户的所有回答链接，存入本地缓存
- **缓存生成 MD** — 从已缓存的链接按需生成 Markdown，支持不选关键词全量输出
- **三种登录方式** — 扫码登录 / 手动 Cookie / 复用 storage_state，灵活应对不同环境
- **断点续传** — 进度记录到 `progress.json`，中断后从断点继续
- **混合输出模式** — 每篇回答输出：内容区截图(独立PNG) + 可搜索 Markdown 文字 + 用户影响力数据
- **增量更新** — 再次运行只抓新回答，不重复下载。INDEX.md 增量合并
- **短时缓存** — 30 分钟 TTL 双层缓存（链接列表 + 回答内容），二次运行间隔内跳过网络请求
- **强制忽略缓存** — 测试用复选框，一键跳过所有缓存+进度，从头重爬
- **多用户批量** — 支持一次配置多个用户，依次滚屏
- **关键词过滤** — 支持多关键词（逗号/顿号/空格/分号分隔），标题或回答内容含任一关键词即保存，不区分大小写，`answer_id` 自动去重不重复保存；属于「输出 MD」阶段的功能
- **关键词分组管理** — 关键词保存为命名分组（如"蔚来相关"），一键应用；支持从 .txt 文件批量导入；自动记录最近使用的关键词
- **用户分组管理** — 用户保存为命名分组（如"蔚来黑子"），一键应用/替换/合并；支持从 .txt 文件批量导入；自动记录最近使用的用户组合
- **已抓取用户列表** — 自动生成 `已抓取用户列表.md`，包含每个用户的基本信息、抓取次数、累计回答数等统计
- **系统 Chrome** — 支持配置已有 Chrome 路径，无需额外下载 Chromium
- **测试模式** — 只滚屏前 3 条链接，方便调试

## 快速开始

### 方式一：GUI 图形界面（推荐）

```bash
pip install -r requirements.txt
python 双击运行.py
```

界面提供完整的 ID 列表管理、Cookie 配置、滚屏/输出MD 两步操作和实时日志。

#### GUI 操作流程

```
① 添加目标用户 → ② 勾选用户 → ③ 开始滚屏（全量收集链接）
                                      ↓
                              ④ 选关键词（可选）
                                      ↓
                              ⑤ 从缓存生成 MD
```

> **三步分离**: 「开始滚屏」「从缓存生成 MD」「手动粘贴链接」三个操作互斥，同一时间只能进行一个。

### 方式二：命令行

```bash
pip install -r requirements.txt

# 首先配置 config.json（可复制 config.example.json 修改）
cp config.example.json config.json

# 直接指定用户运行
python -m src.main zhang-jia-wei

# 批量爬取
python -m src.main zhang-jia-wei liu-bo-wen-27
```

### 登录知乎（三选一）

| 方式 | 适用场景 | 说明 |
|------|----------|------|
| 扫码登录 | 桌面环境 | 运行后弹出浏览器窗口扫码 |
| 手动 Cookie | 无桌面 / 服务器 | 从浏览器复制 `z_c0`, `_zap`, `d_c0` 到 `browser_data/cookies.json` |
| 复用登录态 | 二次运行 | 登录成功自动保存 `browser_data/zhihu_state.json`，下次自动复用 |

---

### 使用已有 Chrome（无需下载 Chromium）

在 `config.json` 中配置本地 Chrome 路径：

```json
{
  "chrome_exe": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
}
```

## 输出结构

```
output/
└── zhang-jia-wei/              # 用户 ID 目录
    ├── cache/                  # 短时缓存目录（TTL 内有效）
    │   ├── links.json          #   回答链接列表缓存
    │   └── {answer_id}.json    #   单条回答内容缓存
    ├── INDEX.md                # 回答索引（增量合并）
    ├── progress.json           # 断点续传进度
    ├── 2024-01-15_如何评价XXX.md      # Markdown（截图引用 + 文字 + 影响力数据）
    ├── {answer_id}.png               # 独立截图文件（MD 中相对路径引用）
    ├── 2024-01-15_如何评价XXX.html    # 法务模式：原始 HTML 副本
    └── ...
```

## 配置说明

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `targets` | `[]` | 目标用户 URL 或 ID 列表 |
| `max_answers` | `0` | 每人最多爬取数，0 = 全部 |
| `keyword` | `""` | 关键词过滤，多关键词用逗号/顿号/空格/分号分隔（如 `AI, 人工智能、NLP`），标题或回答内容匹配任一即保存，不区分大小写 |
| `test_mode` | `false` | 测试模式：只爬前 3 条 |
| `output_dir` | `output` | 输出根目录 |
| `chrome_exe` | `""` | 本地 Chrome 路径，留空则使用 Playwright 自带 Chromium |
| `forensic_mode` | `true` | 法务证据模式：自动保存 HTML + 证据报告 + SHA256 |
| `cache_ttl_minutes` | `30` | 短时缓存有效期（分钟），0 = 禁用 |
| `force_no_cache` | `false` | 强制忽略所有缓存+进度，测试用 |
| `scroll_delay_min` | `2.0` | 滚动加载最小延迟（秒） |
| `scroll_delay_max` | `5.0` | 滚动加载最大延迟（秒） |
| `page_delay_min` | `3.0` | 打开回答页最小延迟（秒） |
| `page_delay_max` | `8.0` | 打开回答页最大延迟（秒） |
| `filename_template` | `{date}_{title}.md` | 文件名模板 |

> 输出模式固定为混合模式（截图 + 文字 + Base64 图片嵌入），无需额外配置。

## 项目结构

```
知乎内容抓取/
├── 双击运行.py          # Tkinter GUI 入口（双击启动）
├── src/                 # 源码目录
│   ├── main.py          # 命令行入口
│   ├── crawler.py       # 核心滚屏 + 回答抓取逻辑
│   ├── auth.py          # 登录认证（扫码/Cookie/storage_state）
│   ├── converter.py     # HTML → Markdown 转换
│   ├── storage.py       # 文件存储、缓存管理、证据报告
│   ├── utils.py         # 工具函数（日期提取、文件名清理等）
│   ├── config.py        # 配置数据类
│   ├── id_manager.py    # 用户 ID 列表管理
│   ├── keyword_manager.py # 关键词分组管理
│   ├── user_manager.py  # 用户分组管理
│   └── log_setup.py     # 日志系统（RotatingFileHandler）
├── config.example.json  # 配置文件模板
├── config.json          # 用户配置（不入库）
├── id_list.json         # ID 列表持久化
├── keyword_groups.json  # 关键词分组数据
├── 已抓取用户列表.md     # 自动生成的用户抓取统计报告
├── 发布文档_知乎内容归档工具.md  # 中文发布说明
├── requirements.txt     # Python 依赖
├── LICENSE              # MIT License
└── README.md
```

## 参考项目

- [zhihu-upvote-exporter](https://github.com/5244DragonLin/zhihu-upvote-exporter) — aiohttp API 方案
- [ZhihuSpider](https://github.com/lemoabc/ZhihuSpider) — DrissionPage 方案
- [ZhiZhu](https://github.com/HeroBlast10/ZhiZhu) — Playwright + stealth 方案
- [ZhiArchive](https://github.com/amchii/ZhiArchive) — 截图存档方案

## 许可证

MIT License — 详见 [LICENSE](./LICENSE)
