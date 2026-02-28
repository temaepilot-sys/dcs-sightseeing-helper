# DCS MIZ Route Builder

Build a DCS `.miz` mission by replacing route waypoints from a text coordinate list.

## What it does

- Reads a template mission (`.miz`)
- Rewrites route waypoints for a target aircraft/helicopter group
- Supports DMS and decimal coordinate input
- Supports waypoint names, descriptions, and per-waypoint AGL
- Adds distance callout triggers (for example `5NM`, `3NM`)
- Works without editing the DCS install (read-only use)

## Requirements

- Python 3.9+
- DCS World installed
- A template `.miz` for the target map

## Quick start

1. Prepare coordinates file (example):

```txt
48deg48'16"N / 2deg07'13"E  # 01: Versailles # Palace and gardens
49deg53'41"N / 2deg17'48"E  # 02: Amiens Cathedral # Gothic landmark
```

2. Run:

```powershell
python miz_route_builder.py ^
  --template AH-64_Normandy2.miz ^
  --output AH-64_Normandy2_ROUTE.miz ^
  --coords-file Normandy2Tour.txt
```

## Common options

- `--group-name "Aerial-1"`: update a specific group (optional)
- `--agl-m 300`: default AGL altitude in meters
- `--wp-distance-callouts-nm 5,3`: distance callout trigger radii
- `--wp-comment-seconds 10`: callout display duration
- `--overwrite`: allow replacing existing output file
- `--dcs-path "C:\Program Files\Eagle Dynamics\DCS World OpenBeta"`: custom DCS path

## Notes on map support

- Most maps are solved via beacon geo references.
- Some maps need fallback reference extraction from installed missions and terrain metadata.
- Fallback maps can be less accurate than direct beacon-based maps.

## Legal

- This project is unofficial and is not affiliated with Eagle Dynamics.
- Do not redistribute DCS proprietary assets (map files, mission assets, bundled data from DCS).
- Keep this repository code-only.

## License

MIT. See `LICENSE`.
