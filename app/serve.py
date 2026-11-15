"""Запуск сервісу з коректною зупинкою за SIGTERM (PID 1, CMD в exec-формі).
Uvicorn за SIGTERM припиняє приймати зʼєднання, дочікується запитів в обробці і виконує
lifespan-завершення (закриття пулу). Нові версії uvicorn після цього повторно надсилають
собі захоплений сигнал, і процес завершується з кодом 143; тут повторне надсилання вимкнено."""
import contextlib
import os
import signal

import uvicorn

from main import app


class GracefulServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        old = {sig: signal.signal(sig, self.handle_exit) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)  # свідомо без raise_signal: інакше код 143


if __name__ == "__main__":
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                            loop="uvloop", http="httptools", access_log=False,
                            timeout_keep_alive=75, timeout_graceful_shutdown=10)
    GracefulServer(config).run()
