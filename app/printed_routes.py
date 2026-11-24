"""Registration and release-time auditing of routes printed in the paper guide.

A printed route is an ordered list of plate numbers.  It stays registerable as
long as every hop can currently be walked in the *active* published index —
even when the active version has advanced past the versions the cataloguer
selected during the cross-version check.

Release audit rules
-------------------
When ``catalog.apply_changes`` publishes a new index version it compares the
old and new ``index_entries`` and only rechecks printed routes that

* touch at least one plate whose index entry changed (added, removed, title or
  reference list), and
* are still ``live`` and have no break record yet.

The first unresolvable hop of a route that fails is recorded together with the
new version.  Break records are append-only: repairing a route never deletes
them.  Because a route with break history is excluded from later automatic
checks, repair is detected through the manual recheck action (which keeps the
history as well).

All audit writes happen inside the caller's transaction, so a failed release
rolls the new index version and any break records back together; the previous
index stays active and the break history is exactly as before.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import routecheck
from .db import active_version, now_str, write_lock


class RouteError(Exception):
    """Registration/recheck refused; nothing was written."""


@dataclass
class BreakEvent:
    route_id: int
    start: str
    end: str
    route: list[str]
    version: int
    position: int
    from_number: str
    to_number: str


@dataclass
class AuditReport:
    checked_route_ids: list[int] = field(default_factory=list)
    new_breaks: list[BreakEvent] = field(default_factory=list)


def route_key(route: list[str]) -> str:
    return json.dumps(route, ensure_ascii=False, separators=(",", ":"))


def parse_route_text(text: object) -> list[str]:
    """Parse a human-typed route such as ``A1 → A2 -> A3``."""
    if text is None:
        return []
    tokens = re.split(r"\s*(?:→|->|=>|>)\s*", str(text).strip())
    return [token.strip() for token in tokens if token and token.strip()]


def _record_break(conn, row, broken: tuple[int, str, str], version: int, now: str) -> BreakEvent:
    position, source, target = broken
    route = json.loads(row["route_json"])
    reason = f"索引 v{version} 中第 {position} 跳 {source} → {target} 已无法跳转。"
    # Keep the remainder of the printed route so the record stays readable
    # after later releases change how the plates connect.
    trace = " → ".join(route[position - 1:])
    conn.execute(
        "INSERT INTO printed_route_breaks "
        "(route_id, broken_version, position, from_number, to_number, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            version,
            position,
            source,
            target,
            f"{reason}此处起的原路线：{trace}。",
            now,
        ),
    )
    conn.execute(
        "UPDATE printed_routes SET status = 'broken', last_checked_version = ?, "
        "last_checked_at = ? WHERE id = ?",
        (version, now, row["id"]),
    )
    return BreakEvent(
        route_id=row["id"],
        start=row["start_number"],
        end=row["end_number"],
        route=route,
        version=version,
        position=position,
        from_number=source,
        to_number=target,
    )


def _mark_checked(conn, route_id: int, version: int, now: str) -> None:
    conn.execute(
        "UPDATE printed_routes SET last_checked_version = ?, last_checked_at = ? "
        "WHERE id = ?",
        (version, now, route_id),
    )


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def register_route(
    conn,
    *,
    route: list[str],
    source_versions: list[int],
    start: str | None = None,
    end: str | None = None,
    now: str | None = None,
) -> dict:
    """Register a route after rechecking every hop against the active version.

    Raises :class:`RouteError` when the route is empty, already registered, or
    any hop is no longer traversable in the active published index.
    """
    route = [str(n).strip() for n in route]
    if any(not n for n in route):
        raise RouteError("路线中的图版号不能为空。")
    if len(route) < 2:
        raise RouteError("路线至少需要起点和终点两个图版号。")
    start = (start or route[0]).strip()
    end = (end or route[-1]).strip()
    if route[0] != start or route[-1] != end:
        raise RouteError("路线与填写的起点/终点不一致。")
    if start == end:
        raise RouteError("起点与终点不能相同。")
    try:
        source_versions = list(
            dict.fromkeys(int(v) for v in source_versions)
        )
    except (TypeError, ValueError) as exc:
        raise RouteError("核对版本清单格式无效。") from exc
    if not source_versions:
        raise RouteError("缺少核对时选用的版本清单。")
    if len(source_versions) > 5:
        raise RouteError("核对版本最多 5 个。")

    now = now or now_str()
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = active_version(conn)
            for source_version in source_versions:
                if conn.execute(
                    "SELECT 1 FROM index_versions WHERE version = ?",
                    (source_version,),
                ).fetchone() is None:
                    raise RouteError(f"索引 v{source_version} 不存在。")
            # The route must really be a common route of the versions the
            # cataloguer checked (historical entries are immutable, so this
            # stays true even after later releases).
            source_graphs = [
                routecheck.load_index_graph(conn, version_)
                for version_ in source_versions
            ]
            for source_version, source_graph in zip(source_versions, source_graphs):
                broken_source = routecheck.first_broken_hop(source_graph, route)
                if broken_source is not None:
                    position, source, target = broken_source
                    raise RouteError(
                        f"索引 v{source_version} 中第 {position} 跳 {source} → "
                        f"{target} 不成立，该路线不是所选版本的共用路线。"
                    )
            graph = routecheck.load_index_graph(conn, version)
            broken = routecheck.first_broken_hop(graph, route)
            if broken is not None:
                position, source, target = broken
                raise RouteError(
                    f"现行索引 v{version} 中第 {position} 跳 {source} → {target} "
                    "已不通，无法登记；请按现行版本重新核对路线。"
                )

            key = route_key(route)
            existing = conn.execute(
                "SELECT id FROM printed_routes WHERE route_key = ?", (key,)
            ).fetchone()
            if existing is not None:
                raise RouteError(f"相同路线已登记（编号 {existing['id']}）。")

            cur = conn.execute(
                """
                INSERT INTO printed_routes
                    (start_number, end_number, route_key, route_json, hops,
                     source_versions, status, registered_version, registered_at,
                     last_checked_version, last_checked_at)
                VALUES (?, ?, ?, ?, ?, ?, 'live', ?, ?, ?, ?)
                """,
                (
                    start,
                    end,
                    key,
                    key,
                    len(route) - 1,
                    json.dumps(source_versions),
                    version,
                    now,
                    version,
                    now,
                ),
            )
            route_id = cur.lastrowid
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            raise RouteError("相同路线已登记，不能重复登记。") from exc
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "id": route_id,
        "start": start,
        "end": end,
        "route": route,
        "hops": len(route) - 1,
        "source_versions": source_versions,
        "registered_version": version,
    }


# --------------------------------------------------------------------------- #
# Release audit (runs inside the publishing transaction)
# --------------------------------------------------------------------------- #
def audit_release(conn, *, old_version: int, new_version: int, now: str) -> AuditReport:
    """Recheck eligible printed routes after a new index version is published.

    Must be called inside the active write transaction; no transaction is
    started here on purpose so a failure rolls the release back as a unit.
    """
    report = AuditReport()
    changed = routecheck.changed_index_plates(conn, old_version, new_version)
    if not changed:
        return report

    rows = conn.execute(
        """
        SELECT * FROM printed_routes AS r
         WHERE r.status = 'live'
           AND NOT EXISTS (
               SELECT 1 FROM printed_route_breaks b WHERE b.route_id = r.id
           )
         ORDER BY r.id
        """
    ).fetchall()
    if not rows:
        return report

    graph = routecheck.load_index_graph(conn, new_version)
    for row in rows:
        try:
            route = json.loads(row["route_json"])
        except (TypeError, ValueError):
            # Corrupt rows must never block a release; they simply skip the
            # automatic check and can be fixed via registration.
            continue
        if not isinstance(route, list) or len(route) < 2:
            continue
        if not changed.intersection(route):
            continue
        report.checked_route_ids.append(row["id"])
        broken = routecheck.first_broken_hop(graph, route)
        if broken is None:
            _mark_checked(conn, row["id"], new_version, now)
        else:
            report.new_breaks.append(
                _record_break(conn, row, broken, new_version, now)
            )
    return report


# --------------------------------------------------------------------------- #
# Manual recheck (detects repairs; keeps history)
# --------------------------------------------------------------------------- #
def recheck_route(conn, route_id: int, *, now: str | None = None) -> dict:
    """Recheck one printed route against the active index.

    A passing check flips the route back to ``live`` without deleting its
    break history.  A failing check on a still-live route records a new break;
    a failing check on an already-broken route only refreshes the check time.
    """
    now = now or now_str()
    with write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM printed_routes WHERE id = ?", (route_id,)
            ).fetchone()
            if row is None:
                raise RouteError(f"已印路线 {route_id} 不存在。")

            version = active_version(conn)
            graph = routecheck.load_index_graph(conn, version)
            try:
                route = json.loads(row["route_json"])
            except (TypeError, ValueError) as exc:
                raise RouteError("已印路线记录损坏，无法复核。") from exc
            broken = routecheck.first_broken_hop(graph, route)

            event = None
            if broken is None:
                conn.execute(
                    "UPDATE printed_routes SET status = 'live', "
                    "last_checked_version = ?, last_checked_at = ? WHERE id = ?",
                    (version, now, route_id),
                )
                status = "live"
            else:
                status = "broken"
                if row["status"] == "live":
                    event = _record_break(conn, row, broken, version, now)
                else:
                    _mark_checked(conn, route_id, version, now)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "id": route_id,
        "status": status,
        "version": version,
        "break": event,
        "repaired": broken is None and row["status"] == "broken",
    }


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #
def list_routes(conn, *, active_only: bool = False) -> list[dict]:
    if active_only:
        rows = conn.execute(
            "SELECT * FROM printed_routes WHERE status = 'live' ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM printed_routes ORDER BY id").fetchall()
    if not rows:
        return []
    break_rows = conn.execute(
        "SELECT * FROM printed_route_breaks ORDER BY route_id, id"
    ).fetchall()
    breaks: dict[int, list[dict]] = {}
    for record in break_rows:
        breaks.setdefault(record["route_id"], []).append(
            {
                "id": record["id"],
                "broken_version": record["broken_version"],
                "position": record["position"],
                "from_number": record["from_number"],
                "to_number": record["to_number"],
                "reason": record["reason"],
                "created_at": record["created_at"],
            }
        )
    out = []
    for row in rows:
        try:
            source_versions = json.loads(row["source_versions"] or "[]")
        except (TypeError, ValueError):
            source_versions = []
        try:
            route = json.loads(row["route_json"])
        except (TypeError, ValueError):
            route = []
        out.append(
            {
                "id": row["id"],
                "start": row["start_number"],
                "end": row["end_number"],
                "route": route,
                "hops": row["hops"],
                "source_versions": source_versions,
                "status": row["status"],
                "registered_version": row["registered_version"],
                "registered_at": row["registered_at"],
                "last_checked_version": row["last_checked_version"],
                "last_checked_at": row["last_checked_at"],
                "breaks": breaks.get(row["id"], []),
            }
        )
    return out
