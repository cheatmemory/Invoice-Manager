"""Start (or reuse) the embedded PostgreSQL used by Invoice-Manager.

The project expects PostgreSQL (docker-compose maps it to port 5435).
This machine has no Docker, so we run the PostgreSQL bundled with the pip
package "pgserver" on a fixed port and keep the connection string in
backend/.env.

The script is idempotent: run it as often as you like. It
  1. reuses the database when it is already running,
  2. clears a stale postmaster.pid left behind by an unclean stop,
  3. initializes the data directory when it is empty/fresh,
  4. starts the server and waits until it accepts connections.

Ctrl+C stops the watcher loop; the database itself keeps running.
"""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
PGDATA = ROOT / ".pgdata"
# The backend reads ".env" relative to its own working directory, which is
# backend when uvicorn is started from there. Writing to
# ROOT/backend/.env would create a stray directory and be ignored.
ENV_FILE = ROOT / "backend" / ".env"
PORT = 5435
PG_BIN = ROOT / ".pgvenv" / "Lib" / "site-packages" / "pgserver" / "pginstall" / "bin"
LOG_FILE = PGDATA / "pgserver.log"


def _run(cmd, timeout=None, **kwargs):
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, **kwargs
    )


def _run_psql(sql, timeout=10):
    env = {**os.environ, "PGPASSWORD": "postgres"}
    cmd = [
        str(PG_BIN / "psql.exe"),
        "-h", "127.0.0.1",
        "-p", str(PORT),
        "-U", "postgres",
        "-d", "postgres",
        "-v", "ON_ERROR_STOP=1",
        "-c", sql,
    ]
    return _run(cmd, env=env, timeout=timeout)


def is_running() -> bool:
    """True when the server accepts connections (fast, no pg_ctl needed)."""
    try:
        return _run_psql("SELECT 1;").returncode == 0
    except subprocess.TimeoutExpired:
        return False


def is_initialized() -> bool:
    return (PGDATA / "PG_VERSION").exists()


def postgres_process_alive():
    """True when a postgres process is running, False when none is, None
    when it could not be determined (psutil missing).

    Checking "does this pid exist" is NOT enough: Windows reuses pids, so a
    dead postmaster's pid can belong to an unrelated process minutes later
    and we would refuse to clean up the pid file forever.
    """
    try:
        import psutil
    except ImportError:
        return None

    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "postgres" in name:
            return True
    return False


def clear_stale_pidfile() -> bool:
    """Make sure postmaster.pid is not blocking a start.

    Returns True when the server may start (nothing to clean, or the stale
    pid file was removed), False when the pid in postmaster.pid points to a
    live postgres process that owns this data directory.

    This is only called after is_running() returned False, so no server is
    answering on our port - any leftover pid is therefore stale and safe to
    clear. We check the *specific* pid recorded in postmaster.pid rather than
    scanning for any "postgres" process, so an unrelated postgres instance
    (e.g. the user's own PostgreSQL on another port) does not wrongly block us.
    """
    pidfile = PGDATA / "postmaster.pid"
    if not pidfile.exists():
        return True
    try:
        pid = int(pidfile.read_text(encoding="utf-8", errors="replace")
                  .splitlines()[0])
    except (OSError, ValueError, IndexError):
        pidfile.unlink()
        return True

    alive_postgres = False
    try:
        import psutil
        try:
            proc = psutil.Process(pid)
            alive_postgres = "postgres" in (proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            alive_postgres = False
    except ImportError:
        # psutil unavailable: cannot verify the pid, but since is_running()
        # was False there is no live server on our port, so clearing is safe.
        alive_postgres = False

    if alive_postgres:
        print("WARNING: postmaster.pid points to a live postgres (pid %d); "
              "keeping it" % pid, flush=True)
        return False
    pidfile.unlink()
    print("removed stale postmaster.pid (pid %d)" % pid, flush=True)
    return True


def init_data_dir() -> None:
    import pgserver

    pgserver.get_server(PGDATA, cleanup_mode=None)
    print("data directory initialized", flush=True)


def pg_ctl_start() -> None:
    """Start the server as a detached background process and return at once.

    We launch postgres.exe directly instead of ``pg_ctl start``. pg_ctl start
    is unreliable on Windows: it opens the log file with ``-l`` while postgres
    also opens it, and the resulting sharing violation makes pg_ctl hang forever
    (it never returns, so the launcher blocks and the whole app fails to start).
    Launching postgres.exe detached avoids that entirely and lets the server
    survive the launcher process exiting. Logs go to PGDATA/log via the
    built-in logging collector.
    """
    log_dir = PGDATA / "pgserver_logs"
    if log_dir.exists() and not log_dir.is_dir():
        # A stray file from an earlier failed launch may sit at this path.
        log_dir.unlink()
    log_dir.mkdir(parents=True, exist_ok=True)
    postgres_exe = PG_BIN / "postgres.exe"
    subprocess.Popen(
        [
            str(postgres_exe),
            "-D", str(PGDATA),
            "-h", "127.0.0.1",
            "-p", str(PORT),
            "-c", "logging_collector=on",
            "-c", f"log_directory={log_dir}",
        ],
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        | getattr(subprocess, "DETACHED_PROCESS", 0),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_ready(seconds: int = 180) -> bool:
    """Poll until the server accepts connections.

    The budget is a real deadline (a single probe may itself be slow), and
    progress is printed so a slow start is visible instead of a hang.
    """
    deadline = time.monotonic() + seconds
    started = time.monotonic()
    while time.monotonic() < deadline:
        if is_running():
            return True
        elapsed = time.monotonic() - started
        if elapsed % 15 < 1:
            print(f"  waiting for postgres ... {int(elapsed)}s", flush=True)
        time.sleep(1)
    return False


def tail_log(lines: int = 15) -> str:
    log_dir = PGDATA / "pgserver_logs"
    if log_dir.is_dir():
        files = sorted(
            log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if files:
            text = files[0].read_text(errors="replace").strip().splitlines()
            return "\n--- last log lines ---\n" + "\n".join(text[-lines:])
    if LOG_FILE.exists():
        text = LOG_FILE.read_text(errors="replace").strip().splitlines()
        return "\n--- last log lines ---\n" + "\n".join(text[-lines:])
    return ""


def write_env() -> None:
    """Merge DATABASE_URL into backend/.env without touching anything else.

    This file is rewritten on every startup, so it MUST NOT clobber the file:
    it holds the user's LLM API keys, which are only ever entered through the
    web UI. Overwriting it silently destroys that configuration.
    """
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    connection_url = (
        f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{PORT}/invoice_db"
    )

    lines: list[str] = []
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines()

    def key_of(line: str) -> str | None:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            return None
        return stripped.split("=", 1)[0].strip()

    replaced = False
    out: list[str] = []
    for line in lines:
        if key_of(line) == "DATABASE_URL":
            out.append(f"DATABASE_URL={connection_url}")
            replaced = True
        else:
            out.append(line)

    if not replaced:
        # Only seed the file on a truly fresh install.
        out.insert(0, f"DATABASE_URL={connection_url}")

    if not any(key_of(l) == "DEBUG" for l in out):
        out.append("DEBUG=true")

    ENV_FILE.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8")
    preserved = [
        k for k in (key_of(l) for l in out)
        if k and k.endswith("_API_KEY")
    ]
    print("updated", ENV_FILE, flush=True)
    if preserved:
        print("preserved existing keys: " + ", ".join(sorted(set(preserved))),
              flush=True)
    else:
        print("note: no LLM API key in .env yet - add one in the web UI",
              flush=True)
    print("DATABASE_URL=" + connection_url, flush=True)


def main() -> int:
    # "--once": ensure the database is up and exit, leaving no background
    # process behind. Without it the script also stays alive as a watcher.
    watch = "--once" not in sys.argv[1:]

    if is_running():
        print(f"postgres already running on port {PORT}", flush=True)
    else:
        if not is_initialized():
            print("data directory not initialized, running initdb ...", flush=True)
            init_data_dir()
        if not clear_stale_pidfile():
            print("WARNING: postmaster.pid exists and its process is alive; "
                  "refusing to remove it. Start the server manually.", flush=True)
            return 1
        print("starting postgres ...", flush=True)
        pg_ctl_start()
        if not wait_for_ready():
            print("server did not become ready:", flush=True)
            print(tail_log(), flush=True)
            return 1
        print("postgres started on port", PORT, flush=True)

    if _run_psql("SELECT 1;").returncode != 0:
        print("connection check failed", flush=True)
        print(tail_log(), flush=True)
        return 1
    print("connection to postgres OK", flush=True)

    try:
        _run_psql("ALTER USER postgres PASSWORD 'postgres';")
    except subprocess.TimeoutExpired:
        pass
    # _run() does not pass check=True, so CalledProcessError is never raised
    # here - the return code has to be inspected explicitly.
    created = _run_psql("CREATE DATABASE invoice_db;")
    if created.returncode == 0:
        print("database invoice_db created", flush=True)
    elif "already exists" in (created.stderr or ""):
        print("database invoice_db already exists (data preserved)", flush=True)
    else:
        detail = (created.stderr or "").strip() or "unknown error"
        print("could not create invoice_db: " + detail.splitlines()[-1], flush=True)

    write_env()
    if not watch:
        return 0

    print("Ctrl+C to stop this watcher (the database keeps running)", flush=True)
    while True:
        time.sleep(30)


if __name__ == "__main__":
    sys.exit(main())
