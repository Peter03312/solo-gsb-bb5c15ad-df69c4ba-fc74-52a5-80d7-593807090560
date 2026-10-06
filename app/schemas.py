"""导入与查询负载的校验、规范化。

所有校验在写库事务开启之前完成，错误以 :class:`ValidationError` 抛出，
调用方负责返回 400 并保留数据库旧版本。

联合预检（:func:`precheck_import`）复用完全相同的校验规则，但以
"收集全部可判定错误"的方式返回结构化报告，且全程不触碰数据库。
"""
from typing import Any, Optional

from .timeutil import TimeError, parse_time


class ValidationError(ValueError):
    """负载非法（未知节点、非正秒数、无效时间区间等）。"""


def _require_object(payload: Any, kind: str) -> dict:
    if not isinstance(payload, dict):
        raise ValidationError(f"{kind}必须是 JSON 对象")
    return payload


def _clean_identifier(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValidationError(f"{field}必须是非空字符串")
    text = str(value).strip()
    if not text:
        raise ValidationError(f"{field}不能为空")
    return text


def _validate_seconds(value: Any) -> int:
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("通行秒数(travel_seconds)必须是正整数")
    if value <= 0:
        raise ValidationError(f"通行秒数(travel_seconds)必须为正数，收到 {value}")
    return value


def _validate_stairs(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError("台阶标记(has_stairs)必须是布尔值 true/false")
    return value


def _collect_reverse_window_errors(
    raw: Any, where: str
) -> tuple[list[dict], list[tuple[int, int]]]:
    """收集一条路段上反向通行时间窗数组的全部错误（不抛异常）。

    每项 ``{"start": ..., "end": ...}`` 使用既有时间格式，区间为半开
    ``[start, end)`` 且必须 ``start < end``。窗内仅允许从原终点走向原起点，
    窗外仅允许原方向。同一数组内的时间窗不得重叠（端点相接允许）。

    返回 ``(errors, windows)``：``errors`` 为 ``[{"path", "message"}]``；
    ``windows`` 为解析成功、按开始时刻排序的 ``(start, end)`` 纪元秒列表。
    重叠检查在合法窗子集上进行（非法窗的错误在先，不影响首错误语义）。
    """
    errors: list[dict] = []
    if not isinstance(raw, list):
        errors.append(
            {"path": f"{where}.reverse_windows",
             "message": f"{where}.reverse_windows 必须是数组"}
        )
        return errors, []

    windows: list[tuple[int, int]] = []
    for j, item in enumerate(raw):
        wwhere = f"{where}.reverse_windows[{j}]"
        if not isinstance(item, dict):
            errors.append({"path": wwhere, "message": f"{wwhere} 必须是对象"})
            continue
        item_ok = True
        if "start" not in item:
            errors.append({"path": wwhere, "message": f"{wwhere} 缺少 start"})
            item_ok = False
        if "end" not in item:
            errors.append({"path": wwhere, "message": f"{wwhere} 缺少 end"})
            item_ok = False
        if not item_ok:
            continue
        try:
            start = parse_time(item["start"])
            end = parse_time(item["end"])
        except TimeError as exc:
            errors.append({"path": wwhere, "message": f"{wwhere}: {exc}"})
            continue
        if start >= end:
            errors.append(
                {"path": wwhere,
                 "message": f"{wwhere} 无效时间窗: start({item['start']}) "
                            f"必须早于 end({item['end']})"}
            )
            continue
        windows.append((start, end))

    windows.sort()
    for (s1, e1), (s2, e2) in zip(windows, windows[1:]):
        if e1 > s2:
            errors.append(
                {"path": f"{where}.reverse_windows",
                 "message": f"{where} 的反向时间窗存在重叠（半开区间仅允许端点相接）: "
                            f"[{s1}, {e1}) 与 [{s2}, {e2}) 重叠"}
            )
    return errors, windows


def collect_graph_errors(payload: Any) -> tuple[list[dict], Optional[dict]]:
    """收集候选步道图负载的全部可判定错误（不抛异常）。

    校验规则、错误消息与发现顺序同 :func:`normalize_graph` 完全一致，
    区别仅在出错时不抛出、继续向后收集。返回 ``(errors, graph)``：
    ``errors`` 为 ``[{"path", "message"}]``（``path`` 含原数组位置，如
    ``edges[1].travel_seconds``）；全部通过时 ``graph`` 为规范化结构，
    否则为 ``None``。

    ``nodes`` 本身不是数组时，节点集合不可判定，边的节点引用检查跳过
    （仅报告 ``nodes`` 错误），避免级联误报。
    """
    errors: list[dict] = []

    def err(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    if not isinstance(payload, dict):
        err("graph", "步道图必须是 JSON 对象")
        return errors, None

    raw_nodes = payload.get("nodes")
    raw_edges = payload.get("edges")
    nodes_declared = isinstance(raw_nodes, list)
    if not nodes_declared:
        err("nodes", "nodes 必须是数组")
        raw_nodes = []
    if not isinstance(raw_edges, list):
        err("edges", "edges 必须是数组")
        raw_edges = []

    nodes: list[str] = []
    node_set: set[str] = set()
    for i, raw in enumerate(raw_nodes):
        try:
            node = _clean_identifier(raw, f"nodes[{i}]")
        except ValidationError as exc:
            err(f"nodes[{i}]", str(exc))
            continue
        if node in node_set:
            err(f"nodes[{i}]", f"节点重复: {node!r}")
            continue
        node_set.add(node)
        nodes.append(node)

    edges: list[dict] = []
    edge_ids: set[str] = set()
    for i, raw in enumerate(raw_edges):
        where = f"edges[{i}]"
        if not isinstance(raw, dict):
            err(where, f"{where} 必须是对象")
            continue
        edge_ok = True

        edge_id: Optional[str] = None
        try:
            edge_id = _clean_identifier(raw.get("id"), f"{where}.id")
        except ValidationError as exc:
            err(f"{where}.id", str(exc))
            edge_ok = False
        else:
            if edge_id in edge_ids:
                err(f"{where}.id", f"路段编号重复: {edge_id!r}")
                edge_ok = False
            else:
                edge_ids.add(edge_id)

        endpoints: dict[str, str] = {}
        for field in ("from", "to"):
            try:
                endpoints[field] = _clean_identifier(raw.get(field), f"{where}.{field}")
            except ValidationError as exc:
                err(f"{where}.{field}", str(exc))
                edge_ok = False
        # 节点引用仅在 nodes 数组合法、且两端标识均可用时才可判定
        if nodes_declared and len(endpoints) == 2:
            unknown = [n for n in (endpoints["from"], endpoints["to"]) if n not in node_set]
            if unknown:
                err(where, f"{where} 引用了未定义的节点: {', '.join(unknown)}")
                edge_ok = False

        seconds: Optional[int] = None
        if "travel_seconds" not in raw:
            err(f"{where}.travel_seconds", f"{where} 缺少 travel_seconds")
            edge_ok = False
        else:
            try:
                seconds = _validate_seconds(raw["travel_seconds"])
            except ValidationError as exc:
                err(f"{where}.travel_seconds", str(exc))
                edge_ok = False

        has_stairs = False
        try:
            has_stairs = _validate_stairs(raw.get("has_stairs", False))
        except ValidationError as exc:
            err(f"{where}.has_stairs", str(exc))
            edge_ok = False

        # 反向通行时间窗：缺省（None）与空数组都表示全程仅允许原方向
        reverse_windows: list[tuple[int, int]] = []
        if "reverse_windows" in raw and raw["reverse_windows"] is not None:
            window_errors, reverse_windows = _collect_reverse_window_errors(
                raw["reverse_windows"], where
            )
            errors.extend(window_errors)
            if window_errors:
                edge_ok = False

        if edge_ok:
            edges.append(
                {
                    "id": edge_id,
                    "src": endpoints["from"],
                    "dst": endpoints["to"],
                    "seconds": seconds,
                    "has_stairs": has_stairs,
                    "reverse_windows": reverse_windows,
                }
            )

    if errors:
        return errors, None
    # 允许空图；允许自环（正秒数下不会被最短路采用）
    return [], {"nodes": nodes, "edges": edges}


def normalize_graph(payload: Any) -> dict:
    """校验步道图导入负载，返回规范化结构。

    期望格式::

        {
          "nodes": ["gate", "plaza", ...],
          "edges": [
            {"id": "E1", "from": "gate", "to": "plaza",
             "travel_seconds": 120, "has_stairs": false,
             "reverse_windows": [
               {"start": "2026-10-05T08:00:00Z",
                "end": "2026-10-05T09:00:00Z"}]},
            ...
          ]
        }

    每条边可选 ``reverse_windows``：半开 ``[start, end)`` 时间窗数组，
    窗内仅允许从 ``to`` 反向走向 ``from``；缺省或为空时行为不变。
    首个校验错误以 :class:`ValidationError` 抛出。
    """
    errors, graph = collect_graph_errors(payload)
    if errors:
        raise ValidationError(errors[0]["message"])
    return graph


def collect_closure_errors(
    raw_closures: Any, known_edges: Optional[set]
) -> tuple[list[dict], Optional[list]]:
    """收集封闭记录数组的全部可判定错误（不抛异常）。

    ``raw_closures`` 为封闭记录数组（即 ``{"closures": [...]}`` 中的数组
    部分）。``known_edges`` 为可用于校验 ``edge_id`` 引用的路段编号集合；
    传 ``None`` 表示引用关系不可判定（例如联合预检中候选图本身无效），
    此时跳过引用检查、绝不误报，其余字段与时间区间校验照常。

    返回 ``(errors, closures)``：全部通过时 ``closures`` 为规范化列表，
    否则为 ``None``。校验规则与 :func:`normalize_closures` 完全一致。
    """
    errors: list[dict] = []
    if not isinstance(raw_closures, list):
        errors.append({"path": "closures", "message": "closures 必须是数组"})
        return errors, None

    closures: list[dict] = []
    for i, raw in enumerate(raw_closures):
        where = f"closures[{i}]"
        if not isinstance(raw, dict):
            errors.append({"path": where, "message": f"{where} 必须是对象"})
            continue
        item_ok = True

        edge_id: Optional[str] = None
        try:
            edge_id = _clean_identifier(raw.get("edge_id"), f"{where}.edge_id")
        except ValidationError as exc:
            errors.append({"path": f"{where}.edge_id", "message": str(exc)})
            item_ok = False
        else:
            if known_edges is not None and edge_id not in known_edges:
                errors.append(
                    {"path": f"{where}.edge_id",
                     "message": f"{where} 引用了不存在的路段: {edge_id!r}"}
                )
                item_ok = False

        have_start = "start" in raw
        have_end = "end" in raw
        if not have_start:
            errors.append({"path": f"{where}.start", "message": f"{where} 缺少 start"})
            item_ok = False
        if not have_end:
            errors.append({"path": f"{where}.end", "message": f"{where} 缺少 end"})
            item_ok = False
        start = end = None
        if have_start and have_end:
            try:
                start = parse_time(raw["start"])
                end = parse_time(raw["end"])
            except TimeError as exc:
                errors.append({"path": where, "message": f"{where}: {exc}"})
                item_ok = False
            else:
                if start >= end:
                    errors.append(
                        {"path": where,
                         "message": f"{where} 无效时间区间: start({raw['start']}) "
                                    f"必须早于 end({raw['end']})"}
                    )
                    item_ok = False
        if item_ok:
            closures.append({"edge_id": edge_id, "start": start, "end": end})

    if errors:
        return errors, None
    return [], closures


def normalize_closures(payload: Any, known_edges: set[str]) -> list[dict]:
    """校验封闭记录负载。

    期望格式::

        {"closures": [
          {"edge_id": "E1", "start": "2026-10-05T08:00:00Z",
           "end": "2026-10-05T10:00:00Z"}, ...]}

    时间区间为半开 ``[start, end)``，必须满足 ``start < end``。
    首个校验错误以 :class:`ValidationError` 抛出。
    """
    data = _require_object(payload, "封闭记录")
    errors, closures = collect_closure_errors(data.get("closures"), known_edges)
    if errors:
        raise ValidationError(errors[0]["message"])
    return closures


def precheck_import(payload: Any) -> dict:
    """联合预检：同时校验候选步道图与封闭记录，返回结构化报告。

    期望格式（``graph`` 同 ``POST /admin/graph`` 负载；``closures`` 为
    封闭记录数组，也兼容 ``POST /admin/closures`` 的
    ``{"closures": [...]}`` 负载形式）::

        {"graph": {"nodes": [...], "edges": [...]},
         "closures": [{"edge_id": "E4", "start": "...", "end": "..."}]}

    校验规则与两个导入接口完全一致（字段、时间区间、节点/路段引用、
    反向窗重叠）；封闭记录的路段引用以**候选图**的编号集合校验，与
    库中当前数据无关。候选图无效时，封闭记录的路段引用不可判定，
    跳过该项检查（不误报），封闭项自身错误照常报告。

    纯内存校验：不触碰数据库，任何结果都不会改变当前数据版本。
    返回报告::

        {"valid": bool, "graph_valid": bool, "closures_valid": bool,
         "errors": [{"source": "graph"|"closures", "path": ..., "message": ...}],
         "counts": {"nodes": n, "edges": n, "closures": n,
                    "reverse_windows": n}}   # 仅 valid 时存在
    """
    if not isinstance(payload, dict):
        return {
            "valid": False,
            "graph_valid": False,
            "closures_valid": False,
            "errors": [
                {"source": "payload", "path": "",
                 "message": "预检负载必须是 JSON 对象，且包含 graph 与 closures 两个字段"}
            ],
        }

    if "graph" in payload:
        graph_errors, graph = collect_graph_errors(payload["graph"])
    else:
        graph_errors, graph = [{"path": "graph", "message": "缺少 graph（候选步道图）"}], None

    if "closures" in payload:
        raw_closures = payload["closures"]
        # 兼容 /admin/closures 的负载形式 {"closures": [...]}
        if isinstance(raw_closures, dict) and "closures" in raw_closures:
            raw_closures = raw_closures["closures"]
        # 图有效时用候选图的路段编号校验封闭引用；图无效时引用不可判定，跳过
        known_edges = {e["id"] for e in graph["edges"]} if graph is not None else None
        closure_errors, closures = collect_closure_errors(raw_closures, known_edges)
    else:
        closure_errors, closures = [
            {"path": "closures", "message": "缺少 closures（封闭记录）"}
        ], None

    graph_valid = not graph_errors
    closures_valid = not closure_errors
    report = {
        "valid": graph_valid and closures_valid,
        "graph_valid": graph_valid,
        "closures_valid": closures_valid,
        "errors": [
            *[{"source": "graph", **e} for e in graph_errors],
            *[{"source": "closures", **e} for e in closure_errors],
        ],
    }
    if report["valid"]:
        report["counts"] = {
            "nodes": len(graph["nodes"]),
            "edges": len(graph["edges"]),
            "closures": len(closures),
            "reverse_windows": sum(len(e["reverse_windows"]) for e in graph["edges"]),
        }
    return report


def _normalize_stop(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一个按顺序停靠点。

    期望格式::

        {"node": "PLAZA",
         "earliest_start": "2026-10-05T10:00:00Z",
         "latest_start":   "2026-10-05T10:30:00Z",
         "stay_seconds": 600}

    时间窗为闭区间 ``[earliest_start, latest_start]``：开始停留不得早于
    ``earliest_start``、不得晚于 ``latest_start``；``stay_seconds`` 为
    非负整数秒。
    """
    where = f"stops[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("node", "earliest_start", "latest_start", "stay_seconds"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    node = _clean_identifier(raw.get("node"), f"{where}.node")
    if node not in node_set:
        raise ValidationError(f"{where} 引用了未知节点: {node!r}")

    try:
        earliest = parse_time(raw["earliest_start"])
        latest = parse_time(raw["latest_start"])
    except TimeError as exc:
        raise ValidationError(f"{where}: {exc}") from None
    if earliest > latest:
        raise ValidationError(
            f"{where} 无效时间窗: earliest_start({raw['earliest_start']}) "
            f"必须不晚于 latest_start({raw['latest_start']})"
        )

    stay = raw["stay_seconds"]
    # 严格类型：拒绝 bool、float、数字字符串等隐式转换
    if isinstance(stay, bool) or not isinstance(stay, int):
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数")
    if stay < 0:
        raise ValidationError(f"{where}.stay_seconds 必须是非负整数，收到 {stay}")

    return {"node": node, "earliest": earliest, "latest": latest, "stay": stay}


def _normalize_meeting_visitor(raw: Any, index: int, node_set: set[str]) -> dict:
    """校验一名汇合查询访客。

    期望格式::

        {"id": "alice",              # 可选，缺省按序号生成
         "origin": "GATE",
         "departure_time": "2026-10-05T09:00:00Z",   # 最早出发时刻
         "avoid_stairs": false}
    """
    where = f"visitors[{index}]"
    if not isinstance(raw, dict):
        raise ValidationError(f"{where} 必须是对象")
    for field in ("origin", "departure_time"):
        if field not in raw:
            raise ValidationError(f"{where} 缺少必填字段: {field}")

    origin = _clean_identifier(raw.get("origin"), f"{where}.origin")
    if origin not in node_set:
        raise ValidationError(f"{where}.origin 引用了未知节点: {origin!r}")

    try:
        departure = parse_time(raw["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"{where}.departure_time: {exc}") from None

    avoid_stairs = raw.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError(f"{where}.avoid_stairs 必须是布尔值 true/false")

    visitor = {
        "id": index + 1,  # 默认按 1 起始序号
        "origin": origin,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
    }
    if "id" in raw:
        visitor["id"] = _clean_identifier(raw.get("id"), f"{where}.id")
    return visitor


def normalize_meeting(payload: Any, node_set: set[str]) -> dict:
    """校验一次多人汇合点查询负载。

    期望格式::

        {"visitors": [{"origin": ..., "departure_time": ...,
                       "avoid_stairs": ...}, ...],
         "candidates": ["PLAZA", "TOWER"],
         "latest_meeting_time": "2026-10-05T12:00:00Z"}
    """
    data = _require_object(payload, "汇合查询")
    for field in ("visitors", "candidates", "latest_meeting_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    raw_visitors = data["visitors"]
    if not isinstance(raw_visitors, list):
        raise ValidationError("visitors 必须是数组")
    if len(raw_visitors) < 2:
        raise ValidationError("visitors 至少需要包含两名访客")
    visitors = [
        _normalize_meeting_visitor(raw, i, node_set)
        for i, raw in enumerate(raw_visitors)
    ]
    seen_ids: set = set()
    for v in visitors:
        if v["id"] in seen_ids:
            raise ValidationError(f"访客标识重复: {v['id']!r}")
        seen_ids.add(v["id"])

    raw_candidates = data["candidates"]
    if not isinstance(raw_candidates, list):
        raise ValidationError("candidates 必须是数组")
    if not raw_candidates:
        raise ValidationError("candidates 不能为空")
    candidates: list[str] = []
    candidate_set: set[str] = set()
    for i, raw in enumerate(raw_candidates):
        node = _clean_identifier(raw, f"candidates[{i}]")
        if node not in node_set:
            raise ValidationError(f"candidates[{i}] 引用了未知节点: {node!r}")
        if node in candidate_set:
            raise ValidationError(f"candidates 存在重复节点: {node!r}")
        candidate_set.add(node)
        candidates.append(node)

    try:
        latest = parse_time(data["latest_meeting_time"])
    except TimeError as exc:
        raise ValidationError(f"latest_meeting_time: {exc}") from None

    return {
        "visitors": visitors,
        "candidates": candidates,
        "latest_meeting_time": latest,
    }


def normalize_query(payload: Any, node_set: set[str]) -> dict:
    """校验一次路线查询负载。"""
    data = _require_object(payload, "查询")
    for field in ("origin", "destination", "departure_time"):
        if field not in data:
            raise ValidationError(f"缺少必填字段: {field}")

    origin = _clean_identifier(data.get("origin"), "origin")
    destination = _clean_identifier(data.get("destination"), "destination")
    for name, node in (("origin", origin), ("destination", destination)):
        if node not in node_set:
            raise ValidationError(f"{name} 引用了未知节点: {node!r}")

    try:
        departure = parse_time(data["departure_time"])
    except TimeError as exc:
        raise ValidationError(f"departure_time: {exc}") from None

    avoid_stairs = data.get("avoid_stairs", False)
    if not isinstance(avoid_stairs, bool):
        raise ValidationError("avoid_stairs 必须是布尔值 true/false")

    raw_stops = data.get("stops", [])
    if not isinstance(raw_stops, list):
        raise ValidationError("stops 必须是数组")
    stops = [_normalize_stop(raw, i, node_set) for i, raw in enumerate(raw_stops)]

    return {
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "avoid_stairs": avoid_stairs,
        "stops": stops,
    }
