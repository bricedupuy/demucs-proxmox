from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import boto3
import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, HttpUrl

DATA_DIR = Path(os.getenv("DEMUX_DATA_DIR", "/var/lib/demucs-api"))
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "jobs.db"
API_KEY = os.environ["DEMUX_API_KEY"]
DEFAULT_MODEL = os.getenv("DEMUX_DEFAULT_MODEL", "htdemucs")
DEFAULT_DEVICE = os.getenv("DEMUX_DEVICE", "cpu").lower()
ALLOWED_DEVICES = {
    x.strip().lower()
    for x in os.getenv("DEMUX_ALLOWED_DEVICES", DEFAULT_DEVICE).split(",")
    if x.strip()
}
CPU_JOBS = int(os.getenv("DEMUX_CPU_JOBS", "6"))
MAX_UPLOAD = int(os.getenv("DEMUX_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
TTL_HOURS = int(os.getenv("DEMUX_JOB_TTL_HOURS", "48"))
PUBLIC_BASE_URL = os.getenv("DEMUX_PUBLIC_BASE_URL", "").rstrip("/")
TIMEZONE = ZoneInfo(os.getenv("DEMUX_TIMEZONE", "Europe/Paris"))
HQ_START_HOUR = int(os.getenv("DEMUX_HQ_START_HOUR", "2"))
HQ_END_HOUR = int(os.getenv("DEMUX_HQ_END_HOUR", "7"))
HQ_DEFAULT_MODEL = os.getenv("DEMUX_HQ_DEFAULT_MODEL", "htdemucs_ft")
WEBHOOK_MAX_ATTEMPTS = int(os.getenv("DEMUX_WEBHOOK_MAX_ATTEMPTS", "5"))
WEBHOOK_TIMEOUT = float(os.getenv("DEMUX_WEBHOOK_TIMEOUT_SECONDS", "15"))
ALLOW_PRIVATE_SOURCE_URLS = os.getenv("DEMUX_ALLOW_PRIVATE_SOURCE_URLS", "true").lower() == "true"
ALLOW_PRIVATE_CALLBACK_URLS = os.getenv("DEMUX_ALLOW_PRIVATE_CALLBACK_URLS", "false").lower() == "true"

ALLOWED_MODELS = {
    "htdemucs", "htdemucs_ft", "htdemucs_6s", "hdemucs_mmi",
    "mdx", "mdx_extra", "mdx_q", "mdx_extra_q",
}
ALLOWED_TWO_STEMS = {"vocals", "drums", "bass", "other", "guitar", "piano"}

work_queue: asyncio.PriorityQueue[tuple[int, float, str, str]] = asyncio.PriorityQueue()
queued_work: set[tuple[str, str]] = set()
queue_lock = asyncio.Lock()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _add_column(conn: sqlite3.Connection, columns: set[str], name: str, declaration: str) -> None:
    if name not in columns:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")


def init_db() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'htdemucs',
                device TEXT NOT NULL DEFAULT 'cpu',
                two_stems TEXT,
                source_name TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT,
                r2_json TEXT
            )
        """)
        columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        additions = {
            "fast_model": "TEXT NOT NULL DEFAULT 'htdemucs'",
            "fast_device": "TEXT NOT NULL DEFAULT 'cpu'",
            "fast_status": "TEXT NOT NULL DEFAULT 'queued'",
            "fast_completed_at": "REAL",
            "fast_r2_json": "TEXT",
            "hq_enabled": "INTEGER NOT NULL DEFAULT 0",
            "hq_model": "TEXT",
            "hq_device": "TEXT",
            "hq_status": "TEXT NOT NULL DEFAULT 'disabled'",
            "hq_not_before": "REAL",
            "hq_completed_at": "REAL",
            "hq_r2_json": "TEXT",
            "callback_url": "TEXT",
            "callback_secret": "TEXT",
        }
        for name, declaration in additions.items():
            _add_column(conn, columns, name, declaration)

        # Migrate v1.x rows into the staged model.
        conn.execute("UPDATE jobs SET fast_model=model WHERE fast_model IS NULL OR fast_model='' ")
        conn.execute("UPDATE jobs SET fast_device=device WHERE fast_device IS NULL OR fast_device='' ")
        conn.execute("UPDATE jobs SET fast_status=status WHERE fast_status IS NULL OR fast_status='' ")
        conn.execute("UPDATE jobs SET fast_r2_json=r2_json WHERE fast_r2_json IS NULL AND r2_json IS NOT NULL")
        conn.execute("UPDATE jobs SET fast_status='queued',status='queued' WHERE fast_status='processing'")
        conn.execute("UPDATE jobs SET hq_status='scheduled' WHERE hq_enabled=1 AND hq_status='processing'")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                event TEXT NOT NULL,
                url TEXT NOT NULL,
                secret TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL,
                last_error TEXT,
                created_at REAL NOT NULL,
                delivered_at REAL
            )
        """)


def auth(x_api_key: Annotated[str | None, Header()] = None) -> None:
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_job(job_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return row


def public_url(path: str) -> str:
    return f"{PUBLIC_BASE_URL}{path}" if PUBLIC_BASE_URL else path


def result_dir(job_id: str, quality: str) -> Path:
    return JOBS_DIR / job_id / "result" / quality


def result_files(job_id: str, quality: str) -> list[Path]:
    root = result_dir(job_id, quality)
    return sorted([p for p in root.glob("*.*") if p.is_file()]) if root.exists() else []


def stage_public(row: sqlite3.Row, quality: Literal["fast", "hq"]) -> dict:
    status = row[f"{quality}_status"]
    files = [
        {
            "name": p.name,
            "download_url": public_url(f"/api/v1/jobs/{row['id']}/files/{quality}/{p.name}"),
        }
        for p in result_files(row["id"], quality)
    ]
    r2_raw = row[f"{quality}_r2_json"]
    output = {
        "status": status,
        "model": row[f"{quality}_model"],
        "device": row[f"{quality}_device"],
        "files": files,
        "r2": json.loads(r2_raw) if r2_raw else None,
    }
    if quality == "fast":
        output["completed_at"] = row["fast_completed_at"]
    else:
        output["enabled"] = bool(row["hq_enabled"])
        output["not_before"] = row["hq_not_before"]
        output["not_before_iso"] = (
            datetime.fromtimestamp(row["hq_not_before"], TIMEZONE).isoformat()
            if row["hq_not_before"] else None
        )
        output["completed_at"] = row["hq_completed_at"]
    return output


def public_job(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "status": row["status"],
        "source_name": row["source_name"],
        "two_stems": row["two_stems"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "error": row["error"],
        "fast": stage_public(row, "fast"),
        "hq": stage_public(row, "hq"),
        "callback_configured": bool(row["callback_url"]),
    }


def validate_model_device(model: str, device: str | None) -> str:
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, f"Unsupported model: {model}")
    selected = (device or DEFAULT_DEVICE).lower()
    if selected not in ALLOWED_DEVICES:
        raise HTTPException(400, f"Device '{selected}' is not enabled on this worker")
    return selected


def validate_two_stems(two_stems: str | None) -> None:
    if two_stems and two_stems not in ALLOWED_TWO_STEMS:
        raise HTTPException(400, "Unsupported two_stems value")


def hq_window_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(TIMEZONE)
    if HQ_START_HOUR == HQ_END_HOUR:
        return True
    if HQ_START_HOUR < HQ_END_HOUR:
        return HQ_START_HOUR <= now.hour < HQ_END_HOUR
    return now.hour >= HQ_START_HOUR or now.hour < HQ_END_HOUR


def next_hq_window_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(TIMEZONE)
    start = now.replace(hour=HQ_START_HOUR, minute=0, second=0, microsecond=0)
    end = now.replace(hour=HQ_END_HOUR, minute=0, second=0, microsecond=0)
    if HQ_END_HOUR <= HQ_START_HOUR:
        # Overnight window, e.g. 22:00 -> 07:00.
        if now.hour < HQ_END_HOUR:
            start -= timedelta(days=1)
        elif now.hour >= HQ_START_HOUR:
            return now
        else:
            return start
    else:
        if start <= now < end:
            return now
    if now >= start:
        start += timedelta(days=1)
    return start


def parse_not_before(value: str | None) -> float:
    if not value:
        return next_hq_window_start().timestamp()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "hq_not_before must be an ISO-8601 datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)
    return dt.timestamp()


def create_job(
    source_name: str,
    fast_model: str,
    fast_device: str | None,
    two_stems: str | None,
    hq_enabled: bool,
    hq_model: str | None,
    hq_device: str | None,
    hq_not_before: str | None,
    callback_url: str | None,
    callback_secret: str | None,
) -> str:
    validate_two_stems(two_stems)
    selected_fast_device = validate_model_device(fast_model, fast_device)
    selected_hq_model = hq_model or HQ_DEFAULT_MODEL
    selected_hq_device = validate_model_device(selected_hq_model, hq_device or selected_fast_device) if hq_enabled else None
    hq_ts = parse_not_before(hq_not_before) if hq_enabled else None
    jid = str(uuid.uuid4())
    now = time.time()
    (JOBS_DIR / jid / "input").mkdir(parents=True)
    result_dir(jid, "fast").mkdir(parents=True)
    result_dir(jid, "hq").mkdir(parents=True)
    with db() as conn:
        conn.execute("""
            INSERT INTO jobs(
                id,status,model,device,two_stems,source_name,created_at,updated_at,
                fast_model,fast_device,fast_status,
                hq_enabled,hq_model,hq_device,hq_status,hq_not_before,
                callback_url,callback_secret
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            jid, "queued", fast_model, selected_fast_device, two_stems, source_name, now, now,
            fast_model, selected_fast_device, "queued",
            1 if hq_enabled else 0, selected_hq_model if hq_enabled else None,
            selected_hq_device, "scheduled" if hq_enabled else "disabled", hq_ts,
            callback_url, callback_secret,
        ))
    return jid


async def save_upload(upload: UploadFile, dest: Path) -> None:
    total = 0
    with dest.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_UPLOAD:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "Upload too large")
            out.write(chunk)


def _host_is_private(hostname: str) -> bool:
    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    for item in addresses:
        try:
            ip = ipaddress.ip_address(item[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def validate_remote_url(url: str, allow_private: bool, purpose: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, f"Only http/https {purpose} URLs are supported")
    if not allow_private and _host_is_private(parsed.hostname):
        raise HTTPException(400, f"Private-network {purpose} URLs are disabled")


async def download_url(url: str, dest: Path) -> None:
    validate_remote_url(url, ALLOW_PRIVATE_SOURCE_URLS, "source")
    total = 0
    timeout = httpx.Timeout(30.0, read=600.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with dest.open("wb") as out:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(413, "Remote file too large")
                    out.write(chunk)


def upload_r2(job_id: str, quality: str, files: list[Path]) -> dict | None:
    if os.getenv("DEMUX_R2_ENABLED", "false").lower() != "true":
        return None
    required = ["DEMUX_R2_ENDPOINT", "DEMUX_R2_ACCESS_KEY_ID", "DEMUX_R2_SECRET_ACCESS_KEY", "DEMUX_R2_BUCKET"]
    if not all(os.getenv(k) for k in required):
        raise RuntimeError("R2 is enabled but its configuration is incomplete")
    prefix = os.getenv("DEMUX_R2_PREFIX", "demucs").strip("/")
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["DEMUX_R2_ENDPOINT"],
        aws_access_key_id=os.environ["DEMUX_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["DEMUX_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["DEMUX_R2_BUCKET"]
    objects = {}
    for path in files:
        key = f"{prefix}/{job_id}/{quality}/{path.name}"
        client.upload_file(str(path), bucket, key)
        objects[path.stem] = {"bucket": bucket, "key": key}
    return objects


def run_demucs(job_id: str, quality: Literal["fast", "hq"]) -> None:
    row = get_job(job_id)
    job = JOBS_DIR / job_id
    inputs = list((job / "input").iterdir())
    if not inputs:
        raise RuntimeError("Input file missing")
    src = inputs[0]
    temp_out = job / f"demucs-out-{quality}"
    log_path = job / f"demucs-{quality}.log"
    model = row[f"{quality}_model"]
    device = row[f"{quality}_device"]
    cmd = [
        "/opt/demucs-api/.venv/bin/demucs",
        "-d", device,
        "-n", model,
        "-o", str(temp_out),
    ]
    if device == "cpu":
        cmd += ["-j", str(CPU_JOBS)]
    if row["two_stems"]:
        cmd += ["--two-stems", row["two_stems"]]
    cmd.append(str(src))

    with log_path.open("w") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Demucs {quality} stage failed (exit {proc.returncode}); see {log_path.name}")

    candidates = list(temp_out.rglob("*.wav")) + list(temp_out.rglob("*.flac")) + list(temp_out.rglob("*.mp3"))
    if not candidates:
        raise RuntimeError("Demucs produced no output files")
    out = result_dir(job_id, quality)
    for p in candidates:
        shutil.copy2(p, out / p.name)
    shutil.rmtree(temp_out, ignore_errors=True)


def webhook_payload(job_id: str, event: str) -> dict:
    row = get_job(job_id)
    return {
        "event": event,
        "job_id": job_id,
        "timestamp": datetime.now(TIMEZONE).isoformat(),
        "job": public_job(row),
    }


def enqueue_notification(job_id: str, event: str) -> None:
    row = get_job(job_id)
    if not row["callback_url"]:
        return
    payload = webhook_payload(job_id, event)
    now = time.time()
    with db() as conn:
        conn.execute("""
            INSERT INTO notifications(job_id,event,url,secret,payload_json,status,attempts,next_attempt,created_at)
            VALUES(?,?,?,?,?,'pending',0,?,?)
        """, (
            job_id, event, row["callback_url"], row["callback_secret"],
            json.dumps(payload, separators=(",", ":")), now, now,
        ))


def process_stage(job_id: str, quality: Literal["fast", "hq"]) -> None:
    now = time.time()
    overall_processing = "fast_processing" if quality == "fast" else "hq_processing"
    with db() as conn:
        conn.execute(
            f"UPDATE jobs SET status=?,{quality}_status='processing',updated_at=?,error=NULL WHERE id=?",
            (overall_processing, now, job_id),
        )
    if quality == "hq":
        enqueue_notification(job_id, "hq.started")
    try:
        run_demucs(job_id, quality)
        r2 = upload_r2(job_id, quality, result_files(job_id, quality))
        finished = time.time()
        if quality == "fast":
            row = get_job(job_id)
            overall = "hq_scheduled" if row["hq_enabled"] else "completed"
            with db() as conn:
                conn.execute("""
                    UPDATE jobs SET status=?,fast_status='completed',fast_completed_at=?,fast_r2_json=?,updated_at=?
                    WHERE id=?
                """, (overall, finished, json.dumps(r2) if r2 else None, finished, job_id))
            enqueue_notification(job_id, "fast.completed")
        else:
            with db() as conn:
                conn.execute("""
                    UPDATE jobs SET status='completed',hq_status='completed',hq_completed_at=?,hq_r2_json=?,updated_at=?
                    WHERE id=?
                """, (finished, json.dumps(r2) if r2 else None, finished, job_id))
            enqueue_notification(job_id, "hq.completed")
    except Exception as exc:
        error = str(exc)[:4000]
        with db() as conn:
            if quality == "fast":
                conn.execute(
                    "UPDATE jobs SET status='failed',fast_status='failed',updated_at=?,error=? WHERE id=?",
                    (time.time(), error, job_id),
                )
            else:
                # Keep fast output usable even if the HQ upgrade fails.
                conn.execute(
                    "UPDATE jobs SET status='fast_completed',hq_status='failed',updated_at=?,error=? WHERE id=?",
                    (time.time(), error, job_id),
                )
        enqueue_notification(job_id, f"{quality}.failed")


async def enqueue_work(job_id: str, quality: Literal["fast", "hq"]) -> None:
    key = (job_id, quality)
    async with queue_lock:
        if key in queued_work:
            return
        queued_work.add(key)
        priority = 0 if quality == "fast" else 10
        await work_queue.put((priority, time.time(), job_id, quality))


async def worker() -> None:
    while True:
        _, _, job_id, quality = await work_queue.get()
        async with queue_lock:
            queued_work.discard((job_id, quality))
        try:
            row = get_job(job_id)
            expected = row[f"{quality}_status"]
            if expected in {"queued", "scheduled"}:
                if quality == "hq" and not hq_window_open():
                    continue
                await asyncio.to_thread(process_stage, job_id, quality)
        except HTTPException:
            pass
        finally:
            work_queue.task_done()


async def scheduler_loop() -> None:
    while True:
        now = time.time()
        with db() as conn:
            fast_rows = conn.execute(
                "SELECT id FROM jobs WHERE fast_status='queued' ORDER BY created_at"
            ).fetchall()
            hq_rows = []
            if hq_window_open():
                hq_rows = conn.execute("""
                    SELECT id FROM jobs
                    WHERE hq_enabled=1 AND hq_status='scheduled' AND fast_status='completed'
                      AND hq_not_before IS NOT NULL AND hq_not_before <= ?
                    ORDER BY hq_not_before, created_at
                    LIMIT 1
                """, (now,)).fetchall()
        for row in fast_rows:
            await enqueue_work(row["id"], "fast")
        for row in hq_rows:
            await enqueue_work(row["id"], "hq")
        await asyncio.sleep(10)


def _retry_delay(attempt: int) -> int:
    schedule = [30, 120, 600, 3600, 3600]
    return schedule[min(max(attempt - 1, 0), len(schedule) - 1)]


async def webhook_loop() -> None:
    while True:
        now = time.time()
        with db() as conn:
            row = conn.execute("""
                SELECT * FROM notifications
                WHERE status='pending' AND next_attempt <= ?
                ORDER BY id LIMIT 1
            """, (now,)).fetchone()
        if not row:
            await asyncio.sleep(5)
            continue

        attempts = row["attempts"] + 1
        try:
            validate_remote_url(row["url"], ALLOW_PRIVATE_CALLBACK_URLS, "callback")
            body = row["payload_json"].encode()
            headers = {"Content-Type": "application/json", "User-Agent": "demucs-api-webhook/1"}
            if row["secret"]:
                signature = hmac.new(row["secret"].encode(), body, hashlib.sha256).hexdigest()
                headers["X-Demucs-Signature"] = f"sha256={signature}"
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT, follow_redirects=False) as client:
                response = await client.post(row["url"], content=body, headers=headers)
                response.raise_for_status()
            with db() as conn:
                conn.execute(
                    "UPDATE notifications SET status='delivered',attempts=?,delivered_at=?,last_error=NULL WHERE id=?",
                    (attempts, time.time(), row["id"]),
                )
        except Exception as exc:
            status = "failed" if attempts >= WEBHOOK_MAX_ATTEMPTS else "pending"
            next_attempt = time.time() + _retry_delay(attempts)
            with db() as conn:
                conn.execute("""
                    UPDATE notifications SET status=?,attempts=?,next_attempt=?,last_error=? WHERE id=?
                """, (status, attempts, next_attempt, str(exc)[:2000], row["id"]))


async def cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        cutoff = time.time() - TTL_HOURS * 3600
        with db() as conn:
            rows = conn.execute("""
                SELECT id FROM jobs
                WHERE status IN ('completed','failed','fast_completed') AND updated_at < ?
            """, (cutoff,)).fetchall()
            for row in rows:
                shutil.rmtree(JOBS_DIR / row["id"], ignore_errors=True)
                conn.execute("DELETE FROM notifications WHERE job_id=?", (row["id"],))
                conn.execute("DELETE FROM jobs WHERE id=?", (row["id"],))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    tasks = [
        asyncio.create_task(worker()),
        asyncio.create_task(scheduler_loop()),
        asyncio.create_task(webhook_loop()),
        asyncio.create_task(cleanup_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Demucs API", version="1.3.0", lifespan=lifespan)


class StageRequest(BaseModel):
    model: str = DEFAULT_MODEL
    device: str | None = None


class HqRequest(BaseModel):
    enabled: bool = True
    model: str = HQ_DEFAULT_MODEL
    device: str | None = None
    not_before: datetime | None = None


class UrlJob(BaseModel):
    source_url: HttpUrl
    fast: StageRequest = StageRequest()
    hq: HqRequest | None = None
    two_stems: str | None = None
    callback_url: HttpUrl | None = None
    callback_secret: str | None = None


class HqScheduleRequest(BaseModel):
    model: str = HQ_DEFAULT_MODEL
    device: str | None = None
    not_before: datetime | None = None


@app.get("/api/v1/health")
def health():
    with db() as conn:
        queued_fast = conn.execute("SELECT COUNT(*) FROM jobs WHERE fast_status='queued'").fetchone()[0]
        scheduled_hq = conn.execute("SELECT COUNT(*) FROM jobs WHERE hq_status='scheduled'").fetchone()[0]
    return {
        "status": "ok",
        "queue_depth": work_queue.qsize(),
        "queued_fast": queued_fast,
        "scheduled_hq": scheduled_hq,
        "default_model": DEFAULT_MODEL,
        "hq_default_model": HQ_DEFAULT_MODEL,
        "default_device": DEFAULT_DEVICE,
        "allowed_devices": sorted(ALLOWED_DEVICES),
        "timezone": str(TIMEZONE),
        "hq_window": {"start_hour": HQ_START_HOUR, "end_hour": HQ_END_HOUR},
    }


@app.get("/api/v1/models", dependencies=[Depends(auth)])
def models():
    return {
        "models": sorted(ALLOWED_MODELS),
        "default": DEFAULT_MODEL,
        "hq_default": HQ_DEFAULT_MODEL,
        "devices": sorted(ALLOWED_DEVICES),
        "default_device": DEFAULT_DEVICE,
    }


@app.get("/api/v1/queue", dependencies=[Depends(auth)])
def queue_status():
    with db() as conn:
        rows = conn.execute("""
            SELECT id,status,source_name,fast_status,hq_status,hq_not_before,created_at
            FROM jobs
            WHERE fast_status IN ('queued','processing') OR hq_status IN ('scheduled','processing')
            ORDER BY created_at
        """).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/v1/jobs", dependencies=[Depends(auth)], status_code=202)
async def upload_job(
    file: UploadFile = File(...),
    model: str = Form(DEFAULT_MODEL),
    device: str | None = Form(None),
    two_stems: str | None = Form(None),
    hq_enabled: bool = Form(False),
    hq_model: str = Form(HQ_DEFAULT_MODEL),
    hq_device: str | None = Form(None),
    hq_not_before: str | None = Form(None),
    callback_url: str | None = Form(None),
    callback_secret: str | None = Form(None),
):
    if callback_url:
        validate_remote_url(callback_url, ALLOW_PRIVATE_CALLBACK_URLS, "callback")
    jid = create_job(
        file.filename or "upload", model, device, two_stems,
        hq_enabled, hq_model, hq_device, hq_not_before,
        callback_url, callback_secret,
    )
    dest = JOBS_DIR / jid / "input" / Path(file.filename or "input.bin").name
    try:
        await save_upload(file, dest)
    except Exception:
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
        with db() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        raise
    await enqueue_work(jid, "fast")
    return public_job(get_job(jid))


@app.post("/api/v1/jobs/url", dependencies=[Depends(auth)], status_code=202)
async def url_job(payload: UrlJob):
    source = str(payload.source_url)
    validate_remote_url(source, ALLOW_PRIVATE_SOURCE_URLS, "source")
    callback = str(payload.callback_url) if payload.callback_url else None
    if callback:
        validate_remote_url(callback, ALLOW_PRIVATE_CALLBACK_URLS, "callback")
    hq = payload.hq
    name = Path(urlparse(source).path).name or "remote-audio"
    jid = create_job(
        name,
        payload.fast.model,
        payload.fast.device,
        payload.two_stems,
        bool(hq and hq.enabled),
        hq.model if hq else None,
        hq.device if hq else None,
        hq.not_before.isoformat() if hq and hq.not_before else None,
        callback,
        payload.callback_secret,
    )
    dest = JOBS_DIR / jid / "input" / Path(name).name
    try:
        await download_url(source, dest)
    except Exception:
        shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
        with db() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        raise
    await enqueue_work(jid, "fast")
    return public_job(get_job(jid))


@app.get("/api/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def job_status(job_id: str):
    return public_job(get_job(job_id))


@app.post("/api/v1/jobs/{job_id}/hq", dependencies=[Depends(auth)], status_code=202)
async def schedule_hq(job_id: str, payload: HqScheduleRequest):
    row = get_job(job_id)
    if row["hq_status"] == "processing":
        raise HTTPException(409, "HQ processing is already running")
    device = validate_model_device(payload.model, payload.device or row["fast_device"])
    not_before = payload.not_before.timestamp() if payload.not_before else next_hq_window_start().timestamp()
    with db() as conn:
        conn.execute("""
            UPDATE jobs SET hq_enabled=1,hq_model=?,hq_device=?,hq_status='scheduled',hq_not_before=?,
                hq_completed_at=NULL,hq_r2_json=NULL,updated_at=?,status=CASE WHEN fast_status='completed' THEN 'hq_scheduled' ELSE status END
            WHERE id=?
        """, (payload.model, device, not_before, time.time(), job_id))
    return public_job(get_job(job_id))


@app.delete("/api/v1/jobs/{job_id}/hq", dependencies=[Depends(auth)])
def cancel_hq(job_id: str):
    row = get_job(job_id)
    if row["hq_status"] == "processing":
        raise HTTPException(409, "Cannot cancel HQ while it is processing")
    with db() as conn:
        conn.execute("""
            UPDATE jobs SET hq_enabled=0,hq_status='cancelled',updated_at=?,
                status=CASE WHEN fast_status='completed' THEN 'completed' ELSE status END
            WHERE id=?
        """, (time.time(), job_id))
    return public_job(get_job(job_id))


@app.get("/api/v1/jobs/{job_id}/log", dependencies=[Depends(auth)], response_class=PlainTextResponse)
def job_log_legacy(job_id: str):
    return job_log(job_id, "fast")


@app.get("/api/v1/jobs/{job_id}/log/{quality}", dependencies=[Depends(auth)], response_class=PlainTextResponse)
def job_log(job_id: str, quality: Literal["fast", "hq"]):
    get_job(job_id)
    path = JOBS_DIR / job_id / f"demucs-{quality}.log"
    if not path.exists():
        return ""
    return path.read_text(errors="replace")[-100_000:]


@app.get("/api/v1/jobs/{job_id}/files/{quality}/{filename}", dependencies=[Depends(auth)])
def job_file(job_id: str, quality: Literal["fast", "hq"], filename: str):
    get_job(job_id)
    safe = Path(filename).name
    path = result_dir(job_id, quality) / safe
    if not path.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(path, filename=safe)


@app.get("/api/v1/jobs/{job_id}/notifications", dependencies=[Depends(auth)])
def notifications(job_id: str):
    get_job(job_id)
    with db() as conn:
        rows = conn.execute("""
            SELECT id,event,status,attempts,next_attempt,last_error,created_at,delivered_at
            FROM notifications WHERE job_id=? ORDER BY id
        """, (job_id,)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.delete("/api/v1/jobs/{job_id}", dependencies=[Depends(auth)])
def delete_job(job_id: str):
    row = get_job(job_id)
    if row["fast_status"] == "processing" or row["hq_status"] == "processing":
        raise HTTPException(409, "Cannot delete a processing job")
    shutil.rmtree(JOBS_DIR / job_id, ignore_errors=True)
    with db() as conn:
        conn.execute("DELETE FROM notifications WHERE job_id=?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    return {"id": job_id, "deleted": True}
