"""Standalone notification worker.

    python -m app.notify_worker              # drain once and exit (for cron)
    python -m app.notify_worker --loop       # stay running, poll every 30s
    python -m app.notify_worker --loop --interval 10 --limit 50

Why this exists: with NOTIFY_DISPATCH=background the API process sends mail
after each response, which is fine for one web container and wrong for several
— retries would be spread across whichever process happened to serve a request.
Set NOTIFY_DISPATCH=worker in production and run this alongside the API, so
delivery is one job with one owner. Concurrent workers are safe regardless:
`dispatch_pending` claims rows with SELECT ... FOR UPDATE SKIP LOCKED.
"""
import argparse
import logging
import signal
import sys
import time

from app import notifications as notify
from app.config import settings

logger = logging.getLogger("vera.notify_worker")

_stop = False


def _handle_signal(signum, _frame):
    """Finish the batch in flight, then exit — never abandon a send mid-flight."""
    global _stop
    _stop = True
    logger.info("Signal %s received; stopping after the current batch.", signum)


def run_once(limit: int = None) -> dict:
    stats = notify.dispatch_in_new_session(limit)
    if stats.get("attempted"):
        logger.info(
            "channel=%s attempted=%s sent=%s retrying=%s failed=%s suppressed=%s",
            stats["channel"], stats["attempted"], stats["sent"],
            stats["retrying"], stats["failed"], stats["suppressed"],
        )
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Drain the Hairshalo notification queue.")
    parser.add_argument("--loop", action="store_true",
                        help="keep running instead of exiting after one pass")
    parser.add_argument("--interval", type=float, default=30.0,
                        help="seconds between passes when looping (default 30)")
    parser.add_argument("--limit", type=int, default=None,
                        help=f"messages per pass (default NOTIFY_BATCH_SIZE={settings.NOTIFY_BATCH_SIZE})")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    logger.info("Notification worker starting — channel=%s, dispatch=%s",
                notify.get_channel().name, settings.NOTIFY_DISPATCH)

    if not args.loop:
        stats = run_once(args.limit)
        return 1 if stats.get("error") else 0

    while not _stop:
        run_once(args.limit)
        # Sleep in short slices so a signal is acted on promptly.
        waited = 0.0
        while waited < args.interval and not _stop:
            time.sleep(min(0.5, args.interval - waited))
            waited += 0.5
    logger.info("Notification worker stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
