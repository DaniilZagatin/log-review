"""Deterministic layout of business operations inside fixed stage columns.

Pure geometry, no HTML. Every text is measured with the embedded font's advance
table and wrapped in full; nothing is truncated or elided.

Placement is finite by construction rather than by search:
- each node side holds a stack of port slots; a labelled exit's slot is as tall
  as its label, which sits in a zone beside that side, centred on its own stub;
- every vertical run has a private track in a gap between stages or in the
  stage's own return lane; a long horizontal run uses a row corridor that is
  checked free of obstacles, otherwise a private track in a band between rows;
- right-side ports snap to a 12 px lattice and left-side ports to the same
  lattice shifted by 6 px, so runs entering a gap from opposite sides never
  coincide.
verify() re-checks every invariant on the finished layout and lists violations.
"""

import math
from dataclasses import dataclass, field
from heapq import heapify, heappop, heappush

from .graph_font import ADVANCE_WIDTHS, UNITS_PER_EM

# Font size and line height of each text role; BASELINE is the first baseline below a line top.
NAME = (14, 18)
LABEL = (12, 16)
TITLE = (15, 20)
SMALL = (11, 14)
BASELINE = {14: 13, 12: 12, 15: 15, 11: 11}

NODE_W = 220
TEXT_LIMIT = {"start": 168, "operation": 180, "decision": 168, "end": 168}
TEXT_INSET = {"start": 22, "operation": 16, "decision": 22, "end": 22}
NODE_PAD = 14
MIN_HEIGHT = {"decision": 60}
DEFAULT_HEIGHT = 52
HEX_TIP = 18
PILL_RADIUS = 22
RECT_RADIUS = 11
LABEL_LIMIT = 156
LABEL_PAD_X, LABEL_PAD_Y = 6, 5
TYPE_HEIGHT = 14      # visible node/arrow type, separate from the unchanged source text
LABEL_SAFETY = 1.04  # browser rasterisation margin on top of the advance table
LATTICE = 12
SLOT = 20            # port slot without label; a label adds its height
TRACK = 10
ZONE_GAP = 10
ZONE_MIN = 12
REGION_PAD = 14
GAP_MIN = 40
BAND_PAD = 14
SEPARATOR_H = 32
C_GAP = 8
CLEAR = 14           # corridor clearance, including the 6 px port snap
MARGIN = 20
TITLE_INSET = 18
HEADER_TOP = 18
SEPARATOR_TEXT = "Другие сценарии"
EMPTY_TEXT = "Операции пока не описаны"
_ROW = 100000        # ordering key of a row in track sorting
_BREAK_AFTER = frozenset("._/\\-:,;=+&|)]}>")


# Measurement


def advance(char):
    code = ord(char)
    width = ADVANCE_WIDTHS.get(code)
    if width is None:
        # A fallback glyph is assumed wider, never narrower, than the table.
        width = UNITS_PER_EM * 5 // 4 if code > 0xFFFF else UNITS_PER_EM
    return width


def units(text):
    return sum(advance(char) for char in text)


def text_width(text, size):
    return units(text) * size / UNITS_PER_EM


def fits(text, size, limit):
    return units(text) * size <= limit * UNITS_PER_EM


def normalize(text):
    return " ".join(str(text).split())


@dataclass(frozen=True)
class Text:
    source: str      # whitespace-normalised complete text
    lines: tuple
    joins: tuple     # what joins line i to line i+1: " " or "" inside a long token
    size: int
    line_height: int
    limit: int

    @property
    def height(self):
        return len(self.lines) * self.line_height

    @property
    def width(self):
        return max((text_width(line, self.size) for line in self.lines), default=0)

    def rebuilt(self):
        return "".join(line + join for line, join in zip(self.lines, self.joins + ("",)))


def _pieces(word, size, limit):
    """Split a token wider than the limit, preferring code-like boundaries; no character is added or lost."""
    pieces = []
    while len(word) > 1 and not fits(word, size, limit):
        end, total = 0, 0
        while end < len(word) and (total + advance(word[end])) * size <= limit * UNITS_PER_EM:
            total += advance(word[end])
            end += 1
        end = max(end, 1)
        cut = end
        for k in range(end, max(1, end // 2) - 1, -1):
            if k < len(word) and (word[k - 1] in _BREAK_AFTER or (word[k - 1].islower() and word[k].isupper())):
                cut = k
                break
        pieces.append(word[:cut])
        word = word[cut:]
    pieces.append(word)
    return pieces


def wrap(text, size, line_height, limit):
    source = normalize(text)
    tokens = []
    for number, word in enumerate(source.split(" ") if source else []):
        for index, piece in enumerate(_pieces(word, size, limit)):
            tokens.append((piece, " " if number and not index else ""))
    lines, joins, current = [], [], None
    for piece, separator in tokens:
        if current is None:
            current = piece
        elif fits(current + separator + piece, size, limit):
            current += separator + piece
        else:
            lines.append(current)
            joins.append(separator)
            current = piece
    if current is not None:
        lines.append(current)
    return Text(source, tuple(lines), tuple(joins), size, line_height, limit)


# Model


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    w: float
    h: float

    def overlaps(self, other, gap=0.0):
        return (self.x < other.x + other.w + gap and other.x < self.x + self.w + gap
                and self.y < other.y + other.h + gap and other.y < self.y + self.h + gap)


@dataclass(eq=False)
class Node:
    key: int
    column: int
    kind: str
    text: Text
    scenarios: frozenset
    x: float = 0
    h: int = 0


@dataclass(eq=False)
class Edge:
    number: int
    source: Node
    target: Node
    label: object
    scenarios: frozenset
    label_w: int = 0
    label_h: int = 0


@dataclass(eq=False)
class Column:
    name: str
    empty: bool
    title: object = None
    lzone: int = ZONE_MIN
    rzone: int = ZONE_MIN
    lane: int = 0
    gap: int = 0
    x: int = 0
    w: int = 0

    @property
    def node_x(self):
        return self.x + REGION_PAD + self.lzone


@dataclass(eq=False)
class Plan:
    kind: str        # A forward, B backward, C next below, D same-stage lane
    out_side: str
    in_side: str
    mode: str = ""
    first: object = None
    band: object = None
    last: object = None


class _Track:
    __slots__ = ("group", "key", "pos")

    def __init__(self, group, key):
        self.group, self.key, self.pos = group, key, 0.0


@dataclass(eq=False)
class View:
    key: str
    active_nodes: frozenset
    active_edges: frozenset
    rows: dict
    order: list
    separator_band: object
    plans: dict = field(default_factory=dict)
    sides: dict = field(default_factory=dict)
    ports: dict = field(default_factory=dict)
    rel: dict = field(default_factory=dict)
    row_h: dict = field(default_factory=dict)
    below: dict = field(default_factory=dict)
    obstacles: dict = field(default_factory=dict)
    gap_tracks: dict = field(default_factory=dict)
    lane_tracks: dict = field(default_factory=dict)
    band_tracks: dict = field(default_factory=dict)
    separator_zone: int = 0
    height: int = 0
    node_y: dict = field(default_factory=dict)
    routes: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    separator: object = None


@dataclass(eq=False)
class Layout:
    columns: list
    nodes: list
    edges: list
    views: list = field(default_factory=list)
    width: int = 0
    header_h: int = 0
    title_top: int = 0
    count_top: int = 0
    empty_top: int = 0

    def view(self, key):
        return next(view for view in self.views if view.key == key)


def member(view_key, scenarios):
    if view_key == "all":
        return True
    if view_key == "unassigned":
        return not scenarios
    return view_key.isdigit() and int(view_key) in scenarios


def _kind(value):
    return value if isinstance(value, str) and value in TEXT_LIMIT else "operation"


def radius(node):
    return min(node.h / 2, PILL_RADIUS) if node.kind in ("start", "end") else RECT_RADIUS


def boundary(node, top, side, y):
    """x where a horizontal line at y meets the outline on the left or right side."""
    dy = abs(y - (top + node.h / 2))
    if node.kind == "decision":
        inset = HEX_TIP * dy / (node.h / 2)
    else:
        corner = radius(node)
        straight = node.h / 2 - corner
        inset = 0.0 if dy <= straight else corner - math.sqrt(max(0.0, corner ** 2 - (dy - straight) ** 2))
    return node.x + inset if side == "L" else node.x + NODE_W - inset


def node_box(view, node):
    return Box(node.x, view.node_y[node.key], NODE_W, node.h)


def count_text(lay, view, column):
    members = [node for node in lay.nodes if node.column == column]
    if view.key == "all":
        return f"Шагов: {len(members)}"
    chosen = sum(node.key in view.active_nodes for node in members)
    prefix = "Вне сценариев" if view.key == "unassigned" else "В сценарии"
    return f"{prefix}: {chosen} · всего: {len(members)}"


# Pipeline


def layout(columns, operations, transitions, views):
    """columns: stage titles; operations: dicts with column, kind, name, scenarios;
    transitions: dicts with number, source and target operation indexes, condition, scenarios;
    views: "all", scenario numbers as strings and optionally "unassigned"."""
    nodes = []
    for key, item in enumerate(operations):
        kind = _kind(item.get("kind"))
        nodes.append(Node(key, item["column"], kind, wrap(item["name"], *NAME, TEXT_LIMIT[kind]),
                          frozenset(item["scenarios"])))
    edges = []
    for item in transitions:
        edge = Edge(item["number"], nodes[item["source"]], nodes[item["target"]], None, frozenset(item["scenarios"]))
        label = wrap(item.get("condition") or "", *LABEL, LABEL_LIMIT)
        if label.lines:
            edge.label = label
            edge.label_w = math.ceil(label.width * LABEL_SAFETY) + 2 * LABEL_PAD_X
            edge.label_h = label.height + 2 * LABEL_PAD_Y + TYPE_HEIGHT
        edges.append(edge)
    lay = Layout([Column(name, not any(node.column == index for node in nodes)) for index, name in enumerate(columns)],
                 nodes, edges)
    lay.views = [_plan(lay, key) for key in views]
    _size_nodes(lay)
    for view in lay.views:
        _rows(lay, view)
        _ports(lay, view)
        _route(lay, view)
    _size_columns(lay)
    for view in lay.views:
        _geometry(lay, view)
    return lay


def _flow_sort(group, edges):
    """Order one stage group by its explicit internal edges.

    Back edges found depth-first in model order are ignored and ties keep model
    order, so placement never implies a connection.
    """
    keys = [node.key for node in group]
    position = {key: index for index, key in enumerate(keys)}
    successors = {key: [] for key in keys}
    for edge in edges:
        a, b = edge.source.key, edge.target.key
        if a in successors and b in successors and a != b:
            successors[a].append(b)
    state, forward = {}, {key: [] for key in keys}
    for root in keys:
        if root in state:
            continue
        state[root] = 1
        stack = [(root, iter(successors[root]))]
        while stack:
            key, children = stack[-1]
            child = next(children, None)
            if child is None:
                state[key] = 2
                stack.pop()
            elif state.get(child) != 1:
                forward[key].append(child)
                if child not in state:
                    state[child] = 1
                    stack.append((child, iter(successors[child])))
    indegree = {key: 0 for key in keys}
    for children in forward.values():
        for child in children:
            indegree[child] += 1
    ready = [position[key] for key in keys if not indegree[key]]
    heapify(ready)
    result = []
    while ready:
        key = keys[heappop(ready)]
        result.append(group[position[key]])
        for child in forward[key]:
            indegree[child] -= 1
            if not indegree[child]:
                heappush(ready, position[child])
    return result


def _plan(lay, key):
    active_nodes = frozenset(node.key for node in lay.nodes if member(key, node.scenarios))
    active_edges = frozenset(edge.number for edge in lay.edges if member(key, edge.scenarios))
    groups = []
    for column in range(len(lay.columns)):
        stage = [node for node in lay.nodes if node.column == column]
        groups.append((_flow_sort([node for node in stage if node.key in active_nodes], lay.edges),
                       _flow_sort([node for node in stage if node.key not in active_nodes], lay.edges)))
    top = max((len(first) for first, _ in groups), default=0)
    context = key != "all" and any(rest for _, rest in groups)
    rows = {}
    for first, rest in groups:
        for index, node in enumerate(first):
            rows[node.key] = index
        for index, node in enumerate(rest):
            rows[node.key] = top + index
    order = [node.key for node in sorted(lay.nodes, key=lambda node: (node.column, rows[node.key]))]
    view = View(key, active_nodes, active_edges, rows, order, top if context else None)
    taken_out, taken_in = set(), set()
    for edge in lay.edges:
        s, t = edge.source, edge.target
        if s.column < t.column:
            plan = Plan("A", "R", "L")
        elif s.column > t.column:
            plan = Plan("B", "L", "R")
        elif s is not t and rows[t.key] == rows[s.key] + 1 and s.key not in taken_out and t.key not in taken_in:
            taken_out.add(s.key)
            taken_in.add(t.key)
            plan = Plan("C", "B", "T")
        else:
            plan = Plan("D", "R", "R")
        view.plans[edge.number] = plan
        if plan.kind == "C":
            continue
        height = edge.label_h + SLOT if edge.label else SLOT
        view.sides.setdefault((s.key, plan.out_side), []).append(((rows[t.key], 0, edge.number), edge.number, "out", height))
        view.sides.setdefault((t.key, plan.in_side), []).append(((rows[s.key], 1, edge.number), edge.number, "in", SLOT))
    for items in view.sides.values():
        items.sort(key=lambda item: item[0])
    return view


def _size_nodes(lay):
    for node in lay.nodes:
        need = max(MIN_HEIGHT.get(node.kind, DEFAULT_HEIGHT), node.text.height + 2 * NODE_PAD + TYPE_HEIGHT)
        for view in lay.views:
            for side in ("L", "R"):
                need = max(need, sum(item[3] for item in view.sides.get((node.key, side), ())))
        node.h = need + need % 2  # even heights keep centres on whole pixels


def _rows(lay, view):
    tallest = {}
    for node in lay.nodes:
        row = view.rows[node.key]
        tallest[row] = max(tallest.get(row, 0), node.h)
    view.below = {edge.source.key: edge for edge in lay.edges
                  if view.plans[edge.number].kind == "C" and edge.label}
    for node in lay.nodes:
        row = view.rows[node.key]
        top = (tallest[row] - node.h) // 2
        view.rel[node.key] = top
        spans = view.obstacles.setdefault((row, node.column), [])
        spans.append((top, top + node.h))
        need = tallest[row]
        edge = view.below.get(node.key)
        if edge:
            spans.append((top + node.h + C_GAP, top + node.h + C_GAP + edge.label_h))
            need = max(need, top + node.h + 2 * C_GAP + edge.label_h)
        view.row_h[row] = max(view.row_h.get(row, 0), need)


def _ports(lay, view):
    for (key, _), items in view.sides.items():
        offset = (lay.nodes[key].h - sum(item[3] for item in items)) // 2
        for _, number, role, height in items:
            view.ports[(number, role)] = offset + height // 2
            offset += height


def _clear(view, row, offset, lo, hi):
    """Whether a horizontal run at this row offset passes every stage strictly between lo and hi."""
    return not any(top - CLEAR < offset < bottom + CLEAR
                   for column in range(lo + 1, hi) for top, bottom in view.obstacles.get((row, column), ()))


def _order(start, end, number):
    # Tracks of downward runs fan right-to-left, upward runs left-to-right: fewer crossings.
    return (0, -start, number) if end > start else (1, start, number)


def _route(lay, view):
    def add(store, place, group, key):
        item = _Track(group, key)
        store.setdefault(place, []).append(item)
        return item

    for edge in lay.edges:
        plan, s, t = view.plans[edge.number], edge.source, edge.target
        if plan.kind == "C":
            plan.mode = "vertical"
            continue
        rs, rt = view.rows[s.key], view.rows[t.key]
        out = view.rel[s.key] + view.ports[(edge.number, "out")]
        into = view.rel[t.key] + view.ports[(edge.number, "in")]
        ys, yt = rs * _ROW + out, rt * _ROW + into
        if plan.kind == "D":
            plan.mode = "lane"
            plan.first = add(view.lane_tracks, s.column, 0, (abs(yt - ys), edge.number))
            continue
        aligned = rs == rt and abs(out - into) <= 2
        a, b = s.column, t.column
        lo, hi = min(a, b), max(a, b)
        near_source, near_target = (a + 1, b) if plan.kind == "A" else (a, b + 1)
        if hi - lo == 1:
            plan.mode = "straight" if aligned else "middle"
        elif _clear(view, rs, out, lo, hi):
            plan.mode = "straight" if aligned else "source-row"
        elif _clear(view, rt, into, lo, hi):
            plan.mode = "target-row"
        else:
            plan.mode = "band"
        if plan.mode in ("middle", "target-row"):
            plan.first = add(view.gap_tracks, near_source, 1, _order(ys, yt, edge.number))
        elif plan.mode == "source-row":
            plan.first = add(view.gap_tracks, near_target, 1, _order(ys, yt, edge.number))
        elif plan.mode == "band":
            band = rs + 1 if rt >= rs else rs
            level = band * _ROW - 1
            # Group 0 tracks connect to the gap's left stage, group 2 to its right stage.
            plan.first = add(view.gap_tracks, near_source, 0 if plan.kind == "A" else 2, _order(ys, level, edge.number))
            plan.band = add(view.band_tracks, band, 0, (edge.number,))
            plan.last = add(view.gap_tracks, near_target, 2 if plan.kind == "A" else 0, _order(level, yt, edge.number))
    for store in (view.gap_tracks, view.lane_tracks, view.band_tracks):
        for items in store.values():
            items.sort(key=lambda item: (item.group, item.key))


def _size_columns(lay):
    for index, column in enumerate(lay.columns):
        left = right = lane = 0
        for view in lay.views:
            for edge in lay.edges:
                if edge.label is None or edge.source.column != index:
                    continue
                kind = view.plans[edge.number].kind
                if kind == "B":
                    left = max(left, edge.label_w)
                elif kind in ("A", "D"):
                    right = max(right, edge.label_w)
            lane = max(lane, len(view.lane_tracks.get(index, ())))
        column.lzone = left + 2 * ZONE_GAP if left else ZONE_MIN
        column.rzone = right + 2 * ZONE_GAP if right else ZONE_MIN
        column.lane = (lane + 1) * TRACK if lane else 0
        column.w = 2 * REGION_PAD + column.lzone + NODE_W + column.rzone + column.lane
    x = MARGIN
    for index, column in enumerate(lay.columns):
        if index:
            tracks = max((len(view.gap_tracks.get(index, ())) for view in lay.views), default=0)
            column.gap = max(GAP_MIN, (tracks + 1) * TRACK)
            x += column.gap
        column.x = x
        x += column.w
    lay.width = x + MARGIN
    for node in lay.nodes:
        node.x = lay.columns[node.column].node_x
    for column in lay.columns:
        column.title = wrap(column.name, *TITLE, column.w - 2 * TITLE_INSET)
    lay.title_top = MARGIN + HEADER_TOP
    lay.count_top = lay.title_top + max((column.title.height for column in lay.columns), default=TITLE[1]) + 6
    lay.empty_top = lay.count_top + SMALL[1] + 4
    lay.header_h = (lay.empty_top + SMALL[1] if any(column.empty for column in lay.columns)
                    else lay.count_top + SMALL[1]) + 12


def _snap(y, side):
    shift = LATTICE // 2 if side == "L" else 0
    return LATTICE * math.floor((y - shift) / LATTICE + 0.5) + shift


def _geometry(lay, view):
    rows = max(view.rows.values(), default=-1) + 1
    row_top, y = {}, lay.header_h
    for k in range(rows + 1):
        tracks = view.band_tracks.get(k, ())
        y += BAND_PAD
        for index, item in enumerate(tracks):
            item.pos = y + index * TRACK + TRACK // 2
        y += len(tracks) * TRACK
        if k == view.separator_band:
            view.separator_zone = y
            y += SEPARATOR_H
        y += BAND_PAD
        if k < rows:
            row_top[k] = y
            y += view.row_h.get(k, 0)
    view.height = y + MARGIN
    for node in lay.nodes:
        view.node_y[node.key] = row_top[view.rows[node.key]] + view.rel[node.key]
    for index, items in view.gap_tracks.items():
        column = lay.columns[index]
        start = column.x - column.gap + (column.gap - (len(items) - 1) * TRACK) / 2
        for number, item in enumerate(items):
            item.pos = start + number * TRACK
    for index, items in view.lane_tracks.items():
        column = lay.columns[index]
        for number, item in enumerate(items):
            item.pos = column.node_x + NODE_W + column.rzone + (number + 1) * TRACK
    for edge in lay.edges:
        plan, s, t = view.plans[edge.number], edge.source, edge.target
        sy, ty = view.node_y[s.key], view.node_y[t.key]
        if plan.kind == "C":
            cx = s.x + NODE_W / 2
            view.routes[edge.number] = [(cx, sy + s.h), (cx, ty)]
            if edge.label:
                view.labels[edge.number] = Box(s.x + (NODE_W - edge.label_w) / 2, sy + s.h + C_GAP, edge.label_w, edge.label_h)
            continue
        ys = _snap(sy + view.ports[(edge.number, "out")], plan.out_side)
        yt = ys if plan.mode == "straight" else _snap(ty + view.ports[(edge.number, "in")], plan.in_side)
        points = [(boundary(s, sy, plan.out_side, ys), ys)]
        if plan.mode == "band":
            points += [(plan.first.pos, ys), (plan.first.pos, plan.band.pos),
                       (plan.last.pos, plan.band.pos), (plan.last.pos, yt)]
        elif plan.mode != "straight":
            points += [(plan.first.pos, ys), (plan.first.pos, yt)]
        points.append((boundary(t, ty, plan.in_side, yt), yt))
        view.routes[edge.number] = points
        if edge.label:
            column = lay.columns[s.column]
            zone_x, zone_w = (column.x + REGION_PAD, column.lzone) if plan.out_side == "L" else (s.x + NODE_W, column.rzone)
            view.labels[edge.number] = Box(zone_x + (zone_w - edge.label_w) / 2, ys - edge.label_h / 2,
                                           edge.label_w, edge.label_h)
    if view.separator_band is not None:
        zone = view.separator_zone
        top, bottom = zone + 3, zone + 19
        width = math.ceil(text_width(SEPARATOR_TEXT, SMALL[0])) + 2
        crossings = sorted(x1 for points in view.routes.values() for (x1, y1), (x2, y2) in zip(points, points[1:])
                           if x1 == x2 and min(y1, y2) < bottom + 2 and max(y1, y2) > top - 2)
        x = MARGIN + TITLE_INSET
        for line in crossings:
            if line < x - 4:
                continue
            if line > x + width + 4:
                break
            x = line + 5
        view.separator = (zone + 24, Box(x, top, width, bottom - top))


# Verification


def texts(lay):
    for node in lay.nodes:
        yield f"node {node.key}", node.text
    for edge in lay.edges:
        if edge.label:
            yield f"label {edge.number}", edge.label
    for index, column in enumerate(lay.columns):
        yield f"stage {index}", column.title


def _hits(box, a, b, gap=0.0):
    (x1, y1), (x2, y2) = a, b
    return (min(x1, x2) < box.x + box.w + gap and max(x1, x2) > box.x - gap
            and min(y1, y2) < box.y + box.h + gap and max(y1, y2) > box.y - gap)


def _carries(segment, box):
    """The segment passes through the label centre and leaves it on both sides."""
    (x1, y1), (x2, y2) = segment
    cx, cy = box.x + box.w / 2, box.y + box.h / 2
    if y1 == y2:
        return abs(y1 - cy) < 0.6 and min(x1, x2) < box.x and max(x1, x2) > box.x + box.w
    return abs(x1 - cx) < 0.6 and min(y1, y2) < box.y and max(y1, y2) > box.y + box.h


def _enters(node, top, a, b):
    """The segment runs inside the node outline (touching the outline is allowed)."""
    (x1, y1), (x2, y2) = a, b
    if y1 == y2:
        if not top < y1 < top + node.h:
            return False
        return min(x1, x2) < boundary(node, top, "R", y1) - 0.5 and max(x1, x2) > boundary(node, top, "L", y1) + 0.5
    if not node.x < x1 < node.x + NODE_W:
        return False
    return min(y1, y2) < top + node.h - 0.5 and max(y1, y2) > top + 0.5


def _on_outline(node, top, side, point):
    x, y = point
    if side in ("L", "R"):
        return top < y < top + node.h and abs(x - boundary(node, top, side, y)) < 0.01
    edge = top + node.h if side == "B" else top
    return abs(y - edge) < 0.01 and node.x < x < node.x + NODE_W


def _collinear(first, second):
    (ax1, ay1), (ax2, ay2) = first
    (bx1, by1), (bx2, by2) = second
    if ay1 == ay2 and by1 == by2:
        overlap = min(max(ax1, ax2), max(bx1, bx2)) - max(min(ax1, ax2), min(bx1, bx2))
        return abs(ay1 - by1) < 3 and overlap > 0.5
    if ax1 == ax2 and bx1 == bx2:
        overlap = min(max(ay1, ay2), max(by1, by2)) - max(min(ay1, ay2), min(by1, by2))
        return abs(ax1 - bx1) < 3 and overlap > 0.5
    return False


def verify(lay):
    """Every violated invariant as 'view: kind: detail'; an empty list means the layout is sound."""
    problems = []

    def bad(view, kind, detail):
        problems.append(f"{view}: {kind}: {detail}")

    for name, text in texts(lay):
        if text.rebuilt() != text.source:
            bad("*", "text-changed", name)
        for line in text.lines:
            if not fits(line, text.size, text.limit):
                bad("*", "text-too-wide", f"{name}: {line}")
    for node in lay.nodes:
        if node.text.limit + 2 * TEXT_INSET[node.kind] > NODE_W or node.text.height + 2 * NODE_PAD > node.h:
            bad("*", "node-text-box", node.key)
        if node.x != lay.columns[node.column].node_x:
            bad("*", "node-column", node.key)
    for edge in lay.edges:
        if edge.label and (edge.label_w < edge.label.width + 2 * LABEL_PAD_X
                           or edge.label_h < edge.label.height + 2 * LABEL_PAD_Y):
            bad("*", "label-box", edge.number)
    for index, column in enumerate(lay.columns):
        limit = column.w - 2 * TITLE_INSET
        if column.title.limit > limit or lay.title_top + column.title.height > lay.count_top:
            bad("*", "stage-title", index)
        if column.empty and not fits(EMPTY_TEXT, SMALL[0], limit):
            bad("*", "stage-empty", index)
        for view in lay.views:
            if not fits(count_text(lay, view, index), SMALL[0], limit):
                bad(view.key, "stage-count", index)
    for view in lay.views:
        _verify_view(lay, view, bad)
    return problems


def _verify_view(lay, view, bad):
    key = view.key
    boxes = []
    for node in lay.nodes:
        column, box = lay.columns[node.column], node_box(view, node)
        if (box.x < column.x or box.x + box.w > column.x + column.w
                or box.y < lay.header_h or box.y + box.h > view.height - MARGIN):
            bad(key, "node-outside-stage", node.key)
        boxes.append((f"node {node.key}", box))
    for number, box in sorted(view.labels.items()):
        if (box.x < MARGIN or box.x + box.w > lay.width - MARGIN
                or box.y < lay.header_h or box.y + box.h > view.height - MARGIN):
            bad(key, "label-outside-canvas", number)
        boxes.append((f"label {number}", box))
    if view.separator:
        boxes.append(("separator", view.separator[1]))
    for index, (first, a) in enumerate(boxes):
        for second, b in boxes[index + 1:]:
            if a.overlaps(b, 4):
                bad(key, "overlap", f"{first} / {second}")
    segments = {}
    for edge in lay.edges:
        points = view.routes.get(edge.number) or []
        segments[edge.number] = list(zip(points, points[1:]))
        if len(points) < 2:
            bad(key, "route-missing", edge.number)
            continue
        plan = view.plans[edge.number]
        if any((x1 != x2) == (y1 != y2) for (x1, y1), (x2, y2) in segments[edge.number]):
            bad(key, "route-not-orthogonal", edge.number)
        if not _on_outline(edge.source, view.node_y[edge.source.key], plan.out_side, points[0]):
            bad(key, "start-off-shape", edge.number)
        if not _on_outline(edge.target, view.node_y[edge.target.key], plan.in_side, points[-1]):
            bad(key, "end-off-shape", edge.number)
        (x1, y1), (x2, y2) = points[-2], points[-1]
        if abs(x2 - x1) + abs(y2 - y1) < 12:
            bad(key, "arrow-too-short", edge.number)
        if any(y < lay.header_h or y > view.height - MARGIN or x < 0 or x > lay.width for x, y in points):
            bad(key, "route-outside-canvas", edge.number)
        for a, b in segments[edge.number]:
            for node in lay.nodes:
                if node is edge.source or node is edge.target:
                    if _enters(node, view.node_y[node.key], a, b):
                        bad(key, "route-through-endpoint", f"{edge.number} / node {node.key}")
                elif _hits(node_box(view, node), a, b, 2):
                    bad(key, "route-through-node", f"{edge.number} / node {node.key}")
            if view.separator and _hits(view.separator[1], a, b, 2):
                bad(key, "route-through-caption", edge.number)
    for edge in lay.edges:
        if edge.label is None:
            continue
        box = view.labels.get(edge.number)
        if box is None:
            bad(key, "label-missing", edge.number)
            continue
        carriers = [segment for segment in segments[edge.number] if _carries(segment, box)]
        if not carriers:
            bad(key, "label-detached", edge.number)
        for number, items in segments.items():
            for segment in items:
                if number == edge.number and carriers and segment == carriers[0]:
                    continue
                if _hits(box, *segment, 2):
                    bad(key, "route-through-label", f"{number} / label {edge.number}")
    flat = [(number, segment) for number, items in segments.items() for segment in items]
    for index, (first, a) in enumerate(flat):
        for second, b in flat[index + 1:]:
            if first != second and _collinear(a, b):
                bad(key, "shared-segment", f"{first} / {second}")
    ends = {}
    for edge in lay.edges:
        points = view.routes.get(edge.number) or []
        if points:
            ends.setdefault(edge.source.key, []).append((edge.number, points[0]))
            ends.setdefault(edge.target.key, []).append((edge.number, points[-1]))
    for node, items in ends.items():
        for index, (first, p) in enumerate(items):
            for second, q in items[index + 1:]:
                if math.dist(p, q) < 6:
                    bad(key, "shared-port", f"node {node}: {first} / {second}")
    if key != "all":
        for column in range(len(lay.columns)):
            inside = [view.node_y[n.key] for n in lay.nodes if n.column == column and n.key in view.active_nodes]
            outside = [view.node_y[n.key] for n in lay.nodes if n.column == column and n.key not in view.active_nodes]
            if inside and outside and max(inside) >= min(outside):
                bad(key, "context-above-scenario", column)
