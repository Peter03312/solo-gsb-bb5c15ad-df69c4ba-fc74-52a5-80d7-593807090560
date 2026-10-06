"""联合预检测试：错误收集与原数组位置、候选图引用校验、图无效时的
引用豁免、计数与版本不变。"""
import os
import tempfile
import unittest
from pathlib import Path

from app.db import (
    connect,
    current_version,
    get_snapshot,
    init_db,
    publish_closures,
    publish_graph,
)
from app.schemas import ValidationError, normalize_closures, normalize_graph, precheck_import

GRAPH = {
    "nodes": ["GATE", "PLAZA", "LAKE", "TOWER"],
    "edges": [
        {"id": "E1", "from": "GATE", "to": "PLAZA", "travel_seconds": 180, "has_stairs": False},
        {"id": "E3", "from": "PLAZA", "to": "LAKE", "travel_seconds": 300, "has_stairs": False},
        {"id": "E5", "from": "LAKE", "to": "TOWER", "travel_seconds": 240, "has_stairs": False,
         "reverse_windows": [
             {"start": "2026-10-05T11:00:00Z", "end": "2026-10-05T13:00:00Z"},
             {"start": "2026-10-05T14:00:00Z", "end": "2026-10-05T16:00:00Z"}]},
    ],
}
CLOSURES = [
    {"edge_id": "E5", "start": "2026-10-05T10:00:00Z", "end": "2026-10-05T11:00:00Z"},
]


def paths(report, source=None):
    return [e["path"] for e in report["errors"]
            if source is None or e["source"] == source]


def messages(report):
    return [e["message"] for e in report["errors"]]


class PrecheckValidTests(unittest.TestCase):
    def test_valid_payload_passes_with_counts(self):
        report = precheck_import({"graph": GRAPH, "closures": CLOSURES})
        self.assertTrue(report["valid"], report)
        self.assertTrue(report["graph_valid"])
        self.assertTrue(report["closures_valid"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(
            report["counts"],
            {"nodes": 4, "edges": 3, "closures": 1, "reverse_windows": 2},
        )

    def test_closures_accepts_import_payload_shape(self):
        # closures 也兼容 /admin/closures 的 {"closures": [...]} 负载形式
        report = precheck_import({"graph": GRAPH, "closures": {"closures": CLOSURES}})
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["counts"]["closures"], 1)

    def test_empty_graph_and_closures_are_valid(self):
        report = precheck_import({"graph": {"nodes": [], "edges": []}, "closures": []})
        self.assertTrue(report["valid"], report)
        self.assertEqual(
            report["counts"],
            {"nodes": 0, "edges": 0, "closures": 0, "reverse_windows": 0},
        )

    def test_no_counts_when_invalid(self):
        report = precheck_import({"graph": {"nodes": []}, "closures": []})
        self.assertFalse(report["valid"])
        self.assertNotIn("counts", report)


class PrecheckGraphErrorTests(unittest.TestCase):
    def test_graph_errors_all_reported_with_positions(self):
        bad_graph = {
            "nodes": ["A", "A", ""],
            "edges": [
                {"id": "X1", "from": "A", "to": "GHOST",
                 "travel_seconds": 10, "has_stairs": False},
                {"id": "X1", "from": "A", "to": "A",
                 "travel_seconds": 0, "has_stairs": False},
                "not-an-object",
                {"id": "X4", "from": "A", "to": "A",
                 "travel_seconds": 5, "has_stairs": "yes"},
            ],
        }
        report = precheck_import({"graph": bad_graph, "closures": []})
        self.assertFalse(report["valid"])
        self.assertFalse(report["graph_valid"])
        self.assertTrue(report["closures_valid"])

        got = paths(report, "graph")
        for expected in ("nodes[1]", "nodes[2]", "edges[0]", "edges[1].id",
                         "edges[1].travel_seconds", "edges[2]", "edges[3].has_stairs"):
            self.assertIn(expected, got, report)
        text = "\n".join(messages(report))
        for fragment in ("节点重复", "不能为空", "引用了未定义的节点",
                         "路段编号重复", "必须为正数", "必须是对象", "必须是布尔值"):
            self.assertIn(fragment, text, report)

    def test_reverse_window_errors_collected_with_positions(self):
        graph = {
            "nodes": ["A", "B"],
            "edges": [
                {"id": "E1", "from": "A", "to": "B", "travel_seconds": 10,
                 "has_stairs": False,
                 "reverse_windows": [
                     {"start": 100, "end": 100},          # start == end
                     {"start": 0, "end": 50},
                     {"start": 40, "end": 60}]},          # 与上一窗重叠
                {"id": "E2", "from": "B", "to": "A", "travel_seconds": 10,
                 "has_stairs": False, "reverse_windows": "not-a-list"},
            ],
        }
        report = precheck_import({"graph": graph, "closures": []})
        self.assertFalse(report["graph_valid"])
        got = paths(report, "graph")
        self.assertIn("edges[0].reverse_windows[0]", got)
        self.assertIn("edges[0].reverse_windows", got)
        self.assertIn("edges[1].reverse_windows", got)
        text = "\n".join(messages(report))
        for fragment in ("无效时间窗", "重叠", "必须是数组"):
            self.assertIn(fragment, text, report)

    def test_nodes_not_a_list_skips_edge_node_reference_check(self):
        # nodes 不可判定时不级联误报边的节点引用
        report = precheck_import({
            "graph": {"nodes": "oops",
                      "edges": [{"id": "X", "from": "A", "to": "B",
                                 "travel_seconds": 10, "has_stairs": False}]},
            "closures": [],
        })
        self.assertEqual(paths(report, "graph"), ["nodes"], report)


class PrecheckClosureErrorTests(unittest.TestCase):
    def test_closure_errors_all_reported_with_positions(self):
        bad_closures = [
            {"edge_id": "NOPE", "start": "2026-10-05T08:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
            {"edge_id": "E1", "start": "2026-10-05T10:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
            {"edge_id": "E1", "start": "bad-time", "end": "2026-10-05T09:00:00Z"},
            {"edge_id": "E1", "end": "2026-10-05T09:00:00Z"},
            42,
        ]
        report = precheck_import({"graph": GRAPH, "closures": bad_closures})
        self.assertFalse(report["valid"])
        self.assertTrue(report["graph_valid"])
        self.assertFalse(report["closures_valid"])

        got = paths(report, "closures")
        for expected in ("closures[0].edge_id", "closures[1]", "closures[2]",
                         "closures[3].start", "closures[4]"):
            self.assertIn(expected, got, report)
        text = "\n".join(messages(report))
        for fragment in ("不存在的路段", "无效时间区间", "无法解析", "缺少 start",
                         "必须是对象"):
            self.assertIn(fragment, text, report)

    def test_closure_reference_checked_against_candidate_graph(self):
        # 候选图只有 ONLY 一条路段：引用 E1（当前库示例数据中存在）也判无效
        candidate = {
            "nodes": ["A", "B"],
            "edges": [{"id": "ONLY", "from": "A", "to": "B",
                       "travel_seconds": 10, "has_stairs": False}],
        }
        closures = [
            {"edge_id": "E1", "start": "2026-10-05T08:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
            {"edge_id": "ONLY", "start": "2026-10-05T08:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
        ]
        report = precheck_import({"graph": candidate, "closures": closures})
        self.assertTrue(report["graph_valid"])
        self.assertFalse(report["closures_valid"])
        self.assertEqual(paths(report, "closures"), ["closures[0].edge_id"], report)
        self.assertIn("不存在的路段", messages(report)[0])

    def test_invalid_graph_skips_closure_edge_reference(self):
        # 图无效：封闭项自身错误照常报告，但路段引用不可判定、不误报
        bad_graph = {
            "nodes": ["A"],
            "edges": [{"id": "X1", "from": "A", "to": "GHOST",
                       "travel_seconds": 10, "has_stairs": False}],
        }
        closures = [
            {"edge_id": "WHATEVER", "start": "2026-10-05T08:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
            {"edge_id": "ALSO_ANY", "start": "2026-10-05T10:00:00Z",
             "end": "2026-10-05T09:00:00Z"},
        ]
        report = precheck_import({"graph": bad_graph, "closures": closures})
        self.assertFalse(report["valid"])
        self.assertFalse(report["graph_valid"])
        self.assertFalse(report["closures_valid"])  # 因 closures[1] 自身区间错误

        closure_paths = paths(report, "closures")
        self.assertNotIn("closures[0]", closure_paths, report)  # 引用未误报
        self.assertNotIn("closures[0].edge_id", closure_paths, report)
        self.assertIn("closures[1]", closure_paths, report)  # 自身错误仍报告
        self.assertNotIn("不存在的路段", "\n".join(messages(report)), report)


class PrecheckPayloadShapeTests(unittest.TestCase):
    def test_missing_sections(self):
        report = precheck_import({})
        self.assertFalse(report["valid"])
        self.assertIn("graph", paths(report, "graph"))
        self.assertIn("closures", paths(report, "closures"))

        report = precheck_import({"graph": GRAPH})
        self.assertTrue(report["graph_valid"])
        self.assertFalse(report["valid"])
        self.assertIn("closures", paths(report, "closures"))

    def test_non_object_payload(self):
        for bad in ("text", 42, [], None):
            report = precheck_import(bad)
            self.assertFalse(report["valid"], bad)
            self.assertEqual(report["errors"][0]["source"], "payload")

    def test_non_object_graph_or_closures(self):
        report = precheck_import({"graph": "nope", "closures": "nope"})
        self.assertFalse(report["graph_valid"])
        self.assertFalse(report["closures_valid"])
        self.assertIn("graph", paths(report, "graph"))
        self.assertIn("closures", paths(report, "closures"))


class PrecheckVersionTests(unittest.TestCase):
    """预检为纯只读：任何结果都不得改变当前数据版本。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "test.db")
        os.environ["APP_DB_PATH"] = self.db_path
        self.conn = connect(Path(self.db_path))
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        os.environ.pop("APP_DB_PATH", None)

    def test_precheck_never_changes_version(self):
        publish_graph(self.conn, GRAPH)
        publish_closures(self.conn, {"closures": CLOSURES})
        before = current_version(self.conn)
        snap_before = get_snapshot(self.conn)

        for payload in (
            {"graph": GRAPH, "closures": CLOSURES},                 # 通过
            {"graph": {"nodes": []}, "closures": []},               # 图无效
            {"graph": GRAPH, "closures": [{"edge_id": "NOPE",
                                           "start": 1, "end": 2}]},  # 封闭无效
            "not-even-a-dict",                                       # 负载无效
        ):
            precheck_import(payload)
            self.assertEqual(current_version(self.conn), before)
            snap = get_snapshot(self.conn)
            self.assertEqual(snap.nodes, snap_before.nodes)
            self.assertEqual(snap.edges, snap_before.edges)
            self.assertEqual(snap.closures, snap_before.closures)
            self.assertEqual(snap.reverse_windows, snap_before.reverse_windows)


class NormalizeWrapperTests(unittest.TestCase):
    """导入路径的抛异常式校验保持首错误语义不变。"""

    def test_normalize_graph_raises_first_error(self):
        payload = {
            "nodes": ["A"],
            "edges": [{"id": "X", "from": "A", "to": "GHOST",
                       "travel_seconds": 0, "has_stairs": False}],
        }
        with self.assertRaises(ValidationError) as ctx:
            normalize_graph(payload)
        # 未知节点引用先于 travel_seconds 校验
        self.assertIn("引用了未定义的节点", str(ctx.exception))

    def test_normalize_closures_raises_first_error(self):
        payload = {"closures": [
            {"edge_id": "NOPE", "start": "bad-time", "end": "2026-10-05T09:00:00Z"}]}
        with self.assertRaises(ValidationError) as ctx:
            normalize_closures(payload, {"E1"})
        # 路段引用先于时间格式校验
        self.assertIn("不存在的路段", str(ctx.exception))

    def test_normalize_graph_valid_payload_unchanged(self):
        graph = normalize_graph(GRAPH)
        self.assertEqual(graph["nodes"], GRAPH["nodes"])
        self.assertEqual(len(graph["edges"]), 3)
        self.assertEqual(len(graph["edges"][2]["reverse_windows"]), 2)


if __name__ == "__main__":
    unittest.main()
