from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import (
    Flask, flash, g, redirect, render_template, request,
    send_from_directory, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

APP_ROOT = Path(__file__).parent.resolve()
INSTANCE_DIR = APP_ROOT / "instance"
UPLOAD_ROOT = INSTANCE_DIR / "uploads"
REPORT_ROOT = INSTANCE_DIR / "reports"
DB_PATH = INSTANCE_DIR / "portal.db"

ALLOWED_BATCH_EXTENSIONS = {".json"}
ALLOWED_BUNDLE_EXTENSIONS = {".zip", ".json"}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-now")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dirs() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    REPORT_ROOT.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    ensure_dirs()
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('manufacturer','auditor')),
        facility_id TEXT,
        instrument_id TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        facility_id TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        upload_type TEXT NOT NULL CHECK(upload_type IN ('batch','audit_bundle')),
        original_filename TEXT NOT NULL,
        stored_path TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_uuid TEXT UNIQUE NOT NULL,
        facility_id TEXT NOT NULL,
        instrument_id TEXT NOT NULL,
        batch_upload_id INTEGER NOT NULL,
        audit_upload_id INTEGER,
        overall_ok INTEGER NOT NULL,
        stored_path TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(batch_upload_id) REFERENCES uploads(id),
        FOREIGN KEY(audit_upload_id) REFERENCES uploads(id)
    );
    """)
    db.commit()

    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()[0] == 0:
        users = [
            ("manufacturer1", generate_password_hash("demo123"), "manufacturer", "ShopA", "Printer07", now_iso()),
            ("manufacturer2", generate_password_hash("demo123"), "manufacturer", "ForgeWorks", "Metal01", now_iso()),
            ("auditor1", generate_password_hash("demo123"), "auditor", None, None, now_iso()),
        ]
        cur.executemany(
            "INSERT INTO users (username, password_hash, role, facility_id, instrument_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            users,
        )
        db.commit()
    db.close()


def sanitize_component(text: str) -> str:
    cleaned = []
    for ch in str(text):
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (" ", "-", "_"):
            cleaned.append("_")
    result = "".join(cleaned).strip("_")
    return result or "unknown"


def facility_instrument_root(facility_id: str, instrument_id: str) -> Path:
    return UPLOAD_ROOT / sanitize_component(facility_id) / sanitize_component(instrument_id)


def save_uploaded_file(file_storage, facility_id: str, instrument_id: str, upload_type: str) -> Tuple[str, str]:
    filename = secure_filename(file_storage.filename or "")
    if not filename:
        raise ValueError("No file was selected.")
    ext = Path(filename).suffix.lower()
    allowed = ALLOWED_BATCH_EXTENSIONS if upload_type == "batch" else ALLOWED_BUNDLE_EXTENSIONS
    if ext not in allowed:
        raise ValueError(f"Invalid file type for {upload_type}: {ext}")

    root = facility_instrument_root(facility_id, instrument_id) / ("batches" if upload_type == "batch" else "audit_bundles")
    root.mkdir(parents=True, exist_ok=True)
    unique_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{filename}"
    path = root / unique_name
    file_storage.save(path)
    return filename, str(path.relative_to(APP_ROOT))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_text(json.dumps(data, sort_keys=True, separators=(",", ":")))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class ExtractedBundle:
    root: Path
    temp_dir: Optional[tempfile.TemporaryDirectory]


def build_merkle_tree(leaf_hashes: List[str]) -> Tuple[str, List[List[str]], List[List[Dict[str, str]]]]:
    if not leaf_hashes:
        raise ValueError("Cannot build a Merkle tree with zero leaves.")
    levels = [leaf_hashes[:]]
    current = leaf_hashes[:]
    while len(current) > 1:
        next_level = []
        i = 0
        while i < len(current):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else current[i]
            next_level.append(sha256_text(left + right))
            i += 2
        levels.append(next_level)
        current = next_level
    root = levels[-1][0]
    proofs = []
    for leaf_index in range(len(leaf_hashes)):
        proof = []
        idx = leaf_index
        for level in levels[:-1]:
            if idx % 2 == 0:
                sibling_index = idx + 1 if idx + 1 < len(level) else idx
                sibling_position = "right"
            else:
                sibling_index = idx - 1
                sibling_position = "left"
            proof.append({"position": sibling_position, "hash": level[sibling_index]})
            idx //= 2
        proofs.append(proof)
    return root, levels, proofs


def verify_merkle_proof(leaf_hash: str, proof: List[Dict[str, str]], expected_root: str) -> bool:
    current = leaf_hash
    for step in proof:
        sibling_hash = step["hash"]
        position = step["position"]
        if position == "left":
            current = sha256_text(sibling_hash + current)
        elif position == "right":
            current = sha256_text(current + sibling_hash)
        else:
            return False
    return current == expected_root


def validate_public_batch(batch: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[str] = []
    errors: List[str] = []
    required = ["batch_id", "merkle_root", "batch_hash", "job_count", "jobs"]
    for field in required:
        if field not in batch:
            errors.append(f"Missing required field: {field}")
    jobs = batch.get("jobs", [])
    if not isinstance(jobs, list):
        errors.append("Field 'jobs' is not a list")
        jobs = []
    if errors:
        return {"ok": False, "errors": errors, "findings": findings}

    leaf_hashes = []
    proof_failures = []
    rel_job_folders = []
    for idx, job in enumerate(jobs):
        rel_job_folder = job.get("rel_job_folder")
        leaf_hash = job.get("leaf_hash")
        proof = job.get("proof")
        rel_job_folders.append(rel_job_folder)
        if not leaf_hash:
            proof_failures.append(f"Job {idx}: missing leaf_hash")
            continue
        if not isinstance(proof, list):
            proof_failures.append(f"Job {idx}: missing or invalid proof")
            continue
        leaf_hashes.append(leaf_hash)
        if not verify_merkle_proof(leaf_hash, proof, batch["merkle_root"]):
            proof_failures.append(f"Job {idx}: proof does not resolve to batch merkle_root")

    if len(jobs) != batch.get("job_count"):
        errors.append(f"job_count mismatch: batch says {batch.get('job_count')} but jobs list has {len(jobs)}")

    recomputed_root = None
    if leaf_hashes:
        recomputed_root, _, _ = build_merkle_tree(leaf_hashes)
        if recomputed_root != batch["merkle_root"]:
            errors.append(f"Merkle root mismatch: batch has {batch['merkle_root']} but recomputed root is {recomputed_root}")

    if proof_failures:
        errors.extend(proof_failures)

    summary_payload = {
        "batch_id": batch.get("batch_id"),
        "description": batch.get("description"),
        "created_at": batch.get("created_at"),
        "jobs_root": batch.get("jobs_root"),
        "job_count": len(jobs),
        "merkle_root": batch.get("merkle_root"),
        "previous_batch_hash": batch.get("previous_batch_hash"),
        "job_identity_hashes": [job.get("job_identity_hash") for job in jobs],
        "job_rel_folders": rel_job_folders,
    }
    recomputed_batch_hash = sha256_json(summary_payload)
    if batch.get("batch_hash") != recomputed_batch_hash:
        errors.append(f"Batch hash mismatch: batch has {batch.get('batch_hash')} but recomputed hash is {recomputed_batch_hash}")

    if batch.get("scan_report", {}).get("status_counts"):
        findings.append("Scan report is present.")
    if batch.get("previous_batch_hash"):
        findings.append("Batch chaining field is present.")
    if batch.get("tree_levels"):
        findings.append("Merkle tree levels are present.")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "findings": findings,
        "recomputed_merkle_root": recomputed_root,
        "recomputed_batch_hash": recomputed_batch_hash,
    }


def compute_leaf_hash(rel_job_folder: str, manifest: Dict[str, Any]) -> str:
    # current schema includes instrument_id
    leaf_payload = {
        "rel_job_folder": rel_job_folder,
        "job_hash": manifest.get("job_hash"),
        "settings_hash": manifest.get("settings_hash"),
        "source_stl_hash": manifest.get("source_stl_hash"),
        "shape": manifest.get("shape"),
        "dimensions": {
            "width_mm": manifest.get("width_mm"),
            "depth_mm": manifest.get("depth_mm"),
            "height_mm": manifest.get("height_mm"),
        },
        "operator_id": manifest.get("operator_id"),
        "facility_id": manifest.get("facility_id"),
        "instrument_id": manifest.get("instrument_id"),
        "material_declaration": manifest.get("material_declaration"),
        "estimated_time_min": manifest.get("estimated_time_min"),
        "estimated_material_g": manifest.get("estimated_material_g"),
        "exported_at_utc": manifest.get("exported_at_utc"),
        "log_entry_hash": ((manifest.get("log_entry") or {}).get("entry_hash")),
    }
    return sha256_json(leaf_payload)


def open_local_bundle(path: Path) -> ExtractedBundle:
    if not path.exists():
        raise FileNotFoundError(f"Local bundle not found: {path}")
    if path.is_dir():
        bundle_contents = path / "audit_bundle_contents"
        if bundle_contents.exists() and bundle_contents.is_dir():
            return ExtractedBundle(root=bundle_contents.resolve(), temp_dir=None)
        return ExtractedBundle(root=path.resolve(), temp_dir=None)
    if path.suffix.lower() == ".zip":
        temp_dir = tempfile.TemporaryDirectory()
        extract_root = Path(temp_dir.name)
        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract_root)
        bundle_contents = extract_root / "audit_bundle_contents"
        if bundle_contents.exists() and bundle_contents.is_dir():
            return ExtractedBundle(root=bundle_contents.resolve(), temp_dir=temp_dir)
        return ExtractedBundle(root=extract_root.resolve(), temp_dir=temp_dir)
    raise ValueError("Local bundle must be a directory or a .zip file")


def list_manifest_files(root: Path) -> List[Path]:
    return sorted(root.rglob("*_manifest.json"))


def normalize_rel_job_folder(manifest_path: Path, bundle_root: Path) -> str:
    parts = manifest_path.parent.parts
    if "selected_jobs" in parts:
        idx = parts.index("selected_jobs")
        return "/".join(parts[idx + 1:])
    return str(manifest_path.parent.relative_to(bundle_root)).replace("\\", "/")


def gather_local_manifests(bundle_root: Path) -> Dict[str, Dict[str, Any]]:
    manifests_by_rel_folder = {}
    for manifest_path in list_manifest_files(bundle_root):
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue
        rel_job_folder = normalize_rel_job_folder(manifest_path, bundle_root)
        manifests_by_rel_folder[rel_job_folder] = {
            "manifest_path": str(manifest_path),
            "manifest": manifest,
            "computed_leaf_hash": compute_leaf_hash(rel_job_folder, manifest),
        }
    return manifests_by_rel_folder


def find_local_registry(bundle_root: Path) -> Optional[Path]:
    matches = list(bundle_root.rglob("batch_registry.json"))
    return matches[0] if matches else None


def find_local_batch_files(bundle_root: Path) -> List[Path]:
    return sorted(bundle_root.rglob("batch_*.json"))


def compare_public_and_local(public_batch: Dict[str, Any], bundle_root: Path) -> Dict[str, Any]:
    findings, errors, warnings = [], [], []
    public_jobs = public_batch.get("jobs", [])
    public_by_rel = {job.get("rel_job_folder"): job for job in public_jobs if job.get("rel_job_folder")}
    local_manifests = gather_local_manifests(bundle_root)
    local_registry_path = find_local_registry(bundle_root)
    local_batch_files = find_local_batch_files(bundle_root)

    matched, missing_local, mismatched_leaf_hashes, extra_local = [], [], [], []
    for rel_job_folder, public_job in public_by_rel.items():
        local_item = local_manifests.get(rel_job_folder)
        if not local_item:
            missing_local.append(rel_job_folder)
            continue
        if local_item["computed_leaf_hash"] != public_job.get("leaf_hash"):
            mismatched_leaf_hashes.append({
                "rel_job_folder": rel_job_folder,
                "public_leaf_hash": public_job.get("leaf_hash"),
                "local_leaf_hash": local_item["computed_leaf_hash"],
            })
        else:
            matched.append(rel_job_folder)

    for rel_job_folder in local_manifests.keys():
        if rel_job_folder not in public_by_rel:
            extra_local.append(rel_job_folder)

    if local_registry_path:
        findings.append(f"Local registry found: {local_registry_path}")
    else:
        warnings.append("No local batch_registry.json found in the uploaded bundle.")
    if local_batch_files:
        findings.append(f"Local batch file count: {len(local_batch_files)}")
    else:
        warnings.append("No local batch_*.json files found in the uploaded bundle.")
    if missing_local:
        errors.append(f"Missing local job folders/manifests for {len(missing_local)} public jobs.")
    if mismatched_leaf_hashes:
        errors.append(f"Leaf hash mismatches found for {len(mismatched_leaf_hashes)} jobs.")
    if extra_local:
        warnings.append(f"{len(extra_local)} local job folders were present but not included in the public batch.")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "findings": findings,
        "matched_jobs": matched,
        "missing_local_jobs": missing_local,
        "mismatched_leaf_hashes": mismatched_leaf_hashes,
        "extra_local_jobs": extra_local,
    }


def run_verification(public_batch_path: Path, local_bundle_path: Optional[Path] = None) -> Dict[str, Any]:
    public_batch = load_json(public_batch_path)
    public_result = validate_public_batch(public_batch)
    report = {
        "public_batch_path": str(public_batch_path),
        "public_batch_verification": public_result,
        "local_comparison": None,
        "overall_ok": False,
    }
    local_ok = True
    bundle_handle = None
    try:
        if local_bundle_path:
            bundle_handle = open_local_bundle(local_bundle_path)
            local_result = compare_public_and_local(public_batch, bundle_handle.root)
            report["local_comparison"] = {**local_result, "bundle_root": str(bundle_handle.root)}
            local_ok = local_result["ok"]
        report["overall_ok"] = bool(public_result["ok"] and local_ok)
        return report
    finally:
        if bundle_handle and bundle_handle.temp_dir:
            bundle_handle.temp_dir.cleanup()


def report_to_text(report: Dict[str, Any]) -> str:
    lines = []
    pub = report.get("public_batch_verification", {})
    lines.append("Public Verification")
    lines.append("-------------------")
    lines.append(f"Public batch path: {report.get('public_batch_path')}")
    lines.append(f"Status: {'PASS' if pub.get('ok') else 'FAIL'}")
    for section, label in [("findings", "Findings"), ("errors", "Errors")]:
        items = pub.get(section) or []
        if items:
            lines.append(f"{label}:")
            for item in items:
                lines.append(f"  - {item}")
    local = report.get("local_comparison")
    if local:
        lines.append("")
        lines.append("Local Bundle Comparison")
        lines.append("-----------------------")
        lines.append(f"Bundle Root: {local.get('bundle_root')}")
        lines.append(f"Status: {'PASS' if local.get('ok') else 'FAIL'}")
        for section, label in [("findings", "Findings"), ("warnings", "Warnings"), ("errors", "Errors")]:
            items = local.get(section) or []
            if items:
                lines.append(f"{label}:")
                for item in items:
                    lines.append(f"  - {item}")
        lines.append(f"Matched Jobs: {len(local.get('matched_jobs', []))}")
        lines.append(f"Missing Jobs: {len(local.get('missing_local_jobs', []))}")
        lines.append(f"Mismatches: {len(local.get('mismatched_leaf_hashes', []))}")
        lines.append(f"Extra Local: {len(local.get('extra_local_jobs', []))}")
    lines.append("")
    lines.append("Overall Result")
    lines.append("--------------")
    lines.append(f"Overall Status: {'PASS' if report.get('overall_ok') else 'FAIL'}")
    return "\\n".join(lines)

# ---------------------------
# AUTH HELPERS (ADD THIS)
# ---------------------------

def get_user(username: str):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def current_user():
    username = session.get("username")
    return get_user(username) if username else None


def login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(view):
        from functools import wraps
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user["role"] not in roles:
                flash("Access denied.", "error")
                return redirect(url_for("index"))
            return view(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------
# END AUTH HELPERS (ADD THIS)
# ---------------------------


@app.route("/")
@login_required
def index():
    user = current_user()
    db = get_db()
    uploads_count = db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
    reports_count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    recent_reports = db.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 5").fetchall()
    rows = db.execute("SELECT facility_id, instrument_id, COUNT(*) AS count FROM uploads GROUP BY facility_id, instrument_id ORDER BY facility_id, instrument_id").fetchall()
    hierarchy = rows
    return render_template("index.html", user=user, uploads_count=uploads_count, reports_count=reports_count, recent_reports=recent_reports, hierarchy=hierarchy)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_user(username)
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Invalid username or password.", "error")
            return render_template("login.html")
        session["username"] = user["username"]
        flash("Logged in.", "success")
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("login"))


@app.route("/manufacturer")
@login_required
@role_required("manufacturer")
def manufacturer_dashboard():
    user = current_user()
    db = get_db()
    uploads = db.execute("SELECT * FROM uploads WHERE facility_id = ? ORDER BY created_at DESC", (user["facility_id"],)).fetchall()
    reports = db.execute("SELECT * FROM reports WHERE facility_id = ? ORDER BY created_at DESC", (user["facility_id"],)).fetchall()
    return render_template("manufacturer_dashboard.html", user=user, uploads=uploads, reports=reports)


@app.route("/manufacturer/upload_batch", methods=["POST"])
@login_required
@role_required("manufacturer")
def upload_batch():
    user = current_user()
    instrument_id = request.form.get("instrument_id", "").strip() or (user["instrument_id"] or "")
    uploaded = request.files.get("batch_file")
    if not instrument_id or not uploaded or not uploaded.filename:
        flash("Instrument ID and batch file are required.", "error")
        return redirect(url_for("manufacturer_dashboard"))
    original_name, stored_rel = save_uploaded_file(uploaded, user["facility_id"], instrument_id, "batch")
    db = get_db()
    db.execute(
        "INSERT INTO uploads (facility_id, instrument_id, upload_type, original_filename, stored_path, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["facility_id"], instrument_id, "batch", original_name, stored_rel, user["username"], now_iso())
    )
    db.commit()
    flash("Batch uploaded.", "success")
    return redirect(url_for("manufacturer_dashboard"))


@app.route("/manufacturer/upload_audit_bundle", methods=["POST"])
@login_required
@role_required("manufacturer")
def upload_audit_bundle():
    user = current_user()
    instrument_id = request.form.get("instrument_id", "").strip() or (user["instrument_id"] or "")
    uploaded = request.files.get("audit_bundle")
    if not instrument_id or not uploaded or not uploaded.filename:
        flash("Instrument ID and audit bundle are required.", "error")
        return redirect(url_for("manufacturer_dashboard"))
    original_name, stored_rel = save_uploaded_file(uploaded, user["facility_id"], instrument_id, "audit_bundle")
    db = get_db()
    db.execute(
        "INSERT INTO uploads (facility_id, instrument_id, upload_type, original_filename, stored_path, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["facility_id"], instrument_id, "audit_bundle", original_name, stored_rel, user["username"], now_iso())
    )
    db.commit()
    flash("Audit bundle uploaded.", "success")
    return redirect(url_for("manufacturer_dashboard"))


@app.route("/auditor")
@login_required
@role_required("auditor")
def auditor_dashboard():
    db = get_db()
    uploads = db.execute("SELECT * FROM uploads ORDER BY facility_id, instrument_id, created_at DESC").fetchall()
    grouped = {}
    for u in uploads:
        key = (u["facility_id"], u["instrument_id"])
        grouped.setdefault(key, {"batches": [], "bundles": []})
        if u["upload_type"] == "batch":
            grouped[key]["batches"].append(u)
        else:
            grouped[key]["bundles"].append(u)
    reports = db.execute("SELECT * FROM reports ORDER BY created_at DESC").fetchall()
    return render_template("auditor_dashboard.html", grouped=grouped, reports=reports)


@app.route("/auditor/verify", methods=["POST"])
@login_required
@role_required("auditor")
def auditor_verify():
    db = get_db()
    batch_upload_id = request.form.get("batch_upload_id", "").strip()
    audit_upload_id = request.form.get("audit_upload_id", "").strip() or None
    if not batch_upload_id:
        flash("A public batch selection is required.", "error")
        return redirect(url_for("auditor_dashboard"))

    batch_upload = db.execute("SELECT * FROM uploads WHERE id = ?", (batch_upload_id,)).fetchone()
    if not batch_upload:
        flash("Selected batch upload was not found.", "error")
        return redirect(url_for("auditor_dashboard"))

    audit_upload = None
    local_bundle_path = None
    if audit_upload_id:
        audit_upload = db.execute("SELECT * FROM uploads WHERE id = ?", (audit_upload_id,)).fetchone()
        if not audit_upload:
            flash("Selected audit bundle was not found.", "error")
            return redirect(url_for("auditor_dashboard"))
        local_bundle_path = APP_ROOT / audit_upload["stored_path"]

    public_batch_path = APP_ROOT / batch_upload["stored_path"]
    report = run_verification(public_batch_path, local_bundle_path)

    report_uuid = str(uuid.uuid4())[:8]
    report_path = REPORT_ROOT / f"{report_uuid}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    db.execute(
        "INSERT INTO reports (report_uuid, facility_id, instrument_id, batch_upload_id, audit_upload_id, overall_ok, stored_path, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_uuid,
            batch_upload["facility_id"],
            batch_upload["instrument_id"],
            batch_upload["id"],
            audit_upload["id"] if audit_upload else None,
            1 if report["overall_ok"] else 0,
            str(report_path.relative_to(APP_ROOT)),
            current_user()["username"],
            now_iso(),
        )
    )
    db.commit()

    record = db.execute("SELECT * FROM reports WHERE report_uuid = ?", (report_uuid,)).fetchone()
    return render_template("report_view.html", report=report, record=record, report_text=report_to_text(report))


@app.route("/reports/<report_uuid>")
@login_required
def report_view(report_uuid: str):
    db = get_db()
    record = db.execute("SELECT * FROM reports WHERE report_uuid = ?", (report_uuid,)).fetchone()
    if not record:
        flash("Report not found.", "error")
        return redirect(url_for("index"))
    user = current_user()
    if user["role"] == "manufacturer" and user["facility_id"] != record["facility_id"]:
        flash("You do not have access to that report.", "error")
        return redirect(url_for("index"))
    path = APP_ROOT / record["stored_path"]
    report = load_json(path)
    return render_template("report_view.html", report=report, record=record, report_text=report_to_text(report))


@app.route("/download/<path:relative_path>")
@login_required
def download(relative_path: str):
    safe_path = (APP_ROOT / relative_path).resolve()
    if APP_ROOT not in safe_path.parents and safe_path != APP_ROOT:
        flash("Invalid download path.", "error")
        return redirect(url_for("index"))
    if not safe_path.exists():
        flash("Requested file does not exist.", "error")
        return redirect(url_for("index"))
    return send_from_directory(safe_path.parent, safe_path.name, as_attachment=True)


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
