"""
用户 ID 列表管理模块
管理爬取目标用户、昵称备注、爬取历史记录
"""
import json
from pathlib import Path
from datetime import datetime


ID_LIST_FILE = Path(__file__).parent.parent / "id_list.json"


class UserEntry:
    """单个用户条目"""
    def __init__(self, user_id: str, nickname: str = "",
                 url: str = "", crawl_history: list = None):
        self.user_id = user_id
        self.nickname = nickname or user_id
        self.url = url or f"https://www.zhihu.com/people/{user_id}"
        self.crawl_history = crawl_history or []

    def to_dict(self) -> dict:
        return {
            'user_id': self.user_id,
            'nickname': self.nickname,
            'url': self.url,
            'crawl_history': self.crawl_history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserEntry":
        return cls(
            user_id=d.get('user_id', ''),
            nickname=d.get('nickname', ''),
            url=d.get('url', ''),
            crawl_history=d.get('crawl_history', []),
        )

    def __repr__(self):
        return f"UserEntry({self.user_id}, {self.nickname})"


class IDManager:
    """用户 ID 列表管理器"""

    def __init__(self, filepath: Path = None):
        self.filepath = filepath or ID_LIST_FILE
        self.users: list[UserEntry] = []
        self._loaded = False

    # ── 加载 / 保存 ──────────────────────────────────

    def load(self):
        """从 id_list.json 加载用户列表"""
        if self.filepath.exists():
            try:
                data = json.loads(self.filepath.read_text(encoding='utf-8'))
                raw_users = data.get('users', [])
                self.users = [UserEntry.from_dict(u) for u in raw_users]
            except Exception:
                self.users = []
        else:
            self.users = []
        self._loaded = True

    def save(self):
        """保存用户列表到 id_list.json"""
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'users': [u.to_dict() for u in self.users],
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.filepath.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )

    # ── 增删改查 ─────────────────────────────────────

    def add(self, user_id: str, nickname: str = "",
            url: str = "") -> bool:
        """添加用户，已存在返回 False"""
        if self.find(user_id):
            return False
        self.users.append(UserEntry(user_id, nickname, url))
        self.save()
        return True

    def remove(self, user_id: str) -> bool:
        """删除用户，不存在返回 False"""
        before = len(self.users)
        self.users = [u for u in self.users if u.user_id != user_id]
        if len(self.users) < before:
            self.save()
            return True
        return False

    def clear(self):
        """清空所有用户"""
        self.users.clear()
        self.save()

    def find(self, user_id: str) -> UserEntry | None:
        """按 user_id 查找"""
        for u in self.users:
            if u.user_id == user_id:
                return u
        return None

    def update_nickname(self, user_id: str, nickname: str) -> bool:
        """更新昵称"""
        u = self.find(user_id)
        if u:
            u.nickname = nickname
            self.save()
            return True
        return False

    def get_all_ids(self) -> list[str]:
        """获取所有用户 ID 列表"""
        return [u.user_id for u in self.users]

    def get_display_list(self) -> list[tuple[str, str, str]]:
        """返回用于列表显示的 [(nickname, user_id, url), ...]"""
        return [(u.nickname, u.user_id, u.url) for u in self.users]

    # ── 爬取历史 ─────────────────────────────────────

    def add_crawl_record(self, user_id: str, answers_scraped: int,
                         output_dir: str):
        """记录一次爬取"""
        u = self.find(user_id)
        if u:
            u.crawl_history.append({
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'answers_scraped': answers_scraped,
                'output_dir': output_dir,
            })
            self.save()

    def get_crawl_history(self, user_id: str) -> list[dict]:
        """获取用户的爬取历史"""
        u = self.find(user_id)
        return u.crawl_history if u else []

    def get_last_crawl(self, user_id: str) -> dict | None:
        """获取最近一次爬取记录"""
        h = self.get_crawl_history(user_id)
        return h[-1] if h else None

    # ── 已抓取用户列表报告 ────────────────────────────

    def build_crawled_users_report(self, output_root: str = "output") -> str:
        """
        生成「已抓取用户列表.md」的 Markdown 内容。
        扫描 id_list.json + 输出目录，汇总每个用户的基本信息和抓取统计。
        """
        import os as _os
        from pathlib import Path as _Path
        output_dir = _Path(output_root)

        lines = []
        lines.append("# 📋 已抓取用户列表")
        lines.append("")
        lines.append(f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")
        lines.append("---")
        lines.append("")

        # 按最后爬取时间排序：有历史的在前，按最近爬取降序
        def _sort_key(u: UserEntry):
            last = self.get_last_crawl(u.user_id)
            if last:
                return (0, last.get('date', ''), u.user_id)
            return (1, '', u.user_id)

        sorted_users = sorted(self.users, key=_sort_key)

        # 统计
        total_users = len(self.users)
        crawled_users = sum(1 for u in self.users if self.get_crawl_history(u.user_id))
        total_answers = sum(
            sum(r.get('answers_scraped', 0) for r in self.get_crawl_history(u.user_id))
            for u in self.users
        )
        total_md_files = 0
        for u in self.users:
            # 扫描输出目录中的 MD 文件
            for hist in self.get_crawl_history(u.user_id):
                od = _Path(hist.get('output_dir', ''))
                if not od.is_absolute():
                    od = output_dir / od.name if od.parts else output_dir / od
                if od.exists():
                    total_md_files += len(list(od.glob("*.md")))

        lines.append(f"**总用户数**: {total_users} | **已抓取**: {crawled_users} | "
                     f"**累计抓取回答**: {total_answers} 条 | **现存 MD 文件**: {total_md_files} 个")
        lines.append("")

        # ── 已抓取用户 ──
        crawled_list = [u for u in sorted_users if self.get_crawl_history(u.user_id)]
        if crawled_list:
            lines.append("## ✅ 已抓取用户")
            lines.append("")
            lines.append("| 序号 | 昵称 | 用户ID | 首次抓取 | 最近抓取 | 抓取次数 | 累计回答 | 现存MD |")
            lines.append("|------|------|--------|----------|----------|----------|----------|--------|")

            for i, u in enumerate(crawled_list, 1):
                hist = self.get_crawl_history(u.user_id)
                first_date = hist[0].get('date', '')[:10] if hist else '-'
                last_date = hist[-1].get('date', '')[:16] if hist else '-'
                crawl_count = len(hist)
                total_ans = sum(r.get('answers_scraped', 0) for r in hist)

                # 扫描现存 MD 数
                md_count = 0
                for h in hist:
                    od = _Path(h.get('output_dir', ''))
                    if not od.is_absolute():
                        od = output_dir / od.name if od.parts else output_dir / od
                    if od.exists():
                        md_count += len(list(od.glob("*.md")))

                nickname = u.nickname if u.nickname != u.user_id else '-'
                lines.append(
                    f"| {i} | {nickname} | `{u.user_id}` | {first_date} | {last_date} | "
                    f"{crawl_count} | {total_ans} | {md_count} |"
                )

            lines.append("")

        # ── 未抓取用户 ──
        uncrawled = [u for u in sorted_users if not self.get_crawl_history(u.user_id)]
        if uncrawled:
            lines.append("## ⏳ 待抓取用户")
            lines.append("")
            lines.append("| 序号 | 昵称 | 用户ID | 主页 |")
            lines.append("|------|------|--------|------|")

            for i, u in enumerate(uncrawled, 1):
                nickname = u.nickname if u.nickname != u.user_id else '-'
                url_short = u.url.replace('https://www.zhihu.com/people/', '…/')
                lines.append(f"| {i} | {nickname} | `{u.user_id}` | [{url_short}]({u.url}) |")

            lines.append("")

        # ── 详情 ──
        if crawled_list:
            lines.append("---")
            lines.append("")
            lines.append("## 📊 抓取详情")
            lines.append("")

            for u in crawled_list:
                hist = self.get_crawl_history(u.user_id)
                nickname = u.nickname if u.nickname != u.user_id else u.user_id
                total_ans = sum(r.get('answers_scraped', 0) for r in hist)
                lines.append(f"### {nickname} (`{u.user_id}`)")
                lines.append("")
                lines.append(f"- **主页**: [{u.url}]({u.url})")
                lines.append(f"- **首次抓取**: {hist[0].get('date', '-')}")
                lines.append(f"- **最近抓取**: {hist[-1].get('date', '-')}")
                lines.append(f"- **总抓取次数**: {len(hist)} 次")
                lines.append(f"- **累计抓取回答**: {total_ans} 条")
                lines.append("")

                # 列出各次抓取
                lines.append("| 时间 | 抓取条数 | 输出目录 |")
                lines.append("|------|----------|----------|")
                for r in hist:
                    date = r.get('date', '')[:16]
                    count = r.get('answers_scraped', 0)
                    od = r.get('output_dir', '').replace('\\', '/')
                    lines.append(f"| {date} | {count} | `{od}/` |")
                lines.append("")

        return "\n".join(lines)

    def save_crawled_users_report(self, output_root: str = "output",
                                  report_path: str = None) -> Path:
        """生成并保存「已抓取用户列表.md」"""
        report_path = Path(report_path or
                          (Path(__file__).parent.parent / "已抓取用户列表.md"))
        content = self.build_crawled_users_report(output_root)
        report_path.write_text(content, encoding='utf-8')
        return report_path


# 全局单例（惰性加载）
_id_manager: IDManager | None = None


def get_id_manager() -> IDManager:
    global _id_manager
    if _id_manager is None:
        _id_manager = IDManager()
        _id_manager.load()
    return _id_manager
