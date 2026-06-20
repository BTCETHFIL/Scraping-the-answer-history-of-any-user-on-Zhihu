"""
知乎爬虫 — 日志配置
使用 RotatingFileHandler，单文件最大 2MB，保留 3 个备份，防止日志无限增长。
提供 log_print() 函数：同时 print 到控制台和写入日志文件。
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent / "log"
LOG_FILE = LOG_DIR / "zhihu_crawler.log"
MAX_BYTES = 2 * 1024 * 1024  # 2 MB
BACKUP_COUNT = 3

_logger_initialized = False
_file_logger = None


def init_logging():
    """初始化日志系统（幂等：多次调用不重复添加 handler）"""
    global _logger_initialized, _file_logger
    if _logger_initialized:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # 文件 logger（不传播到 root，避免重复输出）
    _file_logger = logging.getLogger("zhihu.file")
    _file_logger.setLevel(logging.INFO)
    _file_logger.propagate = False

    fh = RotatingFileHandler(
        str(LOG_FILE), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    _file_logger.addHandler(fh)

    _logger_initialized = True


def log_print(*args, level: str = "info", **kwargs):
    """同时 print 到控制台和写入日志文件。
    用法与 print() 相同，额外支持 level 参数指定日志级别。
    """
    # 构建消息
    sep = kwargs.get('sep', ' ')
    end = kwargs.get('end', '\n')
    msg = sep.join(str(a) for a in args)

    # print 到控制台（保留原始行为）
    print(msg, end=end)

    # 写入日志文件
    init_logging()
    if msg.strip():
        if level == "warning":
            _file_logger.warning(msg.rstrip())
        elif level == "error":
            _file_logger.error(msg.rstrip())
        else:
            _file_logger.info(msg.rstrip())


def get_logger(name: str = "zhihu") -> logging.Logger:
    """获取日志器（自动初始化）"""
    init_logging()
    logger = logging.getLogger(name)
    logger.propagate = False  # 不传播到 root
    # 添加文件 handler
    fh = RotatingFileHandler(
        str(LOG_FILE), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding='utf-8'
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logger.addHandler(fh)
    return logger
