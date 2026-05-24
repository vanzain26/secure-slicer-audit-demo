import hashlib
import json
import math
import os
import struct
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import List, Tuple

LOG_FILE_NAME = "print_log.json"


@dataclass
class SliceSettings:
    shape: str
    width_mm: float
    depth_mm: float
    height_mm: float
    layer_height_mm: float
    nozzle_diameter_mm: float
    line_width_mm: float
    print_speed_mm_s: float
    travel_speed_mm_s: float
    bed_temp_c: int
    nozzle_temp_c: int
    infill_percent: float
    filament_diameter_mm: float
    material_density_g_cm3: float
    material_name: str
    operator_id: str
    instrument_id: str
    facility_id: str
    source_stl_path: str = ""
    source_stl_hash: str = ""


class BareBonesSlicerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Bare Bones Slicer Demo + STL + Append Log")
        self.root.geometry("1200x860")
        self.vars = {}
        self._build_ui()

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)

        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=(0, 12))

        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        fields = [
            ("shape", "cube"),
            ("width_mm", "20"),
            ("depth_mm", "20"),
            ("height_mm", "20"),
            ("layer_height_mm", "0.2"),
            ("nozzle_diameter_mm", "0.4"),
            ("line_width_mm", "0.4"),
            ("print_speed_mm_s", "40"),
            ("travel_speed_mm_s", "120"),
            ("bed_temp_c", "60"),
            ("nozzle_temp_c", "200"),
            ("infill_percent", "15"),
            ("filament_diameter_mm", "1.75"),
            ("material_density_g_cm3", "1.24"),
            ("material_name", "PLA"),
            ("operator_id", "operator_demo"),
            ("facility_id", "facility_demo"),
            ("instrument_id", "Instrument_demo"),
        ]

        ttk.Label(left, text="Part + Print Settings", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        row = 1
        for key, default in fields:
            ttk.Label(left, text=key).grid(row=row, column=0, sticky="w", pady=3)
            if key == "shape":
                var = tk.StringVar(value=default)
                self.vars[key] = var
                combo = ttk.Combobox(left, textvariable=var, values=["cube", "pyramid"], state="readonly", width=18)
                combo.grid(row=row, column=1, sticky="ew", pady=3, columnspan=2)
            else:
                var = tk.StringVar(value=default)
                self.vars[key] = var
                ttk.Entry(left, textvariable=var, width=22).grid(row=row, column=1, sticky="ew", pady=3, columnspan=2)
            row += 1

        ttk.Separator(left, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1

        ttk.Label(left, text="Structured Jobs Root", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.derived_jobs_root_var = tk.StringVar(value=build_jobs_root_path(
            os.path.join(os.getcwd(), "output"),
            self.vars["facility_id"].get(),
            self.vars["instrument_id"].get(),
        ))
        ttk.Entry(left, textvariable=self.derived_jobs_root_var, width=22, state="readonly").grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        row += 1

        ttk.Separator(left, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1

        ttk.Label(left, text="Optional STL Input", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        self.vars["stl_path"] = tk.StringVar(value="")
        ttk.Entry(left, textvariable=self.vars["stl_path"], width=22).grid(row=row, column=0, columnspan=2, sticky="ew", pady=3)
        ttk.Button(left, text="Browse STL", command=self.load_stl).grid(row=row, column=2, sticky="ew", pady=3)
        row += 1

        ttk.Button(left, text="Use Manual Shape", command=self.clear_stl).grid(row=row, column=0, columnspan=3, sticky="ew", pady=3)
        row += 1

        ttk.Button(left, text="Preview + Slice", command=self.preview).grid(row=row, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        row += 1
        ttk.Button(left, text="Export G-code + Log", command=self.export_gcode).grid(row=row, column=0, columnspan=3, sticky="ew")
        row += 1

        ttk.Separator(left, orient="horizontal").grid(row=row, column=0, columnspan=3, sticky="ew", pady=12)
        row += 1

        ttk.Label(left, text="Logging behavior", font=("Segoe UI", 10, "bold")).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        note = (
            "On export, this demo automatically creates <base>/<facility_id>/<instrument_id>/jobs/YYYY-MM-DD/job_NNN_shape_timestamp/\n"
            "under the derived facility/instrument jobs root. It saves the G-code, manifest, and a chained\n"
            "print_log.json there. Editing or removing log entries breaks the chain."
        )
        ttk.Label(left, text=note, justify="left", wraplength=360).grid(row=row, column=0, columnspan=3, sticky="w")

        ttk.Label(right, text="G-code Preview", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.preview_text = tk.Text(right, wrap="none", font=("Consolas", 10))
        self.preview_text.pack(fill="both", expand=True, pady=(8, 0))

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w").pack(fill="x", side="bottom")

    def on_identity_change(self, *args):
        if not hasattr(self, "derived_jobs_root_var"):
            return
        facility_id = self.vars["facility_id"].get()
        instrument_id = self.vars["instrument_id"].get()
        new_root = build_jobs_root_path(os.path.join(os.getcwd(), "output"), facility_id, instrument_id)
        self.derived_jobs_root_var.set(new_root)

    def load_stl(self):
        path = filedialog.askopenfilename(
            title="Select STL",
            filetypes=[("STL files", "*.stl"), ("All files", "*.*")],
        )
        if not path:
            return

        try:
            stl_info = analyze_stl(path)
            self.vars["stl_path"].set(path)
            self.vars["shape"].set(stl_info["shape"])
            self.vars["width_mm"].set(f"{stl_info['width_mm']:.3f}")
            self.vars["depth_mm"].set(f"{stl_info['depth_mm']:.3f}")
            self.vars["height_mm"].set(f"{stl_info['height_mm']:.3f}")
            self.status.set(
                f"Loaded STL | inferred {stl_info['shape']} | {stl_info['width_mm']:.1f} x {stl_info['depth_mm']:.1f} x {stl_info['height_mm']:.1f} mm"
            )
        except Exception as exc:
            messagebox.showerror("STL load error", str(exc))

    def clear_stl(self):
        self.vars["stl_path"].set("")
        self.status.set("Using manual primitive settings")

    def get_settings(self) -> SliceSettings:
        def f(name: str) -> float:
            return float(self.vars[name].get())

        def i(name: str) -> int:
            return int(float(self.vars[name].get()))

        stl_path = self.vars["stl_path"].get().strip()
        source_hash = ""
        if stl_path:
            with open(stl_path, "rb") as fh:
                source_hash = hashlib.sha256(fh.read()).hexdigest()

        return SliceSettings(
            shape=self.vars["shape"].get().strip().lower(),
            width_mm=f("width_mm"),
            depth_mm=f("depth_mm"),
            height_mm=f("height_mm"),
            layer_height_mm=f("layer_height_mm"),
            nozzle_diameter_mm=f("nozzle_diameter_mm"),
            line_width_mm=f("line_width_mm"),
            print_speed_mm_s=f("print_speed_mm_s"),
            travel_speed_mm_s=f("travel_speed_mm_s"),
            bed_temp_c=i("bed_temp_c"),
            nozzle_temp_c=i("nozzle_temp_c"),
            infill_percent=f("infill_percent"),
            filament_diameter_mm=f("filament_diameter_mm"),
            material_density_g_cm3=f("material_density_g_cm3"),
            material_name=self.vars["material_name"].get().strip(),
            operator_id=self.vars["operator_id"].get().strip(),
            facility_id=self.vars["facility_id"].get().strip(),
            instrument_id=self.vars["instrument_id"].get().strip(),
            source_stl_path=stl_path,
            source_stl_hash=source_hash,
        )

    def preview(self):
        try:
            settings = self.get_settings()
            gcode, summary = generate_gcode(settings)
            self.preview_text.delete("1.0", tk.END)
            self.preview_text.insert("1.0", gcode)
            source_mode = "STL" if settings.source_stl_path else "manual"
            self.status.set(
                f"Preview ready ({source_mode}) | job {summary['job_hash'][:12]}... | est {summary['estimated_time_min']:.1f} min | material {summary['estimated_material_g']:.2f} g"
            )
        except Exception as exc:
            messagebox.showerror("Slice error", str(exc))

    def export_gcode(self):
        try:
            settings = self.get_settings()
            gcode, summary = generate_gcode(settings)

            jobs_root = build_jobs_root_path(
                os.path.join(os.getcwd(), "output"),
                settings.facility_id,
                settings.instrument_id,
            )

            export_dir, base_name = create_structured_job_folder(jobs_root, settings.shape)
            path = os.path.join(export_dir, base_name + ".gcode")
            log_path = os.path.join(export_dir, LOG_FILE_NAME)

            manifest_path = os.path.join(export_dir, base_name + "_manifest.json")
            manifest_payload = dict(summary)
            manifest_payload["jobs_root"] = os.path.abspath(jobs_root)
            manifest_payload["facility_id"] = settings.facility_id
            manifest_payload["instrument_id"] = settings.instrument_id
            manifest_payload["job_folder"] = os.path.abspath(export_dir)
            manifest_payload["gcode_path"] = os.path.abspath(path)
            manifest_payload["manifest_path"] = os.path.abspath(manifest_path)
            manifest_payload["exported_at_utc"] = datetime.utcnow().isoformat() + "Z"
            log_entry = append_log_entry(log_path, manifest_payload)
            manifest_payload["log_entry"] = log_entry

            gcode_with_log = inject_log_metadata(gcode, log_entry, log_path)

            with open(path, "w", encoding="utf-8") as f:
                f.write(gcode_with_log)

            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest_payload, f, indent=2)

            self.status.set(f"Exported structured job in {export_dir}")
            messagebox.showinfo(
                "Export complete",
                f"Job folder:\n{export_dir}\n\nG-code:\n{path}\n\nManifest:\n{manifest_path}\n\nAppend log:\n{log_path}\n\nEntry hash:\n{log_entry['entry_hash']}"
            )
        except Exception as exc:
            messagebox.showerror("Export error", str(exc))


def build_jobs_root_path(base_dir: str, facility_id: str, instrument_id: str) -> str:
    facility_slug = sanitize_name(facility_id or "facility_demo")
    instrument_slug = sanitize_name(instrument_id or "Instrument_demo")
    return os.path.join(os.path.abspath(base_dir), facility_slug, instrument_slug, "jobs")


def sanitize_name(text: str) -> str:
    cleaned = []
    for ch in str(text):
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in (" ", "-", "_"):
            cleaned.append("_")
    result = "".join(cleaned).strip("_")
    return result or "job"


def create_structured_job_folder(jobs_root: str, shape: str):
    now = datetime.now()
    date_folder = now.strftime("%Y-%m-%d")
    shape_slug = sanitize_name(shape)
    day_root = os.path.join(os.path.abspath(jobs_root), date_folder)
    os.makedirs(day_root, exist_ok=True)

    existing = []
    for name in os.listdir(day_root):
        full = os.path.join(day_root, name)
        if os.path.isdir(full) and name.startswith("job_"):
            parts = name.split("_", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                existing.append(int(parts[1]))

    next_index = (max(existing) + 1) if existing else 1
    base_name = f"{shape_slug}_{now.strftime('%Y%m%d_%H%M%S')}"
    folder_name = f"job_{next_index:03d}_{base_name}"
    export_dir = os.path.join(day_root, folder_name)
    os.makedirs(export_dir, exist_ok=False)
    return export_dir, base_name


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def roundish(value: float, ndigits: int = 5) -> float:
    return round(value, ndigits)


def read_stl_triangles(path: str) -> List[Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]]:
    with open(path, "rb") as fh:
        raw = fh.read()

    if len(raw) < 84:
        raise ValueError("STL file is too small to parse")

    tri_count = struct.unpack("<I", raw[80:84])[0]
    expected_size = 84 + tri_count * 50
    if expected_size == len(raw) and tri_count > 0:
        triangles = []
        offset = 84
        for _ in range(tri_count):
            offset += 12
            verts = []
            for _ in range(3):
                x, y, z = struct.unpack("<fff", raw[offset:offset + 12])
                verts.append((float(x), float(y), float(z)))
                offset += 12
            triangles.append((verts[0], verts[1], verts[2]))
            offset += 2
        return triangles

    text = raw.decode("utf-8", errors="ignore")
    triangles = []
    current: List[Tuple[float, float, float]] = []
    for line in text.splitlines():
        parts = line.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            current.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(current) == 3:
                triangles.append((current[0], current[1], current[2]))
                current = []

    if not triangles:
        raise ValueError("Could not parse STL triangles")
    return triangles


def analyze_stl(path: str) -> dict:
    triangles = read_stl_triangles(path)
    points = [p for tri in triangles for p in tri]
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    zs = [p[2] for p in points]

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)

    width = max_x - min_x
    depth = max_y - min_y
    height = max_z - min_z

    if width <= 0 or depth <= 0 or height <= 0:
        raise ValueError("STL dimensions must be positive")

    eps_z = max(0.001, height * 0.01)
    top_points = {(roundish(x), roundish(y), roundish(z)) for x, y, z in points if abs(z - max_z) <= eps_z}
    base_points = {(roundish(x), roundish(y), roundish(z)) for x, y, z in points if abs(z - min_z) <= eps_z}

    shape = "cube"
    if len(top_points) <= 2 and len(base_points) >= 4:
        shape = "pyramid"

    return {
        "path": path,
        "shape": shape,
        "width_mm": width,
        "depth_mm": depth,
        "height_mm": height,
        "triangle_count": len(triangles),
        "min": [min_x, min_y, min_z],
        "max": [max_x, max_y, max_z],
    }


def extrusion_for_move(length_mm: float, layer_height_mm: float, line_width_mm: float, filament_diameter_mm: float) -> float:
    deposited_area = layer_height_mm * line_width_mm
    filament_area = math.pi * (filament_diameter_mm / 2.0) ** 2
    return (length_mm * deposited_area) / filament_area


def volume_mm3_to_filament_mass_g(volume_mm3: float, density_g_cm3: float) -> float:
    volume_cm3 = volume_mm3 / 1000.0
    return volume_cm3 * density_g_cm3


def rect_path(x0: float, y0: float, x1: float, y1: float):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]


def generate_layer_geometry(settings: SliceSettings, z: float):
    if settings.shape == "cube":
        return settings.width_mm, settings.depth_mm
    if settings.shape == "pyramid":
        t = max(0.0, 1.0 - (z / settings.height_mm))
        return max(settings.line_width_mm * 2, settings.width_mm * t), max(settings.line_width_mm * 2, settings.depth_mm * t)
    raise ValueError("Shape must be cube or pyramid")


def generate_gcode(settings: SliceSettings):
    if settings.layer_height_mm <= 0:
        raise ValueError("layer_height_mm must be > 0")
    if settings.line_width_mm <= 0:
        raise ValueError("line_width_mm must be > 0")
    if settings.height_mm <= 0 or settings.width_mm <= 0 or settings.depth_mm <= 0:
        raise ValueError("Part dimensions must be > 0")
    if settings.print_speed_mm_s <= 0 or settings.travel_speed_mm_s <= 0:
        raise ValueError("Speeds must be > 0")

    machine_x_center = 110.0
    machine_y_center = 110.0
    e_total = 0.0
    layers = max(1, math.ceil(settings.height_mm / settings.layer_height_mm))

    settings_dict = asdict(settings)
    settings_json = json.dumps(settings_dict, sort_keys=True)
    settings_hash = sha256_text(settings_json)

    job_basis = {
        "settings_hash": settings_hash,
        "created_at_utc": datetime.utcnow().isoformat() + "Z",
        "shape": settings.shape,
        "facility_id": settings.facility_id,
        "instrument_id": settings.instrument_id,
        "operator_id": settings.operator_id,
        "source_stl_hash": settings.source_stl_hash,
    }
    job_hash = sha256_text(json.dumps(job_basis, sort_keys=True))

    lines = []
    total_extruded_volume_mm3 = 0.0
    total_print_distance = 0.0
    total_travel_distance = 0.0

    lines.append("; Bare Bones Slicer Demo + STL + Append Log")
    lines.append(f"; job_hash={job_hash}")
    lines.append(f"; settings_hash={settings_hash}")
    lines.append(f"; facility_id={settings.facility_id}")
    lines.append(f"; instrument_id={settings.instrument_id}")
    lines.append(f"; operator_id={settings.operator_id}")
    lines.append(f"; material_declaration={settings.material_name}")
    lines.append(f"; source_stl_path={settings.source_stl_path}")
    lines.append(f"; source_stl_hash={settings.source_stl_hash}")
    lines.append(f"; estimated_layers={layers}")
    lines.append("G21 ; mm mode")
    lines.append("G90 ; absolute positioning")
    lines.append("M82 ; absolute extrusion")
    lines.append(f"M104 S{settings.nozzle_temp_c}")
    lines.append(f"M140 S{settings.bed_temp_c}")
    lines.append(f"M109 S{settings.nozzle_temp_c}")
    lines.append(f"M190 S{settings.bed_temp_c}")
    lines.append("G28")
    lines.append("G92 E0")

    current_x, current_y = 0.0, 0.0

    for layer in range(layers):
        z = min(settings.height_mm, (layer + 1) * settings.layer_height_mm)
        width, depth = generate_layer_geometry(settings, z)
        x0 = machine_x_center - width / 2.0
        x1 = machine_x_center + width / 2.0
        y0 = machine_y_center - depth / 2.0
        y1 = machine_y_center + depth / 2.0
        path = rect_path(x0, y0, x1, y1)

        lines.append(f";LAYER:{layer}")
        lines.append(f"G0 Z{z:.3f} F{settings.travel_speed_mm_s * 60:.0f}")

        start_x, start_y = path[0]
        travel = math.dist((current_x, current_y), (start_x, start_y))
        total_travel_distance += travel
        lines.append(f"G0 X{start_x:.3f} Y{start_y:.3f} F{settings.travel_speed_mm_s * 60:.0f}")
        current_x, current_y = start_x, start_y

        for i in range(1, len(path)):
            nx, ny = path[i]
            length = math.dist((current_x, current_y), (nx, ny))
            e_move = extrusion_for_move(length, settings.layer_height_mm, settings.line_width_mm, settings.filament_diameter_mm)
            e_total += e_move
            total_print_distance += length
            total_extruded_volume_mm3 += length * settings.layer_height_mm * settings.line_width_mm
            lines.append(f"G1 X{nx:.3f} Y{ny:.3f} E{e_total:.5f} F{settings.print_speed_mm_s * 60:.0f}")
            current_x, current_y = nx, ny

        usable_width = max(0.0, width - 2 * settings.line_width_mm)
        usable_depth = max(0.0, depth - 2 * settings.line_width_mm)
        if settings.infill_percent > 0 and usable_width > settings.line_width_mm and usable_depth > settings.line_width_mm:
            spacing = max(settings.line_width_mm, settings.line_width_mm * (100.0 / max(1.0, settings.infill_percent)))
            x_left = machine_x_center - usable_width / 2.0
            x_right = machine_x_center + usable_width / 2.0
            y = machine_y_center - usable_depth / 2.0
            toggle = False
            while y <= machine_y_center + usable_depth / 2.0:
                sx, ex = (x_left, x_right) if not toggle else (x_right, x_left)
                travel = math.dist((current_x, current_y), (sx, y))
                total_travel_distance += travel
                lines.append(f"G0 X{sx:.3f} Y{y:.3f} F{settings.travel_speed_mm_s * 60:.0f}")
                current_x, current_y = sx, y
                length = abs(ex - sx)
                e_move = extrusion_for_move(length, settings.layer_height_mm, settings.line_width_mm, settings.filament_diameter_mm)
                e_total += e_move
                total_print_distance += length
                total_extruded_volume_mm3 += length * settings.layer_height_mm * settings.line_width_mm
                lines.append(f"G1 X{ex:.3f} Y{y:.3f} E{e_total:.5f} F{settings.print_speed_mm_s * 60:.0f}")
                current_x, current_y = ex, y
                y += spacing
                toggle = not toggle

    lines.append("M104 S0")
    lines.append("M140 S0")
    lines.append("G0 X0 Y220 F7200")
    lines.append("M84")

    print_time_s = (total_print_distance / settings.print_speed_mm_s) + (total_travel_distance / settings.travel_speed_mm_s)
    estimated_material_g = volume_mm3_to_filament_mass_g(total_extruded_volume_mm3, settings.material_density_g_cm3)

    summary = {
        "job_hash": job_hash,
        "settings_hash": settings_hash,
        "facility_id": settings.facility_id,
        "instrument_id": settings.instrument_id,
        "operator_id": settings.operator_id,
        "material_declaration": settings.material_name,
        "shape": settings.shape,
        "source_stl_path": settings.source_stl_path,
        "source_stl_hash": settings.source_stl_hash,
        "estimated_time_sec": round(print_time_s, 2),
        "estimated_time_min": round(print_time_s / 60.0, 2),
        "estimated_material_g": round(estimated_material_g, 4),
        "estimated_layers": layers,
        "settings": settings_dict,
        "job_basis": job_basis,
    }

    lines.insert(8, f"; estimated_time_sec={summary['estimated_time_sec']}")
    lines.insert(9, f"; estimated_material_g={summary['estimated_material_g']}")

    return "\n".join(lines) + "\n", summary


def load_log(log_path: str):
    if not os.path.exists(log_path):
        return []
    with open(log_path, "r", encoding="utf-8") as f:
        return json.load(f)



def append_log_entry(log_path: str, manifest_payload: dict) -> dict:
    log_entries = load_log(log_path)
    previous_hash = log_entries[-1]["entry_hash"] if log_entries else "GENESIS"
    manifest_hash = sha256_bytes(canonical_json_bytes(manifest_payload))

    entry = {
        "index": len(log_entries),
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "job_hash": manifest_payload["job_hash"],
        "settings_hash": manifest_payload["settings_hash"],
        "manifest_hash": manifest_hash,
        "previous_hash": previous_hash,
        "operator_id": manifest_payload["operator_id"],
        "facility_id": manifest_payload["facility_id"],
        "instrument_id": manifest_payload["instrument_id"],
        "gcode_path": manifest_payload["gcode_path"],
        "manifest_path": manifest_payload["manifest_path"],
    }
    entry["entry_hash"] = sha256_bytes(canonical_json_bytes(entry))

    log_entries.append(entry)
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log_entries, f, indent=2)

    return entry



def inject_log_metadata(gcode: str, log_entry: dict, log_path: str) -> str:
    injected = [
        f"; append_log_path={os.path.abspath(log_path)}",
        f"; append_log_index={log_entry['index']}",
        f"; append_log_previous_hash={log_entry['previous_hash']}",
        f"; append_log_entry_hash={log_entry['entry_hash']}",
    ]
    lines = gcode.splitlines()
    insertion_index = 1 if lines else 0
    for offset, line in enumerate(injected):
        lines.insert(insertion_index + offset, line)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    root = tk.Tk()
    app = BareBonesSlicerApp(root)
    root.mainloop()
