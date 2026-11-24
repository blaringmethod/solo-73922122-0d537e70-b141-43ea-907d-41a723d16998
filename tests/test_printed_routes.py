# -*- coding: utf-8 -*-
"""Cross-version route check plus registration and release-time auditing."""
import html
import json

import pytest

from app import catalog
from app.db import active_version, connect, init_db
from app.graph import CatalogRow
from app.printed_routes import RouteError, register_route
from app.routecheck import analyse_versions, common_graph, shortest_route


def R(number, refs=None):
    return CatalogRow(number=number, title=number, refs=refs or [])


def upload(conn, rows):
    return catalog.apply_changes(conn, upserts=rows, source="test")


# --------------------------------------------------------------------------- #
# Pure graph helpers
# --------------------------------------------------------------------------- #
def test_shortest_route_is_minimal_and_deterministic():
    graph = {
        "A": ["B", "C"],
        "B": ["D"],
        "C": ["D"],
        "D": ["E"],
    }
    assert shortest_route(graph, "A", "E") == ["A", "B", "D", "E"]
    assert shortest_route(graph, "E", "A") is None
    assert shortest_route(graph, "A", "A") == ["A"]
    assert shortest_route(graph, "X", "A") is None


def test_common_graph_intersects_every_version():
    g1 = {"A": ["B", "C"], "B": ["D"], "C": ["D"], "D": []}
    g2 = {"A": ["B"], "B": ["D"], "D": ["C"], "C": []}
    common = common_graph([g1, g2])
    # C is published by both versions, so it stays a node (with no shared
    # outgoing hop); D's outgoing hop differs between versions and is dropped.
    assert common == {"A": ["B"], "B": ["D"], "C": [], "D": []}
    assert shortest_route(common, "A", "D") == ["A", "B", "D"]
    assert shortest_route(common, "A", "C") is None


# --------------------------------------------------------------------------- #
# Access to historical published versions
# --------------------------------------------------------------------------- #
def test_history_keeps_old_index_and_route_check_uses_it(conn):
    # v2: A -> B -> C all serving.
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    v2 = active_version(conn)
    # v3: B's reference to C removed, so B -> C only exists in v2.
    upload(conn, [R("B", [])])
    v3 = active_version(conn)
    assert v2 != v3

    old = conn.execute(
        "SELECT COUNT(*) AS c FROM index_entries WHERE version = ?", (v2,)
    ).fetchone()["c"]
    new = conn.execute(
        "SELECT COUNT(*) AS c FROM index_entries WHERE version = ?", (v3,)
    ).fetchone()["c"]
    assert old == 3 and new == 3

    analysis = analyse_versions(conn, [v2, v3], "A", "C")
    assert analysis.common_route is None
    assert analysis.unreachable_versions == [v3]
    assert analysis.per_version[0].reachable is True  # v2 alone works
    assert analysis.per_version[1].reachable is False

    # v2 alone still has the hop; common route must be empty.
    only_old = analyse_versions(conn, [v2], "A", "C")
    assert only_old.common_route == ["A", "B", "C"]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #
def test_register_route_rechecks_against_active_version(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    v2 = active_version(conn)
    # Active version moves on (title-only change of A, hop B->C still present).
    upload(conn, [R("A", ["B"], )])
    conn.execute(
        "UPDATE plates SET title = 'A 新题名' WHERE number = 'A'"
    )
    upload(conn, [R("C", ["B"])])
    v3 = active_version(conn)
    assert v3 != v2

    result = register_route(
        conn, route=["A", "B", "C"], source_versions=[v2]
    )
    assert result["id"] > 0
    row = conn.execute("SELECT * FROM printed_routes WHERE id = ?", (result["id"],)).fetchone()
    assert row["registered_version"] == v3
    assert row["status"] == "live"
    assert json.loads(row["source_versions"]) == [v2]

    # Registering the identical route twice is refused.
    with pytest.raises(RouteError):
        register_route(conn, route=["A", "B", "C"], source_versions=[v2])


def test_register_route_refuses_a_hop_missing_in_current_version(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    v2 = active_version(conn)
    upload(conn, [R("B", [])])  # B -> C now only in v2
    with pytest.raises(RouteError) as excinfo:
        register_route(conn, route=["A", "B", "C"], source_versions=[v2])
    assert "第 2 跳" in str(excinfo.value)
    assert conn.execute("SELECT COUNT(*) AS c FROM printed_routes").fetchone()["c"] == 0


def test_register_route_validates_endpoints(conn):
    upload(conn, [R("A", ["B"]), R("B")])
    with pytest.raises(RouteError):
        register_route(conn, route=["A"], source_versions=[active_version(conn)])
    with pytest.raises(RouteError):
        register_route(
            conn, route=["A", "B"], source_versions=[active_version(conn)],
            start="B", end="A",
        )


# --------------------------------------------------------------------------- #
# Release-time auditing rules
# --------------------------------------------------------------------------- #
def _register(conn, route):
    return register_route(
        conn, route=route, source_versions=[active_version(conn)]
    )["id"]


def test_release_audit_only_rechecks_routes_with_changed_entries(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C"), R("X", ["Y"]), R("Y")])
    route_ab = _register(conn, ["A", "B"])
    route_xy = _register(conn, ["X", "Y"])

    # Change X/Y, which touches only the second route.  First route untouched.
    report = upload(conn, [R("Y", ["X"])])
    assert route_xy in report.route_audit.checked_route_ids
    assert route_ab not in report.route_audit.checked_route_ids

    row = conn.execute(
        "SELECT last_checked_version FROM printed_routes WHERE id = ?", (route_ab,)
    ).fetchone()
    assert row["last_checked_version"] == 2  # unchanged by the release

    # A change entirely outside both routes triggers no recheck at all.
    report = upload(conn, [R("Z")])
    assert report.route_audit.checked_route_ids == []


def test_first_break_records_version_and_first_bad_hop(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C", ["D"]), R("D")])
    route_id = _register(conn, ["A", "B", "C", "D"])

    # Withdraw C: B -> C is the first unusable hop; the later C -> D also
    # breaks, but only the first is recorded.
    report = catalog.apply_changes(conn, withdrawals=["C"], source="withdraw")
    version = report.version
    breaks = conn.execute(
        "SELECT * FROM printed_route_breaks WHERE route_id = ?", (route_id,)
    ).fetchall()
    assert len(breaks) == 1
    assert breaks[0]["broken_version"] == version
    # B is no longer published (it references withdrawn C), so the kiosk
    # cannot even take the first hop A -> B.
    assert breaks[0]["position"] == 1
    assert (breaks[0]["from_number"], breaks[0]["to_number"]) == ("A", "B")
    status = conn.execute(
        "SELECT status FROM printed_routes WHERE id = ?", (route_id,)
    ).fetchone()["status"]
    assert status == "broken"


def test_break_history_survives_repair_and_suppresses_later_auto_checks(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    route_id = _register(conn, ["A", "B", "C"])
    report = catalog.apply_changes(conn, withdrawals=["B"], source="withdraw")
    first_break_version = report.version

    # Re-add B: the route works again, but history stays and no auto-check
    # runs (the break record exists).
    report = upload(conn, [R("B", ["C"])])
    assert route_id not in report.route_audit.checked_route_ids
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM printed_route_breaks WHERE route_id = ?",
        (route_id,),
    ).fetchone()["c"] == 1

    from app.printed_routes import recheck_route

    result = recheck_route(conn, route_id)
    assert result["repaired"] is True
    assert result["status"] == "live"
    # Historical break retained with the original version number.
    rows = conn.execute(
        "SELECT * FROM printed_route_breaks WHERE route_id = ?", (route_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["broken_version"] == first_break_version


def test_second_break_after_repair_is_appended_not_overwritten(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    route_id = _register(conn, ["A", "B", "C"])
    v_break1 = catalog.apply_changes(conn, withdrawals=["C"]).version
    # Repair via reupload of C, then manual recheck (auto-check suppressed).
    upload(conn, [R("C")])
    from app.printed_routes import recheck_route

    assert recheck_route(conn, route_id)["repaired"] is True

    # Break again with a different hop; the auto-check is suppressed by the
    # existing break history, so the manual recheck appends a second record.
    v_break2 = catalog.apply_changes(conn, withdrawals=["A"]).version
    assert v_break2 > v_break1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM printed_route_breaks WHERE route_id = ?",
        (route_id,),
    ).fetchone()["c"] == 1
    result = recheck_route(conn, route_id)
    assert result["break"] is not None
    assert result["break"].from_number == "A"
    assert result["break"].to_number == "B"
    rows = conn.execute(
        "SELECT * FROM printed_route_breaks WHERE route_id = ? ORDER BY id",
        (route_id,),
    ).fetchall()
    assert len(rows) == 2


def test_failed_release_keeps_old_version_and_break_history(conn):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C"), R("E")])
    route_id = _register(conn, ["A", "B", "C"])

    # First real break: recorded with the release version.
    v_break = catalog.apply_changes(conn, withdrawals=["C"]).version
    rows_before = conn.execute(
        "SELECT * FROM printed_route_breaks ORDER BY id"
    ).fetchall()
    entries_before = {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT * FROM index_entries WHERE version = ?", (v_break,)
        )
    }

    def boom():
        raise RuntimeError("publish failed")

    with pytest.raises(RuntimeError):
        catalog.apply_changes(
            conn, upserts=[R("B", ["D"])], fail_hook=boom
        )

    assert active_version(conn) == v_break
    rows_after = conn.execute(
        "SELECT * FROM printed_route_breaks ORDER BY id"
    ).fetchall()
    assert [dict(r) for r in rows_after] == [dict(r) for r in rows_before]
    entries_after = {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT * FROM index_entries WHERE version = ?", (v_break,)
        )
    }
    assert entries_after == entries_before
    # Old index still downloadable/servable: unrelated E stays published.
    assert "E" in entries_after


def test_route_check_web_page_end_to_end(client):
    def upload_csv(text):
        return client.post("/upload", data={"csv_text": text}, follow_redirects=True)

    upload_csv(
        "图版号,题名,引用号\n"
        "A1,一,A2\nA2,二,A3\nA3,三,A4\nA4,四,\n"
    )
    v2 = client.get("/api/index").get_json()["version"]
    # Second release with a different hop layout.
    upload_csv("图版号,题名,引用号\nA1,一,A2;A4\n")
    v3 = client.get("/api/index").get_json()["version"]
    assert v2 != v3

    page = client.get(f"/routes?versions={v2}&versions={v3}&start=A1&end=A4")
    assert page.status_code == 200
    body = page.data.decode()
    assert "共用路线" in body
    assert "A1" in body and "A4" in body

    # Route common to both versions: A1 -> A4 exists only in v3, so the common
    # route is A1 -> A2 -> A3 -> A4 (all hops in both versions).
    import re

    match = re.search(r'name="route"\s+value="([^"]+)"', body)
    assert match is not None
    route = json.loads(html.unescape(match.group(1)))
    assert route == ["A1", "A2", "A3", "A4"]

    register = client.post(
        "/routes/register",
        data={
            "start": "A1",
            "end": "A4",
            "versions": f"{v2},{v3}",
            "route": json.dumps(route),
        },
        follow_redirects=True,
    )
    assert register.status_code == 200
    assert "已登记已印路线".encode() in register.data
    api = client.get("/api/routes").get_json()["routes"]
    assert len(api) == 1
    assert api[0]["status"] == "live"
    assert api[0]["source_versions"] == [v2, v3]


def test_route_check_reports_versions_that_cannot_reach_end(client):
    client.post(
        "/upload",
        data={"csv_text": "图版号,题名,引用号\nA,甲,B\nB,乙,C\nC,丙,\n"},
        follow_redirects=True,
    )
    v2 = client.get("/api/index").get_json()["version"]
    client.post(
        "/upload",
        data={"csv_text": "图版号,题名,引用号\nB,乙,\n"},
        follow_redirects=True,
    )
    v3 = client.get("/api/index").get_json()["version"]

    page = client.get(f"/routes?versions={v2}&versions={v3}&start=A&end=C")
    body = page.data.decode()
    assert "没有共用路线" in body
    assert "单独也走不到终点" in body
    assert "走不到终点" in body


def test_route_check_rejects_bad_selection(client):
    v = client.get("/api/index").get_json()["version"]
    page = client.get(f"/routes?versions={v}&start=A&end=C")
    assert "请选择 2 到 5 个已发布索引版本" in page.data.decode()
    page = client.get(f"/routes?versions={v}&versions={v + 99}&start=A&end=C")
    assert "不是已发布索引版本" in page.data.decode()


def test_first_bad_hop_is_where_the_route_stops(conn):
    # Every plate stays published in the new version, but B drops its C hop.
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C", ["D"]), R("D")])
    route_id = _register(conn, ["A", "B", "C", "D"])

    report = upload(conn, [R("B", [])])
    assert [r["status"] for r in conn.execute(
        "SELECT status FROM printed_routes WHERE id = ?", (route_id,)
    )] == ["broken"]
    # A and B are still published, so the first unusable hop is B -> C.
    rows = conn.execute(
        "SELECT * FROM printed_route_breaks WHERE route_id = ?", (route_id,)
    ).fetchall()
    assert len(rows) == 1
    assert (rows[0]["broken_version"], rows[0]["position"], rows[0]["from_number"],
            rows[0]["to_number"]) == (report.version, 2, "B", "C")
    assert "第 2 跳" in rows[0]["reason"]


def test_registration_rechecks_against_active_version_not_selected_history(conn):
    # v2 and v3 both contain A -> B -> C.
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C")])
    v2 = active_version(conn)
    upload(conn, [R("C", ["A"])])
    v3 = active_version(conn)
    assert analyse_versions(conn, [v2, v3], "A", "C").common_route == ["A", "B", "C"]

    # A newer release keeps every hop but changes A's entry; registration is
    # checked against this active version, not the two older selected ones.
    upload(conn, [R("A", ["B"])])
    conn.execute("UPDATE index_versions SET source = 'title only' WHERE version = (SELECT MAX(version) FROM index_versions)")
    upload(conn, [R("A", ["B"])])
    active = active_version(conn)
    assert active not in (v2, v3)

    result = register_route(conn, route=["A", "B", "C"], source_versions=[v2, v3])
    assert result["registered_version"] == active
    row = conn.execute(
        "SELECT * FROM printed_routes WHERE id = ?", (result["id"],)
    ).fetchone()
    assert row["status"] == "live"
    assert json.loads(row["source_versions"]) == [v2, v3]

    # A later release dropping B -> C leaves the route valid in the selected
    # history but unusable on the active version: the active hop check refuses.
    upload(conn, [R("B", [])])
    with pytest.raises(RouteError) as excinfo:
        register_route(conn, route=["A", "B", "C"], source_versions=[v2, v3])
    assert "已不通" in str(excinfo.value)

    # A route that was never a common route of the selected versions is
    # refused even when the active version happens to have that hop.
    upload(conn, [R("A", ["B", "C"]), R("B", ["C"])])
    with pytest.raises(RouteError) as excinfo:
        register_route(conn, route=["A", "C"], source_versions=[v2, v3])
    assert "不是所选版本的共用路线" in str(excinfo.value)


def test_no_common_route_but_every_version_reaches_end(conn):
    # v2 goes A -> B -> C; v3 goes A -> D -> C.  Both versions reach C alone,
    # but no hop sequence is usable in both.
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C"), R("D")])
    v2 = active_version(conn)
    upload(conn, [R("A", ["D"]), R("D", ["C"])])
    v3 = active_version(conn)

    analysis = analyse_versions(conn, [v2, v3], "A", "C")
    assert analysis.common_route is None
    assert analysis.unreachable_versions == []
    assert analysis.per_version[0].route == ["A", "B", "C"]
    assert analysis.per_version[1].route == ["A", "D", "C"]


def test_route_check_web_page_lists_only_unreachable_versions(client):
    client.post(
        "/upload",
        data={"csv_text": "图版号,题名,引用号\nA,甲,B\nB,乙,C\nC,丙,\n"},
        follow_redirects=True,
    )
    v2 = client.get("/api/index").get_json()["version"]
    client.post(
        "/upload",
        data={"csv_text": "图版号,题名,引用号\nA,甲,D\nD,丁,C\n"},
        follow_redirects=True,
    )
    v3 = client.get("/api/index").get_json()["version"]

    page = client.get(f"/routes?versions={v2}&versions={v3}&start=A&end=C")
    body = page.data.decode()
    assert "没有共用路线" in body
    # Both versions reach C on their own, so there is no unreachable list.
    assert "单独也走不到终点" not in body
    assert f"v{v2}" in body and f"v{v3}" in body


def test_release_audit_skips_routes_whose_entries_are_unchanged(client):
    def upload_csv(text):
        return client.post("/upload", data={"csv_text": text}, follow_redirects=True)

    upload_csv(
        "图版号,题名,引用号\nA1,一,A2\nA2,二,\nB1,乙,B2\nB2,丙,\n"
    )
    v = client.get("/api/index").get_json()["version"]
    upload_csv("图版号,题名,引用号\nA1,一,A2\n")
    v2 = client.get("/api/index").get_json()["version"]
    for start, end in (("A1", "A2"), ("B1", "B2")):
        client.post(
            "/routes/register",
            data={"start": start, "end": end, "versions": f"{v},{v2}",
                  "route": json.dumps([start, end])},
            follow_redirects=True,
        )
    assert len(client.get("/api/routes").get_json()["routes"]) == 2

    # Withdraw B2: only the B1 -> B2 route is touched and breaks.
    resp = client.post("/withdraw", data={"number": "B2"}, follow_redirects=True)
    body = resp.data.decode()
    routes = {r["start"]: r for r in client.get("/api/routes").get_json()["routes"]}
    assert routes["B1"]["status"] == "broken"
    assert routes["B1"]["breaks"][0]["position"] == 1
    assert routes["A1"]["status"] == "live"
    assert routes["A1"]["breaks"] == []
    assert "复核已印路线 1 条" in body


def test_register_route_validates_version_list(client):
    client.post(
        "/upload",
        data={"csv_text": "图版号,题名,引用号\nA,甲,B\nB,乙,\n"},
        follow_redirects=True,
    )
    v = client.get("/api/index").get_json()["version"]
    body = client.post(
        "/routes/register",
        data={"start": "A", "end": "B", "versions": str(v),
              "route": json.dumps(["A", "B"])},
        follow_redirects=True,
    ).data.decode()
    assert "核对版本应为 2 到 5 个" in body
    assert client.get("/api/routes").get_json()["routes"] == []


def test_audit_failure_rolls_back_release_and_break_record(conn, monkeypatch):
    upload(conn, [R("A", ["B"]), R("B", ["C"]), R("C"), R("E")])
    route_id = _register(conn, ["A", "B", "C"])
    version_before = active_version(conn)
    entries_before = {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT * FROM index_entries WHERE version = ?", (version_before,)
        )
    }
    original = catalog.printed_routes.audit_release

    def audit_then_fail(*args, **kwargs):
        original(*args, **kwargs)  # would record the break ...
        raise RuntimeError("simulated audit write failure")  # ... then fail

    monkeypatch.setattr(catalog.printed_routes, "audit_release", audit_then_fail)
    with pytest.raises(RuntimeError):
        catalog.apply_changes(conn, withdrawals=["C"], source="withdraw")

    # Release rolled back as a unit: old version, old entries, no new break.
    assert active_version(conn) == version_before
    assert {
        r["plate_number"]: (r["title"], r["refs_text"])
        for r in conn.execute(
            "SELECT * FROM index_entries WHERE version = ?", (version_before,)
        )
    } == entries_before
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM printed_route_breaks"
    ).fetchone()["c"] == 0
    assert conn.execute(
        "SELECT status FROM printed_routes WHERE id = ?", (route_id,)
    ).fetchone()["status"] == "live"
    # Database still usable for the next release.
    monkeypatch.setattr(catalog.printed_routes, "audit_release", original)
    report = catalog.apply_changes(conn, withdrawals=["C"], source="withdraw")
    assert report.version == version_before + 1
    assert conn.execute(
        "SELECT COUNT(*) AS c FROM printed_route_breaks WHERE route_id = ?",
        (route_id,),
    ).fetchone()["c"] == 1


def test_init_db_adds_route_tables_to_existing_database(tmp_path):
    path = str(tmp_path / "existing.db")
    init_db(path)
    conn = connect(path)
    conn.execute("DROP TABLE printed_route_breaks")
    conn.execute("DROP TABLE printed_routes")
    conn.close()

    init_db(path)
    conn = connect(path)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"printed_routes", "printed_route_breaks"} <= tables
    conn.close()


def test_hop_requires_published_endpoints(conn):
    from app.routecheck import first_broken_hop

    graph = {"A": ["B"], "B": ["C"], "C": []}
    # Normal hop passes.
    assert first_broken_hop(graph, ["A", "B"]) is None
    # Edge exists but the target is not published in this version.
    assert first_broken_hop({"A": ["B"], "B": []}, ["A", "B", "C"]) == (2, "B", "C")
    # Source not published at all.
    assert first_broken_hop({"B": ["C"]}, ["A", "B"]) == (1, "A", "B")


def test_parse_route_text_accepts_ascii_and_unicode_arrows():
    from app.printed_routes import parse_route_text

    assert parse_route_text("A1 → A2 → A3") == ["A1", "A2", "A3"]
    assert parse_route_text("A1->A2->A3") == ["A1", "A2", "A3"]
    assert parse_route_text(" A1 > A2 ") == ["A1", "A2"]
    assert parse_route_text("") == []
    assert parse_route_text(None) == []


def test_routes_api_can_filter_to_live_routes(client):
    def upload_csv(text):
        return client.post("/upload", data={"csv_text": text}, follow_redirects=True)

    upload_csv("图版号,题名,引用号\nA,甲,B\nB,乙,\nC,丙,D\nD,丁,\n")
    v2 = client.get("/api/index").get_json()["version"]
    upload_csv("图版号,题名,引用号\nA,甲,B\n")
    v3 = client.get("/api/index").get_json()["version"]
    for start, end in (("A", "B"), ("C", "D")):
        client.post(
            "/routes/register",
            data={"start": start, "end": end, "versions": f"{v2},{v3}",
                  "route": json.dumps([start, end])},
            follow_redirects=True,
        )
    client.post("/withdraw", data={"number": "D"}, follow_redirects=True)

    all_routes = client.get("/api/routes").get_json()["routes"]
    live_routes = client.get("/api/routes?status=live").get_json()["routes"]
    assert {r["start"] for r in all_routes} == {"A", "C"}
    assert [r["start"] for r in live_routes] == ["A"]
    assert live_routes[0]["status"] == "live"


def test_corrupt_route_row_does_not_block_release(client):
    def upload_csv(text):
        return client.post("/upload", data={"csv_text": text}, follow_redirects=True)

    upload_csv("图版号,题名,引用号\nA,甲,B\nB,乙,\n")
    v2 = client.get("/api/index").get_json()["version"]
    upload_csv("图版号,题名,引用号\nA,甲,B;\n")
    v3 = client.get("/api/index").get_json()["version"]
    client.post(
        "/routes/register",
        data={"start": "A", "end": "B", "versions": f"{v2},{v3}",
              "route": json.dumps(["A", "B"])},
        follow_redirects=True,
    )
    import sqlite3
    from app.db import connect
    db_path = client.application.config["DATABASE"]
    con = connect(db_path)
    con.execute("UPDATE printed_routes SET route_json = 'not json'")
    con.commit()
    con.close()

    # Release still succeeds; corrupted route is skipped, not fatal.
    resp = upload_csv("图版号,题名,引用号\nB,乙,A\n")
    assert resp.status_code == 200
    assert client.get("/api/index").get_json()["version"] == v3 + 1
    page = client.get("/routes")
    assert page.status_code == 200


def test_any_still_working_common_route_can_be_registered(conn):
    # A longer detour that is valid in both selected versions and the active
    # version is acceptable: the rule is "route still works", not "shortest".
    upload(conn, [R("A", ["B", "D"]), R("B", ["C"]), R("C"), R("D", ["B"])])
    v2 = active_version(conn)
    upload(conn, [R("C", ["A"])])
    v3 = active_version(conn)
    assert analyse_versions(conn, [v2, v3], "A", "C").common_route == ["A", "B", "C"]

    result = register_route(conn, route=["A", "D", "B", "C"], source_versions=[v2, v3])
    assert result["hops"] == 3
    row = conn.execute(
        "SELECT status FROM printed_routes WHERE id = ?", (result["id"],)
    ).fetchone()
    assert row["status"] == "live"
