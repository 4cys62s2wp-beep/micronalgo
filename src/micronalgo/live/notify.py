"""Notifications that need no paid service.

Three sinks, all optional and all failure-tolerant: a notification that throws
must never take the trading loop down with it.
"""

from __future__ import annotations

from pathlib import Path

import requests

from .audit import get_logger

log = get_logger("notify")


class Notifier:
    def __init__(self, webhook: str = "", alert_file: Path | str | None = None, timeout: int = 10) -> None:
        self.webhook = webhook
        self.alert_file = Path(alert_file) if alert_file else None
        self.timeout = timeout

    def send(self, title: str, message: str, *, level: str = "info") -> None:
        text = f"[micronalgo/{level}] {title}\n{message}"
        log.info("%s | %s", title, message) if level == "info" else log.warning("%s | %s", title, message)

        if self.alert_file:
            try:
                self.alert_file.parent.mkdir(parents=True, exist_ok=True)
                with self.alert_file.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError as exc:
                log.warning("could not write alert file: %s", exc)

        if self.webhook:
            try:
                requests.post(
                    self.webhook,
                    json={"text": text, "title": title, "message": message, "level": level},
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"},
                )
            except Exception as exc:  # never let a notifier break trading
                log.warning("webhook failed (ignored): %s", exc)

    def alert(self, title: str, message: str) -> None:
        self.send(title, message, level="alert")
