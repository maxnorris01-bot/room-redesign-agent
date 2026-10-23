from __future__ import annotations

import copy
import importlib.util
import json
import re
import socket
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

from app import report
from app.config import Config
from app.images import synthetic_room_image
from app.report import (
    PlanShapes,
    build_index,
    build_report,
    critical_rows,
    plan_shapes,
    price_bar_positions,
    safe_url,
    write_index,
    write_report,
)
from app.spike import run_spike, write_outputs


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setenv("APP_LLM_MODE", "mock")


def _run(tmp_path: Path, **kw: Any) -> tuple[dict[str, Any], Path]:
    kw.setdefault("style", "warm minimalist")
    result = run_spike(synthetic_room_image(), config=Config(), run_id="r1", **kw)
    out = write_outputs(result, tmp_path)
    return json.loads((out / "result.json").read_text()), out


@pytest.fixture(scope="module")
def mock_result(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _run(tmp_path_factory.mktemp("m"), slots=6, replace=["sofa"])[0]


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(html: str) -> list[tuple[str, dict[str, str | None]]]:
    p = _Tags()
    p.feed(html)
    return p.tags


def _assert_safe(html: str) -> None:
    tags = _tags(html)
    names = {t for t, _ in tags}
    assert "script" not in names and "iframe" not in names and "link" not in names
    for tag, attrs in tags:
        assert not any(k.startswith("on") for k in attrs), (tag, attrs)
        if (
            tag == "a"
            and attrs.get("href")
            and not attrs["href"].endswith((".json", ".jsonl", ".html"))
        ):
            assert attrs["href"].startswith(("http://", "https://"))
            assert "noopener" in (attrs.get("rel") or "") and attrs.get("target") == "_blank"
        if tag == "img":
            assert attrs["src"].startswith(("https://", "data:image/")), attrs
    assert "Content-Security-Policy" in html and "default-src 'none'" in unescape(html)
    assert "style-src 'unsafe-inline'" in unescape(html) and "script-src" not in html
    assert not re.search(
        r"(src|href)=\"(?!https://|data:|#|result|trace|report|[\w%-]+/report)", html
    )
    assert "@import" not in html and "url(http" not in html


def test_mock_report_has_every_section_and_is_safe(mock_result: dict[str, Any]) -> None:
    html = build_report(mock_result, has_trace=True)
    _assert_safe(html)
    for heading in ("Room", "Rough floor plan", "Proposal", "Notes", "Totals", "Cost and time"):
        assert f"<h2>{heading}</h2>" in html
    assert "result.json" in html and "trace.jsonl" in html
    assert "provider (estimated)" in html.lower() and "critical path" in html.lower()
    assert "replace requested" in html and "keep by default" in html
    assert "matches f1" in html or "matches f2" in html
    assert "image based" in html
    r = copy.deepcopy(mock_result)
    r["proposal"]["items"][0].update(style_clash="mild", style_clash_with="armchair (f2)")
    clash_html = build_report(r)
    assert "mild clash" in clash_html and "armchair (f2)" in clash_html
    assert "view at retailer" in html and "opens Google Shopping, retailer link on request" in html
    assert "Room photo not saved" in html  # no image handed in


def test_report_is_deterministic(mock_result: dict[str, Any]) -> None:
    assert build_report(mock_result) == build_report(copy.deepcopy(mock_result))


def test_hostile_strings_are_escaped_and_links_dropped(mock_result: dict[str, Any]) -> None:
    r = copy.deepcopy(mock_result)
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    r["style"] = evil
    r["perception"]["furniture"][0]["note"] = evil
    r["perception"]["room_type"] = evil
    r["warnings"].append(evil)
    item = r["proposal"]["items"][0]
    item["pick"]["name"] = evil
    item["pick"]["url"] = "javascript:alert(1)"
    item["pick"]["thumbnail_url"] = "javascript:alert(3)"
    item["rationale"] = evil
    item["unverified_alternates"][0]["url"] = 'https://x.example/a"onclick="alert(4)'
    item["unverified_alternates"][0]["name"] = evil
    html = build_report(r)
    _assert_safe(html)
    assert "&lt;script&gt;" in html
    assert "javascript:" not in html
    assert 'onclick="alert' not in html
    assert "no usable link" in html


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://shop.example/p/1?a=1&b=2", True),
        ("http://shop.example/p", True),
        ("javascript:alert(1)", False),
        ("data:text/html,<b>", False),
        ("//shop.example/p", False),
        ("https://shop.example/a b", False),
        ('https://shop.example/"x', False),
        ("", False),
        (None, False),
        ("file:///etc/passwd", False),
    ],
)
def test_safe_url(url: Any, ok: bool) -> None:
    assert (safe_url(url) is not None) is ok


def test_thumbnail_only_https_and_has_placeholder(mock_result: dict[str, Any]) -> None:
    html = build_report(mock_result)
    assert 'class="thumb"' in html and "https://mock.example/thumb/" in html
    r = copy.deepcopy(mock_result)
    r["proposal"]["items"][0]["pick"]["thumbnail_url"] = "http://insecure.example/t.jpg"
    html2 = build_report(r)
    assert "http://insecure.example" not in html2 and "no image" in html2


def test_missing_fields_everywhere_do_not_crash() -> None:
    for minimal in (
        {},
        {"run_id": "x"},
        {"perception": None, "rows": None, "retrieval": "bad"},
        {"proposal": {"items": [{}, {"pick": None}]}, "retrieval": [None, {}]},
    ):
        html = build_report(minimal)
        _assert_safe(html)
        assert "<h1>" in html


def test_old_run_shape_without_mode_or_preferences(mock_result: dict[str, Any]) -> None:
    r = copy.deepcopy(mock_result)
    for key in (
        "mode",
        "price_tier",
        "items",
        "preferences",
        "kept_items",
        "slot_plan",
        "item_count_report",
        "seller_concentration",
        "caps",
        "warnings",
    ):
        r.pop(key, None)
    r["budget_usd"] = 800.0
    for it in r["proposal"]["items"]:
        for k in ("style_basis", "style_clash", "style_clash_with", "replace_size_note"):
            it.pop(k, None)
            it["pick"] and it["pick"].pop("thumbnail_url", None)
    html = build_report(r)
    _assert_safe(html)
    assert "budget" in html and "$800.00" in html


def test_perception_only_and_no_proposal_runs(tmp_path: Path) -> None:
    r, _ = _run(tmp_path / "a", perception_only=True)
    html = build_report(r)
    assert "perception-only run" in html and "Rough floor plan" in html
    r2, _ = _run(tmp_path / "b", with_proposal=False, slots=3)
    html2 = build_report(r2)
    assert "Retrieval (no proposal)" in html2 and "Finalists (no proposal was made)" in html2
    _assert_safe(html2)


def test_web_search_mode_run(tmp_path: Path) -> None:
    result = run_spike(
        synthetic_room_image(),
        budget_usd=900,
        style="x",
        config=Config(retrieval_mode="web_search"),
        run_id="w1",
    )
    html = build_report(json.loads(json.dumps(result.to_json(), default=str)))
    _assert_safe(html)
    assert "web_search" in html


def test_budget_run_shows_budget_and_remaining(tmp_path: Path) -> None:
    r, _ = _run(tmp_path, budget_usd=2500.0, slots=3)
    html = build_report(r)
    assert "$2,500.00" in html and "Remaining" in html


def test_empty_slot_over_budget_and_alternates(mock_result: dict[str, Any]) -> None:
    r = copy.deepcopy(mock_result)
    first, second = r["proposal"]["items"][0], r["proposal"]["items"][1]
    first["pick"], first["pick_id"], first["note"] = None, "", "nothing under the limit"
    first["over_budget_options"] = [copy.deepcopy(second["pick"])]
    html = build_report(r)
    assert "No pick for this slot." in html and "nothing under the limit" in html
    assert "Over-budget options" in html
    assert "Unverified alternates" in html
    _assert_safe(html)


def test_plan_geometry_for_a_known_example() -> None:
    r = {
        "perception": {
            "dimensions": {"width_m": 4.0, "length_m": 5.0},
            "furniture": [
                {
                    "id": "f1",
                    "category": "sofa",
                    "position": {"x": 0.3, "y": 0.6},
                    "size": {"width_m": 2.0, "depth_m": 0.9},
                },
                {
                    "id": "f2",
                    "category": "chair",
                    "position": {"x": 0.5, "y": 0.5},
                    "size": {"width_m": 1.0, "depth_m": 1.0},
                },
            ],
            "fixed_items": [
                {
                    "category": "radiator",
                    "position": {"x": 0.9, "y": 0.5},
                    "size": {"width_m": 0.2, "depth_m": 1.0},
                }
            ],
            "openings": [
                {"kind": "window", "wall": "east", "position_along_wall": 0.5, "width_m": 1.2},
                {"kind": "door", "wall": "north", "position_along_wall": 0.25, "width_m": 0.8},
            ],
        },
        "preferences": {
            "resolved": [
                {"id": "f1", "decision": "replace", "source": "requested"},
                {"id": "f2", "decision": "keep", "source": "default"},
            ]
        },
        "proposal": {
            "items": [
                {
                    "slot": "sofa",
                    "pick": {"name": "n"},
                    "replaces": {"id": "f1", "category": "sofa"},
                },
                {"slot": "lamp", "pick": {"name": "n"}, "replaces": None},
            ]
        },
    }
    s = plan_shapes(r)
    assert isinstance(s, PlanShapes) and (s.width_m, s.length_m) == (4.0, 5.0)
    by = {(x.kind, x.label): x for x in s.rects}
    sofa_old = by[("replaced", "f1 sofa")]
    assert (sofa_old.x, sofa_old.y, sofa_old.w, sofa_old.h) == pytest.approx((0.2, 2.55, 2.0, 0.9))
    new = by[("new", "sofa")]
    assert (new.x, new.y, new.w, new.h) == (sofa_old.x, sofa_old.y, sofa_old.w, sofa_old.h)
    assert ("kept", "f2 chair") in by and by[("fixed", "radiator")].x == pytest.approx(3.5)
    assert s.unplaced == ["lamp"]
    east = next(o for o in s.openings if o.kind == "window")
    assert (east.x1, east.x2) == (4.0, 4.0) and (east.y1, east.y2) == pytest.approx((1.9, 3.1))
    north = next(o for o in s.openings if o.kind == "door")
    assert (north.y1, north.x1, north.x2) == (0.0, pytest.approx(0.6), pytest.approx(1.4))
    svg = report.render_plan(s)
    assert 'class="p-new"' in svg and 'class="p-replaced"' in svg and "N</text>" in svg


def test_plan_is_omitted_without_room_dimensions() -> None:
    assert plan_shapes({"perception": {}}) is None
    assert "Rough floor plan" not in build_report({"perception": {"room_type": "x"}})


def test_price_bar_positions() -> None:
    stats = {
        "min_usd": 100.0,
        "max_usd": 500.0,
        "p33_usd": 200.0,
        "median_usd": 300.0,
        "p67_usd": 400.0,
    }
    pos = price_bar_positions(stats, 300.0)
    assert pos is not None
    lo, hi = report.BAR_PAD, report.BAR_W - report.BAR_PAD
    assert pos["min"] == lo and pos["max"] == hi
    assert pos["pick"] == pos["median"] == pytest.approx((lo + hi) / 2)
    assert lo < pos["p33"] < pos["median"] < pos["p67"] < hi
    assert price_bar_positions(stats, 9999.0)["pick"] == hi  # type: ignore[index]
    flat = price_bar_positions({"min_usd": 5, "max_usd": 5}, 5)
    assert flat is not None and flat["pick"] == report.BAR_W / 2
    assert price_bar_positions({}, 5) is None
    assert "pricebar" in report.render_price_bar(stats, 250.0)


def test_cost_table_marks_estimated_rows_and_critical_path(mock_result: dict[str, Any]) -> None:
    html = build_report(mock_result)
    assert "estimated</span>" in html and "critical path</span>" in html
    rows = [
        {"stage": "a", "slot": None, "group": None, "seconds": 1.0},
        {"stage": "s1", "slot": "x", "kind": "search", "group": None, "seconds": 2.0},
        {"stage": "l1", "slot": "x", "kind": "lookup", "group": None, "seconds": 1.0},
        {"stage": "s2", "slot": "y", "kind": "search", "group": None, "seconds": 0.5},
    ]
    assert critical_rows(rows) == {0, 1, 2}


def test_write_report_room_image_and_fallback(tmp_path: Path) -> None:
    _, out = _run(tmp_path / "runs")
    assert (out / "room.jpg").is_file()
    path = write_report(out)
    html = path.read_text()
    assert path.name == "report.html" and 'src="data:image/jpeg;base64,' in html
    (out / "room.jpg").unlink()
    assert "Room photo not saved" in write_report(out).read_text()
    photos = tmp_path / "photos"
    photos.mkdir()
    data = json.loads((out / "result.json").read_text())
    (photos / "room1.png").write_bytes(synthetic_room_image())
    data["photo_name"] = "room1.png"
    (out / "result.json").write_text(json.dumps(data))
    assert "data:image/jpeg;base64," in write_report(out, photo_dir=photos).read_text()


def test_index_lists_runs_and_survives_a_broken_one(
    tmp_path: Path, mock_result: dict[str, Any]
) -> None:
    for name in ("20261001T000000Z-aaaaaa", "20261002T000000Z-bbbbbb"):
        d = tmp_path / name
        d.mkdir()
        (d / "result.json").write_text(json.dumps({**mock_result, "run_id": name}))
        write_report(d)
    bad = tmp_path / "20261003T000000Z-cccccc"
    bad.mkdir()
    (bad / "result.json").write_text("{not json")
    (tmp_path / "stray").mkdir()
    index = write_index(tmp_path)
    html = index.read_text()
    _assert_safe(html)
    assert html.index("bbbbbb") < html.index("aaaaaa")
    assert 'href="20261001T000000Z-aaaaaa/report.html"' in html
    assert "unreadable result.json" in html and "freeplay" in html and "warm minimalist" in html
    assert build_index([]).count("No runs yet.") == 1


def _cli(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_spike_writes_report_by_default_and_no_report_turns_it_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = _cli("spike")
    assert cli.main(["--synthetic", "--style", "x", "--items", "3"]) == 0
    (run_dir,) = list((tmp_path / "spike-output").iterdir())
    assert (run_dir / "report.html").is_file() and (run_dir / "room.jpg").is_file()
    assert "report.html" in capsys.readouterr().out
    assert "room_image" not in (run_dir / "result.json").read_text()
    assert cli.main(["--synthetic", "--style", "x", "--items", "3", "--no-report"]) == 0
    reports = list((tmp_path / "spike-output").glob("*/report.html"))
    assert len(reports) == 1


def test_report_failure_never_fails_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    cli = _cli("spike")

    def boom(_: Path) -> Path:
        raise RuntimeError("template exploded")

    monkeypatch.setattr(cli, "write_report", boom)
    assert cli.main(["--synthetic", "--style", "x", "--items", "3"]) == 0
    err = capsys.readouterr().err
    assert "WARNING: report not generated" in err and "RuntimeError" in err
    (run_dir,) = list((tmp_path / "spike-output").iterdir())
    assert (run_dir / "result.json").is_file() and not (run_dir / "report.html").exists()


def test_report_script_latest_one_and_all(
    tmp_path: Path, capsys: Any, mock_result: dict[str, Any]
) -> None:
    cli = _cli("report")
    root = tmp_path / "out"
    for name in ("20261001T000000Z-aaaaaa", "20261002T000000Z-bbbbbb"):
        (root / name).mkdir(parents=True)
        (root / name / "result.json").write_text(json.dumps(mock_result))
    assert cli.main(["--root", str(root)]) == 0
    out = capsys.readouterr().out
    assert "bbbbbb/report.html" in out and out.splitlines()[-1].startswith("open ")
    assert not (root / "20261001T000000Z-aaaaaa" / "report.html").exists()
    assert cli.main(["--root", str(root), "--all"]) == 0
    assert (root / "index.html").is_file() and (
        root / "20261001T000000Z-aaaaaa" / "report.html"
    ).is_file()
    assert cli.main([str(root / "20261001T000000Z-aaaaaa"), "--root", str(root)]) == 0
    assert cli.main(["--root", str(tmp_path / "missing")]) == 2
