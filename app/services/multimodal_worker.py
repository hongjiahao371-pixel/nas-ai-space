"""Low-priority, restartable media indexer; the application database is read-only."""
import logging
import os
import signal
import threading

from app.config import settings
from app.services.multimodal import MultimodalService


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    service = MultimodalService(settings)
    if not service.enabled:
        raise RuntimeError("多模态索引尚未启用")
    while not stop.is_set():
        try:
            result = service.run_batch()
            logging.info("多模态批次 %s", result)
        except Exception:
            logging.exception("多模态批次暂不可用")
        stop.wait(settings.multimodal_poll_seconds)


if __name__ == "__main__":
    main()
