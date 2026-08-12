from __future__ import annotations

import math
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.database import Database


def _offline_places() -> list[dict[str, Any]]:
    try:
        values = json.loads(Path(__file__).with_name("offline_places.json").read_text(encoding="utf-8"))
        return values if isinstance(values, list) else []
    except (OSError, json.JSONDecodeError):
        return []


OFFLINE_PLACES = _offline_places()


def _effective_time(row: dict[str, Any]) -> datetime:
    captured = str(row.get("captured_at") or "").strip()
    if captured:
        try:
            value = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if value.tzinfo is not None:
                return value.astimezone(timezone.utc).replace(tzinfo=None)
            return value
        except ValueError:
            pass
    return datetime.fromtimestamp(int(row.get("mtime_ns") or 0) / 1_000_000_000)


def _valid_coordinates(latitude: object, longitude: object) -> bool:
    try:
        latitude_value = float(latitude)
        longitude_value = float(longitude)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(latitude_value)
        and math.isfinite(longitude_value)
        and -90 <= latitude_value <= 90
        and -180 <= longitude_value <= 180
        and not (abs(latitude_value) < 0.0001 and abs(longitude_value) < 0.0001)
    )


def _distance_m(first: tuple[float, float], second: tuple[float, float]) -> float:
    latitude_1, longitude_1 = map(math.radians, first)
    latitude_2, longitude_2 = map(math.radians, second)
    delta_latitude = latitude_2 - latitude_1
    delta_longitude = longitude_2 - longitude_1
    value = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(latitude_1) * math.cos(latitude_2) * math.sin(delta_longitude / 2) ** 2
    )
    return 6_371_000 * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _place_name(latitude: float, longitude: float) -> str:
    nearest = min(
        OFFLINE_PLACES,
        key=lambda item: _distance_m(
            (latitude, longitude),
            (float(item["latitude"]), float(item["longitude"])),
        ),
        default=None,
    )
    if nearest:
        distance = _distance_m(
            (latitude, longitude),
            (float(nearest["latitude"]), float(nearest["longitude"])),
        )
        if distance <= 65_000:
            return str(nearest["name"])
    return f"位置 {latitude:.3f}, {longitude:.3f}"


def analyze_places(
    database: Database,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, int]:
    rows = database.fetchall(
        """SELECT id, latitude, longitude, captured_at, mtime_ns, width, height
           FROM files WHERE kind IN ('image', 'video') AND latitude IS NOT NULL AND longitude IS NOT NULL"""
    )
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        if cancelled():
            raise InterruptedError("任务已取消")
        if not _valid_coordinates(row.get("latitude"), row.get("longitude")):
            continue
        latitude = float(row["latitude"])
        longitude = float(row["longitude"])
        key = (round(latitude / 0.02), round(longitude / 0.02))
        buckets.setdefault(key, []).append(row)
        if index and index % 1000 == 0:
            progress(index / max(1, len(rows)) * 0.7, f"已分析 {index:,}/{len(rows):,} 个定位文件")

    groups: list[dict[str, Any]] = []
    for key, members in buckets.items():
        latitude = sum(float(row["latitude"]) for row in members) / len(members)
        longitude = sum(float(row["longitude"]) for row in members) / len(members)
        distances = [
            _distance_m((latitude, longitude), (float(row["latitude"]), float(row["longitude"])))
            for row in members
        ]
        cover = max(
            members,
            key=lambda row: (
                int(row.get("width") or 0) * int(row.get("height") or 0),
                _effective_time(row),
            ),
        )
        groups.append({
            "key": f"{key[0]}:{key[1]}",
            "name": _place_name(latitude, longitude),
            "latitude": latitude,
            "longitude": longitude,
            "radius_m": max(distances, default=0.0),
            "cover_file_id": int(cover["id"]),
            "members": [(int(row["id"]), distance) for row, distance in zip(members, distances)],
        })
    groups.sort(key=lambda group: len(group["members"]), reverse=True)
    database.replace_places(groups)
    progress(1, f"生成 {len(groups):,} 个地点相册")
    return {"places": len(groups), "files": sum(len(group["members"]) for group in groups)}


def _event_name(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start.year}年{start.month}月{start.day}日"
    if start.year == end.year:
        return f"{start.year}年{start.month}月{start.day}日－{end.month}月{end.day}日"
    return f"{start.year}年{start.month}月{start.day}日－{end.year}年{end.month}月{end.day}日"


def _finish_event(members: list[dict[str, Any]], output: list[dict[str, Any]]) -> None:
    if len(members) < 3:
        return
    members = sorted(members, key=lambda row: (_effective_time(row), int(row["id"])))
    start = _effective_time(members[0])
    end = _effective_time(members[-1])
    located = [row for row in members if _valid_coordinates(row.get("latitude"), row.get("longitude"))]
    latitude = sum(float(row["latitude"]) for row in located) / len(located) if located else None
    longitude = sum(float(row["longitude"]) for row in located) / len(located) if located else None
    location_key = (
        f"{round(latitude / 0.05)}:{round(longitude / 0.05)}"
        if latitude is not None and longitude is not None
        else "none"
    )
    cover = max(
        members,
        key=lambda row: (
            int(row.get("width") or 0) * int(row.get("height") or 0),
            _effective_time(row),
        ),
    )
    output.append({
        "key": f"{start.date().isoformat()}:{location_key}:{members[0]['id']}",
        "name": _event_name(start, end),
        "start_at": start.isoformat(timespec="seconds"),
        "end_at": end.isoformat(timespec="seconds"),
        "latitude": latitude,
        "longitude": longitude,
        "cover_file_id": int(cover["id"]),
        "members": [int(row["id"]) for row in members],
    })


def analyze_events(
    database: Database,
    progress: Callable[[float, str], None],
    cancelled: Callable[[], bool],
) -> dict[str, int]:
    rows = database.fetchall(
        """SELECT id, captured_at, mtime_ns, latitude, longitude, width, height
           FROM files WHERE kind IN ('image', 'video')
           AND id NOT IN (
             SELECT ef.file_id FROM event_files ef JOIN events e ON e.id = ef.event_id WHERE e.is_named = 1
           )"""
    )
    rows.sort(key=lambda row: (_effective_time(row), int(row["id"])))
    events: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if cancelled():
            raise InterruptedError("任务已取消")
        current_time = _effective_time(row)
        split = False
        if current:
            previous = current[-1]
            previous_time = _effective_time(previous)
            gap_hours = (current_time - previous_time).total_seconds() / 3600
            duration_days = (current_time - _effective_time(current[0])).total_seconds() / 86400
            split = gap_hours > 12 or duration_days > 7
            if (
                not split
                and gap_hours > 2
                and _valid_coordinates(previous.get("latitude"), previous.get("longitude"))
                and _valid_coordinates(row.get("latitude"), row.get("longitude"))
            ):
                split = _distance_m(
                    (float(previous["latitude"]), float(previous["longitude"])),
                    (float(row["latitude"]), float(row["longitude"])),
                ) > 80_000
        if split:
            _finish_event(current, events)
            current = []
        current.append(row)
        if index and index % 1000 == 0:
            progress(index / max(1, len(rows)) * 0.8, f"已分析 {index:,}/{len(rows):,} 个媒体文件")
    _finish_event(current, events)
    events.sort(key=lambda event: event["start_at"], reverse=True)
    name_counts: dict[str, int] = {}
    for event in events:
        name_counts[event["name"]] = name_counts.get(event["name"], 0) + 1
    for event in events:
        if name_counts[event["name"]] > 1:
            event["name"] = f"{event['name']} · {datetime.fromisoformat(event['start_at']).strftime('%H:%M')}"
    database.replace_events(events)
    progress(1, f"生成 {len(events):,} 个事件相册")
    return {"events": len(events), "files": sum(len(event["members"]) for event in events)}
