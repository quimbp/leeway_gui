#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Leeway Simulation GUI
====

An interactive Tkinter desktop application that drives an OpenDrift ``Leeway``
search-and-rescue drift simulation.

Project:	ICATMAR
Author:		Joaquim Ballabrera
Institution:	ICM-CSIC, Spain
Created:	2026-07-10
Version:	1.0

Description:
  * Choose the ocean current source: a local CF-compliant NetCDF file OR fetch
    surface currents from the ICATMAR ERDDAP server.
  * Load or manually enter release information (time, lon, lat).
  * Optionally add a NetCDF wind file.
  * Configure the simulation parameters (object type, title, current
    uncertainty, simulation length, time step, advection scheme).
  * Choose the output filenames (NetCDF trajectory, GIF animation, PNG image).
  * Run the simulation and produce NetCDF, PNG and GIF outputs.

License:
  This program is free software: you can redistribute it and/or modify
  it under the terms of the GNU General Public License as published by
  the Free Software Foundation, either version 3 of the License, or
  (at your option) any later version.

Requirements:
    pip install opendrift xarray netcdf4 cmocean trajan

Copyright (C) 2026 [Joaquim Ballabrera / ICM-CSIC,ICATMAR]
"""

import ast
import os
import queue
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ----
# CONFIGURATION / DEFAULTS
# ----
ERDDAP_URL = "https://erddap.icatmar.cat/erddap/griddap/predictions"
LOCAL_NC = "icatmar_predictions_currents.nc"

# Optional spatial sub-setting used for the ERDDAP download and for plotting.
DOMAINS_FILE = "domains.dat"

LONMIN, LONMAX = 1.5, 4.0     # degrees_east   (off the Catalan coast)
LATMIN, LATMAX = 40.5, 42.0   # degrees_north

SEED_RADIUS = 250             # meters
SEED_NUMBER = 100             # number of elements per release

# Advection schemes supported by OpenDrift's Leeway model.
ADVECTION_SCHEMES = ["euler", "runge-kutta", "runge-kutta4"]

CONFIG_FILE = "leeway.config"

DEFAULTS = {
    "object_type": 67,
    "title": "Leeway simulation",
    "seed_radius": SEED_RADIUS,
    "seed_number": SEED_NUMBER,
    "sim_length_hours": 12.0,
    "time_step_minutes": 10.0,
    "advection_scheme": "euler",
    "out_nc": "output-trajectory.nc",
    "out_gif": "output-trajectory.gif",
    "out_png": "output-trajectory.png",
}


def read_config_value(text):
    """Convert a configuration-file value to its Python representation."""
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return text.strip()


def load_or_create_leeway_config(filename=CONFIG_FILE):
    """Return configuration values, creating a full Leeway default file if needed.

    A temporary Leeway instance is deliberately created before the GUI so that a
    missing file reflects the installed OpenDrift version's defaults.
    """
    from opendrift.models.leeway import Leeway

    model = Leeway(loglevel=20)
    valid_keys = model._config.keys()
    config_file = Path(filename)

    if not config_file.exists():
        with config_file.open("w", encoding="utf-8") as file:
            file.write("# OpenDrift Leeway configuration file\n\n")
            for key in sorted(valid_keys):
                file.write(f"{key} = {model.get_config(key)!r}\n")
        print(f"Created configuration file: {config_file}")

    values = {}
    with config_file.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                print(f"Skipping invalid line {line_number}: {line}")
                continue
            key, value_text = line.split("=", 1)
            key = key.strip()
            if key not in valid_keys:
                print(f"Warning, line {line_number}: unknown configuration key: {key}")
                continue
            values[key] = read_config_value(value_text)

    print(f"Loaded configuration from: {config_file}")
    return values


def apply_leeway_config(model, values, log=print):
    """Apply all valid values read from leeway.config to *model*."""
    valid_keys = model._config.keys()
    for key, value in values.items():
        if key not in valid_keys:
            continue
        try:
            model.set_config(key, value)
        except Exception as exc:
            log(f"Could not set configuration {key} = {value!r}: {exc}")


# ----
# SIMULATION HELPERS (adapted from erddap_leeway.py)
# ----

def create_default_domains_file(filepath, lonmin, lonmax, latmin, latmax):  
    """Create a default domains.dat file with None and Default entries."""  
    DOMAINS_HEADER = "# Name      West    East    South   North\n"  
    DOMAINS_COMMENT = "# Use 'None' for all coordinates to auto-calculate from data\n" 
    with open(filepath, "w") as f:  
        f.write(DOMAINS_HEADER)  
        f.write(DOMAINS_COMMENT)  
        f.write(f"{'None':<12} {'None':>8} {'None':>8} {'None':>8} {'None':>8}\n")  
        f.write(f"{'Default':<12} {lonmin:>8.3f} {lonmax:>8.3f} {latmin:>8.3f} {latmax:>8.3f}\n")  
    print(f"[INFO] Created '{filepath}' with default entries.")  

def parse_domains(filepath):
    """
    Read domain definitions from a text file.
    Returns a dict: {name: (west, east, south, north)} or {name: None} for auto domains.
    """
    domains = {}
    with open(filepath, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue  # Skip empty lines and comments
            parts = line.split()
            name = parts[0]
            coords = parts[1:]
            if all(c.lower() == "none" for c in coords):
                domains[name] = None  # Auto-calculate from data
            else:
                west, east, south, north = map(float, coords)
                domains[name] = (west, east, south, north)
    return domains


def parse_release_text(text, source="release information"):
    """Parse release rows from a file or GUI text box.

    Each non-empty, non-comment line must begin with
    ``yyyy mm dd hh mm ss lon lat``. Extra columns are retained in a saved
    release file but are not used by OpenDrift.
    """
    releases = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 8:
            raise ValueError(
                f"Invalid line {line_number} in {source}: expected at least 8 columns "
                "(yyyy mm dd hh mm ss lon lat)."
            )
        try:
            year, month, day, hour, minute, second = map(int, parts[:6])
            seed_lon, seed_lat = map(float, parts[6:8])
            seed_time = datetime(year, month, day, hour, minute, second)
        except ValueError as exc:
            raise ValueError(
                f"Invalid date, time, or coordinate on line {line_number} in {source}: {line}"
            ) from exc
        if not (-180 <= seed_lon <= 180 and -90 <= seed_lat <= 90):
            raise ValueError(f"Invalid longitude/latitude on line {line_number} in {source}.")
        releases.append((seed_lon, seed_lat, seed_time))

    if not releases:
        raise ValueError(f"No valid release rows found in {source}.")
    return releases


def parse_initial_condition_file(path):
    """Read and parse an ASCII release file."""
    with open(path, "r", encoding="utf-8") as file:
        return parse_release_text(file.read(), str(path))


def save_release_file(path, text):
    """Validate and save GUI release text without overwriting on validation errors."""
    releases = parse_release_text(text)
    destination = Path(path)
    if not destination.parent.exists():
        raise ValueError(f"Release-file folder does not exist: {destination.parent}")
    with destination.open("w", encoding="utf-8", newline="\n") as file:
        file.write("# yyyy mm dd hh mm ss lon lat [optional data]\n")
        file.write(text.strip() + "\n")
    return releases


def download_erddap_currents(url=ERDDAP_URL, out_path=LOCAL_NC, log=print, domain=None):
    """Open the ERDDAP dataset over OPeNDAP, keep the U/V current components,
    optionally subset in space using *domain* = (lonmin, lonmax, latmin, latmax),
    and save a compact CF-compliant NetCDF file.

    Returns the path to the written NetCDF file.
    """
    import xarray as xr

    log(f"Opening ERDDAP dataset: {url}")
    with xr.open_dataset(url, engine="netcdf4") as ds:
        log(f"lon:   {ds.longitude.values.min()} -> {ds.longitude.values.max()}")
        log(f"lat:   {ds.latitude.values.min()} -> {ds.latitude.values.max()}")
        log(f"depth: {ds.depth.values}")
        log(f"time from {ds.time.values[0]} to {ds.time.values[-1]}")

        keep_vars = ["UO", "VO"]
        ds = ds[keep_vars]

        if domain is not None:
            lonmin, lonmax, latmin, latmax = domain
            lat_ascending = bool(ds.latitude.values[0] < ds.latitude.values[-1])
            lat_slice = (slice(latmin, latmax) if lat_ascending
                    else slice(latmax, latmin))
            ds = ds.sel(longitude=slice(lonmin, lonmax), latitude=lat_slice)
            log(f"Subset grid to lon[{lonmin},{lonmax}] lat[{latmin},{latmax}] "
                f"-> shape U{tuple(ds.UO.shape)}")

        ds["UO"].attrs["standard_name"] = "eastward_sea_water_velocity"
        ds["VO"].attrs["standard_name"] = "northward_sea_water_velocity"
        ds["UO"].attrs.setdefault("units", "m s-1")
        ds["VO"].attrs.setdefault("units", "m s-1")

        ds.load()

    log(f"Writing local CF NetCDF: {out_path}")
    ds.to_netcdf(out_path)
    ds.close()
    return out_path


def make_current_reader(nc_path):
    """Create an OpenDrift current reader from a CF NetCDF file."""
    from opendrift.readers import reader_netCDF_CF_generic

    reader = reader_netCDF_CF_generic.Reader(
        nc_path,
        standard_name_mapping={
            "eastward_sea_water_velocity": "x_sea_water_velocity",
            "northward_sea_water_velocity": "y_sea_water_velocity",
        },
    )
    return reader


def make_wind_reader(nc_path):
    """Create an OpenDrift wind reader from a CF NetCDF file (optional)."""
    from opendrift.readers import reader_netCDF_CF_generic

    reader = reader_netCDF_CF_generic.Reader(nc_path)
    return reader


# ----
# DOMAIN SELECTOR WIDGET (from momo.py)
# ----
class DomainSelectorFrame(ttk.LabelFrame):
    """A self-contained LabelFrame that lets the user pick a preset domain
    from domains.dat or enter a fully custom bounding box (W, E, S, N)."""

    def __init__(self, parent, domains, default_extent=(LONMIN, LONMAX, LATMIN, LATMAX), **kwargs):
        super().__init__(parent, text="0. Domain Selection", **kwargs)
        self.domains = domains
        self.default_extent = default_extent
        self.mode = tk.StringVar(value="preset")

        # Column layout:
        # 0: radiobutton label   1: combobox / spacer
        # 2: "W"  3: W field   4: "E"  5: E field
        # 6: "S"  7: S field   8: "N"  9: N field

        # --- Row 0: column headers ---
        for col, lbl in zip([3, 5, 7, 9], ["W (lon)", "E (lon)", "S (lat)", "N (lat)"]):
            ttk.Label(self, text=lbl, foreground="gray").grid(
                row=0, column=col, sticky="s", padx=4, pady=(4, 1))

        # --- Row 1: Preset ---
        ttk.Radiobutton(
            self, text="Preset:", variable=self.mode, value="preset",
            command=self._on_mode_change,
        ).grid(row=1, column=0, sticky="w", padx=(6, 2), pady=3)

        domain_names = list(self.domains.keys())
        self.combo_var = tk.StringVar(value=domain_names[0] if domain_names else "")
        self.combo = ttk.Combobox(
            self, textvariable=self.combo_var,
            values=domain_names, state="readonly", width=14,
        )
        self.combo.grid(row=1, column=1, padx=(2, 10), pady=3)
        self.combo.bind("<<ComboboxSelected>>", self._on_combo_change)

        self.preset_vars = []
        for col in [3, 5, 7, 9]:
            var = tk.StringVar()
            ttk.Label(self, textvariable=var, width=8, anchor="center",
                    relief="sunken", background="white", foreground="navy",
                    ).grid(row=1, column=col, padx=3, pady=3)
            self.preset_vars.append(var)

        # --- Row 2: Custom ---
        ttk.Radiobutton(
            self, text="Custom:", variable=self.mode, value="custom",
            command=self._on_mode_change,
        ).grid(row=2, column=0, sticky="w", padx=(6, 2), pady=3)

        self.custom_entries = []
        for col in [3, 5, 7, 9]:
            var = tk.StringVar()
            entry = ttk.Entry(self, textvariable=var, width=9,
                    state="disabled", justify="center")
            entry.grid(row=2, column=col, padx=3, pady=3)
            self.custom_entries.append((var, entry))

        # --- Initialize display ---
        self._update_preset_display()
        self._on_mode_change()

    # ---- internal helpers ----
    def _on_combo_change(self, event=None):
        self._update_preset_display()

    def _update_preset_display(self):
        name = self.combo_var.get()
        extent = self.domains.get(name)
        if extent is None:
            for var in self.preset_vars:
                var.set("auto")
        else:
            for var, val in zip(self.preset_vars, extent):
                var.set(f"{val:.3f}")

    def _on_mode_change(self):
        if self.mode.get() == "preset":
            self.combo.configure(state="readonly")
            for _, entry in self.custom_entries:
                entry.configure(state="disabled")
        else:
            self.combo.configure(state="disabled")
            for _, entry in self.custom_entries:
                entry.configure(state="normal")

    # ---- public API ----
    def get_extent(self):
        """Return (west, east, south, north) or None for auto/invalid."""
        if self.mode.get() == "preset":
            return self.domains.get(self.combo_var.get())   # may be None (auto)
        else:
            try:
                return tuple(float(var.get()) for var, _ in self.custom_entries)
            except ValueError:
                return None


# ----
# THE GUI APPLICATION
# ----
class LeewayGUI(tk.Tk):
    def __init__(self, config_values=None):
        super().__init__()
        self.config_values = config_values or {}
        self.title("OpenDrift / Leeway Simulation")
        self.geometry("880x820")
        self.minsize(780, 700)
        self.img = None

        # Runtime state
        self.releases = None            # parsed list of (lon, lat, datetime)
        self.log_queue = queue.Queue()  # thread-safe log messages
        self.worker = None              # background simulation thread

        # Tk variables

        self.current_source = tk.StringVar(value="erddap")   # "erddap" | "local"
        self.current_file = tk.StringVar(value="")
        self.release_source = tk.StringVar(value="file")  # "file" | "manual"
        self.release_file = tk.StringVar(value="")
        self.wind_file = tk.StringVar(value="")

        self.object_type = tk.StringVar(value=str(DEFAULTS["object_type"]))
        self.title_var = tk.StringVar(value=DEFAULTS["title"])
        # These GUI-controlled settings use leeway.config as their first guess.
        self.current_uncertainty = tk.StringVar(value=str(
            self.config_values.get("drift:current_uncertainty", 0.0)))
        self.sim_length = tk.StringVar(value=str(DEFAULTS["sim_length_hours"]))
        self.time_step = tk.StringVar(value=str(
            self.config_values.get("general:time_step_minutes", DEFAULTS["time_step_minutes"])))
        self.advection_scheme = tk.StringVar(value=str(
            self.config_values.get("drift:advection_scheme", DEFAULTS["advection_scheme"])))

        self.seed_radius = tk.StringVar(value=str(DEFAULTS["seed_radius"]))
        self.seed_number = tk.StringVar(value=str(
            self.config_values.get("seed:number", DEFAULTS["seed_number"])))

        self.out_nc = tk.StringVar(value=DEFAULTS["out_nc"])
        self.out_gif = tk.StringVar(value=DEFAULTS["out_gif"])
        self.out_png = tk.StringVar(value=DEFAULTS["out_png"])

        self.status_var = tk.StringVar(value="Ready.")

        # Load domains for the domain selector
        self._domains = parse_domains(DOMAINS_FILE)

        self._build_ui()
        self._toggle_current_source()
        self._toggle_release_source()
        self.after(150, self._drain_log_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- UI construction ----
    def _on_close(self):
        """Close the application only when no simulation worker is active."""
        if self.worker and self.worker.is_alive():
            messagebox.showwarning(
            "Simulation running",
            "Please wait for the simulation to finish before closing the application.",
            )
            return
        try:
            import matplotlib.pyplot as plt
            plt.close("all")
        except Exception:
            pass
        self.destroy()

    def _build_ui(self):
        # A scrollable main container so everything fits on smaller screens.
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        vscroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.scroll_frame = ttk.Frame(canvas)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=self.scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=vscroll.set)

        canvas.pack(side="left", fill="both", expand=True)
        vscroll.pack(side="right", fill="y")

        # Mouse-wheel scrolling support
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        pad = {"padx": 8, "pady": 4}
        root = self.scroll_frame

        # --- Section 0: Domain Selection ---
        self.domain_selector = DomainSelectorFrame(
            root,
            domains=self._domains,
            default_extent=(LONMIN, LONMAX, LATMIN, LATMAX),
        )
        self.domain_selector.pack(fill="x", **pad)

        # --- Section 1: Ocean current input ---
        cur_frame = ttk.LabelFrame(root, text="1. Ocean Currents (required)")
        cur_frame.pack(fill="x", **pad)

        ttk.Radiobutton(
            cur_frame, text="Fetch currents from ERDDAP (ICATMAR)",
            variable=self.current_source, value="erddap",
            command=self._toggle_current_source,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=2)

        ttk.Radiobutton(
            cur_frame, text="Use a local NetCDF current file",
            variable=self.current_source, value="local",
            command=self._toggle_current_source,
        ).grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=2)

        self.current_entry = ttk.Entry(cur_frame, textvariable=self.current_file, width=64)
        self.current_entry.grid(row=2, column=0, columnspan=2, sticky="we", padx=6, pady=2)
        self.current_browse = ttk.Button(
            cur_frame, text="Browse...", command=self._browse_current_file)
        self.current_browse.grid(row=2, column=2, sticky="e", padx=6, pady=2)
        cur_frame.columnconfigure(0, weight=1)

        # --- Section 2: Release information ---
        rel_frame = ttk.LabelFrame(root, text="2. Release Information (required)")
        rel_frame.pack(fill="x", **pad)
        rel_frame.columnconfigure(0, weight=1)

        ttk.Label(
            rel_frame,
            text="One release per line: yyyy mm dd hh mm ss lon lat (optional trailing data allowed)",
        ).grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=2)

        ttk.Radiobutton(rel_frame, text="Load an existing release file", variable=self.release_source,
                        value="file", command=self._toggle_release_source).grid(
            row=1, column=0, sticky="w", padx=6, pady=2)
        self.release_entry = ttk.Entry(rel_frame, textvariable=self.release_file, width=58)
        self.release_entry.grid(row=2, column=0, columnspan=2, sticky="we", padx=6, pady=2)
        self.release_browse = ttk.Button(rel_frame, text="Browse...", command=self._browse_release_file)
        self.release_browse.grid(row=2, column=2, sticky="e", padx=6, pady=2)

        ttk.Radiobutton(rel_frame, text="Enter release information here and save it as a file", variable=self.release_source,
                        value="manual", command=self._toggle_release_source).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 2))
        self.release_text = scrolledtext.ScrolledText(rel_frame, height=5, wrap="none")
        self.release_text.grid(row=4, column=0, columnspan=4, sticky="we", padx=6, pady=2)
        self.release_save = ttk.Button(rel_frame, text="Save entered release file...", command=self._save_entered_release_file)
        self.release_save.grid(row=5, column=0, sticky="w", padx=6, pady=2)

        ttk.Label(rel_frame, text="Parsed releases:").grid(
            row=6, column=0, sticky="w", padx=6, pady=(6, 0))
        self.release_preview = scrolledtext.ScrolledText(rel_frame, height=5, wrap="none", state="disabled")
        self.release_preview.grid(row=7, column=0, columnspan=4, sticky="we", padx=6, pady=4)

        # --- Section 3: Wind file (optional) ---
        wind_frame = ttk.LabelFrame(root, text="3. Wind File (optional)")
        wind_frame.pack(fill="x", **pad)

        ttk.Label(
            wind_frame,
            text="Optional NetCDF wind file. Leave empty to use zero wind.",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=2)

        ttk.Entry(wind_frame, textvariable=self.wind_file, width=64).grid(
            row=1, column=0, columnspan=2, sticky="we", padx=6, pady=2)
        ttk.Button(wind_frame, text="Browse...", command=self._browse_wind_file).grid(
            row=1, column=2, sticky="e", padx=6, pady=2)
        ttk.Button(wind_frame, text="Clear", command=lambda: self.wind_file.set("")).grid(
            row=1, column=3, sticky="e", padx=6, pady=2)
        wind_frame.columnconfigure(0, weight=1)

        # --- Section 4: Simulation parameters ---
        par_frame = ttk.LabelFrame(root, text="4. Simulation Parameters")
        par_frame.pack(fill="x", **pad)

        ttk.Label(par_frame, text="Object type (1-120):").grid(
            row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Spinbox(par_frame, from_=1, to=120, textvariable=self.object_type,
                    width=10).grid(row=0, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Simulation title:").grid(
            row=0, column=2, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.title_var, width=30).grid(
            row=0, column=3, sticky="we", padx=6, pady=3)

        ttk.Label(par_frame, text="Seed radius (meters):").grid(
            row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.seed_radius, width=12).grid(
            row=1, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Seed number:").grid(
            row=1, column=2, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.seed_number, width=12).grid(
            row=1, column=3, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Current uncertainty:").grid(
            row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.current_uncertainty, width=12).grid(
            row=2, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Simulation length (hours):").grid(
            row=2, column=2, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.sim_length, width=12).grid(
            row=2, column=3, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Time step (minutes):").grid(
            row=3, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(par_frame, textvariable=self.time_step, width=12).grid(
            row=3, column=1, sticky="w", padx=6, pady=3)

        ttk.Label(par_frame, text="Advection scheme:").grid(
            row=3, column=2, sticky="w", padx=6, pady=3)
        ttk.Combobox(par_frame, textvariable=self.advection_scheme,
                    values=ADVECTION_SCHEMES, state="readonly", width=27).grid(
            row=3, column=3, sticky="we", padx=6, pady=3)
        par_frame.columnconfigure(3, weight=1)

        # --- Section 5: Output filenames ---
        out_frame = ttk.LabelFrame(root, text="5. Output Filenames")
        out_frame.pack(fill="x", **pad)

        ttk.Label(out_frame, text="NetCDF trajectory:").grid(
            row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out_frame, textvariable=self.out_nc, width=48).grid(
            row=0, column=1, sticky="we", padx=6, pady=3)
        ttk.Button(out_frame, text="Save as...",
                   command=lambda: self._browse_save(self.out_nc, ".nc")).grid(
            row=0, column=2, padx=6, pady=3)

        ttk.Label(out_frame, text="GIF animation:").grid(
            row=1, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out_frame, textvariable=self.out_gif, width=48).grid(
            row=1, column=1, sticky="we", padx=6, pady=3)
        ttk.Button(out_frame, text="Save as...",
                   command=lambda: self._browse_save(self.out_gif, ".gif")).grid(
            row=1, column=2, padx=6, pady=3)

        ttk.Label(out_frame, text="PNG image:").grid(
            row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(out_frame, textvariable=self.out_png, width=48).grid(
            row=2, column=1, sticky="we", padx=6, pady=3)
        ttk.Button(out_frame, text="Save as...",
                   command=lambda: self._browse_save(self.out_png, ".png")).grid(
            row=2, column=2, padx=6, pady=3)
        out_frame.columnconfigure(1, weight=1)

        # --- Run button + status ---
        action_frame = ttk.Frame(root)
        action_frame.pack(fill="x", **pad)

        self.run_button = ttk.Button(
            action_frame, text="Run Simulation", command=self._on_run)
        self.run_button.pack(side="left", padx=6, pady=4)

        self.progress = ttk.Progressbar(action_frame, mode="indeterminate", length=180)
        self.progress.pack(side="left", padx=6)

        ttk.Label(action_frame, textvariable=self.status_var,
                  foreground="#0a5").pack(side="left", padx=10)

        # --- Log output ---
        log_frame = ttk.LabelFrame(root, text="Log / Status Messages")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_widget = scrolledtext.ScrolledText(log_frame, height=12, wrap="word",
                    state="disabled")
        self.log_widget.pack(fill="both", expand=True, padx=6, pady=6)

    # ---- UI callbacks ----
    def _toggle_current_source(self):
        local = self.current_source.get() == "local"
        state = "normal" if local else "disabled"
        self.current_entry.configure(state=state)
        self.current_browse.configure(state=state)

    def _browse_current_file(self):
        path = filedialog.askopenfilename(
            title="Select NetCDF ocean current file",
            filetypes=[("NetCDF files", "*.nc"), ("All files", "*.*")],
        )
        if path:
            self.current_file.set(path)

    def _browse_wind_file(self):
        path = filedialog.askopenfilename(
            title="Select NetCDF wind file (optional)",
            filetypes=[("NetCDF files", "*.nc"), ("All files", "*.*")],
        )
        if path:
            self.wind_file.set(path)

    def _toggle_release_source(self):
        """Enable only the controls relevant to the selected release source."""
        from_file = self.release_source.get() == "file"
        self.release_entry.configure(state="normal" if from_file else "disabled")
        self.release_browse.configure(state="normal" if from_file else "disabled")
        self.release_text.configure(state="disabled" if from_file else "normal")
        self.release_save.configure(state="disabled" if from_file else "normal")

    def _browse_release_file(self):
        path = filedialog.askopenfilename(
            title="Select ASCII release file",
            filetypes=[("Text/ASCII files", "*.txt *.asc *.dat"), ("All files", "*.*")],
        )
        if path:
            self.release_file.set(path)
            try:
                self._show_release_preview(parse_initial_condition_file(path), f"Release file loaded: {path}")
            except (OSError, ValueError) as exc:
                self._show_release_error(exc)

    def _save_entered_release_file(self):
        text = self.release_text.get("1.0", "end-1c")
        path = filedialog.asksaveasfilename(
            title="Save entered release information",
            defaultextension=".txt",
            initialfile="release.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.releases = save_release_file(path, text)
            self.release_file.set(path)
            self._show_release_preview(self.releases, f"Release file saved: {path}")
        except (OSError, ValueError) as exc:
            self._show_release_error(exc)
            messagebox.showerror("Release file error", str(exc))

    def _show_release_preview(self, releases, status):
        self.releases = releases
        self.release_preview.configure(state="normal")
        self.release_preview.delete("1.0", "end")
        lines = [f"Loaded {len(releases)} release point(s):"]
        lines.extend(
            f"  #{i}: time={time}   lon={lon}   lat={lat}"
            for i, (lon, lat, time) in enumerate(releases, start=1)
        )
        self.release_preview.insert("end", "\n".join(lines))
        self.release_preview.configure(state="disabled")
        self._set_status(status)

    def _show_release_error(self, exc):
        self.releases = None
        self.release_preview.configure(state="normal")
        self.release_preview.delete("1.0", "end")
        self.release_preview.insert("end", f"ERROR parsing release information:\n{exc}")
        self.release_preview.configure(state="disabled")
        self._set_status("Failed to parse release information.", error=True)

    def _browse_save(self, var, ext):
        path = filedialog.asksaveasfilename(
            title="Choose output file",
            defaultextension=ext,
            initialfile=os.path.basename(var.get()) or ("output" + ext),
            filetypes=[(f"*{ext}", f"*{ext}"), ("All files", "*.*")],
        )
        if path:
            var.set(path)

    def _load_release_preview(self, path):
        self.release_preview.configure(state="normal")
        self.release_preview.delete("1.0", "end")
        try:
            self.releases = parse_initial_condition_file(path)
            lines = [f"Loaded {len(self.releases)} release point(s):"]
            for i, (lon, lat, t) in enumerate(self.releases, start=1):
                lines.append(f"  #{i}:  time={t}   lon={lon}   lat={lat}")
            self.release_preview.insert("end", "\n".join(lines))
            self._set_status(f"Release file loaded: {len(self.releases)} point(s).")
        except Exception as exc:
            self.releases = None
            self.release_preview.insert("end", f"ERROR parsing release file:\n{exc}")
            self._set_status("Failed to parse release file.", error=True)
        finally:
            self.release_preview.configure(state="disabled")

    # ---- logging ----
    def _log(self, message):
        """Thread-safe log: push to a queue drained by the Tk main loop."""
        self.log_queue.put(str(message))

    def _drain_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_widget.configure(state="normal")
                self.log_widget.insert("end", msg + "\n")
                self.log_widget.see("end")
                self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._drain_log_queue)

    def _set_status(self, text, error=False):
        self.status_var.set(text)

    # ---- domain helper ----
    def _get_domain(self):
        """Return (lonmin, lonmax, latmin, latmax) from the domain selector,
        or None if the domain is set to auto (None preset)."""
        extent = self.domain_selector.get_extent()
        if extent is None:
            return None   # auto — no spatial subsetting
        west, east, south, north = extent
        return west, east, south, north

    # ---- input validation ----
    def _validate_inputs(self):
        """Validate all fields; returns a params dict or raises ValueError."""
        # Domain
        domain = self._get_domain()
        if domain is not None:
            west, east, south, north = domain
            if west >= east:
                raise ValueError("Domain: West longitude must be less than East longitude.")
            if south >= north:
                raise ValueError("Domain: South latitude must be less than North latitude.")

        # Currents
        source = self.current_source.get()
        current_file = self.current_file.get().strip()
        if source == "local":
            if not current_file:
                raise ValueError("Please select a local NetCDF current file.")
            if not os.path.isfile(current_file):
                raise ValueError(f"Current file not found:\n{current_file}")

        # Release information. Manual entries must be saved first so every run
        # has a reproducible release file.
        release_file = self.release_file.get().strip()
        if self.release_source.get() == "manual":
            if not release_file or not os.path.isfile(release_file):
                raise ValueError("Save the entered release information as a release file before running.")
        elif not release_file:
            raise ValueError("Please select an ASCII release file.")
        elif not os.path.isfile(release_file):
            raise ValueError(f"Release file not found:\n{release_file}")

        # Wind file (optional)
        wind_file = self.wind_file.get().strip()
        if wind_file and not os.path.isfile(wind_file):
            raise ValueError(f"Wind file not found:\n{wind_file}")

        # Numeric parameters
        try:
            object_type = int(self.object_type.get())
        except ValueError:
            raise ValueError("Object type must be an integer between 1 and 120.")
        if not (1 <= object_type <= 120):
            raise ValueError("Object type must be between 1 and 120.")

        try:
            current_uncertainty = float(self.current_uncertainty.get())
        except ValueError:
            raise ValueError("Current uncertainty must be a number.")
        if current_uncertainty < 0:
            raise ValueError("Current uncertainty must be >= 0.")

        try:
            sim_length = float(self.sim_length.get())
        except ValueError:
            raise ValueError("Simulation length must be a number (hours).")
        if sim_length <= 0:
            raise ValueError("Simulation length must be > 0 hours.")

        try:
            time_step = float(self.time_step.get())
        except ValueError:
            raise ValueError("Time step must be a number (minutes).")
        if time_step <= 0:
            raise ValueError("Time step must be > 0 minutes.")

        advection_scheme = self.advection_scheme.get()
        if advection_scheme not in ADVECTION_SCHEMES:
            raise ValueError("Invalid advection scheme.")

        try:
            seed_radius = float(self.seed_radius.get())
        except ValueError:
            raise ValueError("Seed radius must be a number (meters).")
        if seed_radius <= 0:
            raise ValueError("Seed radius must be > 0 meters.")

        try:
            seed_number = int(self.seed_number.get())
        except ValueError:
            raise ValueError("Seed number must be an integer.")
        if seed_number <= 0:
            raise ValueError("Seed number must be > 0.")

        out_nc = self.out_nc.get().strip() or DEFAULTS["out_nc"]
        out_gif = self.out_gif.get().strip() or DEFAULTS["out_gif"]
        out_png = self.out_png.get().strip() or DEFAULTS["out_png"]
        for label, output_path in (("NetCDF", out_nc), ("GIF", out_gif), ("PNG", out_png)):
            parent = Path(output_path).expanduser().parent
            if not parent.exists():
                raise ValueError(f"{label} output folder does not exist: {parent}")

        return {
            "source": source,
            "current_file": current_file,
            "release_file": release_file,
            "wind_file": wind_file,
            "seed_radius": seed_radius,
            "seed_number": seed_number,
            "object_type": object_type,
            "title": self.title_var.get().strip(),
            "current_uncertainty": current_uncertainty,
            "sim_length": sim_length,
            "time_step": time_step,
            "advection_scheme": advection_scheme,
            "out_nc": out_nc,
            "out_gif": out_gif,
            "out_png": out_png,
            "config_values": self.config_values.copy(),
            "domain": domain,   # (lonmin, lonmax, latmin, latmax) or None
        }

    # ---- run ----
    def _on_run(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Busy", "A simulation is already running.")
            return
        try:
            params = self._validate_inputs()
        except ValueError as exc:
            messagebox.showerror("Invalid input", str(exc))
            self._set_status("Fix the highlighted input and try again.", error=True)
            return

        # Make sure releases are parsed (re-parse for freshness).
        try:
            params["releases"] = parse_initial_condition_file(params["release_file"])
        except Exception as exc:
            messagebox.showerror("Release file error", str(exc))
            return

        self.run_button.configure(state="disabled")
        self.progress.start(12)
        self._set_status("Running simulation...")
        self._log("=" * 60)
        self._log("Starting Leeway simulation...")

        self.worker = threading.Thread(
            target=self._run_simulation, args=(params,), daemon=True)
        self.worker.start()

    def _run_simulation(self, params):
        """Runs in a background thread. Never touches Tk widgets directly."""
        try:
            import opendrift
            self._log(f"OpenDrift version: {opendrift.__version__}")
            from opendrift.models.leeway import Leeway

            # 1. Resolve the ocean current source.
            domain = params["domain"]   # (lonmin, lonmax, latmin, latmax) or None
            if domain is not None:
                self._log(f"Domain: W={domain[0]}  E={domain[1]}  "
                    f"S={domain[2]}  N={domain[3]}")
            else:
                self._log("Domain: auto (no spatial subsetting)")

            if params["source"] == "local":
                nc_path = params["current_file"]
                self._log(f"Using local NetCDF current file: {nc_path}")
            else:
                self._log("Downloading currents from ERDDAP...")
                nc_path = download_erddap_currents(log=self._log, domain=domain)

            current_reader = make_current_reader(nc_path)
            self._log(str(current_reader))

            # 2. Set up the Leeway model.
            o = Leeway(loglevel=20)
            apply_leeway_config(o, params["config_values"], self._log)
            o.add_reader(current_reader)

            # Wind: optional reader, otherwise zero-wind fallback.
            if params["wind_file"]:
                self._log(f"Adding wind reader: {params['wind_file']}")
                wind_reader = make_wind_reader(params["wind_file"])
                o.add_reader(wind_reader)
                self._log(str(wind_reader))
            else:
                self._log("No wind file provided -> using zero-wind fallback.")
                o.set_config("environment:fallback:x_wind", 0)
                o.set_config("environment:fallback:y_wind", 0)

            # Current fallback outside grid coverage.
            o.set_config("environment:fallback:x_sea_water_velocity", 0)
            o.set_config("environment:fallback:y_sea_water_velocity", 0)

            # Drift configuration.
            o.set_config("drift:current_uncertainty", params["current_uncertainty"])
            try:
                o.set_config("drift:advection_scheme", params["advection_scheme"])
            except Exception as exc:
                self._log(f"Could not set advection scheme "
                    f"'{params['advection_scheme']}': {exc}")

            # 3. Seed releases.
            releases = params["releases"]
            self._log(f"Seeding {len(releases)} release point(s)...")
            for i, (seed_lon, seed_lat, seed_time) in enumerate(releases, start=1):
                if (seed_time < current_reader.start_time
                    or seed_time > current_reader.end_time):
                    raise ValueError(
                    f"Release #{i}: seed time {seed_time} is outside the "
                    f"available current data range "
                    f"[{current_reader.start_time}, {current_reader.end_time}]"
                    )
                self._log(f"  #{i}: lon={seed_lon}, lat={seed_lat}, time={seed_time}")
                o.seed_elements(
                    lon=seed_lon,
                    lat=seed_lat,
                    radius=params["seed_radius"],
                    number=params["seed_number"],
                    time=seed_time,
                    object_type=params["object_type"],
                )

            # 4. Run.
            duration = timedelta(hours=params["sim_length"])
            time_step_seconds = params["time_step"] * 60.0
            self._log(f"Running: duration={duration}, "
                    f"time_step={time_step_seconds:.0f}s, "
                    f"scheme={params['advection_scheme']}")

            out_nc = params["out_nc"]
            o.run(
                duration=duration,
                time_step=time_step_seconds,
                time_step_output=3600,
                outfile=out_nc,
            )
            self._log(f"Simulation finished. Trajectory written to: {out_nc}")

            # 5. Plot / animate.
            #corners = [LONMIN, LONMAX, LATMIN, LATMAX]
            try:
                self._log(f"Saving PNG plot: {params['out_png']}")
                o.plot(corners=domain, fast=True, filename=params["out_png"])
            except Exception as exc:
                self._log(f"PNG plotting skipped: {exc}")

            try:
                import cmocean
                cmap = cmocean.cm.speed
            except Exception:
                cmap = None
            try:
                self._log(f"Saving GIF animation: {params['out_gif']}")
                o.animation(
                    background=["x_sea_water_velocity", "y_sea_water_velocity"],
                    corners=domain,
                    skip=5,
                    cmap=cmap,
                    bgalpha=0.7,
                    land_color="#6666",
                    fast=True,
                    filename=params["out_gif"],
                )
            except Exception as exc:
                self._log(f"GIF animation skipped: {exc}")

            self._log("=" * 60)
            self._log("DONE.")

            self.after(0, lambda: self._on_finished(True, params))

        except Exception as exc:
            error_msg = str(exc)
            tb = traceback.format_exc()
            self._log("ERROR during simulation:")
            self._log(tb)
            self.after(0, lambda message=error_msg: self._on_finished(False, params, message),)

    def _on_finished(self, success, params, error_msg=None):
        self.progress.stop()
        self.run_button.configure(state="normal")
        if success:
            self._set_status("Simulation completed successfully.")
            messagebox.showinfo(
                "Success",
                "Simulation completed.\n\n"
                f"NetCDF: {params['out_nc']}\n"
                f"PNG:    {params['out_png']}\n"
                f"GIF:    {params['out_gif']}",
            )
        else:
            self._set_status("Simulation failed. See log.", error=True)
            messagebox.showerror("Simulation failed", error_msg or "Unknown error.")


def check_or_create_domains_file(filepath, lonmin, lonmax, latmin, latmax):  
    """Check if domains file exists; create it with defaults if not."""  
    if not os.path.exists(filepath):  
        print(f"[INFO] '{filepath}' not found. Creating it...")  
        create_default_domains_file(filepath, lonmin, lonmax, latmin, latmax)  
    else:  
        print(f"[INFO] '{filepath}' found. Reading domains...")  
  

def main():
    check_or_create_domains_file(DOMAINS_FILE, LONMIN, LONMAX, LATMIN, LATMAX)
    try:
        config_values = load_or_create_leeway_config(CONFIG_FILE)
    except Exception as exc:
        # Keep the GUI usable for input preparation when OpenDrift is not
        # installed or its configuration cannot yet be loaded.
        print(f"Could not load {CONFIG_FILE}; using GUI defaults: {exc}")
        config_values = {}
    app = LeewayGUI(config_values=config_values)
    app.mainloop()


if __name__ == "__main__":
    main()
