"""One-time environment bootstrap for Invoice-Manager (Windows).

Run by start.bat. It:
  1. Creates the embedded PostgreSQL venv (.pgvenv) with `pgserver`.
  2. Creates the backend venv and installs requirements.txt. If the full
     install fails (most often the large paddle/paddleocr download), it
     retries with only the core packages so the app still runs (OCR just
     stays disabled until paddle is installed).
  3. Starts PostgreSQL via start_pg.py --once.

All steps are logged to setup_log.txt. PaddleOCR is imported lazily by the
backend, so a missing paddle install does NOT prevent the app from starting.
"""
import os
import sys
import time
import subprocess
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
BE = ROOT / "backend"
FE = ROOT / "frontend"
PGVENV = ROOT / ".pgvenv"
BEVENV = BE / "venv"
PGDATA = ROOT / ".pgdata"
SETUP_LOG = ROOT / "setup_log.txt"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(SETUP_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def run(cmd, **kw):
    log("RUN: " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, **kw)


def ensure_pgvenv():
    py = PGVENV / "Scripts" / "python.exe"
    if py.exists():
        t = run([str(py), "-c", "import pgserver"], capture_output=True, text=True)
        if t.returncode == 0:
            log(".pgvenv verified")
            return py
        log(".pgvenv missing pgserver; reinstalling")
    else:
        log("Creating .pgvenv ...")
        run([sys.executable, "-m", "venv", str(PGVENV)]).check_returncode()
    pip = str(PGVENV / "Scripts" / "python.exe")
    run([pip, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True, text=True)
    run([pip, "-m", "pip", "install", "pgserver"], capture_output=True, text=True).check_returncode()
    return pip


def ensure_bevenv():
    py = BEVENV / "Scripts" / "python.exe"
    sentinel = BEVENV / "INSTALLED_OK"
    if py.exists() and sentinel.exists():
        log("backend/venv already present and verified")
        return str(py)
    if py.exists():
        t = run([str(py), "-c", "import fastapi, uvicorn, sqlalchemy, openpyxl, dotenv"],
                capture_output=True, text=True)
        if t.returncode == 0:
            sentinel.write_text("ok", encoding="utf-8")
            log("backend/venv verified via import test")
            return str(py)
        log("backend/venv exists but core imports failed; reinstalling")
    else:
        log("Creating backend/venv ...")
        run([sys.executable, "-m", "venv", str(BEVENV)]).check_returncode()

    pip = str(BEVENV / "Scripts" / "python.exe")
    run([pip, "-m", "pip", "install", "--upgrade", "pip"], capture_output=True, text=True)
    req = BE / "requirements.txt"
    log("Installing requirements (first run can take several minutes) ...")
    r = run([pip, "-m", "pip", "install", "-r", str(req)], capture_output=True, text=True)
    if r.returncode != 0:
        log("Full install failed; installing core deps without OCR packages.")
        for line in (r.stderr or "").strip().splitlines()[-15:]:
            log("  pip> " + line)
        light = [
            l.strip() for l in req.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")
            and not any(l.strip().startswith(p) for p in ("paddlepaddle", "paddleocr", "opencv-python"))
        ]
        tmp = BE / "_requirements_core.txt"
        tmp.write_text("\n".join(light) + "\n", encoding="utf-8")
        run([pip, "-m", "pip", "install", "-r", str(tmp)], capture_output=True, text=True).check_returncode()
        tmp.unlink(missing_ok=True)
        log("WARNING: paddle/paddleocr not installed -> OCR disabled, app still works.")
    sentinel.write_text("ok", encoding="utf-8")
    return str(py)


def start_postgres(pgpy):
    log("Starting PostgreSQL via start_pg.py --once ...")
    r = run([str(pgpy), str(ROOT / "start_pg.py"), "--once"])
    if r.returncode != 0:
        raise RuntimeError("start_pg.py failed (rc=%d)" % r.returncode)


def main():
    if sys.version_info < (3, 10):
        log(f"ERROR: need Python 3.10+, found {sys.version_info.major}.{sys.version_info.minor}")
        sys.exit(2)
    try:
        with open(SETUP_LOG, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass
    log("Using Python " + sys.version.split()[0] + " at " + sys.executable)
    ensure_pgvenv()
    ensure_bevenv()
    start_postgres(PGVENV / "Scripts" / "python.exe")
    (ROOT / ".env_setup_done").write_text("1", encoding="utf-8")
    log("ENV READY")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL: " + str(e))
        sys.exit(1)
