#!/usr/bin/env python3
"""
anchor_to_github_registry_gui.py

Simple GUI wrapper for publishing a batch JSON file to GitHub as a Gist and
writing the returned anchor metadata back into batch_registry.json.

Features:
- select batch_*.json input file
- optional select github_token.txt
- private gist checkbox
- optional description
- scrolling previews for:
  - input batch JSON
  - registry before update
  - registry after update / gist result

Run:
    python anchor_to_github_registry_gui.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import requests
except ImportError:
    requests = None


GITHUB_GISTS_API = "https://api.github.com/gists"
REGISTRY_FILE_NAME = "batch_registry.json"


def read_text_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def load_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc


def save_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def resolve_jobs_root_and_registry(batch_path: Path, batch_data: Dict[str, Any]) -> Tuple[Path, Path]:
    jobs_root_raw = batch_data.get("jobs_root")
    if jobs_root_raw:
        jobs_root = Path(jobs_root_raw).resolve()
    else:
        jobs_root = batch_path.parent.parent.resolve()

    registry_path = jobs_root / REGISTRY_FILE_NAME
    return jobs_root, registry_path


def build_anchor_payload(
    batch_path: Path,
    batch_data: Dict[str, Any],
    gist_filename: str,
) -> Dict[str, Any]:
    merkle_root = batch_data.get("merkle_root")
    if not merkle_root:
        raise ValueError("Batch JSON does not contain a 'merkle_root' field.")

    anchor_record = {
        "anchor_type": "github_gist",
        "anchored_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_batch_file": batch_path.name,
        "batch_id": batch_data.get("batch_id"),
        "batch_hash": batch_data.get("batch_hash"),
        "merkle_root": merkle_root,
        "batch_summary": {
            "job_count": batch_data.get("job_count"),
            "created_at": batch_data.get("created_at"),
            "jobs_root": batch_data.get("jobs_root"),
            "previous_batch_hash": batch_data.get("previous_batch_hash"),
        },
        "note": (
            "This Gist is an external publication of the Merkle batch root. "
            "It proves this exact batch content was published to GitHub at or "
            "before the Gist timestamp and revision history."
        ),
        "batch_json": batch_data,
    }

    gist_content = json.dumps(anchor_record, indent=2, sort_keys=False)
    return {
        "filename": gist_filename,
        "content": gist_content,
    }


def create_gist(
    token: str,
    description: str,
    public: bool,
    gist_file: Dict[str, str],
) -> Dict[str, Any]:
    if requests is None:
        raise RuntimeError("The 'requests' package is not installed. Run: pip install requests")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "anchor-to-github-gui",
    }

    payload = {
        "description": description,
        "public": public,
        "files": {
            gist_file["filename"]: {
                "content": gist_file["content"]
            }
        }
    }

    response = requests.post(GITHUB_GISTS_API, headers=headers, json=payload, timeout=30)

    if response.status_code not in (200, 201):
        try:
            error_body = response.json()
        except Exception:
            error_body = response.text
        raise RuntimeError(f"GitHub API error {response.status_code}: {error_body}")

    return response.json()


def update_registry_with_anchor(
    registry_path: Path,
    batch_path: Path,
    batch_data: Dict[str, Any],
    gist_result: Dict[str, Any],
    description: str,
) -> Dict[str, Any]:
    if not registry_path.exists():
        raise FileNotFoundError(f"Registry file not found: {registry_path}")

    registry = load_json(registry_path)
    batches = registry.get("batches")
    if not isinstance(batches, list):
        raise ValueError(f"Invalid registry format in {registry_path}")

    batch_id = batch_data.get("batch_id")
    batch_hash = batch_data.get("batch_hash")
    batch_path_resolved = str(batch_path.resolve())

    matched = False
    for batch in batches:
        same_id = bool(batch_id) and batch.get("batch_id") == batch_id
        same_hash = bool(batch_hash) and batch.get("batch_hash") == batch_hash
        same_path = str(batch.get("batch_file", "")).strip() == batch_path_resolved

        if same_id or same_hash or same_path:
            batch["github_anchor"] = {
                "anchor_type": "github_gist",
                "anchored_at_utc": datetime.now(timezone.utc).isoformat(),
                "gist_id": gist_result.get("id"),
                "html_url": gist_result.get("html_url"),
                "api_url": gist_result.get("url"),
                "created_at": gist_result.get("created_at"),
                "updated_at": gist_result.get("updated_at"),
                "public": gist_result.get("public"),
                "description": description,
            }
            matched = True
            break

    if not matched:
        raise ValueError("Could not find a matching batch entry in batch_registry.json for the provided batch file.")

    save_json(registry_path, registry)
    return registry


class AnchorGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Submission Tool")
        self.root.geometry("1200x760")
        self.root.minsize(980, 620)

        self.batch_file_var = tk.StringVar(value="")
        self.token_file_var = tk.StringVar(value=str(Path.cwd() / "github_token.txt"))
        self.private_var = tk.BooleanVar(value=False)
        self.include_description_var = tk.BooleanVar(value=False)
        self.description_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Select a batch file, review the previews, then click Submit Batch.")

        self.batch_data: Dict[str, Any] | None = None
        self.registry_before: Dict[str, Any] | None = None
        self.registry_path: Path | None = None
        self.jobs_root: Path | None = None

        self.build_ui()

    def build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        top = ttk.Frame(main)
        top.pack(fill="x")

        ttk.Label(top, text="Batch File", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(top, textvariable=self.batch_file_var, width=90).grid(row=1, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(top, text="Select Batch", command=self.select_batch_file).grid(row=1, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(top, text="Token File (optional picker)", font=("Segoe UI", 10, "bold")).grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Entry(top, textvariable=self.token_file_var, width=90).grid(row=3, column=0, columnspan=2, sticky="ew", pady=4)
        ttk.Button(top, text="Select Token", command=self.select_token_file).grid(row=3, column=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Checkbutton(top, text="Private submission", variable=self.private_var).grid(row=4, column=0, sticky="w", pady=(10, 2))

        ttk.Checkbutton(
            top,
            text="Include description",
            variable=self.include_description_var,
            command=self.toggle_description_state,
        ).grid(row=5, column=0, sticky="w", pady=(4, 2))

        self.description_label = ttk.Label(top, text="Description")
        self.description_label.grid(row=6, column=0, sticky="w")

        self.description_entry = tk.Text(top, height=3, width=90, wrap="word")
        self.description_entry.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)

        ttk.Button(top, text="Load Preview", command=self.load_preview).grid(row=8, column=0, sticky="w", pady=(12, 0))
        ttk.Button(top, text="Submit Batch", command=self.submit_batch).grid(row=8, column=1, sticky="w", pady=(12, 0))

        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=1)

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=10)

        preview_wrap = ttk.Frame(main)
        preview_wrap.pack(fill="both", expand=True)

        self.input_preview = self.make_preview_panel(preview_wrap, "Input Batch JSON", 0)
        self.registry_before_preview = self.make_preview_panel(preview_wrap, "Registry Before Update", 1)
        self.output_preview = self.make_preview_panel(preview_wrap, "Registry After Update / Result", 2)

        preview_wrap.columnconfigure(0, weight=1)
        preview_wrap.columnconfigure(1, weight=1)
        preview_wrap.columnconfigure(2, weight=1)
        preview_wrap.rowconfigure(0, weight=1)

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor="w")

        self.toggle_description_state()

    def make_preview_panel(self, parent, title: str, column: int):
        frame = ttk.Frame(parent)
        frame.grid(row=0, column=column, sticky="nsew", padx=4)

        ttk.Label(frame, text=title, font=("Segoe UI", 10, "bold")).pack(anchor="w")

        text = tk.Text(frame, wrap="none")
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        text.pack(side="top", fill="both", expand=True)
        y_scroll.pack(side="right", fill="y")
        x_scroll.pack(side="bottom", fill="x")

        return text

    def toggle_description_state(self):
        state = "normal" if self.include_description_var.get() else "disabled"
        self.description_entry.configure(state=state)
        if state == "disabled":
            self.description_entry.delete("1.0", "end")

    def select_batch_file(self):
        path = filedialog.askopenfilename(
            title="Select batch JSON",
            filetypes=[("Batch JSON", "batch_*.json"), ("JSON files", "*.json"), ("All files", "*.*")],
        )
        if path:
            self.batch_file_var.set(path)
            self.load_preview()

    def select_token_file(self):
        path = filedialog.askopenfilename(
            title="Select github_token.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.token_file_var.set(path)

    def set_text(self, widget: tk.Text, content: str):
        widget.delete("1.0", "end")
        widget.insert("1.0", content)

    def load_preview(self):
        try:
            batch_path = Path(self.batch_file_var.get().strip()).resolve()
            if not batch_path.exists():
                raise FileNotFoundError(f"Batch file not found: {batch_path}")

            self.batch_data = load_json(batch_path)
            self.jobs_root, self.registry_path = resolve_jobs_root_and_registry(batch_path, self.batch_data)

            if not self.registry_path.exists():
                raise FileNotFoundError(f"Registry file not found: {self.registry_path}")

            self.registry_before = load_json(self.registry_path)

            self.set_text(self.input_preview, json.dumps(self.batch_data, indent=2))
            self.set_text(self.registry_before_preview, json.dumps(self.registry_before, indent=2))
            self.set_text(self.output_preview, "Ready to submit.\n\nSelect Submit Batch when you are satisfied with the inputs.")

            self.status_var.set(f"Loaded batch preview from {batch_path}")
        except Exception as exc:
            self.set_text(self.output_preview, str(exc))
            messagebox.showerror("Preview Error", str(exc))
            self.status_var.set("Failed to load preview.")

    def submit_batch(self):
        try:
            if requests is None:
                raise RuntimeError("The 'requests' package is not installed. Run: pip install requests")

            batch_path = Path(self.batch_file_var.get().strip()).resolve()
            if not batch_path.exists():
                raise FileNotFoundError("Please select a valid batch JSON file.")

            token_path = Path(self.token_file_var.get().strip()).resolve()
            token = read_text_file(token_path)
            if not token:
                raise ValueError(f"Token file is empty: {token_path}")

            batch_data = load_json(batch_path)
            jobs_root, registry_path = resolve_jobs_root_and_registry(batch_path, batch_data)

            registry_before = load_json(registry_path)
            self.set_text(self.registry_before_preview, json.dumps(registry_before, indent=2))

            description = None
            if self.include_description_var.get():
                description = self.description_entry.get("1.0", "end").strip()

            gist_filename = f"{batch_path.stem}_github_anchor.json"
            final_description = description or f"Merkle anchor for {batch_path.name}"

            gist_file = build_anchor_payload(batch_path, batch_data, gist_filename)

            gist_result = create_gist(
                token=token,
                description=final_description,
                public=not self.private_var.get(),
                gist_file=gist_file,
            )

            updated_registry = update_registry_with_anchor(
                registry_path=registry_path,
                batch_path=batch_path,
                batch_data=batch_data,
                gist_result=gist_result,
                description=final_description,
            )

            output_payload = {
                "gist_result": gist_result,
                "updated_registry": updated_registry,
            }
            self.set_text(self.output_preview, json.dumps(output_payload, indent=2))

            self.status_var.set(f"Submission completed for {batch_data.get('batch_id')}")
            messagebox.showinfo(
                "Submission Complete",
                f"Gist created successfully.\n\nURL:\n{gist_result.get('html_url')}\n\nRegistry updated:\n{registry_path}"
            )

        except Exception as exc:
            self.set_text(self.output_preview, str(exc))
            messagebox.showerror("Submission Error", str(exc))
            self.status_var.set("Submission failed.")


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    AnchorGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
