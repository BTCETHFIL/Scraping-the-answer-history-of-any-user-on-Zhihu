"""
用户 ID 列表管理模块
管理爬取目标用户、昵称备注、爬取历史记录
"""
import json
from pathlib import Path
from datetime import datetime


ID_LIST_FILE = Path(__file__).parent / "id_list.json"


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

    def get_display_list(self) -> list[tuple[str, str]]:
        """返回用于列表显示的 [(nickname, user_id), ...]"""
        return [(u.nickname, u.user_id) for u in self.users]

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


# 全局单例（惰性加载）
_id_manager: IDManager | None = None


def get_id_manager() -> IDManager:
    global _id_manager
    if _id_manager is None:
        _id_manager = IDManager()
        _id_manager.load()
    return _id_manager
