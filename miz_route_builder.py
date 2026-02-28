#!/usr/bin/env python3
"""
Build a DCS .miz by replacing the route waypoints with lat/lon coordinates.

Workflow:
1) Use a template .miz (already configured in DCS).
2) Provide a coords file (lat/lon in decimal or DMS).
3) The script converts lat/lon -> mission x/y using beacon geo data
   from the installed DCS map, then rewrites the route points.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_DCS_PATH = r"C:\Program Files\Eagle Dynamics\DCS World OpenBeta"

AIRDROME_START_ACTIONS = {
    "From Runway",
    "From Ground Area",
    "From Parking Area",
    "From Parking Area Hot",
}

# map_name -> normalized airfield name -> towns.lua key
AIRFIELD_TOWN_ALIASES: Dict[str, Dict[str, str]] = {
    "Normandy": {
        "st pierre du mont": "St.Pierre-du-Mont",
        "maurpertus": "Maupertus",
        "longues sur mer": "Longues",
        "saint croix sur mer": "Ste.Croix sur Mer",
        "tricqueville": "Tricqueville",
    },
    "TheChannel": {
        "bigginhill": "Biggin Hill",
        "eastchurch": "Eastchurch",
        "mervillecalonne": "Merville",
        "saintomer": "Saint-Omer",
    },
}

# map_name -> another map_name to borrow beacon geo reference from
BEACON_SOURCE_MAP_ALIASES: Dict[str, str] = {
    "MarianasWWII": "MarianaIslands",
}


@dataclass
class EntrySpan:
    start: int
    end: int


@dataclass
class Waypoint:
    lat: float
    lon: float
    name: Optional[str] = None
    description: Optional[str] = None
    agl_m: Optional[float] = None


def _find_matching_brace(text: str, start_idx: int) -> int:
    depth = 0
    in_str = False
    escape = False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            continue
        if ch == "\"":
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced braces while searching for matching '}'")


def _find_key_table_at_depth(text: str, key: str, depth_target: int) -> Tuple[int, int, str]:
    i = 0
    depth = 0
    in_str = False
    escape = False
    key_token = f'["{key}"]'
    while i < len(text):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            i += 1
            continue
        if ch == "\"":
            in_str = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue

        if depth == depth_target and text.startswith(key_token, i):
            eq_idx = text.find("=", i + len(key_token))
            if eq_idx == -1:
                break
            val_start = eq_idx + 1
            while val_start < len(text) and text[val_start].isspace():
                val_start += 1
            if val_start < len(text) and text[val_start] == "{":
                val_end = _find_matching_brace(text, val_start)
                return val_start, val_end, text[val_start : val_end + 1]
        i += 1
    raise ValueError(f'Could not find table key ["{key}"] at depth {depth_target}')


def _replace_key_table_at_depth(text: str, key: str, depth_target: int, new_table_text: str) -> str:
    start, end, _ = _find_key_table_at_depth(text, key, depth_target)
    return text[:start] + new_table_text + text[end + 1 :]


def _compute_brace_pairs(text: str) -> dict:
    stack: List[int] = []
    pairs: dict = {}
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            continue
        if ch == "\"":
            in_str = True
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}":
            if not stack:
                raise ValueError("Unbalanced braces while building brace pairs")
            start = stack.pop()
            pairs[start] = i
    if stack:
        raise ValueError("Unbalanced braces while building brace pairs")
    return pairs


def _find_table_entries(text: str, table_start: int, table_end: int, max_entries: Optional[int] = None) -> List[EntrySpan]:
    entries: List[EntrySpan] = []
    i = table_start + 1
    depth = 0
    in_str = False
    escape = False
    while i < table_end:
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            i += 1
            continue

        if ch == "\"":
            in_str = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue

        if depth == 0 and ch == "[":
            j = i + 1
            while j < table_end and text[j].isdigit():
                j += 1
            if j > i + 1 and j < table_end and text[j] == "]":
                k = j + 1
                while k < table_end and text[k].isspace():
                    k += 1
                if k < table_end and text[k] == "=":
                    k += 1
                    while k < table_end and text[k].isspace():
                        k += 1
                    if k < table_end and text[k] == "{":
                        brace_start = k
                        brace_end = _find_matching_brace(text, brace_start)
                        entry_end = brace_end + 1
                        # Include trailing comment and newline, if any.
                        while entry_end < len(text) and text[entry_end] != "\n":
                            entry_end += 1
                        if entry_end < len(text):
                            entry_end += 1
                        entries.append(EntrySpan(start=i, end=entry_end))
                        i = entry_end
                        if max_entries and len(entries) >= max_entries:
                            break
                        continue
        i += 1
    return entries


def _replace_first_key_value_at_depth(text: str, key: str, depth_target: int, new_value: str) -> Tuple[str, bool]:
    i = 0
    depth = 0
    in_str = False
    escape = False
    key_token = f'["{key}"]'
    while i < len(text):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            i += 1
            continue
        if ch == "\"":
            in_str = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue

        if depth == depth_target and text.startswith(key_token, i):
            eq_idx = text.find("=", i + len(key_token))
            if eq_idx == -1:
                return text, False
            val_start = eq_idx + 1
            while val_start < len(text) and text[val_start].isspace():
                val_start += 1
            if val_start >= len(text):
                return text, False

            if text[val_start] == "\"":
                val_end = val_start + 1
                esc = False
                while val_end < len(text):
                    if esc:
                        esc = False
                    elif text[val_end] == "\\":
                        esc = True
                    elif text[val_end] == "\"":
                        val_end += 1
                        break
                    val_end += 1
            else:
                val_end = val_start
                while val_end < len(text) and text[val_end] not in ",\r\n":
                    val_end += 1

            return text[:val_start] + new_value + text[val_end:], True

        i += 1
    return text, False


def _get_first_string_key_at_depth(text: str, key: str, depth_target: int) -> Optional[str]:
    i = 0
    depth = 0
    in_str = False
    escape = False
    key_token = f'["{key}"]'
    while i < len(text):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            i += 1
            continue
        if ch == "\"":
            in_str = True
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue

        if depth == depth_target and text.startswith(key_token, i):
            eq_idx = text.find("=", i + len(key_token))
            if eq_idx == -1:
                return None
            val_start = eq_idx + 1
            while val_start < len(text) and text[val_start].isspace():
                val_start += 1
            if val_start >= len(text) or text[val_start] != "\"":
                return None
            val_end = val_start + 1
            esc = False
            out_chars: List[str] = []
            while val_end < len(text):
                c = text[val_end]
                if esc:
                    out_chars.append(c)
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == "\"":
                    return "".join(out_chars)
                else:
                    out_chars.append(c)
                val_end += 1
            return None
        i += 1
    return None


def _get_top_level_numbered_indices(table_text: str) -> List[int]:
    indices: List[int] = []
    entries = _find_table_entries(table_text, 0, len(table_text) - 1)
    for entry in entries:
        m = re.match(r"\s*\[(\d+)\]\s*=", table_text[entry.start : entry.end])
        if m:
            indices.append(int(m.group(1)))
    return indices


def _append_entries_to_table(table_text: str, entries_text: str) -> str:
    insert_at = table_text.rfind("}")
    if insert_at == -1:
        raise ValueError("Invalid table text (missing closing brace)")
    prefix = table_text[:insert_at]
    if not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + entries_text + table_text[insert_at:]


def _renumber_entry(entry_text: str, new_index: int) -> str:
    entry_text = re.sub(r"\[\d+\]\s*=", f"[{new_index}] =", entry_text, count=1)
    entry_text = re.sub(r"-- end of \[\d+\]", f"-- end of [{new_index}]", entry_text, count=1)
    return entry_text


def _format_num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _decimal_to_dms(value: float, is_lat: bool) -> str:
    hemi = "N" if is_lat else "E"
    if value < 0:
        hemi = "S" if is_lat else "W"
    value = abs(value)
    deg = int(value)
    minutes_full = (value - deg) * 60.0
    minutes = int(minutes_full)
    seconds = int(round((minutes_full - minutes) * 60.0))
    if seconds == 60:
        seconds = 0
        minutes += 1
    if minutes == 60:
        minutes = 0
        deg += 1
    # Avoid the double-quote symbol to keep Lua strings valid without escaping.
    return f"{deg}\u00b0{minutes:02d}'{seconds:02d}{hemi}"


def _format_name(lat: float, lon: float) -> str:
    return f"{_decimal_to_dms(lat, True)} / {_decimal_to_dms(lon, False)}"


def _escape_lua_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", "\\\"")


def _set_or_insert_key(entry_text: str, key: str, value_lua: str, anchor_key: str = "speed_locked") -> str:
    updated, replaced = _replace_first_key_value_at_depth(entry_text, key, 1, value_lua)
    if replaced:
        return updated

    # Some first waypoint templates omit keys; insert near known field.
    m = re.search(rf'^(\s*)\["{re.escape(anchor_key)}"\]\s*=', updated, re.MULTILINE)
    if m:
        indent = m.group(1)
        insert = f'{indent}["{key}"] = {value_lua},\n'
        pos = m.start()
        return updated[:pos] + insert + updated[pos:]

    # Fallback: insert before the closing brace of the waypoint table.
    table_start = updated.find("{")
    if table_start == -1:
        return updated
    table_end = _find_matching_brace(updated, table_start)
    nl = updated.rfind("\n", table_start, table_end)
    indent = "\t"
    if nl != -1:
        m2 = re.match(r"(\s*)", updated[nl + 1 : table_end])
        if m2:
            indent = m2.group(1)
    insert = f'{indent}["{key}"] = {value_lua},\n'
    return updated[:table_end] + insert + updated[table_end:]


def _set_or_insert_name(entry_text: str, name: str) -> str:
    return _set_or_insert_key(entry_text, "name", f"\"{name}\"", anchor_key="speed_locked")


def _dms_to_decimal(parts: Tuple[str, str, str, str]) -> float:
    deg, minutes, seconds, ref = parts
    value = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    if ref.upper() in ("S", "W"):
        value = -value
    return value


def _split_coord_and_comment(line: str) -> Tuple[str, Optional[str]]:
    marker_idx = -1
    marker_len = 0
    for marker in ("//", "#", ";"):
        idx = line.find(marker)
        if idx != -1 and (marker_idx == -1 or idx < marker_idx):
            marker_idx = idx
            marker_len = len(marker)
    if marker_idx == -1:
        return line.strip(), None

    coord_part = line[:marker_idx].strip()
    comment = line[marker_idx + marker_len :].strip()
    if not comment:
        return coord_part, None
    return coord_part, (comment if comment else None)


def _extract_comment_fields(comment: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    if not comment:
        return None, None, None

    text = comment.strip()

    agl_match = re.search(r"\bagl\s*=\s*([-+]?\d+(?:\.\d+)?)\b", text, re.IGNORECASE)
    agl_m: Optional[float] = None
    if agl_match:
        agl_m = float(agl_match.group(1))
        text = (text[: agl_match.start()] + text[agl_match.end() :]).strip()
        text = re.sub(r"^[,|/\\\-\s]+|[,|/\\\-\s]+$", "", text)

    # Support "Label # Description" in comment body.
    description: Optional[str] = None
    if "#" in text:
        parts = [p.strip() for p in text.split("#") if p.strip()]
        if len(parts) >= 2:
            text = parts[0]
            description = " # ".join(parts[1:]).strip() or None

    # "01: Basra Intl" -> "Basra Intl"
    text = re.sub(r"^\d+\s*[:：\-]\s*", "", text).strip()
    return (text if text else None), description, agl_m


def _parse_coord_line(line: str) -> Optional[Waypoint]:
    original = line
    line = line.strip()
    if not line:
        return None
    if line.startswith("#") or line.startswith(";") or line.startswith("//"):
        return None

    coord_part, comment = _split_coord_and_comment(line)
    comment_name, comment_description, comment_agl = _extract_comment_fields(comment)
    if not coord_part:
        return None

    dms_pattern = re.compile(r"(\d+)\s*(?:\u00b0|deg)\s*(\d+)\s*'\s*(\d+(?:\.\d+)?)\s*\"?\s*([NSEW])", re.IGNORECASE)
    matches = dms_pattern.findall(coord_part)
    if len(matches) >= 2:
        lat = _dms_to_decimal(matches[0])
        lon = _dms_to_decimal(matches[1])
        return Waypoint(lat=lat, lon=lon, name=comment_name, description=comment_description, agl_m=comment_agl)

    # Decimal format: extract first two numbers
    nums = re.findall(r"[-+]?\d+(?:\.\d+)?", coord_part)
    if len(nums) >= 2:
        return Waypoint(lat=float(nums[0]), lon=float(nums[1]), name=comment_name, description=comment_description, agl_m=comment_agl)

    raise ValueError(f"Unrecognized coordinate line: {original}")


def read_coords_file(path: Path) -> List[Waypoint]:
    coords: List[Waypoint] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        parsed = _parse_coord_line(line)
        if parsed:
            coords.append(parsed)
    return coords


def _parse_beacons(beacons_path: Path) -> List[Tuple[float, float, float, float]]:
    text = beacons_path.read_text(encoding="utf-8", errors="ignore")
    pattern = re.compile(
        r"position\s*=\s*\{\s*([-\d\.]+)\s*,\s*([-\d\.]+)\s*,\s*([-\d\.]+)\s*\}\s*;.*?"
        r"positionGeo\s*=\s*\{\s*latitude\s*=\s*([-\d\.]+)\s*,\s*longitude\s*=\s*([-\d\.]+)\s*\}",
        re.S,
    )
    pairs: List[Tuple[float, float, float, float]] = []
    for m in pattern.finditer(text):
        x = float(m.group(1))
        z = float(m.group(3))
        lat = float(m.group(4))
        lon = float(m.group(5))
        pairs.append((lat, lon, x, z))
    return pairs


def _normalize_name_key(value: str) -> str:
    s = unicodedata.normalize("NFKD", value)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = s.replace("_", " ").replace("-", " ").replace(".", "").replace("'", "").replace("\u2019", "")
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_towns_latlon(towns_path: Path) -> Dict[str, Tuple[float, float]]:
    text = towns_path.read_text(encoding="utf-8", errors="ignore")
    pattern = re.compile(
        r'\["([^"]+)"\]\s*=\s*\{\s*latitude\s*=\s*([-\d\.]+)\s*,\s*longitude\s*=\s*([-\d\.]+)'
    )
    out: Dict[str, Tuple[float, float]] = {}
    for m in pattern.finditer(text):
        out[m.group(1)] = (float(m.group(2)), float(m.group(3)))
    return out


def _parse_airfield_names_from_radio(radio_path: Path) -> Dict[int, str]:
    text = radio_path.read_text(encoding="utf-8", errors="ignore")
    out: Dict[int, str] = {}
    for m_id in re.finditer(r"radioId\s*=\s*'airfield(\d+)_0';", text):
        airdrome_id = int(m_id.group(1))
        if airdrome_id in out:
            continue

        block_start = text.rfind("{", 0, m_id.start())
        if block_start == -1:
            continue
        try:
            block_end = _find_matching_brace(text, block_start)
        except ValueError:
            continue
        block = text[block_start : block_end + 1]

        # Preferred source (legacy maps): explicit comment line.
        m_comment = re.search(r"--\s*(.+)", block)
        if m_comment:
            name = m_comment.group(1).strip()
            if name:
                out[airdrome_id] = name
                continue

        # Fallback source (maps without comments): derive from callsign.
        m_call = re.search(r'callsign\s*=\s*\{\{.*?,\s*"([^"]+)"\}\}', block, re.S)
        if m_call:
            name = m_call.group(1).strip()
            if name:
                out[airdrome_id] = name
    return out


def _extract_airdrome_xy_samples_from_mission_text(mission_text: str) -> List[Tuple[int, float, float]]:
    samples: List[Tuple[int, float, float]] = []
    search_pos = 0
    while True:
        points_idx = mission_text.find('["points"]', search_pos)
        if points_idx == -1:
            break
        points_brace_start = mission_text.find("{", points_idx)
        if points_brace_start == -1:
            search_pos = points_idx + 1
            continue
        try:
            points_brace_end = _find_matching_brace(mission_text, points_brace_start)
        except ValueError:
            search_pos = points_idx + 1
            continue

        points_text = mission_text[points_brace_start : points_brace_end + 1]
        try:
            entries = _find_table_entries(points_text, 0, len(points_text) - 1)
        except ValueError:
            search_pos = points_brace_end + 1
            continue

        for entry in entries:
            entry_text = points_text[entry.start : entry.end]
            m_id = re.search(r'\["airdromeId"\]\s*=\s*(\d+)', entry_text)
            if not m_id:
                continue
            m_x = re.search(r'\["x"\]\s*=\s*([-+]?\d+(?:\.\d+)?)', entry_text)
            m_y = re.search(r'\["y"\]\s*=\s*([-+]?\d+(?:\.\d+)?)', entry_text)
            if not m_x or not m_y:
                continue
            m_action = re.search(r'\["action"\]\s*=\s*"([^"]+)"', entry_text)
            action = m_action.group(1) if m_action else ""
            if action not in AIRDROME_START_ACTIONS:
                continue
            samples.append((int(m_id.group(1)), float(m_x.group(1)), float(m_y.group(1))))

        search_pos = points_brace_end + 1
    return samples


def _collect_map_airdrome_reference_pairs(dcs_path: Path, map_name: str) -> List[Tuple[float, float, float, float]]:
    terrain_root = dcs_path / "Mods" / "terrains" / map_name
    towns_path = terrain_root / "Map" / "towns.lua"
    if not towns_path.exists():
        towns_path = terrain_root / "map" / "towns.lua"
    radio_path = terrain_root / "Radio.lua"
    if not radio_path.exists():
        radio_path = terrain_root / "radio.lua"
    if not towns_path.exists() or not radio_path.exists():
        return []

    towns = _parse_towns_latlon(towns_path)
    id_to_airfield_name = _parse_airfield_names_from_radio(radio_path)
    if not towns or not id_to_airfield_name:
        return []

    normalized_town_to_name: Dict[str, str] = {}
    for town_name in towns:
        key = _normalize_name_key(town_name)
        normalized_town_to_name[key] = town_name
        normalized_town_to_name[key.replace(" ", "")] = town_name

    map_aliases_raw = AIRFIELD_TOWN_ALIASES.get(map_name, {})
    map_aliases = {_normalize_name_key(k): v for k, v in map_aliases_raw.items()}

    id_to_xy_samples: Dict[int, List[Tuple[float, float]]] = {}
    for miz_path in dcs_path.rglob("*.miz"):
        try:
            with zipfile.ZipFile(miz_path, "r") as zf:
                if "theatre" not in zf.namelist() or "mission" not in zf.namelist():
                    continue
                miz_map = zf.read("theatre").decode("utf-8", errors="ignore").strip()
                if miz_map != map_name:
                    continue
                mission_text = zf.read("mission").decode("utf-8", errors="ignore")
        except Exception:
            continue

        for airdrome_id, x, y in _extract_airdrome_xy_samples_from_mission_text(mission_text):
            id_to_xy_samples.setdefault(airdrome_id, []).append((x, y))

    pairs: List[Tuple[float, float, float, float]] = []
    for airdrome_id, samples in id_to_xy_samples.items():
        name = id_to_airfield_name.get(airdrome_id)
        if not name:
            continue
        key = _normalize_name_key(name)
        key_no_prefix = re.sub(r"^[ab]\d+\s*", "", key)
        key_compact = key.replace(" ", "")
        key_no_prefix_compact = key_no_prefix.replace(" ", "")

        town_name = normalized_town_to_name.get(key)
        if not town_name:
            town_name = normalized_town_to_name.get(key_no_prefix)
        if not town_name:
            town_name = normalized_town_to_name.get(key_compact)
        if not town_name:
            town_name = normalized_town_to_name.get(key_no_prefix_compact)
        if not town_name:
            town_name = map_aliases.get(key)
        if not town_name:
            town_name = map_aliases.get(key_no_prefix)
        if not town_name:
            town_name = map_aliases.get(key_compact)
        if not town_name:
            town_name = map_aliases.get(key_no_prefix_compact)
        if not town_name:
            continue
        if town_name not in towns:
            continue

        lat, lon = towns[town_name]
        avg_x = sum(x for x, _ in samples) / len(samples)
        avg_y = sum(y for _, y in samples) / len(samples)
        pairs.append((lat, lon, avg_x, avg_y))

    return pairs


def _solve_least_squares(A: List[List[float]], b: List[float]) -> List[float]:
    # Solve (A^T A) c = (A^T b)
    m = len(A[0])
    normal = [[0.0 for _ in range(m)] for _ in range(m)]
    rhs = [0.0 for _ in range(m)]
    for row, bi in zip(A, b):
        for i in range(m):
            rhs[i] += row[i] * bi
            ri = row[i]
            for j in range(m):
                normal[i][j] += ri * row[j]

    # Gaussian elimination with partial pivoting
    for col in range(m):
        pivot = max(range(col, m), key=lambda r: abs(normal[r][col]))
        if abs(normal[pivot][col]) < 1e-12:
            raise ValueError("Singular matrix in least squares fit")
        if pivot != col:
            normal[col], normal[pivot] = normal[pivot], normal[col]
            rhs[col], rhs[pivot] = rhs[pivot], rhs[col]

        pivot_val = normal[col][col]
        for j in range(col, m):
            normal[col][j] /= pivot_val
        rhs[col] /= pivot_val

        for r in range(m):
            if r == col:
                continue
            factor = normal[r][col]
            if factor == 0:
                continue
            for j in range(col, m):
                normal[r][j] -= factor * normal[col][j]
            rhs[r] -= factor * rhs[col]

    return rhs


def _fit_geo_model(pairs: List[Tuple[float, float, float, float]]) -> Tuple[List[float], List[float], float, float]:
    # Try higher-order first, then fall back to affine if the matrix is singular.
    # Higher-order model:
    # x = a*lon + b*lat + c*lon^2 + d*lat^2 + e*lon*lat + f
    # y = ...
    models = [
        ("poly2", lambda lat, lon: [lon, lat, lon * lon, lat * lat, lon * lat, 1.0], 6),
        ("affine", lambda lat, lon: [lon, lat, 1.0], 3),
    ]

    last_error: Optional[Exception] = None
    for _, feature_builder, min_points in models:
        if len(pairs) < min_points:
            continue
        A: List[List[float]] = []
        bx: List[float] = []
        by: List[float] = []
        for lat, lon, x, y in pairs:
            A.append(feature_builder(lat, lon))
            bx.append(x)
            by.append(y)

        try:
            coef_x = _solve_least_squares(A, bx)
            coef_y = _solve_least_squares(A, by)
        except Exception as exc:
            last_error = exc
            continue

        errs = []
        for lat, lon, x, y in pairs:
            feats = feature_builder(lat, lon)
            px = sum(c * v for c, v in zip(coef_x, feats))
            py = sum(c * v for c, v in zip(coef_y, feats))
            errs.append(math.hypot(px - x, py - y))
        rmse = math.sqrt(sum(e * e for e in errs) / len(errs)) if errs else 0.0
        max_err = max(errs) if errs else 0.0
        return coef_x, coef_y, rmse, max_err

    if last_error is not None:
        raise ValueError(f"Could not fit geo model: {last_error}")
    raise ValueError("Could not fit geo model: insufficient reference points")


def _latlon_to_xy(lat: float, lon: float, coef_x: List[float], coef_y: List[float]) -> Tuple[float, float]:
    if len(coef_x) == 6 and len(coef_y) == 6:
        v = [lon, lat, lon * lon, lat * lat, lon * lat, 1.0]
    elif len(coef_x) == 3 and len(coef_y) == 3:
        v = [lon, lat, 1.0]
    else:
        raise ValueError("Unsupported geo model coefficient length")
    x = sum(c * vv for c, vv in zip(coef_x, v))
    y = sum(c * vv for c, vv in zip(coef_y, v))
    return x, y


def _sanitize_trigger_text(text: str) -> str:
    # Lua long-bracket strings cannot contain "]]".
    s = text.replace("\r", " ").replace("\n", " ").strip()
    return s.replace("]]", "] ]")


def _format_distance_label(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:g}"


def _parse_callout_miles(value: str) -> List[float]:
    items = re.split(r"[,\s]+", value.strip())
    out: List[float] = []
    for item in items:
        if not item:
            continue
        dist = float(item)
        if dist <= 0:
            continue
        if all(abs(dist - x) > 1e-9 for x in out):
            out.append(dist)
    out.sort(reverse=True)
    return out


def _extract_group_id(group_table_text: str) -> int:
    m = re.search(r'\["groupId"\]\s*=\s*(\d+)', group_table_text)
    if not m:
        raise ValueError("Could not find groupId in group table")
    return int(m.group(1))


def _inject_wp_comment_triggers(
    mission_text: str,
    waypoints: List[Waypoint],
    coef_x: List[float],
    coef_y: List[float],
    group_id: int,
    callout_nm: List[float],
    display_seconds: int,
) -> Tuple[str, int]:
    if not callout_nm or display_seconds <= 0:
        return mission_text, 0

    # Skip WP1 so callouts refer to "next" waypoint from mission start.
    target_waypoints = waypoints[1:] if len(waypoints) > 1 else []
    if not target_waypoints:
        return mission_text, 0

    wp_data: List[Tuple[int, float, float, str, Optional[str]]] = []
    for idx, wp in enumerate(target_waypoints, start=2):
        x, y = _latlon_to_xy(wp.lat, wp.lon, coef_x, coef_y)
        text = wp.name if wp.name else _format_name(wp.lat, wp.lon)
        desc = _sanitize_trigger_text(wp.description) if wp.description else None
        wp_data.append((idx, x, y, _sanitize_trigger_text(text), desc))

    callout_specs: List[Tuple[int, float, float, str, float, str]] = []
    for wp_idx, x, y, target_name, target_desc in wp_data:
        for nm in callout_nm:
            if target_desc:
                msg = f"{target_name} - {target_desc} ({_format_distance_label(nm)} NM)"
            else:
                msg = f"{target_name} in {_format_distance_label(nm)} NM"
            callout_specs.append((wp_idx, x, y, target_name, nm, _sanitize_trigger_text(msg)))

    # 1) Add zones under mission["triggers"]["zones"].
    triggers_start, triggers_end, triggers_table = _find_key_table_at_depth(mission_text, "triggers", 1)
    _, _, zones_table = _find_key_table_at_depth(triggers_table, "zones", 1)

    existing_zone_entry_indices = _get_top_level_numbered_indices(zones_table)
    next_zone_entry_idx = max(existing_zone_entry_indices, default=0)
    existing_zone_ids = [int(v) for v in re.findall(r'\["zoneId"\]\s*=\s*(\d+)', zones_table)]
    next_zone_id = max(existing_zone_ids, default=0)

    zone_ids: List[int] = []
    zone_entries: List[str] = []
    for wp_idx, x, y, _, nm, _ in callout_specs:
        next_zone_entry_idx += 1
        next_zone_id += 1
        zone_ids.append(next_zone_id)
        zone_name = _escape_lua_string(f"WP_DIST_{wp_idx:02d}_{_format_distance_label(nm)}NM")
        range_m = nm * 1852.0
        zone_entries.append(
            f'\t\t\t[{next_zone_entry_idx}] = \n'
            f'\t\t\t{{\n'
            f'\t\t\t\t["radius"] = {_format_num(range_m)},\n'
            f'\t\t\t\t["zoneId"] = {next_zone_id},\n'
            f'\t\t\t\t["color"] = \n'
            f'\t\t\t\t{{\n'
            f'\t\t\t\t\t[1] = 1,\n'
            f'\t\t\t\t\t[2] = 1,\n'
            f'\t\t\t\t\t[3] = 1,\n'
            f'\t\t\t\t\t[4] = 0.15,\n'
            f'\t\t\t\t}}, -- end of ["color"]\n'
            f'\t\t\t\t["properties"] = {{}},\n'
            f'\t\t\t\t["hidden"] = true,\n'
            f'\t\t\t\t["y"] = {_format_num(y)},\n'
            f'\t\t\t\t["x"] = {_format_num(x)},\n'
            f'\t\t\t\t["name"] = "{zone_name}",\n'
            f'\t\t\t\t["heading"] = 0,\n'
            f'\t\t\t\t["type"] = 0,\n'
            f'\t\t\t}}, -- end of [{next_zone_entry_idx}]\n'
        )
    zones_table = _append_entries_to_table(zones_table, "".join(zone_entries))
    triggers_table = _replace_key_table_at_depth(triggers_table, "zones", 1, zones_table)
    mission_text = mission_text[:triggers_start] + triggers_table + mission_text[triggers_end + 1 :]

    # 2) Add editor trigger rules under mission["trigrules"].
    trigrules_start, trigrules_end, trigrules_table = _find_key_table_at_depth(mission_text, "trigrules", 1)
    existing_rule_indices = _get_top_level_numbered_indices(trigrules_table)
    next_rule_idx = max(existing_rule_indices, default=0)

    trigrule_entries: List[str] = []
    for (wp_idx, _, _, _, nm, text_msg), zone_id in zip(callout_specs, zone_ids):
        next_rule_idx += 1
        script = _escape_lua_string(f"trigger.action.outText([[{text_msg}]], {display_seconds})")
        trigrule_entries.append(
            f'\t\t[{next_rule_idx}] = \n'
            f'\t\t{{\n'
            f'\t\t\t["rules"] = \n'
            f'\t\t\t{{\n'
            f'\t\t\t\t[1] = \n'
            f'\t\t\t\t{{\n'
            f'\t\t\t\t\t["coalitionlist"] = "all",\n'
            f'\t\t\t\t\t["unitType"] = "ALL",\n'
            f'\t\t\t\t\t["zone"] = {zone_id},\n'
            f'\t\t\t\t\t["group"] = {group_id},\n'
            f'\t\t\t\t\t["predicate"] = "c_part_of_group_in_zone",\n'
            f'\t\t\t\t}}, -- end of [1]\n'
            f'\t\t\t}}, -- end of ["rules"]\n'
            f'\t\t\t["eventlist"] = "",\n'
            f'\t\t\t["actions"] = \n'
            f'\t\t\t{{\n'
            f'\t\t\t\t[1] = \n'
            f'\t\t\t\t{{\n'
                f'\t\t\t\t\t["text"] = "{script}",\n'
            f'\t\t\t\t\t["predicate"] = "a_do_script",\n'
            f'\t\t\t\t}}, -- end of [1]\n'
            f'\t\t\t}}, -- end of ["actions"]\n'
            f'\t\t\t["predicate"] = "triggerFront",\n'
            f'\t\t\t["comment"] = "WP Dist {wp_idx:02d} {_format_distance_label(nm)}NM",\n'
            f'\t\t}}, -- end of [{next_rule_idx}]\n'
        )
    trigrules_table = _append_entries_to_table(trigrules_table, "".join(trigrule_entries))
    mission_text = mission_text[:trigrules_start] + trigrules_table + mission_text[trigrules_end + 1 :]

    # 3) Add runtime trigger functions under mission["trig"].
    trig_start, trig_end, trig_table = _find_key_table_at_depth(mission_text, "trig", 1)
    _, _, actions_table = _find_key_table_at_depth(trig_table, "actions", 1)
    _, _, func_table = _find_key_table_at_depth(trig_table, "func", 1)
    _, _, flag_table = _find_key_table_at_depth(trig_table, "flag", 1)
    _, _, conditions_table = _find_key_table_at_depth(trig_table, "conditions", 1)

    trig_max_idx = max(
        max(_get_top_level_numbered_indices(actions_table), default=0),
        max(_get_top_level_numbered_indices(func_table), default=0),
        max(_get_top_level_numbered_indices(flag_table), default=0),
        max(_get_top_level_numbered_indices(conditions_table), default=0),
    )

    action_entries: List[str] = []
    func_entries: List[str] = []
    flag_entries: List[str] = []
    condition_entries: List[str] = []

    for (_, _, _, _, _, text_msg), zone_id in zip(callout_specs, zone_ids):
        trig_max_idx += 1
        action_script = f"trigger.action.outText([[{text_msg}]], {display_seconds})"
        action_cmd = f'a_do_script("{_escape_lua_string(action_script)}");'
        condition_cmd = f"return(c_part_of_group_in_zone({group_id}, {zone_id}) )"
        func_cmd = (
            f"if mission.trig.conditions[{trig_max_idx}]() then "
            f"if not mission.trig.flag[{trig_max_idx}] then mission.trig.actions[{trig_max_idx}](); "
            f"mission.trig.flag[{trig_max_idx}] = true;end; "
            f"else mission.trig.flag[{trig_max_idx}] = false; end;"
        )

        action_entries.append(f'\t\t\t[{trig_max_idx}] = "{_escape_lua_string(action_cmd)}",\n')
        func_entries.append(f'\t\t\t[{trig_max_idx}] = "{_escape_lua_string(func_cmd)}",\n')
        flag_entries.append(f"\t\t\t[{trig_max_idx}] = false,\n")
        condition_entries.append(f'\t\t\t[{trig_max_idx}] = "{_escape_lua_string(condition_cmd)}",\n')

    actions_table = _append_entries_to_table(actions_table, "".join(action_entries))
    func_table = _append_entries_to_table(func_table, "".join(func_entries))
    flag_table = _append_entries_to_table(flag_table, "".join(flag_entries))
    conditions_table = _append_entries_to_table(conditions_table, "".join(condition_entries))

    trig_table = _replace_key_table_at_depth(trig_table, "actions", 1, actions_table)
    trig_table = _replace_key_table_at_depth(trig_table, "func", 1, func_table)
    trig_table = _replace_key_table_at_depth(trig_table, "flag", 1, flag_table)
    trig_table = _replace_key_table_at_depth(trig_table, "conditions", 1, conditions_table)

    mission_text = mission_text[:trig_start] + trig_table + mission_text[trig_end + 1 :]
    return mission_text, len(callout_specs)


def _update_points_table(
    points_text: str,
    waypoints: List[Waypoint],
    coef_x: List[float],
    coef_y: List[float],
    default_agl_m: Optional[float],
) -> str:
    points_start = 0
    points_end = len(points_text) - 1
    entries = _find_table_entries(points_text, points_start, points_end)
    if not entries:
        raise ValueError("No waypoint entries found in points table")

    template1 = points_text[entries[0].start:entries[0].end]
    templateN = points_text[entries[1].start:entries[1].end] if len(entries) > 1 else template1

    new_entries: List[str] = []
    for idx, wp in enumerate(waypoints, start=1):
        x, y = _latlon_to_xy(wp.lat, wp.lon, coef_x, coef_y)
        template = template1 if idx == 1 else templateN
        entry = _renumber_entry(template, idx)
        entry, _ = _replace_first_key_value_at_depth(entry, "x", 1, _format_num(x))
        entry, _ = _replace_first_key_value_at_depth(entry, "y", 1, _format_num(y))
        agl_m = wp.agl_m if wp.agl_m is not None else default_agl_m
        if agl_m is not None:
            entry = _set_or_insert_key(entry, "alt", _format_num(agl_m), anchor_key="action")
            entry = _set_or_insert_key(entry, "alt_type", "\"RADIO\"", anchor_key="action")
        name = wp.name if wp.name else _format_name(wp.lat, wp.lon)
        name = _escape_lua_string(name)
        entry = _set_or_insert_name(entry, name)
        new_entries.append(entry)

    prefix = points_text[:entries[0].start]
    suffix = points_text[entries[-1].end:]
    return prefix + "".join(new_entries) + suffix


def _update_first_unit_xy(group_table_text: str, x0: float, y0: float, initial_agl_m: Optional[float]) -> str:
    units_idx = group_table_text.find('["units"]')
    if units_idx == -1:
        return group_table_text
    units_brace_start = group_table_text.find("{", units_idx)
    units_brace_end = _find_matching_brace(group_table_text, units_brace_start)
    units_text = group_table_text[units_brace_start:units_brace_end + 1]

    entries = _find_table_entries(units_text, 0, len(units_text) - 1, max_entries=1)
    if not entries:
        return group_table_text
    entry = entries[0]
    unit_entry_text = units_text[entry.start:entry.end]

    unit_table_start = unit_entry_text.find("{")
    if unit_table_start == -1:
        return group_table_text
    unit_table_end = _find_matching_brace(unit_entry_text, unit_table_start)
    unit_table = unit_entry_text[unit_table_start:unit_table_end + 1]

    unit_table, _ = _replace_first_key_value_at_depth(unit_table, "x", 1, _format_num(x0))
    unit_table, _ = _replace_first_key_value_at_depth(unit_table, "y", 1, _format_num(y0))
    if initial_agl_m is not None:
        unit_table = _set_or_insert_key(unit_table, "alt", _format_num(initial_agl_m), anchor_key="speed")
        unit_table = _set_or_insert_key(unit_table, "alt_type", "\"RADIO\"", anchor_key="speed")

    unit_entry_text = unit_entry_text[:unit_table_start] + unit_table + unit_entry_text[unit_table_end + 1:]
    units_text = units_text[:entry.start] + unit_entry_text + units_text[entry.end:]
    return group_table_text[:units_brace_start] + units_text + group_table_text[units_brace_end + 1:]


def _update_group_table(
    group_table_text: str,
    waypoints: List[Waypoint],
    coef_x: List[float],
    coef_y: List[float],
    default_agl_m: Optional[float],
) -> str:
    points_idx = group_table_text.find('["points"]')
    if points_idx == -1:
        raise ValueError("Could not find route points in group table")
    points_brace_start = group_table_text.find("{", points_idx)
    points_brace_end = _find_matching_brace(group_table_text, points_brace_start)
    points_text = group_table_text[points_brace_start:points_brace_end + 1]

    updated_points_text = _update_points_table(points_text, waypoints, coef_x, coef_y, default_agl_m)
    group_table_text = group_table_text[:points_brace_start] + updated_points_text + group_table_text[points_brace_end + 1:]

    # Update group-level x/y and first unit x/y to match first waypoint
    x0, y0 = _latlon_to_xy(waypoints[0].lat, waypoints[0].lon, coef_x, coef_y)
    group_table_text, _ = _replace_first_key_value_at_depth(group_table_text, "x", 1, _format_num(x0))
    group_table_text, _ = _replace_first_key_value_at_depth(group_table_text, "y", 1, _format_num(y0))
    first_agl_m = waypoints[0].agl_m if waypoints[0].agl_m is not None else default_agl_m
    if first_agl_m is not None:
        group_table_text = _set_or_insert_key(group_table_text, "alt", _format_num(first_agl_m), anchor_key="x")
        group_table_text = _set_or_insert_key(group_table_text, "alt_type", "\"RADIO\"", anchor_key="x")
    group_table_text = _update_first_unit_xy(group_table_text, x0, y0, first_agl_m)
    return group_table_text


def _extract_group_entry(mission_text: str, group_name: Optional[str]) -> Tuple[str, int, int, str]:
    # Returns (group_entry_text, entry_start, entry_end, resolved_group_name)
    if group_name:
        needle = f'["name"] = "{group_name}"'
        idx = mission_text.find(needle)
        if idx != -1:
            pairs = _compute_brace_pairs(mission_text)
            candidates = [(s, e) for s, e in pairs.items() if s < idx < e]
            candidates.sort(key=lambda t: t[1] - t[0])
            group_brace_start = None
            group_brace_end = None
            for s, e in candidates:
                block = mission_text[s:e + 1]
                if '["route"]' in block and '["units"]' in block:
                    group_brace_start, group_brace_end = s, e
                    break
            if group_brace_start is not None:
                # Find entry header before the group table: [n] = {
                i = group_brace_start - 1
                while i >= 0 and mission_text[i].isspace():
                    i -= 1
                if i >= 0 and mission_text[i] == "=":
                    i -= 1
                    while i >= 0 and mission_text[i].isspace():
                        i -= 1
                    if i >= 0 and mission_text[i] == "]":
                        i -= 1
                        while i >= 0 and mission_text[i].isdigit():
                            i -= 1
                        if i >= 0 and mission_text[i] == "[":
                            entry_start = i
                            entry_brace_start = group_brace_start
                            entry_brace_end = group_brace_end
                            entry_end = entry_brace_end + 1
                            while entry_end < len(mission_text) and mission_text[entry_end] != "\n":
                                entry_end += 1
                            if entry_end < len(mission_text):
                                entry_end += 1
                            entry_text = mission_text[entry_start:entry_end]
                            return entry_text, entry_start, entry_end, group_name

    # Fallback: pick the first available aircraft group from plane/helicopter sections.
    for domain_key in ("plane", "helicopter"):
        token = f'["{domain_key}"]'
        search_pos = 0
        while True:
            domain_token_idx = mission_text.find(token, search_pos)
            if domain_token_idx == -1:
                break

            eq_idx = mission_text.find("=", domain_token_idx + len(token))
            if eq_idx == -1:
                break
            domain_table_start = eq_idx + 1
            while domain_table_start < len(mission_text) and mission_text[domain_table_start].isspace():
                domain_table_start += 1
            if domain_table_start >= len(mission_text) or mission_text[domain_table_start] != "{":
                search_pos = domain_token_idx + len(token)
                continue

            try:
                domain_table_end = _find_matching_brace(mission_text, domain_table_start)
            except ValueError:
                search_pos = domain_token_idx + len(token)
                continue

            domain_table_text = mission_text[domain_table_start:domain_table_end + 1]
            try:
                group_start_rel, _, group_table_text = _find_key_table_at_depth(domain_table_text, "group", 1)
            except ValueError:
                search_pos = domain_table_end + 1
                continue

            entries = _find_table_entries(group_table_text, 0, len(group_table_text) - 1, max_entries=1)
            if not entries:
                search_pos = domain_table_end + 1
                continue

            first_entry = entries[0]
            entry_start = domain_table_start + group_start_rel + first_entry.start
            entry_end = domain_table_start + group_start_rel + first_entry.end
            entry_text = mission_text[entry_start:entry_end]
            entry_table_start = entry_text.find("{")
            resolved_name = "<unknown>"
            if entry_table_start != -1:
                entry_table_end = _find_matching_brace(entry_text, entry_table_start)
                entry_table_text = entry_text[entry_table_start:entry_table_end + 1]
                detected = _get_first_string_key_at_depth(entry_table_text, "name", 1)
                if detected:
                    resolved_name = detected
            return entry_text, entry_start, entry_end, resolved_name

            # unreachable

        # search next domain

    raise ValueError('Could not find any aircraft group in ["plane"] or ["helicopter"] sections')


def update_mission_text(
    mission_text: str,
    waypoints: List[Waypoint],
    coef_x: List[float],
    coef_y: List[float],
    group_name: Optional[str],
    default_agl_m: Optional[float],
) -> Tuple[str, str, int]:
    entry_text, entry_start, entry_end, resolved_name = _extract_group_entry(mission_text, group_name)
    entry_brace_start = entry_text.find("{")
    if entry_brace_start == -1:
        raise ValueError("Group entry has no table block")
    entry_brace_end = _find_matching_brace(entry_text, entry_brace_start)
    group_table_text = entry_text[entry_brace_start:entry_brace_end + 1]

    updated_group_table = _update_group_table(group_table_text, waypoints, coef_x, coef_y, default_agl_m)
    group_id = _extract_group_id(updated_group_table)
    updated_entry_text = entry_text[:entry_brace_start] + updated_group_table + entry_text[entry_brace_end + 1:]
    updated_mission = mission_text[:entry_start] + updated_entry_text + mission_text[entry_end:]
    return updated_mission, resolved_name, group_id


def _read_text_from_zip(zip_path: Path, member: str) -> str:
    with zipfile.ZipFile(zip_path, "r") as zf:
        return zf.read(member).decode("utf-8", errors="ignore")


def _read_theatre(zip_path: Path) -> str:
    return _read_text_from_zip(zip_path, "theatre").strip()


def _write_updated_miz(template_path: Path, output_path: Path, updated_mission_text: str) -> None:
    with zipfile.ZipFile(template_path, "r") as zin:
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                data = zin.read(item.filename)
                if item.filename == "mission":
                    data = updated_mission_text.encode("utf-8")
                zout.writestr(item, data)


def _load_map_bounds(dcs_path: Path, map_name: str) -> Optional[Tuple[float, float, float, float]]:
    nodes_map = dcs_path / "Mods" / "terrains" / map_name / "MissionGenerator" / "nodesMap.lua"
    if not nodes_map.exists():
        return None
    text = nodes_map.read_text(encoding="utf-8", errors="ignore")
    m = re.search(r"nodesMapBorders\s*=\s*\{\s*([-\d\.]+)\s*,\s*([-\d\.]+)\s*,\s*([-\d\.]+)\s*,\s*([-\d\.]+)\s*\}", text)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a DCS .miz with route points from lat/lon.")
    parser.add_argument("--template", required=True, help="Template .miz path")
    parser.add_argument("--output", required=True, help="Output .miz path (must not already exist)")
    parser.add_argument("--coords-file", default="coords.txt", help="Text file with lat/lon per line (optional '# name, agl=300')")
    parser.add_argument("--group-name", default=None, help="Group name to update (optional; falls back to first aircraft group if not found)")
    parser.add_argument("--dcs-path", default=DEFAULT_DCS_PATH, help="DCS install path")
    parser.add_argument("--map-name", default=None, help="Override map name (default: read from template .miz)")
    parser.add_argument("--agl-m", type=float, default=None, help="Default AGL altitude in meters for all waypoints")
    parser.add_argument("--wp-distance-callouts-nm", default="5,3", help="Comma-separated waypoint distance callouts in nautical miles (e.g. '5,3'; empty or <=0 entries disable)")
    parser.add_argument("--wp-comment-seconds", type=int, default=10, help="How long each waypoint comment is displayed")
    # Backward-compat option from earlier revision (interpreted as NM).
    parser.add_argument("--wp-comment-range-miles", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting the output .miz if it exists")
    args = parser.parse_args()

    template_path = Path(args.template)
    output_path = Path(args.output)
    coords_path = Path(args.coords_file)
    dcs_path = Path(args.dcs_path)

    if not template_path.exists():
        print(f"Template not found: {template_path}", file=sys.stderr)
        return 1
    if output_path.exists() and not args.overwrite:
        print(f"Output already exists (won't overwrite): {output_path}", file=sys.stderr)
        print("Use --overwrite to replace it.", file=sys.stderr)
        return 1
    if not coords_path.exists():
        print(f"Coords file not found: {coords_path}", file=sys.stderr)
        return 1

    waypoints = read_coords_file(coords_path)
    if not waypoints:
        print("No valid coordinates found.", file=sys.stderr)
        return 1
    if args.agl_m is not None and args.agl_m < 0:
        print("--agl-m must be >= 0", file=sys.stderr)
        return 1
    if args.wp_comment_seconds < 1:
        print("--wp-comment-seconds must be >= 1", file=sys.stderr)
        return 1

    if args.wp_comment_range_miles is not None:
        if args.wp_comment_range_miles < 0:
            print("--wp-comment-range-miles must be >= 0", file=sys.stderr)
            return 1
        callout_nm = [args.wp_comment_range_miles] if args.wp_comment_range_miles > 0 else []
    else:
        try:
            callout_nm = _parse_callout_miles(args.wp_distance_callouts_nm)
        except ValueError:
            print("--wp-distance-callouts-nm must be a comma-separated numeric list, e.g. '5,3'", file=sys.stderr)
            return 1

    for idx, wp in enumerate(waypoints, start=1):
        if wp.agl_m is not None and wp.agl_m < 0:
            print(f"Waypoint {idx} has negative agl value: {wp.agl_m}", file=sys.stderr)
            return 1

    map_name = args.map_name or _read_theatre(template_path)
    beacons_path = dcs_path / "Mods" / "terrains" / map_name / "beacons.lua"
    geo_source = "beacons.lua"
    if not beacons_path.exists():
        print(f"beacons.lua not found for map '{map_name}': {beacons_path}", file=sys.stderr)
        return 1

    pairs = _parse_beacons(beacons_path)
    if len(pairs) < 6:
        alias_map_name = BEACON_SOURCE_MAP_ALIASES.get(map_name)
        if alias_map_name:
            alias_beacons_path = dcs_path / "Mods" / "terrains" / alias_map_name / "beacons.lua"
            if not alias_beacons_path.exists():
                alias_beacons_path = dcs_path / "Mods" / "terrains" / alias_map_name / "Beacons.lua"
            if alias_beacons_path.exists():
                alias_pairs = _parse_beacons(alias_beacons_path)
                if len(alias_pairs) >= 6:
                    pairs = alias_pairs
                    geo_source = f"beacons.lua ({alias_map_name})"

    if len(pairs) < 6:
        fallback_pairs = _collect_map_airdrome_reference_pairs(dcs_path, map_name)
        if len(fallback_pairs) >= 6:
            pairs = fallback_pairs
            geo_source = "radio/towns + installed missions"
        else:
            print("Not enough beacon geo points to fit conversion model.", file=sys.stderr)
            print(
                f"Fallback source also insufficient for map '{map_name}' "
                f"({len(fallback_pairs)} usable reference points).",
                file=sys.stderr,
            )
            return 1

    coef_x, coef_y, rmse, max_err = _fit_geo_model(pairs)
    fit_model = "poly2" if len(coef_x) == 6 else "affine"

    mission_text = _read_text_from_zip(template_path, "mission")
    updated_mission, resolved_group, resolved_group_id = update_mission_text(
        mission_text,
        waypoints,
        coef_x,
        coef_y,
        args.group_name,
        args.agl_m,
    )
    comments_added = 0
    if callout_nm:
        updated_mission, comments_added = _inject_wp_comment_triggers(
            updated_mission,
            waypoints,
            coef_x,
            coef_y,
            resolved_group_id,
            callout_nm,
            args.wp_comment_seconds,
        )

    _write_updated_miz(template_path, output_path, updated_mission)

    print(f"Map: {map_name}")
    print(f"Geo source: {geo_source} ({len(pairs)} points)")
    print(f"Fit model: {fit_model}")
    print(f"Group updated: {resolved_group}")
    print(f"Waypoints: {len(waypoints)}")
    if callout_nm:
        callout_text = ", ".join(f"{_format_distance_label(x)}NM" for x in callout_nm)
        print(f"Waypoint distance callouts: {comments_added} (at {callout_text})")
    print(f"Fit RMSE: {rmse:.2f} m (max {max_err:.2f} m)")

    bounds = _load_map_bounds(dcs_path, map_name)
    if bounds:
        min_x, min_y, max_x, max_y = bounds
        out_of_bounds = 0
        for wp in waypoints:
            x, y = _latlon_to_xy(wp.lat, wp.lon, coef_x, coef_y)
            if not (min_x <= x <= max_x and min_y <= y <= max_y):
                out_of_bounds += 1
        if out_of_bounds:
            print(f"Warning: {out_of_bounds} waypoint(s) outside map bounds.")

    print(f"Saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
