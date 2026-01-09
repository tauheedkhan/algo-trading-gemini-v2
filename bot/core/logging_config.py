import logging
import logging.handlers
import sys
import os
from datetime import datetime
from pathlib import Path


class ColoredFormatter(logging.Formatter):
    """Custom formatter with colors for terminal output."""

    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
    }
    RESET = '\033[0m'

    def format(self, record: logging.LogRecord) -> str:
        # Add color based on level
        color = self.COLORS.get(record.levelname, '')
        reset = self.RESET

        # Format: timestamp - level - logger - message
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted = f"{timestamp} - {color}{record.levelname:8}{reset} - {record.name} - {record.getMessage()}"

        # Add exception info if present
        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


class SimpleFormatter(logging.Formatter):
    """Simple human-readable formatter without colors (for file logging)."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        formatted = f"{timestamp} - {record.levelname:8} - {record.name} - {record.getMessage()}"

        if record.exc_info:
            formatted += f"\n{self.formatException(record.exc_info)}"

        return formatted


def setup_logging(log_level: str = "INFO", use_colors: bool = True, log_dir: str = "logs"):
    """
    Configures logging with console output and file rotation.

    Features:
    - Console output with colors
    - Daily log file rotation (new file each day)
    - New log file on each bot start (session-based)
    - Keeps last 30 days of logs

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        use_colors: If True, use colored output (for terminal)
        log_dir: Directory to store log files
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Create logs directory if it doesn't exist
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)

    # === Console Handler ===
    console_handler = logging.StreamHandler(sys.stdout)
    if use_colors and sys.stdout.isatty():
        console_handler.setFormatter(ColoredFormatter())
    else:
        console_handler.setFormatter(SimpleFormatter())
    root_logger.addHandler(console_handler)

    # === Session Log File (new file each bot start) ===
    session_timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    session_log_file = log_path / f"bot_session_{session_timestamp}.log"

    session_handler = logging.FileHandler(session_log_file, encoding='utf-8')
    session_handler.setFormatter(SimpleFormatter())
    session_handler.setLevel(logging.DEBUG)  # Capture all levels in session log
    root_logger.addHandler(session_handler)

    # === Daily Rotating Log File ===
    daily_log_file = log_path / "bot_daily.log"

    daily_handler = logging.handlers.TimedRotatingFileHandler(
        daily_log_file,
        when='midnight',
        interval=1,
        backupCount=30,  # Keep 30 days of logs
        encoding='utf-8'
    )
    daily_handler.suffix = "%Y-%m-%d"  # Append date to rotated files
    daily_handler.setFormatter(SimpleFormatter())
    daily_handler.setLevel(logging.INFO)
    root_logger.addHandler(daily_handler)

    # === Error Log File (errors only) ===
    error_log_file = log_path / "bot_errors.log"

    error_handler = logging.handlers.TimedRotatingFileHandler(
        error_log_file,
        when='midnight',
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    error_handler.suffix = "%Y-%m-%d"
    error_handler.setFormatter(SimpleFormatter())
    error_handler.setLevel(logging.ERROR)  # Only errors and critical
    root_logger.addHandler(error_handler)

    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured: level={log_level}, log_dir={log_dir}")
    logger.info(f"Session log: {session_log_file}")
    logger.info(f"Daily log: {daily_log_file}")
    logger.info(f"Error log: {error_log_file}")

    return root_logger


def log_bot_start():
    """Log bot startup marker."""
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("BOT STARTED")
    logger.info(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)


def log_bot_stop(reason: str = "Normal shutdown"):
    """Log bot shutdown marker."""
    logger = logging.getLogger(__name__)
    logger.info("=" * 60)
    logger.info("BOT STOPPED")
    logger.info(f"Stop Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"Reason: {reason}")
    logger.info("=" * 60)
