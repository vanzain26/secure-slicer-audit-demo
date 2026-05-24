#!/usr/bin/env python3
"""
merkle_batch_jobs_tree_gui.py

GUI wrapper around the batch-centered Merkle utility.

Features:
- select structured jobs root folder
- optional description field
- checkboxes for:
  - dry run
  - include submitted
  - include description
- builds batch files and updates batch_registry.json
- shows a plain-language result summary

Run:
    python merkle_batch_jobs_tree_gui.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


REGISTRY_FILE_NAME = "batch_registry.json"
BATCHES_DIR_NAME = "batches"


def sanitize_batch_component(text: str) -> str:
    cleaned = []
    for ch in str(text):
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (" ", "-", "_"):
            cleaned.append("_")
    result = "".join(cleaned).strip("_")
    return result or "unknown"


@dataclass
class JobCandidate:
    rel_job_folder: str
    manifest_path: Path
    manifest: Dict[str, Any]
    status: str
    reason: Optional[str]
    job_identity_hash: Optional[str]
    leaf_hash: Optional[str]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_json(data: Any) -> str:
    return sha256_text(json.dumps(data, sort_keys=True, separators=(",", ":")))


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ensure_registry(jobs_root: Path) -> Path:
    registry_path = jobs_root / REGISTRY_FILE_NAME
    if not registry_path.exists():
        registry = {
            "registry_type": "batch_registry",
            "created_at": utc_now_iso(),
            "jobs_root": str(jobs_root.resolve()),
            "batches_dir": BATCHES_DIR_NAME,
            "batches": []
        }
        save_json(registry_path, registry)
    return registry_path


def load_registry(registry_path: Path) -> Dict[str, Any]:
    data = load_json(registry_path)
    if "batches" not in data or not isinstance(data["batches"], list):
        raise ValueError(f"Invalid registry format: {registry_path}")
    return data


def list_manifest_files(jobs_root: Path) -> List[Path]:
    manifests: List[Path] = []
    for path in jobs_root.rglob("*_manifest.json"):
        if BATCHES_DIR_NAME in path.parts:
            continue
        if path.name == REGISTRY_FILE_NAME:
            continue
        manifests.append(path)
    manifests.sort()
    return manifests


def rel_job_folder_from_manifest(jobs_root: Path, manifest_path: Path) -> str:
    return str(manifest_path.parent.relative_to(jobs_root)).replace("\\", "/")


def compute_job_identity_hash(rel_job_folder: str, manifest: Dict[str, Any]) -> str:
    identity = {
        "rel_job_folder": rel_job_folder,
        "job_hash": manifest.get("job_hash"),
        "settings_hash": manifest.get("settings_hash"),
        "source_stl_hash": manifest.get("source_stl_hash"),
        "shape": manifest.get("shape"),
        "operator_id": manifest.get("operator_id"),
        "instrument_id": manifest.get("instrument_id"),
        "facility_id": manifest.get("facility_id"),
        "gcode_filename": Path(manifest.get("gcode_path", "")).name,
        "manifest_filename": Path(manifest.get("manifest_path", "")).name,
    }
    return sha256_json(identity)


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


def validate_manifest(rel_job_folder: str, manifest_path: Path, manifest: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    required_fields = [
        "job_hash",
        "settings_hash",
        "shape",
        "operator_id",
        "facility_id",
        "instrument_id",
        "material_declaration",
    ]
    missing = [field for field in required_fields if not manifest.get(field)]
    if missing:
        return "invalid_manifest", f"Missing required fields: {', '.join(missing)}", None, None

    log_entry = manifest.get("log_entry")
    if not isinstance(log_entry, dict) or not log_entry.get("entry_hash"):
        return "missing_log_entry", "Manifest does not contain log_entry.entry_hash", None, None

    log_path = manifest_path.parent / "print_log.json"
    if not log_path.exists():
        return "missing_log_file", "print_log.json not found in job folder", None, None

    identity_hash = compute_job_identity_hash(rel_job_folder, manifest)
    leaf_hash = compute_leaf_hash(rel_job_folder, manifest)
    return "new", None, identity_hash, leaf_hash


def get_submitted_identity_hashes(registry: Dict[str, Any]) -> set:
    seen = set()
    for batch in registry.get("batches", []):
        for job in batch.get("jobs", []):
            identity_hash = job.get("job_identity_hash")
            if identity_hash:
                seen.add(identity_hash)
    return seen


def collect_candidates(jobs_root: Path, include_submitted: bool, submitted_identity_hashes: set) -> Tuple[List[JobCandidate], List[JobCandidate]]:
    all_candidates: List[JobCandidate] = []
    eligible: List[JobCandidate] = []

    for manifest_path in list_manifest_files(jobs_root):
        rel_job_folder = rel_job_folder_from_manifest(jobs_root, manifest_path)

        try:
            manifest = load_json(manifest_path)
            status, reason, identity_hash, leaf_hash = validate_manifest(rel_job_folder, manifest_path, manifest)
        except Exception as exc:
            all_candidates.append(JobCandidate(
                rel_job_folder=rel_job_folder,
                manifest_path=manifest_path,
                manifest={},
                status="unreadable_manifest",
                reason=str(exc),
                job_identity_hash=None,
                leaf_hash=None,
            ))
            continue

        if identity_hash and identity_hash in submitted_identity_hashes and not include_submitted:
            all_candidates.append(JobCandidate(
                rel_job_folder=rel_job_folder,
                manifest_path=manifest_path,
                manifest=manifest,
                status="already_batched",
                reason="Job identity already present in prior batch registry",
                job_identity_hash=identity_hash,
                leaf_hash=leaf_hash,
            ))
            continue

        candidate = JobCandidate(
            rel_job_folder=rel_job_folder,
            manifest_path=manifest_path,
            manifest=manifest,
            status=status,
            reason=reason,
            job_identity_hash=identity_hash,
            leaf_hash=leaf_hash,
        )
        all_candidates.append(candidate)

        if status == "new":
            eligible.append(candidate)

    return all_candidates, eligible


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


def next_batch_number(registry: Dict[str, Any]) -> int:
    nums = []
    for batch in registry.get("batches", []):
        batch_id = str(batch.get("batch_id", ""))
        match = re.search(r"(\d+)$", batch_id)
        if match:
            nums.append(int(match.group(1)))
    return (max(nums) + 1) if nums else 1


def previous_batch_hash(registry: Dict[str, Any]) -> str:
    batches = registry.get("batches", [])
    if not batches:
        return "GENESIS"
    return batches[-1].get("batch_hash", "GENESIS")


def make_batch_summary_payload(
    batch_id: str,
    description: str,
    jobs_root: Path,
    job_records: List[Dict[str, Any]],
    merkle_root: str,
    created_at: str,
    previous_hash: str,
) -> Dict[str, Any]:
    return {
        "batch_id": batch_id,
        "description": description,
        "created_at": created_at,
        "jobs_root": str(jobs_root.resolve()),
        "job_count": len(job_records),
        "merkle_root": merkle_root,
        "previous_batch_hash": previous_hash,
        "job_identity_hashes": [job["job_identity_hash"] for job in job_records],
        "job_rel_folders": [job["rel_job_folder"] for job in job_records],
    }


def run_batch_build(
    jobs_root: Path,
    description: str = "",
    include_submitted: bool = False,
    dry_run: bool = False,
) -> Dict[str, Any]:
    if not jobs_root.exists() or not jobs_root.is_dir():
        raise FileNotFoundError(f"Jobs root does not exist or is not a directory: {jobs_root}")

    registry_path = ensure_registry(jobs_root)
    registry = load_registry(registry_path)
    submitted_identity_hashes = get_submitted_identity_hashes(registry)

    all_candidates, eligible = collect_candidates(
        jobs_root=jobs_root,
        include_submitted=include_submitted,
        submitted_identity_hashes=submitted_identity_hashes,
    )

    status_counts: Dict[str, int] = {}
    for candidate in all_candidates:
        status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1

    result: Dict[str, Any] = {
        "jobs_root": str(jobs_root),
        "registry_path": str(registry_path),
        "found_candidates": len(all_candidates),
        "eligible_count": len(eligible),
        "status_counts": status_counts,
        "dry_run": dry_run,
        "batch_written": False,
    }

    if not eligible:
        result["message"] = "No eligible jobs found for batching."
        return result

    leaf_hashes = [candidate.leaf_hash for candidate in eligible if candidate.leaf_hash]
    merkle_root, levels, proofs = build_merkle_tree(leaf_hashes)

    created_at = utc_now_iso()
    batch_num = next_batch_number(registry)

    first_manifest = eligible[0].manifest if eligible else {}
    facility_id = sanitize_batch_component(first_manifest.get("facility_id", "unknown"))
    instrument_id = sanitize_batch_component(first_manifest.get("instrument_id", "unknown"))

    mismatched_identity = []
    for candidate in eligible:
        cand_facility = sanitize_batch_component(candidate.manifest.get("facility_id", "unknown"))
        cand_instrument = sanitize_batch_component(candidate.manifest.get("instrument_id", "unknown"))
        if cand_facility != facility_id or cand_instrument != instrument_id:
            mismatched_identity.append(candidate.rel_job_folder)

    batch_id = f"batch_{facility_id}_{instrument_id}_{batch_num:03d}"
    previous_hash = previous_batch_hash(registry)
    description = description or f"Submission {batch_id}"

    job_records: List[Dict[str, Any]] = []
    for idx, candidate in enumerate(eligible):
        job_records.append({
            "rel_job_folder": candidate.rel_job_folder,
            "manifest_path": str(candidate.manifest_path.resolve()),
            "job_identity_hash": candidate.job_identity_hash,
            "leaf_hash": candidate.leaf_hash,
            "proof": proofs[idx],
            "job_hash": candidate.manifest.get("job_hash"),
            "settings_hash": candidate.manifest.get("settings_hash"),
            "shape": candidate.manifest.get("shape"),
            "operator_id": candidate.manifest.get("operator_id"),
            "facility_id": candidate.manifest.get("facility_id"),
            "instrument_id": candidate.manifest.get("instrument_id"),
            "exported_at_utc": candidate.manifest.get("exported_at_utc"),
        })

    batch_summary = make_batch_summary_payload(
        batch_id=batch_id,
        description=description,
        jobs_root=jobs_root,
        job_records=job_records,
        merkle_root=merkle_root,
        created_at=created_at,
        previous_hash=previous_hash,
    )
    batch_hash = sha256_json(batch_summary)

    batch_payload = {
        "batch_type": "merkle_jobs_submission",
        "batch_id": batch_id,
        "description": description,
        "created_at": created_at,
        "jobs_root": str(jobs_root.resolve()),
        "merkle_root": merkle_root,
        "previous_batch_hash": previous_hash,
        "batch_hash": batch_hash,
        "job_count": len(job_records),
        "tree_levels": levels,
        "jobs": job_records,
        "scan_report": {
            "status_counts": status_counts,
            "all_candidates": [
                {
                    "rel_job_folder": c.rel_job_folder,
                    "manifest_path": str(c.manifest_path.resolve()),
                    "status": c.status,
                    "reason": c.reason,
                }
                for c in all_candidates
            ],
        },
    }

    batches_dir = jobs_root / BATCHES_DIR_NAME
    batch_path = batches_dir / f"{batch_id}.json"

    result.update({
        "batch_id": batch_id,
        "description": description,
        "merkle_root": merkle_root,
        "batch_hash": batch_hash,
        "batch_file": str(batch_path),
        "facility_id": first_manifest.get("facility_id", "unknown"),
        "instrument_id": first_manifest.get("instrument_id", "unknown"),
    })
    if mismatched_identity:
        result["identity_warning"] = (
            "Some eligible jobs had different facility_id/instrument_id values. "
            "Batch ID was based on the first eligible job."
        )
        result["identity_warning_jobs"] = mismatched_identity

    if dry_run:
        result["message"] = "Dry run complete. No files were written."
        return result

    batches_dir.mkdir(parents=True, exist_ok=True)
    save_json(batch_path, batch_payload)

    registry["batches"].append({
        "batch_id": batch_id,
        "created_at": created_at,
        "description": description,
        "batch_file": str(batch_path.resolve()),
        "merkle_root": merkle_root,
        "job_count": len(job_records),
        "previous_batch_hash": previous_hash,
        "batch_hash": batch_hash,
        "jobs": [
            {
                "rel_job_folder": job["rel_job_folder"],
                "job_identity_hash": job["job_identity_hash"],
                "leaf_hash": job["leaf_hash"],
            }
            for job in job_records
        ],
    })
    save_json(registry_path, registry)

    result["batch_written"] = True
    result["message"] = "Batch file written and registry updated."
    return result


class BatchGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Batch Builder")
        self.root.geometry("900x700")
        self.root.minsize(760, 560)

        self.jobs_root_var = tk.StringVar(value=os.path.join(os.getcwd(), "jobs"))
        self.include_description_var = tk.BooleanVar(value=False)
        self.description_var = tk.StringVar(value="")
        self.dry_run_var = tk.BooleanVar(value=False)
        self.include_submitted_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Choose a jobs folder, set options, then click Build Batch.")

        self.build_ui()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        controls = ttk.Frame(main)
        controls.pack(fill="x")

        ttk.Label(controls, text="Structured Jobs Root: (jobs folder)", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(controls, textvariable=self.jobs_root_var, width=70).grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(controls, text="Browse", command=self.select_jobs_root).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Separator(controls, orient="horizontal").grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        ttk.Checkbutton(
            controls,
            text="Include description",
            variable=self.include_description_var,
            command=self.toggle_description_state,
        ).grid(row=3, column=0, sticky="w")

        self.description_label = ttk.Label(controls, text="Description")
        self.description_label.grid(row=4, column=0, sticky="w", pady=(6, 0))

        self.description_entry = tk.Text(controls, height=4, width=70, wrap="word")
        self.description_entry.grid(row=5, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Checkbutton(
            controls,
            text="Dry run",
            variable=self.dry_run_var,
        ).grid(row=6, column=0, sticky="w", pady=(10, 2))

        ttk.Checkbutton(
            controls,
            text="Include submitted",
            variable=self.include_submitted_var,
        ).grid(row=7, column=0, sticky="w", pady=2)

        ttk.Button(
            controls,
            text="Build Batch",
            command=self.on_build_batch,
        ).grid(row=8, column=0, sticky="w", pady=(14, 4))

        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=1)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=10)

        ttk.Label(main, text="Result", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.result_box = tk.Text(main, height=22, wrap="word")
        self.result_box.pack(fill="both", expand=True)

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")

        self.toggle_description_state()

    def select_jobs_root(self):
        path = filedialog.askdirectory(title="Select Structured Jobs Root")
        if path:
            self.jobs_root_var.set(path)
            self.status_var.set(f"Jobs root set to {path}")

    def toggle_description_state(self):
        state = "normal" if self.include_description_var.get() else "disabled"
        self.description_entry.configure(state=state)
        if state == "disabled":
            self.description_entry.delete("1.0", "end")

    def format_result(self, result: Dict[str, Any]) -> str:
        lines = []
        lines.append(f"Jobs root: {result.get('jobs_root')}")
        lines.append(f"Registry: {result.get('registry_path')}")
        lines.append(f"Found candidates: {result.get('found_candidates')}")
        lines.append(f"Eligible for batch: {result.get('eligible_count')}")
        lines.append(f"Dry run: {result.get('dry_run')}")
        lines.append("")

        status_counts = result.get("status_counts", {})
        if status_counts:
            lines.append("Status summary:")
            for key in sorted(status_counts):
                lines.append(f"  - {key}: {status_counts[key]}")
            lines.append("")

        if result.get("batch_id"):
            lines.append(f"Batch ID: {result.get('batch_id')}")
            lines.append(f"Facility ID: {result.get('facility_id')}")
            lines.append(f"Instrument ID: {result.get('instrument_id')}")
            lines.append(f"Description: {result.get('description')}")
            lines.append(f"Merkle root: {result.get('merkle_root')}")
            lines.append(f"Batch hash: {result.get('batch_hash')}")
            lines.append(f"Batch file: {result.get('batch_file')}")
            if result.get("identity_warning"):
                lines.append(f"Warning: {result.get('identity_warning')}")
                for item in result.get("identity_warning_jobs", []):
                    lines.append(f"  - {item}")
            lines.append("")

        lines.append(result.get("message", "Done."))
        return "\n".join(lines)

    def on_build_batch(self):
        try:
            jobs_root = Path(self.jobs_root_var.get().strip()).resolve()
            description = ""
            if self.include_description_var.get():
                description = self.description_entry.get("1.0", "end").strip()

            result = run_batch_build(
                jobs_root=jobs_root,
                description=description,
                include_submitted=self.include_submitted_var.get(),
                dry_run=self.dry_run_var.get(),
            )

            self.result_box.delete("1.0", "end")
            self.result_box.insert("1.0", self.format_result(result))

            if result.get("batch_written"):
                self.status_var.set(f"Built {result.get('batch_id')} successfully.")
                messagebox.showinfo("Batch Builder", result.get("message", "Batch complete."))
            else:
                self.status_var.set(result.get("message", "No batch written."))
                if result.get("dry_run"):
                    messagebox.showinfo("Batch Builder", result.get("message", "Dry run complete."))
        except Exception as exc:
            tb = traceback.format_exc()
            self.result_box.delete("1.0", "end")
            self.result_box.insert("1.0", tb)
            self.status_var.set("Batch build failed.")
            messagebox.showerror("Batch Builder Error", str(exc))


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    BatchGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
