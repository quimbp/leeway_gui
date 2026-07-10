# OpenDrift Leeway Simulation GUI

A desktop graphical interface for running **OpenDrift Leeway** search-and-rescue drift simulations. The application supports one or more release points, optional wind forcing, either local ocean-current files or ICATMAR ERDDAP currents, and creation of NetCDF, PNG, and GIF outputs.

## Contents

| File | Purpose |
|---|---|
| `leeway_gui.py` | Main GUI application. |
| `requirements.txt` | Python package requirements for a dedicated virtual environment. |
| `domains.dat` | Optional named geographical domains. Created automatically with a `None` and `Default` domain if missing. |
| `leeway.config` | OpenDrift Leeway configuration. Created automatically on the first successful start with OpenDrift installed. |
| `release_example.txt` | Example release-information file. |

## Requirements

* **Python 3.11** is the recommended interpreter version.
* A desktop session capable of opening Tkinter windows.
  * Windows and standard macOS Python installations normally include Tkinter.
  * On Debian/Ubuntu systems it is commonly supplied by the operating-system package `python3-tk`.
* Internet access is required only when **Fetch currents from ERDDAP (ICATMAR)** is selected.
* Write permission in the folder containing the application and in the selected output folders.

## Installation

### 1. Obtain the source

Clone or download this repository, then open a terminal in the repository folder.

### 2. Create and activate a dedicated virtual environment

**Windows PowerShell**

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**Windows Command Prompt**

```bat
py -3.11 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

**macOS / Linux**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### 3. Start the application

```bash
python "leeway_gui.py"
```

On the first successful start, the program creates `domains.dat` if necessary. It also creates `leeway.config` from the installed OpenDrift version's available Leeway settings when no configuration file is present.

## Quick start

1. Start the GUI.
2. Select a spatial domain:
   * Use a preset domain from `domains.dat`;
   * select **Custom** and enter West, East, South, and North; or
   * choose the `None` preset to avoid spatial subsetting.
3. Select an ocean-current source:
   * **Fetch currents from ERDDAP** downloads ICATMAR currents for the chosen domain; or
   * **Use a local NetCDF current file** uses a local CF-compliant NetCDF file.
4. Provide release information:
   * load an existing release file; or
   * select the manual-entry option, paste/type release lines, then click **Save entered release file...**.
5. Optionally select a wind NetCDF file. If no wind file is selected, the model uses zero-wind fallback values.
6. Review simulation parameters and output paths.
7. Click **Run Simulation**. Progress and diagnostics appear in the log panel.

## Release information

Each release occupies one non-comment line. The first eight whitespace-separated columns are required:

```text
yyyy mm dd hh mm ss lon lat
```

* `yyyy mm dd hh mm ss` is the UTC release timestamp.
* `lon` is longitude in decimal degrees, within -180 to 180.
* `lat` is latitude in decimal degrees, within -90 to 90.
* Blank lines and lines beginning with `#` are ignored.
* Additional columns after latitude may be included and are retained when manual entries are saved, but only the first eight fields are used to position the release.

Example:

```text
# UTC release time             longitude latitude
2026 07 10 12 00 00  2.1734    41.3851
2026 07 10 13 00 00  2.1800    41.3900  second_release
```

### Manual release entry

The GUI validates every entered line when it is saved. Saving produces a standard text release file with a short header and makes that file the active release source. A manual release must be saved before a run can start; this preserves the exact release input used for each simulation.

## Input data

### Ocean currents

The current reader expects a CF-compliant NetCDF dataset. For the ERDDAP workflow, the application requests the `UO` and `VO` components and maps their standard names to OpenDrift sea-water velocity variables. A local current file must be readable by OpenDrift's generic CF NetCDF reader and provide compatible eastward and northward current variables.

The selected release time must fall within the time coverage of the current dataset. The application stops the run with an explanatory error if it does not.

### Wind

A wind file is optional. If supplied, it must be a CF-compliant NetCDF dataset readable by OpenDrift. Without one, the simulation uses zero wind; this is a modelling choice and should be considered carefully for operational use.

### Domains

`domains.dat` has one domain per line:

```text
Name  West  East  South  North
```

Use `None` in every coordinate column for an automatic/full-data domain:

```text
None     None  None  None  None
Catalan  1.500 4.000 40.500 42.000
```

For custom and named domains, West must be less than East and South must be less than North.

## Configuration

`leeway.config` contains OpenDrift Leeway configuration values. It is generated using the installed OpenDrift version, so keys and defaults remain aligned with that version. Keep this file under version control only when the team intends to share a common modelling configuration.

GUI controls override the applicable runtime settings for the current uncertainty, advection scheme, seed radius, seed number, simulation duration, and time step.

## Outputs

A successful run normally writes:

| Output | Description |
|---|---|
| NetCDF (`.nc`) | Trajectory data produced by OpenDrift. This is the primary scientific output. |
| PNG (`.png`) | Static visualisation of the simulation. |
| GIF (`.gif`) | Animation of the simulation. |

The NetCDF trajectory is written first. PNG/GIF rendering failures are logged and do not invalidate an otherwise successful trajectory run. Store all outputs, the saved release file, the relevant `leeway.config`, and a record of input current/wind data for reproducibility.

## Operational and quality notes

* This software is a modelling and visualisation tool. Results depend on the forcing data, selected leeway object type, uncertainty settings, release information, and model configuration.
* Check time zones, coordinate reference conventions, data coverage, and forcing-data quality before interpreting results.
* A zero-wind run is not equivalent to a wind-informed run.
* ERDDAP availability, network connectivity, and remote dataset content are outside the application's control. Keep local copies of forcing data for reproducible or offline workflows.
* Large areas, long durations, many particles, and high-resolution forcing data can require substantial memory and runtime.
* The GUI runs the simulation in a background thread so that the interface and log remain responsive. Avoid closing the application until the current run finishes.

## Troubleshooting

### The window does not open / Tkinter is missing

Install the system Tk package (for example `python3-tk` on Debian/Ubuntu), then recreate or reuse the virtual environment.

### `ModuleNotFoundError` or OpenDrift import error

Activate the project virtual environment and reinstall requirements:

```bash
python -m pip install -r requirements.txt
```

### A release is rejected

Confirm that each non-comment line begins with exactly valid numeric timestamp and coordinate fields, and that the timestamp is valid. Confirm longitude and latitude are supplied in that order.

### The release is outside the current data range

Choose a current dataset covering the release time, alter the release time, or obtain forcing data with the required temporal coverage.

### Current or wind file cannot be read

Verify the file is a complete CF-compliant NetCDF file, is not corrupted, and contains the required time and velocity/wind metadata. Inspect the GUI log for OpenDrift's detailed reader message.

### ERDDAP download fails

Verify network connectivity and retry later. Use a local current NetCDF file if the remote service is unavailable or if a fully reproducible workflow is required.

### PNG or GIF generation fails

The trajectory NetCDF may still have been written successfully. Review the log, verify that the selected output folder exists and is writable, and check the installed graphical/scientific package versions.

## Reproducible team environments

`requirements.txt` intentionally specifies conservative compatibility ranges rather than an unverified machine-specific lockfile. After a team member has successfully validated the application on the target operating system, capture the exact resolved environment for that release:

```bash
python -m pip freeze > requirements-lock.txt
```

Commit `requirements-lock.txt` with a tag or release note and install it on matching platforms with:

```bash
python -m pip install -r requirements-lock.txt
```

A lockfile can be platform-specific, especially for NetCDF and geospatial packages. Retain the compatibility-range `requirements.txt` as the normal installation entry point.

## Recommended GitHub release checklist

Before publishing a release:

1. Run one small simulation using a local current file.
2. Run one small simulation using the ERDDAP path, if remote access is part of the intended workflow.
3. Test both loaded and manually entered/saved release information.
4. Confirm NetCDF, PNG, and GIF outputs are produced or clearly logged.
5. Record the exact environment with `pip freeze > requirements-lock.txt` after testing.
6. Add a Git tag and release notes describing the tested OS, Python version, package lockfile, and any known forcing-data limitations.
7. Do not commit large generated NetCDF, GIF, PNG, virtual-environment, or downloaded-current files unless they are intentionally published examples.

## Licence and attribution

This project is distributed under the GNU General Public License, version 3 (GPL-3.0-or-later). The complete licence terms are provided in the repository's LICENSE file.

You may use, copy, modify, and redistribute this software under the GPLv3 terms, provided that redistributed versions retain the applicable copyright and licence notices and are distributed under the same licence. The software is provided without warranty, to the maximum extent permitted by law.

This repository includes or interoperates with third-party software and data services, including OpenDrift and ICATMAR ERDDAP. Their respective licences, terms of use, data-attribution requirements, and other conditions remain applicable. Before public distribution, verify and include any required attribution for OpenDrift, ICATMAR data, and bundled example forcing data.
