import subprocess
import sys
import time
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watcher] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("watcher")

POLL_INTERVAL = 30   # seconds between git pull checks
RESTART_DELAY = 8    # seconds before restart — даём Telegram отпустить старый
                     # getUpdates, иначе новый инстанс ловит TelegramConflictError
CWD = os.path.dirname(os.path.abspath(__file__))


def git_pull() -> bool:
    result = subprocess.run(
        ["git", "pull"],
        capture_output=True,
        text=True,
        cwd=CWD,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        logger.error(f"git pull failed: {output}")
        return False
    logger.info(f"git pull: {output}")
    return "Already up to date." not in output


def start_bot() -> subprocess.Popen:
    logger.info("Starting main.py ...")
    return subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=CWD,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )


def stop_bot(proc: subprocess.Popen):
    # На Windows terminate() убивает только main.py, оставляя дочерние Chrome
    # от Playwright осиротевшими — они копятся и жрут RAM. taskkill /T /F
    # валит всё дерево процессов разом.
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("Bot did not stop gracefully — killing")
        proc.kill()
        proc.wait()


def main():
    proc = start_bot()

    while True:
        time.sleep(POLL_INTERVAL)

        # Auto-restart if bot crashed
        if proc.poll() is not None:
            logger.warning(f"Bot exited (code={proc.returncode}) — restarting in {RESTART_DELAY}s ...")
            time.sleep(RESTART_DELAY)
            proc = start_bot()
            continue

        try:
            has_changes = git_pull()
        except Exception as e:
            logger.error(f"git pull error: {e}")
            continue

        if has_changes:
            logger.info("Changes pulled — restarting bot ...")
            stop_bot(proc)
            time.sleep(RESTART_DELAY)
            proc = start_bot()


if __name__ == "__main__":
    main()
