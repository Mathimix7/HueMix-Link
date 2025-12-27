"""Logging service providing rotating file + pretty console handlers.

This merges the previous standalone `logging_setup.py` into the
service so logging can be started/reconfigured as part of the app
service lifecycle.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
from rich.logging import RichHandler
from constants import LOG_FORMAT
from rich.console import Console

def setup_logging():
    log_dir = Path('logs')
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    root_logger = logging.getLogger()
    # Remove any existing handlers to avoid duplicate logs
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    root_logger.setLevel(logging.INFO)

    # File handler (rotating)
    log_file = log_dir / 'app.log'
    try:
        fh = RotatingFileHandler(str(log_file), maxBytes=10 * 1024 * 1024, backupCount=7, encoding='utf-8')
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(fh)
    except Exception:
        root_logger.warning('Could not create file handler for logging; continuing with console only.')

    rh = RichHandler(rich_tracebacks=True, omit_repeated_times=False, show_path=False, console=Console(force_terminal=True))
    inline_format = "[%(filename)s:%(lineno)d] %(message)s"
    rh.setFormatter(logging.Formatter(inline_format))
    rh.setLevel(logging.INFO)
    root_logger.addHandler(rh)


class LoggingService:
    def __init__(self):
        self._started = False

    def start(self):
        if self._started:
            return
        try:
            setup_logging()
            logging.getLogger(__name__).info('Logging service started')
            self._started = True
        except Exception:
            logging.getLogger(__name__).exception('Failed to start logging service')

    def reconfigure(self):
        try:
            setup_logging()
            logging.getLogger(__name__).info('Logging service reconfigured')
            self._started = True
        except Exception:
            logging.getLogger(__name__).exception('Failed to reconfigure logging service')

    def stop(self):
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        self._started = False
        logging.getLogger(__name__).info('Logging service stopped')


logging_service = LoggingService()
