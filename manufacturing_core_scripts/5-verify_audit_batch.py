#!/usr/bin/env python3
"""
verify_audit_batch_gui_gist.py

GUI wrapper for verifying:
1. a selected local public batch JSON
2. optionally the corresponding GitHub Gist-anchored batch JSON using github_token.txt
3. optionally a local audit bundle

Features:
- select local public batch_*.json
- optional GitHub token selector to retrieve the anchored gist batch
- optional local audit bundle selector (folder or zip)
- report shows local public verification, gist retrieval/comparison, and local bundle comparison

Run:
    python verify_audit_batch_gui_gist.py
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import requests
except ImportError:
    requests = None


REGISTRY_FILE_NAME = "batch_registry.json"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_text(json.dumps(data, sort_keys=True, separators=(",", ":")))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def compute_leaf_hash(rel_job_folder: str, manifest: Dict[str, Any]) -> str:
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


def build_merkle_tree(leaf_hashes: List[str]) -> Tuple[str, List[List[str]], List[List[Dict[str, str]]]]:
    if not leaf_hashes:
        raise ValueError("Cannot build a Merkle tree with zero leaves.")

    levels: List[List[str]] = [leaf_hashes[:]]
    current = leaf_hashes[:]

    while len(current) > 1:
        next_level: List[str] = []
        i = 0
        while i < len(current):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else current[i]
            next_level.append(sha256_text(left + right))
            i += 2
        levels.append(next_level)
        current = next_level

    root = levels[-1][0]

    proofs: List[List[Dict[str, str]]] = []
    for leaf_index in range(len(leaf_hashes)):
        proof: List[Dict[str, str]] = []
        idx = leaf_index
        for level in levels[:-1]:
            if idx % 2 == 0:
                sibling_index = idx + 1 if idx + 1 < len(level) else idx
                sibling_position = "right"
            else:
                sibling_index = idx - 1
                sibling_position = "left"
            proof.append({
                "position": sibling_position,
                "hash": level[sibling_index],
            })
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


@dataclass
class ExtractedBundle:
    root: Path
    temp_dir: Optional[tempfile.TemporaryDirectory]


def open_local_bundle(path: Path) -> ExtractedBundle:
    if not path.exists():
        raise FileNotFoundError(f"Local bundle not found: {path}")

    if path.is_dir():
        bundle_contents = path / "audit_bundle_contents"
        if bundle_contents.exists() and bundle_contents.is_dir():
            return ExtractedBundle(root=bundle_contents.resolve(), temp_dir=None)
        return ExtractedBundle(root=path.resolve(), temp_dir=None)

    if path.suffix.lower() == ".zip":
        script_dir = Path(__file__).parent.resolve()
        parent_name = path.parent.name

        if parent_name.startswith("audit_"):
            extract_root = script_dir / parent_name
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            extract_root = script_dir / f"audit_{timestamp}"

        extract_root.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract_root)

        bundle_contents = extract_root / "audit_bundle_contents"
        if bundle_contents.exists() and bundle_contents.is_dir():
            return ExtractedBundle(root=bundle_contents.resolve(), temp_dir=None)

        return ExtractedBundle(root=extract_root.resolve(), temp_dir=None)
  

    if path.suffix.lower() == ".zip":
        script_dir = Path(__file__).parent.resolve()
        parent_name = path.parent.name

        if parent_name.startswith("audit_"):
            extract_root = script_dir / parent_name
        else:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            extract_root = script_dir / f"audit_{timestamp}"

        extract_root.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(path, "r") as zf:
            zf.extractall(extract_root)

        bundle_contents = extract_root / "audit_bundle_contents"
        if bundle_contents.exists() and bundle_contents.is_dir():
            return ExtractedBundle(root=bundle_contents.resolve(), temp_dir=None)

        return ExtractedBundle(root=extract_root.resolve(), temp_dir=None)

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
    manifests_by_rel_folder: Dict[str, Dict[str, Any]] = {}
    for manifest_path in list_manifest_files(bundle_root):
        try:
            manifest = load_json(manifest_path)
        except Exception:
            continue

        rel_job_folder = normalize_rel_job_folder(manifest_path, bundle_root)

        manifests_by_rel_folder[rel_job_folder] = {
            "manifest_path": manifest_path,
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
    findings: List[str] = []
    errors: List[str] = []
    warnings: List[str] = []

    public_jobs = public_batch.get("jobs", [])
    public_by_rel = {job.get("rel_job_folder"): job for job in public_jobs if job.get("rel_job_folder")}

    local_manifests = gather_local_manifests(bundle_root)
    local_registry_path = find_local_registry(bundle_root)
    local_batch_files = find_local_batch_files(bundle_root)

    matched = []
    missing_local = []
    mismatched_leaf_hashes = []
    extra_local = []

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
        try:
            registry = load_json(local_registry_path)
            found_batch = False
            for batch in registry.get("batches", []):
                if batch.get("batch_id") == public_batch.get("batch_id") or batch.get("batch_hash") == public_batch.get("batch_hash"):
                    found_batch = True
                    if batch.get("merkle_root") != public_batch.get("merkle_root"):
                        errors.append("Local registry contains a matching batch record, but its merkle_root does not match the public batch.")
                    else:
                        findings.append("Local registry contains a matching batch record with the same merkle_root.")
                    break
            if not found_batch:
                warnings.append("Local registry was found, but no matching batch entry was located.")
        except Exception as exc:
            warnings.append(f"Could not read local registry: {exc}")
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


def find_registry_for_public_batch(public_batch_path: Path, public_batch: Dict[str, Any]) -> Optional[Path]:
    jobs_root = public_batch.get("jobs_root")
    if jobs_root:
        registry_path = Path(jobs_root) / REGISTRY_FILE_NAME
        if registry_path.exists():
            return registry_path.resolve()

    # fallback from jobs/batches/batch_*.json
    parent = public_batch_path.parent
    if parent.name == "batches":
        candidate = parent.parent / REGISTRY_FILE_NAME
        if candidate.exists():
            return candidate.resolve()
    return None


def fetch_gist_json(api_url: str, token: str) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("The 'requests' package is not installed. Run: pip install requests")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "verify-audit-batch-gui",
    }
    response = requests.get(api_url, headers=headers, timeout=30)
    if response.status_code != 200:
        raise RuntimeError(f"GitHub API error {response.status_code}: {response.text}")

    gist = response.json()
    files = gist.get("files") or {}
    if not files:
        raise ValueError("Gist response contained no files.")

    first_file = next(iter(files.values()))
    content = first_file.get("content")
    if not content:
        raw_url = first_file.get("raw_url")
        if not raw_url:
            raise ValueError("Gist file had no inline content or raw_url.")
        raw_resp = requests.get(raw_url, headers=headers, timeout=30)
        if raw_resp.status_code != 200:
            raise RuntimeError(f"Gist raw fetch error {raw_resp.status_code}: {raw_resp.text}")
        content = raw_resp.text

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gist file content was not valid JSON: {exc}") from exc


def compare_local_public_with_gist(public_batch: Dict[str, Any], gist_anchor_payload: Dict[str, Any]) -> Dict[str, Any]:
    findings: List[str] = []
    errors: List[str] = []

    gist_batch = gist_anchor_payload.get("batch_json")
    if not isinstance(gist_batch, dict):
        raise ValueError("Gist anchor payload does not contain batch_json.")

    local_hash = sha256_json(public_batch)
    gist_hash = sha256_json(gist_batch)

    if public_batch.get("batch_id") == gist_anchor_payload.get("batch_id"):
        findings.append("Gist batch_id matches local public batch_id.")
    else:
        errors.append("Gist batch_id does not match local public batch_id.")

    if public_batch.get("batch_hash") == gist_anchor_payload.get("batch_hash"):
        findings.append("Gist batch_hash matches local public batch_hash.")
    else:
        errors.append("Gist batch_hash does not match local public batch_hash.")

    if public_batch.get("merkle_root") == gist_anchor_payload.get("merkle_root"):
        findings.append("Gist merkle_root matches local public merkle_root.")
    else:
        errors.append("Gist merkle_root does not match local public merkle_root.")

    if local_hash == gist_hash:
        findings.append("Local public batch JSON matches the gist batch_json content.")
    else:
        errors.append("Local public batch JSON does not exactly match the gist batch_json content.")

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "findings": findings,
        "gist_batch_json": gist_batch,
        "local_public_json_hash": local_hash,
        "gist_batch_json_hash": gist_hash,
    }


def run_verification(
    public_batch_path: Path,
    local_bundle_path: Optional[Path] = None,
    token_file_path: Optional[Path] = None,
) -> Dict[str, Any]:
    public_batch = load_json(public_batch_path)
    public_result = validate_public_batch(public_batch)

    report: Dict[str, Any] = {
        "public_batch_path": str(public_batch_path),
        "public_batch_verification": public_result,
        "gist_verification": None,
        "local_comparison": None,
        "overall_ok": False,
    }

    gist_ok = True
    if token_file_path:
        token = read_text_file(token_file_path)
        registry_path = find_registry_for_public_batch(public_batch_path, public_batch)
        if not registry_path:
            report["gist_verification"] = {
                "ok": False,
                "errors": ["Could not locate batch_registry.json for the selected public batch."],
                "findings": [],
            }
            gist_ok = False
        else:
            registry = load_json(registry_path)
            batches = registry.get("batches", [])
            matching = None
            for batch in batches:
                if batch.get("batch_id") == public_batch.get("batch_id") or batch.get("batch_hash") == public_batch.get("batch_hash"):
                    matching = batch
                    break

            if not matching:
                report["gist_verification"] = {
                    "ok": False,
                    "errors": ["No matching batch entry found in batch_registry.json."],
                    "findings": [f"Registry located: {registry_path}"],
                }
                gist_ok = False
            else:
                anchor = matching.get("github_anchor") or {}
                api_url = anchor.get("api_url")
                html_url = anchor.get("html_url")
                if not api_url:
                    report["gist_verification"] = {
                        "ok": False,
                        "errors": ["Matching registry entry does not contain github_anchor.api_url."],
                        "findings": [f"Registry located: {registry_path}"],
                    }
                    gist_ok = False
                else:
                    gist_payload = fetch_gist_json(api_url, token)
                    gist_result = compare_local_public_with_gist(public_batch, gist_payload)
                    gist_result["registry_path"] = str(registry_path)
                    gist_result["gist_api_url"] = api_url
                    gist_result["gist_html_url"] = html_url
                    gist_result["gist_anchor_present"] = True
                    report["gist_verification"] = gist_result
                    gist_ok = gist_result["ok"]

    local_ok = True
    bundle_handle: Optional[ExtractedBundle] = None
    try:
        if local_bundle_path:
            bundle_handle = open_local_bundle(local_bundle_path)
            local_result = compare_public_and_local(public_batch, bundle_handle.root)
            report["local_comparison"] = {
                **local_result,
                "bundle_root": str(bundle_handle.root),
            }
            local_ok = local_result["ok"]

        report["overall_ok"] = bool(public_result["ok"] and gist_ok and local_ok)
        return report
    finally:
        if bundle_handle and bundle_handle.temp_dir:
            bundle_handle.temp_dir.cleanup()


def format_report_text(report: Dict[str, Any]) -> str:
    lines: List[str] = []

    pub = report.get("public_batch_verification", {})
    lines.append("Public Verification")
    lines.append("-------------------")
    lines.append(f"Public batch path: {report.get('public_batch_path')}")
    lines.append(f"Local public batch status: {'PASS' if pub.get('ok') else 'FAIL'}")

    findings = pub.get("findings") or []
    errors = pub.get("errors") or []

    if findings:
        lines.append("Local public findings:")
        for item in findings:
            lines.append(f"  - {item}")
    if errors:
        lines.append("Local public errors:")
        for item in errors:
            lines.append(f"  - {item}")

    gist = report.get("gist_verification")
    if gist is not None:
        lines.append(f"Gist retrieval/comparison status: {'PASS' if gist.get('ok') else 'FAIL'}")
        if gist.get("registry_path"):
            lines.append(f"Registry path: {gist.get('registry_path')}")
        if gist.get("gist_html_url"):
            lines.append(f"Gist URL: {gist.get('gist_html_url')}")
        gfindings = gist.get("findings") or []
        gerrors = gist.get("errors") or []
        if gfindings:
            lines.append("Gist findings:")
            for item in gfindings:
                lines.append(f"  - {item}")
        if gerrors:
            lines.append("Gist errors:")
            for item in gerrors:
                lines.append(f"  - {item}")

    local = report.get("local_comparison")
    if local is not None:
        lines.append("")
        lines.append("Local Bundle Comparison")
        lines.append("-----------------------")
        lines.append(f"Bundle Root:   {local.get('bundle_root')}")
        lines.append(f"Status:        {'PASS' if local.get('ok') else 'FAIL'}")

        findings = local.get("findings") or []
        warnings = local.get("warnings") or []
        errors = local.get("errors") or []

        if findings:
            lines.append("Findings:")
            for item in findings:
                lines.append(f"  - {item}")
        if warnings:
            lines.append("Warnings:")
            for item in warnings:
                lines.append(f"  - {item}")
        if errors:
            lines.append("Errors:")
            for item in errors:
                lines.append(f"  - {item}")

        lines.append(f"Matched Jobs:  {len(local.get('matched_jobs', []))}")
        lines.append(f"Missing Jobs:  {len(local.get('missing_local_jobs', []))}")
        lines.append(f"Mismatches:    {len(local.get('mismatched_leaf_hashes', []))}")
        lines.append(f"Extra Local:   {len(local.get('extra_local_jobs', []))}")

    lines.append("")
    lines.append("Overall Result")
    lines.append("--------------")
    lines.append(f"Overall Status: {'PASS' if report.get('overall_ok') else 'FAIL'}")

    return "\n".join(lines)


class VerifyGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Audit Verifier")
        self.root.geometry("1080x760")
        self.root.minsize(860, 580)

        self.public_batch_var = tk.StringVar(value="")
        self.use_gist_var = tk.BooleanVar(value=False)
        self.token_file_var = tk.StringVar(value=str(Path.cwd() / "github_token.txt"))
        self.use_local_var = tk.BooleanVar(value=False)
        self.local_bundle_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Select a public batch file. Optionally enable gist retrieval and/or local bundle comparison.")

        self.last_report: Optional[Dict[str, Any]] = None
        self.build_ui()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        ttk.Label(top, text="Public Batch File", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(top, textvariable=self.public_batch_var, width=88).grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(top, text="Select Public Batch", command=self.select_public_batch).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Checkbutton(
            top,
            text="Retrieve and compare anchored GitHub gist using token file",
            variable=self.use_gist_var,
            command=self.toggle_gist_state,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 4))

        ttk.Label(top, text="GitHub Token File", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, columnspan=3, sticky="w")
        self.token_entry = ttk.Entry(top, textvariable=self.token_file_var, width=88)
        self.token_entry.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)
        self.token_btn = ttk.Button(top, text="Select Token", command=self.select_token_file)
        self.token_btn.grid(row=4, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Checkbutton(
            top,
            text="Use local audit bundle comparison",
            variable=self.use_local_var,
            command=self.toggle_local_state,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))

        ttk.Label(top, text="Local Audit Bundle (folder or zip)", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=3, sticky="w")
        self.local_entry = ttk.Entry(top, textvariable=self.local_bundle_var, width=88)
        self.local_entry.grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)

        local_buttons = ttk.Frame(top)
        local_buttons.grid(row=7, column=2, sticky="ew", padx=(8, 0), pady=4)
        self.local_folder_btn = ttk.Button(local_buttons, text="Select Folder", command=self.select_local_folder)
        self.local_folder_btn.pack(fill="x")
        self.local_zip_btn = ttk.Button(local_buttons, text="Select Zip", command=self.select_local_zip)
        self.local_zip_btn.pack(fill="x", pady=(4, 0))

        action_frame = ttk.Frame(top)
        action_frame.grid(row=8, column=0, columnspan=3, sticky="w", pady=(12, 4))
        ttk.Button(action_frame, text="Run Verification", command=self.run_verification_action).pack(side="left")
        ttk.Button(action_frame, text="Save JSON Report", command=self.save_report_action).pack(side="left", padx=(8, 0))

        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=10)

        ttk.Label(main, text="Verification Report", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.report_box = tk.Text(main, wrap="word")
        self.report_box.pack(fill="both", expand=True)

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")

        self.toggle_gist_state()
        self.toggle_local_state()

    def toggle_gist_state(self):
        state = "normal" if self.use_gist_var.get() else "disabled"
        self.token_entry.configure(state=state)
        self.token_btn.configure(state=state)
        if state == "disabled":
            self.token_file_var.set(str(Path.cwd() / "github_token.txt"))

    def toggle_local_state(self):
        state = "normal" if self.use_local_var.get() else "disabled"
        self.local_entry.configure(state=state)
        self.local_folder_btn.configure(state=state)
        self.local_zip_btn.configure(state=state)
        if state == "disabled":
            self.local_bundle_var.set("")

    def set_report_text(self, text: str):
        self.report_box.delete("1.0", "end")
        self.report_box.insert("1.0", text)

    def select_public_batch(self):
        path = filedialog.askopenfilename(
            title="Select Public Batch JSON",
            filetypes=[("Batch JSON", "batch_*.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.public_batch_var.set(path)
            self.status_var.set(f"Selected public batch: {path}")

    def select_token_file(self):
        path = filedialog.askopenfilename(
            title="Select github_token.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.token_file_var.set(path)

    def select_local_folder(self):
        path = filedialog.askdirectory(title="Select Local Audit Folder")
        if path:
            self.local_bundle_var.set(path)

    def select_local_zip(self):
        path = filedialog.askopenfilename(
            title="Select Local Audit Bundle Zip",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
        )
        if path:
            self.local_bundle_var.set(path)

    def run_verification_action(self):
        try:
            public_batch = Path(self.public_batch_var.get().strip()).resolve()
            if not public_batch.exists():
                raise FileNotFoundError("Please select a valid public batch JSON file.")

            token_path = None
            if self.use_gist_var.get():
                token_text = self.token_file_var.get().strip()
                if not token_text:
                    raise ValueError("Gist retrieval is enabled, but no token file was selected.")
                token_path = Path(token_text).resolve()
                if not token_path.exists():
                    raise FileNotFoundError(f"Token file not found: {token_path}")

            local_bundle = None
            if self.use_local_var.get():
                local_text = self.local_bundle_var.get().strip()
                if not local_text:
                    raise ValueError("Local bundle comparison is enabled, but no local bundle was selected.")
                local_bundle = Path(local_text).resolve()

            report = run_verification(public_batch, local_bundle, token_path)
            self.last_report = report
            self.set_report_text(format_report_text(report))
            self.status_var.set(f"Verification complete. Overall status: {'PASS' if report.get('overall_ok') else 'FAIL'}")
        except Exception as exc:
            self.last_report = None
            self.set_report_text(str(exc))
            self.status_var.set("Verification failed.")
            messagebox.showerror("Verification Error", str(exc))

    def save_report_action(self):
        try:
            if not self.last_report:
                raise ValueError("No verification report is available yet. Run verification first.")

            path = filedialog.asksaveasfilename(
                title="Save JSON Report",
                defaultextension=".json",
                initialfile="audit_report.json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            )
            if not path:
                return

            out_path = Path(path)
            out_path.write_text(json.dumps(self.last_report, indent=2), encoding="utf-8")
            self.status_var.set(f"Saved report to {out_path}")
            messagebox.showinfo("Report Saved", f"Saved JSON report to:\n{out_path}")
        except Exception as exc:
            messagebox.showerror("Save Report Error", str(exc))


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    VerifyGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
