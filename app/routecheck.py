"""Cross-version jump-route checks for the printed guide.

A published index version is a directed graph: every index entry maps a plate
number to the reference numbers printed in that version.  A route is usable on
all kiosks only when *every* hop exists in *every* selected version, so this
module intersects the per-version edge sets before running BFS.

Nothing here writes to the database; release-time auditing lives in
:mod:`app.printed_routes`.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


def load_index_graph(conn, version: int) -> dict[str, list[str]]:
    """Return ``{plate_number: [ref_number, ...]}`` for one published version."""
    graph: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT plate_number, refs_text FROM index_entries "
        "WHERE version = ? ORDER BY plate_number",
        (version,),
    )
    for row in rows:
        graph[row["plate_number"]] = [
            token for token in (row["refs_text"] or "").split(";") if token
        ]
    return graph


def shortest_route(
    graph: dict[str, list[str]], start: str, end: str
) -> list[str] | None:
    """Shortest directed route from ``start`` to ``end`` (BFS).

    Neighbours are visited in sorted order so the same inputs always produce
    the same printed route.  ``start == end`` yields the zero-hop route
    ``[start]``; the web UI rejects that case before calling this.
    """
    if start == end:
        return [start]
    if start not in graph:
        return None

    previous: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        if current == end:
            break
        for neighbour in sorted(graph.get(current, ())):
            if neighbour not in previous:
                previous[neighbour] = current
                queue.append(neighbour)

    if end not in previous:
        return None
    route: list[str] = []
    node: str | None = end
    while node is not None:
        route.append(node)
        node = previous[node]
    route.reverse()
    return route


def common_graph(graphs: list[dict[str, list[str]]]) -> dict[str, list[str]]:
    """Intersect per-version edge sets (all versions must contain a hop)."""
    if not graphs:
        return {}
    first = graphs[0]
    common: dict[str, list[str]] = {}
    for plate, refs in first.items():
        # A plate is usable on a kiosk only if that version publishes the
        # entry, so plates absent from any selected version drop out entirely.
        if any(plate not in graph for graph in graphs[1:]):
            continue
        shared = set(refs)
        for graph in graphs[1:]:
            shared.intersection_update(graph[plate])
        common[plate] = sorted(shared)
    return common


def first_broken_hop(
    graph: dict[str, list[str]], route: list[str]
) -> tuple[int, str, str] | None:
    """Return ``(1-based position, from, to)`` for the first unusable hop.

    A hop is usable only when the version publishes both ends and the source's
    reference list contains the target — a kiosk can neither stand on an
    unpublished plate nor jump to one.
    """
    for position, (source, target) in enumerate(zip(route, route[1:]), start=1):
        if (
            source not in graph
            or target not in graph
            or target not in graph[source]
        ):
            return position, source, target
    return None


@dataclass
class VersionReach:
    version: int
    reachable: bool
    hops: int | None
    route: list[str] = field(default_factory=list)
    start_present: bool = True
    end_present: bool = True


@dataclass
class RouteAnalysis:
    versions: list[int]
    start: str
    end: str
    common_route: list[str] | None
    per_version: list[VersionReach]

    @property
    def common_hops(self) -> int | None:
        return None if self.common_route is None else len(self.common_route) - 1

    @property
    def unreachable_versions(self) -> list[int]:
        """Selected versions that cannot reach the end even on their own."""
        return [item.version for item in self.per_version if not item.reachable]


def analyse_versions(
    conn, versions: list[int], start: str, end: str
) -> RouteAnalysis:
    """Check a common route and each selected version individually."""
    graphs = [load_index_graph(conn, version) for version in versions]
    per_version: list[VersionReach] = []
    for version, graph in zip(versions, graphs):
        start_present = start in graph
        end_present = end in graph
        # Both ends must be published in the version: a kiosk can only make a
        # jump to an entry its index actually contains.
        route = (
            shortest_route(graph, start, end)
            if start_present and end_present
            else None
        )
        per_version.append(
            VersionReach(
                version=version,
                reachable=route is not None,
                hops=None if route is None else len(route) - 1,
                route=route or [],
                start_present=start_present,
                end_present=end_present,
            )
        )
    both_ends_everywhere = all(
        start in graph and end in graph for graph in graphs
    )
    common = (
        shortest_route(common_graph(graphs), start, end)
        if both_ends_everywhere
        else None
    )
    return RouteAnalysis(
        versions=list(versions),
        start=start,
        end=end,
        common_route=common,
        per_version=per_version,
    )


def changed_index_plates(conn, old_version: int, new_version: int) -> set[str]:
    """Plate numbers whose published index entry changed between two versions.

    An entry counts as changed when it was added, removed, or its title /
    reference list differs.  Release-time route auditing only touches printed
    routes that contain at least one such plate.
    """
    rows = conn.execute(
        """
        SELECT plate_number FROM (
            SELECT plate_number, title, refs_text
              FROM index_entries WHERE version = ?
            EXCEPT
            SELECT plate_number, title, refs_text
              FROM index_entries WHERE version = ?
        )
        UNION
        SELECT plate_number FROM (
            SELECT plate_number, title, refs_text
              FROM index_entries WHERE version = ?
            EXCEPT
            SELECT plate_number, title, refs_text
              FROM index_entries WHERE version = ?
        )
        """,
        (old_version, new_version, new_version, old_version),
    )
    return {row[0] for row in rows}
