import importlib
import logging
from os import getenv
from os.path import expandvars
from threading import Thread, Event
from time import time
from typing import Type

from clearml.backend_api.session import Session
from clearml.backend_interface.session import SendError


class ErrorLog:
    handler_cls = getenv("APP_USAGE_LOG_HANDLER_CLS") or "fluent.handler.FluentHandler"
    handler_host = getenv("APP_USAGE_LOG_HANDLER_HOST") or "172.17.0.1"
    handler_port = int(getenv("APP_USAGE_LOG_HANDLER_PORT") or "24224")
    handler_tag = expandvars(
        getenv("APP_USAGE_LOG_HANDLER_TAG") or "app-{app_id}-$HOSTNAME"
    )
    formatter_cls = (
        getenv("APP_USAGE_LOG_FORMATTER_CLS") or "fluent.handler.FluentRecordFormatter"
    )

    @staticmethod
    def _import_type(typename: str) -> Type:
        module_name, _, type_name = typename.rpartition(".")
        module = importlib.import_module(module_name, package=__package__)
        if not module:
            raise ValueError(f"Failed loading module for {typename}")
        res = getattr(module, type_name, None)
        if not res:
            raise ValueError(f"Failed loading type {type_name} from {module}")
        return res

    @classmethod
    def get(cls, app_id: str) -> logging.Logger:
        logger = logging.getLogger("usage_reporter")
        try:
            handler_cls = cls._import_type(cls.handler_cls)
            formatter_cls = cls._import_type(cls.formatter_cls)
            tag = cls.handler_tag.format(app_id=app_id)
            handler = handler_cls(tag=tag)
            formatter = formatter_cls()
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        except (ImportError, Exception):
            # TODO: write something, somewhere
            logger.addHandler(logging.NullHandler())
        return logger


class UsageReporter:
    _min_report_interval_sec = 5
    _max_report_interval_sec = 10 * 60

    def __init__(
        self,
        session: Session,
        app_id: str,
        units: int = 1,
        report_interval_sec: int = 300,
    ):
        self._ref_time = time()
        self._app_id = app_id
        self._units = max(units, 1)
        self._extra_units = 0
        self._report_interval = min(
            max(report_interval_sec, self._min_report_interval_sec),
            self._max_report_interval_sec,
        )
        self._reported_usage_sec = 0

        if not self._should_run(session):
            return

        self._session = session
        self._thread = Thread(target=self._monitor)
        self._thread.daemon = True
        self._stopped = Event()
        self._log = ErrorLog.get(app_id)
        self._thread.start()

    @staticmethod
    def _should_run(session: Session) -> bool:
        # noinspection PyBroadException
        try:
            token_dict = session.get_decoded_token(session.token)
            env = token_dict.get('env', '')
            if "onprem" in env:
                return False
        except Exception:
            pass
        return True

    @property
    def pending_usage_sec(self):
        return (time() - self._ref_time) - self._reported_usage_sec

    @property
    def reported_usage_sec(self):
        return self._reported_usage_sec

    def set_logger(self, logger: logging.Logger):
        self._log = logger

    def set_extra_units(self, extra_units: int):
        self._extra_units = max(extra_units, 0)

    def _send_usage_report(self, usage_sec: float):
        # TODO: If we fail, we'll report pending_usage_sec next time for the last set extra_units...
        units = self._units + self._extra_units

        # noinspection PyBroadException
        try:
            self._log.info("Sending usage report for %d usage seconds, %d units", usage_sec, units)
            res = self._session.send_request(
                service="billing",
                action="usage_report",
                json={
                    "type": "application",
                    "id": self._app_id,
                    "usage_sec": usage_sec,
                    "units": units,
                },
            )
            if res.status_code != 200:
                self._log.warning(f"Failed sending usage report: {res.text}")
            else:
                self._reported_usage_sec += usage_sec
        except SendError as ex:
            self._log.warning(f"Failed sending usage report: {ex}")
        except Exception as ex:
            self._log.exception(f"Error sending usage report")

    def _monitor(self):
        while not self._stopped.wait(self._report_interval):
            self._send_usage_report(self.pending_usage_sec)
