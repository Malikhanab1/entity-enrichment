"""Central Background Scrapers & Pipeline Supervisor.

Orchestrates all background scraping and ingestion services:
  1. lead-classifier         (:8001 - FastAPI intent classification)
  2. whatsappSenderService   (:3000 - Node.js Baileys WhatsApp Gateway)
  3. leadIngestion           (:8000 - Node.js/TS central router & round-robin)
  4. RedditScraper           (Python RSS / Reddit polling loop)
  5. FbScraper               (Go poller, if available)
  6. pymapscraper            (Subprocess background Google Maps discovery engine)

All leads feed the single unified CRM database at workspace/.enrichment_contacts.sqlite3.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = REPO_DIR / "workspace"
LOGS_DIR = REPO_DIR / "IngestionAutomationPipeline" / "logs"
PID_FILE = WORKSPACE_DIR / ".scrapers_pids.json"
SINGLE_DB_PATH = WORKSPACE_DIR / ".enrichment_contacts.sqlite3"

LOGS_DIR.mkdir(parents=True, exist_ok=True)
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is open and listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def is_pid_running(pid: int) -> bool:
    """Check if a process with the given PID is actively running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pids() -> dict[str, int]:
    if not PID_FILE.exists():
        return {}
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {k: int(v) for k, v in data.items() if isinstance(v, (int, str)) and str(v).isdigit()}
    except Exception:
        return {}


def _write_pids(pids: dict[str, int]) -> None:
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            json.dump(pids, f, indent=2)
    except Exception:
        pass


def get_services_status() -> dict[str, dict[str, Any]]:
    """Return live status of all background pipeline services."""
    pids = _read_pids()
    services = {
        "lead-classifier": {
            "name": "Lead Intent Classifier",
            "port": 8001,
            "type": "fastapi",
            "log": str(LOGS_DIR / "classifier.log"),
        },
        "whatsappSenderService": {
            "name": "WhatsApp Gateway Service",
            "port": 3000,
            "type": "nodejs",
            "log": str(LOGS_DIR / "whatsappSenderService.log"),
        },
        "leadIngestion": {
            "name": "Lead Ingestion & Assignment",
            "port": 8000,
            "type": "nodejs",
            "log": str(LOGS_DIR / "leadIngestion.log"),
        },
        "RedditScraper": {
            "name": "Reddit Community Scraper",
            "port": None,
            "type": "python",
            "log": str(LOGS_DIR / "redditScraper.log"),
        },
        "pymapscraper": {
            "name": "Google Maps Subprocess Scraper",
            "port": None,
            "type": "subprocess",
            "log": str(LOGS_DIR / "pymapscraper.log"),
        },
    }

    result = {}
    for key, info in services.items():
        pid = pids.get(key)
        alive = is_pid_running(pid) if pid else False
        port_open = is_port_in_use(info["port"]) if info["port"] else False

        # If port is open, service is up even if started outside this manager
        running = alive or port_open
        status_label = "Running" if running else "Stopped"

        result[key] = {
            **info,
            "pid": pid if alive else None,
            "running": running,
            "port_open": port_open,
            "status": status_label,
        }
    return result


def start_service(service_name: str) -> dict[str, Any]:
    """Start a specific background service if not already running."""
    status = get_services_status()
    if service_name not in status:
        return {"error": f"Unknown service: {service_name}"}

    info = status[service_name]
    if info["running"]:
        return {"status": "already_running", "pid": info["pid"]}

    env = os.environ.copy()
    env["COGNIVELLE_DB_PATH"] = str(SINGLE_DB_PATH)
    env["CENTRAL_DB_PATH"] = str(SINGLE_DB_PATH)
    env["DB_FILE"] = str(SINGLE_DB_PATH)
    env["PYTHONPATH"] = f"{REPO_DIR}:{REPO_DIR / 'mcp' / 'src'}:{env.get('PYTHONPATH', '')}"

    pipeline_dir = REPO_DIR / "IngestionAutomationPipeline"
    pids = _read_pids()
    log_file = LOGS_DIR / f"{service_name}.log"

    try:
        fout = open(log_file, "a", encoding="utf-8")
        proc = None

        if service_name == "lead-classifier":
            classifier_dir = pipeline_dir / "finetuning"
            if (classifier_dir / "serve.py").exists():
                cmd = ["uv", "run", "--project", str(classifier_dir), "python", "serve.py"]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(classifier_dir),
                    stdout=fout,
                    stderr=subprocess.STDOUT,
                    env=env,
                    preexec_fn=os.setpgrp,
                )

        elif service_name == "whatsappSenderService":
            wa_dir = pipeline_dir / "whatsappSenderService"
            if (wa_dir / "src" / "index.js").exists():
                cmd = ["node", "src/index.js"]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(wa_dir),
                    stdout=fout,
                    stderr=subprocess.STDOUT,
                    env=env,
                    preexec_fn=os.setpgrp,
                )

        elif service_name == "leadIngestion":
            ingest_dir = pipeline_dir / "leadIngestion"
            if (ingest_dir / "dist" / "index.js").exists():
                cmd = ["node", "dist/index.js"]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(ingest_dir),
                    stdout=fout,
                    stderr=subprocess.STDOUT,
                    env=env,
                    preexec_fn=os.setpgrp,
                )

        elif service_name == "RedditScraper":
            reddit_dir = pipeline_dir / "RedditScraper"
            if (reddit_dir / "main.py").exists():
                cmd = ["uv", "run", "python", "main.py"]
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(reddit_dir),
                    stdout=fout,
                    stderr=subprocess.STDOUT,
                    env=env,
                    preexec_fn=os.setpgrp,
                )

        if proc:
            pids[service_name] = proc.pid
            _write_pids(pids)
            return {"status": "started", "pid": proc.pid}
        else:
            return {"status": "failed", "error": "Executable script not found"}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def stop_service(service_name: str) -> dict[str, Any]:
    """Stop a background service."""
    pids = _read_pids()
    pid = pids.get(service_name)
    if not pid or not is_pid_running(pid):
        return {"status": "not_running"}

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass

    pids.pop(service_name, None)
    _write_pids(pids)
    return {"status": "stopped", "pid": pid}


def start_all_scrapers() -> dict[str, Any]:
    """Start all scraper and ingestion services sequentially."""
    order = ["lead-classifier", "whatsappSenderService", "leadIngestion", "RedditScraper"]
    results = {}
    for s in order:
        results[s] = start_service(s)
        time.sleep(0.3)
    return results


def stop_all_scrapers() -> dict[str, Any]:
    """Stop every managed background service and clear the PID file.

    Called both from the UI 'Stop All' button and from the atexit/signal
    handler that fires when the Streamlit process exits.  The function is
    intentionally defensive: it tries stop_service() for every known key in
    the PID file, then does a best-effort SIGTERM on any remaining alive PIDs
    before wiping the file so stale entries cannot confuse the next run.
    """
    all_known = ["lead-classifier", "whatsappSenderService", "leadIngestion",
                 "RedditScraper", "pymapscraper"]
    pids = _read_pids()

    results = {}
    # 1. Stop all keys currently in the PID file (may include extra subprocess names)
    for name in list(pids.keys()):
        results[name] = stop_service(name)

    # 2. For any known service whose name wasn't in the PID file, still try (no-op if stopped)
    for name in all_known:
        if name not in results:
            results[name] = stop_service(name)

    # 3. Wipe the PID file so no stale PIDs survive the shutdown
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    return results


def launch_pymapscraper_subprocess(
    query: str,
    city: str = "",
    max_places: int = 15,
    delay: float = 2.0,
    workers: int = 2,
) -> dict[str, Any]:
    """Launch pymapscraper as a background subprocess writing to the single database."""
    status = get_services_status()
    if status.get("pymapscraper", {}).get("running"):
        return {"status": "already_running", "error": "Maps scraper is already executing a search."}

    pids = _read_pids()
    log_file = LOGS_DIR / "pymapscraper.log"
    fout = open(log_file, "a", encoding="utf-8")

    full_query = f"{query} {city}".strip()
    pymap_dir = REPO_DIR / "pymapscraper"

    env = os.environ.copy()
    env["COGNIVELLE_DB_PATH"] = str(SINGLE_DB_PATH)
    env["CENTRAL_DB_PATH"] = str(SINGLE_DB_PATH)
    env["PYTHONPATH"] = f"{REPO_DIR}:{pymap_dir}:{env.get('PYTHONPATH', '')}"

    cmd = [
        sys.executable,
        str(pymap_dir / "main.py"),
        "search",
        full_query,
        "--max-places",
        str(max_places),
        "--delay",
        str(delay),
        "--workers",
        str(workers),
        "--db",
        str(SINGLE_DB_PATH),
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(pymap_dir),
            stdout=fout,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=os.setpgrp,
        )
        pids["pymapscraper"] = proc.pid
        _write_pids(pids)
        return {
            "status": "started",
            "pid": proc.pid,
            "query": full_query,
            "max_places": max_places,
            "log": str(log_file),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def get_recent_log(service_name: str, max_lines: int = 30) -> str:
    """Read the last N lines of a service's log file."""
    log_file = LOGS_DIR / f"{service_name}.log"
    if not log_file.exists():
        return "No logs recorded yet."
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-max_lines:])
    except Exception as exc:
        return f"Error reading log: {exc}"
