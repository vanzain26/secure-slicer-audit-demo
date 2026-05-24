#!/usr/bin/env python3
"""
build_audit_bundle_gui.py

Simple GUI wrapper for building an audit bundle from batch_registry.json.

Features:
- select structured jobs root
- load available batch IDs from batch_registry.json
- choose a batch ID
- optionally include G-code files
- choose an output folder
- build:
    output_root/audit/audit_YYYYMMDD_HHMMSS/audit_bundle.zip

Run:
    python build_audit_bundle_gui.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


REGISTRY_FILE_NAME = "batch_registry.json"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_batch_entry(registry: Dict[str, Any], batch_id: str) -> Dict[str, Any]:
    batches = registry.get("batches", [])
    for batch in batches:
        if batch.get("batch_id") == batch_id:
            return batch
    raise ValueError(f"Batch ID not found in registry: {batch_id}")


def find_manifest_in_job_folder(job_folder: Path):
    manifests = sorted(job_folder.glob("*_manifest.json"))
    return manifests[0] if manifests else None


def find_gcode_in_job_folder(job_folder: Path):
    gcodes = sorted(job_folder.glob("*.gcode"))
    return gcodes[0] if gcodes else None


def collect_files_for_batch(
    jobs_root: Path,
    batch_entry: Dict[str, Any],
    include_gcode: bool,
) -> Tuple[List[Tuple[Path, str]], List[str]]:
    files_to_copy: List[Tuple[Path, str]] = []
    warnings: List[str] = []

    registry_path = jobs_root / REGISTRY_FILE_NAME
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry not found: {registry_path}")

    files_to_copy.append((registry_path, REGISTRY_FILE_NAME))

    batch_file_raw = batch_entry.get("batch_file")
    if not batch_file_raw:
        raise ValueError("Selected batch entry does not contain 'batch_file'")

    batch_file = Path(batch_file_raw)
    if not batch_file.is_absolute():
        batch_file = (jobs_root / batch_file).resolve()
    else:
        batch_file = batch_file.resolve()

    if not batch_file.exists():
        raise FileNotFoundError(f"Batch file not found: {batch_file}")

    files_to_copy.append((batch_file, f"batches/{batch_file.name}"))

    jobs = batch_entry.get("jobs", [])
    if not isinstance(jobs, list):
        raise ValueError("Selected batch entry contains invalid 'jobs' list")

    for job in jobs:
        rel_job_folder = job.get("rel_job_folder")
        if not rel_job_folder:
            warnings.append("A batch job entry is missing rel_job_folder; skipping.")
            continue

        job_folder = (jobs_root / rel_job_folder).resolve()
        if not job_folder.exists():
            warnings.append(f"Job folder missing: {rel_job_folder}")
            continue

        manifest = find_manifest_in_job_folder(job_folder)
        if manifest:
            files_to_copy.append((manifest, f"selected_jobs/{rel_job_folder}/{manifest.name}"))
        else:
            warnings.append(f"Manifest missing in job folder: {rel_job_folder}")

        log_file = job_folder / "print_log.json"
        if log_file.exists():
            files_to_copy.append((log_file, f"selected_jobs/{rel_job_folder}/print_log.json"))
        else:
            warnings.append(f"print_log.json missing in job folder: {rel_job_folder}")

        if include_gcode:
            gcode = find_gcode_in_job_folder(job_folder)
            if gcode:
                files_to_copy.append((gcode, f"selected_jobs/{rel_job_folder}/{gcode.name}"))
            else:
                warnings.append(f"G-code missing in job folder: {rel_job_folder}")

    return files_to_copy, warnings


def write_bundle_manifest(
    dest_dir: Path,
    jobs_root: Path,
    batch_entry: Dict[str, Any],
    warnings: List[str],
    files_to_copy: List[Tuple[Path, str]],
) -> Path:
    manifest = {
        "bundle_type": "audit_bundle",
        "created_at_local": datetime.now().isoformat(),
        "jobs_root": str(jobs_root.resolve()),
        "batch_id": batch_entry.get("batch_id"),
        "batch_hash": batch_entry.get("batch_hash"),
        "merkle_root": batch_entry.get("merkle_root"),
        "job_count": batch_entry.get("job_count"),
        "included_files": [dest_rel for _, dest_rel in files_to_copy],
        "warnings": warnings,
    }
    manifest_path = dest_dir / "audit_bundle_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def copy_files_to_staging(staging_root: Path, files_to_copy: List[Tuple[Path, str]]) -> None:
    for src, dest_rel in files_to_copy:
        dest = staging_root / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


def make_zip_from_folder(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                arcname = str(path.relative_to(source_dir)).replace("\\", "/")
                zf.write(path, arcname)


def build_audit_bundle(
    jobs_root: Path,
    batch_id: str,
    include_gcode: bool,
    output_root: Path,
) -> Dict[str, Any]:
    jobs_root = jobs_root.resolve()
    if not jobs_root.exists() or not jobs_root.is_dir():
        raise FileNotFoundError(f"Jobs root does not exist or is not a directory: {jobs_root}")

    registry_path = jobs_root / REGISTRY_FILE_NAME
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry file not found: {registry_path}")

    registry = load_json(registry_path)
    batch_entry = find_batch_entry(registry, batch_id)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audit_root = output_root.resolve() / "audit"
    audit_root.mkdir(parents=True, exist_ok=True)

    audit_dir = audit_root / f"audit_{timestamp}"
    staging_dir = audit_dir / "audit_bundle_contents"
    zip_path = audit_dir / "audit_bundle.zip"

    audit_dir.mkdir(parents=True, exist_ok=False)
    staging_dir.mkdir(parents=True, exist_ok=False)

    files_to_copy, warnings = collect_files_for_batch(
        jobs_root=jobs_root,
        batch_entry=batch_entry,
        include_gcode=include_gcode,
    )

    copy_files_to_staging(staging_dir, files_to_copy)
    bundle_manifest_path = write_bundle_manifest(
        dest_dir=staging_dir,
        jobs_root=jobs_root,
        batch_entry=batch_entry,
        warnings=warnings,
        files_to_copy=files_to_copy,
    )

    make_zip_from_folder(staging_dir, zip_path)

    return {
        "batch_id": batch_entry.get("batch_id"),
        "audit_dir": str(audit_dir),
        "zip_path": str(zip_path),
        "bundle_manifest_path": str(bundle_manifest_path),
        "files_included": len(files_to_copy) + 1,
        "warnings": warnings,
        "batch_entry": batch_entry,
    }


class AuditBundleGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Audit Bundle Builder")
        self.root.geometry("980x700")
        self.root.minsize(820, 560)

        self.jobs_root_var = tk.StringVar(value=str(Path.cwd() / "jobs"))
        self.output_root_var = tk.StringVar(value=str(Path.cwd()))
        self.include_gcode_var = tk.BooleanVar(value=False)
        self.batch_id_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Select a jobs folder, load batches, choose a batch ID, then build the audit bundle.")

        self.registry: Dict[str, Any] | None = None
        self.batch_map: Dict[str, Dict[str, Any]] = {}

        self.build_ui()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        ttk.Label(top, text="Structured Jobs Root", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(top, textvariable=self.jobs_root_var, width=80).grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(top, text="Browse Jobs Root", command=self.select_jobs_root).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Button(top, text="Load Batches", command=self.load_batches).grid(row=2, column=0, sticky="w", pady=(8, 4))

        ttk.Label(top, text="Batch ID", font=("Segoe UI", 10, "bold")).grid(row=3, column=0, columnspan=3, sticky="w")
        self.batch_combo = ttk.Combobox(top, textvariable=self.batch_id_var, state="readonly", width=40)
        self.batch_combo.grid(row=4, column=0, columnspan=2, sticky="w", pady=4)
        self.batch_combo.bind("<<ComboboxSelected>>", self.on_batch_selected)

        ttk.Checkbutton(top, text="Include G-code files", variable=self.include_gcode_var).grid(row=5, column=0, sticky="w", pady=(10, 4))

        ttk.Label(top, text="Output Folder", font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=3, sticky="w")
        ttk.Entry(top, textvariable=self.output_root_var, width=80).grid(row=7, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(top, text="Browse Output Folder", command=self.select_output_root).grid(row=7, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Button(top, text="Build Audit Bundle", command=self.on_build_bundle).grid(row=8, column=0, sticky="w", pady=(14, 0))

        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=10)

        ttk.Label(main, text="Batch Preview / Result", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.preview_box = tk.Text(main, height=24, wrap="word")
        self.preview_box.pack(fill="both", expand=True)

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")

    def set_preview(self, text: str):
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", text)

    def select_jobs_root(self):
        path = filedialog.askdirectory(title="Select Structured Jobs Root")
        if path:
            self.jobs_root_var.set(path)
            self.status_var.set(f"Jobs root set to {path}")

    def select_output_root(self):
        path = filedialog.askdirectory(title="Select Output Folder")
        if path:
            self.output_root_var.set(path)
            self.status_var.set(f"Output folder set to {path}")

    def load_batches(self):
        try:
            jobs_root = Path(self.jobs_root_var.get().strip()).resolve()
            registry_path = jobs_root / REGISTRY_FILE_NAME
            if not registry_path.exists():
                raise FileNotFoundError(f"Registry file not found: {registry_path}")

            self.registry = load_json(registry_path)
            batches = self.registry.get("batches", [])
            if not isinstance(batches, list):
                raise ValueError("Invalid registry format: 'batches' is not a list")

            self.batch_map = {str(batch.get("batch_id")): batch for batch in batches if batch.get("batch_id")}
            batch_ids = list(self.batch_map.keys())
            self.batch_combo["values"] = batch_ids

            if batch_ids:
                self.batch_id_var.set(batch_ids[-1])
                self.show_batch_preview(self.batch_map[batch_ids[-1]])
                self.status_var.set(f"Loaded {len(batch_ids)} batch entries.")
            else:
                self.batch_id_var.set("")
                self.set_preview("No batches found in batch_registry.json")
                self.status_var.set("No batches found.")
        except Exception as exc:
            self.set_preview(str(exc))
            messagebox.showerror("Load Batches Error", str(exc))
            self.status_var.set("Failed to load batches.")

    def show_batch_preview(self, batch_entry: Dict[str, Any]):
        self.set_preview(json.dumps(batch_entry, indent=2))

    def on_batch_selected(self, event=None):
        batch_id = self.batch_id_var.get().strip()
        if batch_id and batch_id in self.batch_map:
            self.show_batch_preview(self.batch_map[batch_id])

    def on_build_bundle(self):
        try:
            jobs_root = Path(self.jobs_root_var.get().strip()).resolve()
            output_root = Path(self.output_root_var.get().strip()).resolve()
            batch_id = self.batch_id_var.get().strip()

            if not batch_id:
                raise ValueError("Please load batches and select a batch ID.")

            result = build_audit_bundle(
                jobs_root=jobs_root,
                batch_id=batch_id,
                include_gcode=self.include_gcode_var.get(),
                output_root=output_root,
            )

            summary = {
                "result": "success",
                "batch_id": result["batch_id"],
                "audit_dir": result["audit_dir"],
                "zip_path": result["zip_path"],
                "bundle_manifest_path": result["bundle_manifest_path"],
                "files_included": result["files_included"],
                "warnings": result["warnings"],
                "batch_entry": result["batch_entry"],
            }
            self.set_preview(json.dumps(summary, indent=2))

            self.status_var.set(f"Built audit bundle for {batch_id}")
            messagebox.showinfo(
                "Audit Bundle Created",
                f"Batch ID: {result['batch_id']}\n\nAudit folder:\n{result['audit_dir']}\n\nBundle zip:\n{result['zip_path']}"
            )
        except Exception as exc:
            self.set_preview(str(exc))
            messagebox.showerror("Build Audit Bundle Error", str(exc))
            self.status_var.set("Audit bundle build failed.")


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    AuditBundleGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
