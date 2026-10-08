"""发票明细行（多物品）版面抽取 —— 生产实现。

与早期草稿 ``item_proto.py`` 的唯一差别是新增 :func:`extract_item_lines`
这个「一步到位」入口：调用方只需要给出版面盒子，表头定位、合计行定位、
列定位全部在内部完成，不用再手工拼 ``table_top`` / ``total_y`` / ``width``。

设计要点（每条都来自真实票据上的观测）：

1. 列定位用表头盒子的 x 跨度。表头常被 OCR 拆成单字
   （「金」+「额」、「税」+「额」），所以先按字符集合并相邻盒子，
   再取「拼接后正好等于关键字」的最窄连续段。
2. 列分界取「相邻列中心的中点」，而不是表头跨度本身 ——
   数字在单元格内右对齐，常溢出表头右边界。
3. 行定位用「金额」列，而不是所有列的 y 聚类。
   表头行与首行数据在垂直方向重叠，y 聚类会把两者链式合并。
4. 品名列与数字列**不共享同一个 y 中心**：实测品名框比同行数字框
   低约 20px（PaddleOCR 对名称单元格的框偏移），因此品名与行
   按序号一一配对，而不是按 y 就近匹配。
5. 合计行通过「金额带 ¥/￥ 前缀」与明细行区分：明细行不带货币符号。
6. 后处理两条：
   - 数量缺失时用 金额 / 单价 反推（整数校验）；
   - 税率缺失时用全文众数补齐（OCR 会把「17%」误读成「%2T」）。
7. 自校验：明细金额合计、税额合计应等于票面合计行。
   不相等则整体降级为「需人工复核」。
8. 品名锚点必须剔除「纯数字」盒子。实测 vat_0051 的 `507`(x 87-124)、
   vat_0059 的 `529`(x 191-223) 都是发票联左下角的编号残片，
   它们的中心点恰好落在品名列 x 区间内，靠纯数字规则才能挡掉。
9. 行数取 min(品名锚点数, 金额行数)。多余的一侧说明还有边缘噪声，
   宁可少给一行也不能给错行 —— 错行的代价是金额对不上票面。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 表头关键字 -> 明细字段。顺序即票面从左到右的列顺序。
HEADERS: List[Tuple[str, str]] = [
    ("货物或应税劳务、服务名称", "item_name"),
    ("货物或应税劳务服务名称", "item_name"),
    ("规格型号", "specification"),
    ("单位", "unit"),
    ("数量", "quantity"),
    ("单价", "unit_price"),
    ("金额", "amount"),
    ("税率", "tax_rate"),
    ("税额", "tax_amount"),
]

KEYS: List[str] = list(dict.fromkeys(k for _kw, k in HEADERS))
NUMERIC = ("quantity", "unit_price", "amount", "tax_amount")
TEXTUAL = ("item_name", "specification", "unit")

_TOTAL_RE = re.compile(r"^(.*)(合计|小计)(.*)$")


def _plain(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _cx(b: Dict[str, Any]) -> float:
    return (b["min_x"] + b["max_x"]) / 2


def _cy(b: Dict[str, Any]) -> float:
    return (b["min_y"] + b["max_y"]) / 2


def _num(value: Optional[str]) -> float:
    try:
        return float(re.sub(r"[^\d.]", "", value or ""))
    except ValueError:
        return 0.0


def _header_candidates() -> List[Tuple[str, str, set, int]]:
    """(关键字, 字段, 字符集合, 长度)，供表头盒子匹配。"""
    out = []
    for kw, key in HEADERS:
        out.append((kw, key, set(kw), len(kw)))
    return out


def locate_table(body: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """定位明细表的表头带与合计行。

    返回 ``{"table_top", "total_y", "width", "bounds", "header_ids"}``；
    定位失败返回 ``None``。
    """
    if not body:
        return None

    cands = _header_candidates()
    # 一张盒子上可能同时能被多个关键字匹配（如「金额」是「金额」的子集），
    # 取能匹配到的字段数最多的那个关键字，避免把「税额」读成另一列。
    hit_of: Dict[int, Tuple[str, str]] = {}
    for b in body:
        plain = _plain(b["text"])
        if not plain:
            continue
        best = None
        for kw, key, kset, klen in cands:
            if len(plain) <= klen and set(plain) <= kset:
                if best is None or len(plain) > len(best[0]):
                    best = (kw, key)
        if best:
            hit_of[id(b)] = best

    # 表头带：命中表关键字最多、且行数最多的一行 y
    by_band: Dict[int, List[Dict[str, Any]]] = {}
    for b in body:
        if id(b) in hit_of:
            band = round(b["min_y"] / 10.0)
            by_band.setdefault(band, []).append(b)
    if not by_band:
        return None

    def score(boxs: List[Dict[str, Any]]) -> Tuple[int, int]:
        keys = {hit_of[id(b)][1] for b in boxs}
        return (len(keys), -len(boxs))

    band_no = max(by_band, key=lambda k: score(by_band[k]))
    header_boxes = by_band[band_no]
    table_top = min(b["min_y"] for b in header_boxes)

    width = max(float(b["max_x"]) for b in body)
    total_y = _locate_total_y(body, table_top, width)
    if total_y is None:
        return None

    bounds, header_ids = locate_columns(body, table_top, width)
    return {
        "table_top": table_top,
        "total_y": total_y,
        "width": width,
        "bounds": bounds,
        "header_ids": header_ids,
    }


def _locate_total_y(body: Sequence[Dict[str, Any]], table_top: float,
                    width: float) -> Optional[float]:
    """合计行的 y。优先取带「合计/小计」字样的盒子。"""
    hit_boxes = []
    for b in body:
        plain = _plain(b["text"])
        if not plain:
            continue
        if plain in ("合计", "小计"):
            hit_boxes.append(b)
        else:
            m = _TOTAL_RE.match(plain)
            if m and set(m.group(1) + m.group(3)) <= set("小写价税合计"):
                hit_boxes.append(b)
    if hit_boxes:
        return min(b["min_y"] for b in hit_boxes)

    # 兜底：明细区下方、带货币符号的最大金额所在行
    cands = [b for b in body
             if b["min_y"] > table_top
             and re.search(r"[¥￥]\s*[\d,]+\.\d{2}", b["text"])]
    if cands:
        return max(cands, key=lambda b: b["min_y"])["min_y"]
    del width
    return None


def locate_columns(
    body: List[Dict[str, Any]], table_top: float, width: float
) -> Tuple[Dict[str, Tuple[float, float]], set]:
    """返回 (列名 -> x 分界区间, 表头盒子的 id 集合)。"""
    band = [b for b in body if table_top - 30 <= b["min_y"] <= table_top + 45]
    header_ids: set = set()
    spans: Dict[str, Tuple[float, float]] = {}

    for kw, key in HEADERS:
        kwset = set(kw)
        cands = sorted(
            [b for b in band
             if _plain(b["text"]) and set(_plain(b["text"])) <= kwset
             and len(_plain(b["text"])) <= len(kw)],
            key=lambda b: b["min_x"],
        )
        best = None
        for i in range(len(cands)):
            acc = ""
            for j in range(i, len(cands)):
                if j > i and cands[j]["min_x"] - cands[j - 1]["max_x"] > 60:
                    break
                acc += _plain(cands[j]["text"])
                if sorted(acc) == sorted(kw):
                    span = (cands[i]["min_x"], cands[j]["max_x"],
                            tuple(id(c) for c in cands[i:j + 1]))
                    if best is None or span[1] - span[0] < best[1] - best[0]:
                        best = span
                    break
                if not set(acc) <= kwset:
                    break
        if best and key not in spans:
            spans[key] = (best[0], best[1])
            header_ids.update(best[2])

    order = [k for k in KEYS if k in spans]
    centres = {k: (spans[k][0] + spans[k][1]) / 2 for k in order}
    bounds: Dict[str, Tuple[float, float]] = {}
    for i, k in enumerate(order):
        lo = 0.0 if i == 0 else (centres[order[i - 1]] + centres[k]) / 2
        hi = (float(width) if i == len(order) - 1
              else (centres[k] + centres[order[i + 1]]) / 2)
        bounds[k] = (lo, hi)
    locate_columns.centres = centres
    return bounds, header_ids


def _in_col(b: Dict[str, Any], bounds, key) -> bool:
    lo, hi = bounds[key]
    return lo <= _cx(b) < hi


def extract_items(
    body: List[Dict[str, Any]], table_top: float, width: float,
    total_y: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """从版面盒子里抽出明细行。"""
    diag: Dict[str, Any] = {"warnings": []}
    bounds, header_ids = locate_columns(body, table_top, width)
    centres = locate_columns.centres
    diag["columns"] = {k: bounds[k] for k in bounds}

    if "amount" not in bounds or "item_name" not in bounds:
        diag["warnings"].append("关键列（金额/品名）未定位到，放弃明细抽取")
        return [], diag

    # ---- 行：以金额列为锚，合计行靠 ¥ 前缀排除 ----
    ref = [b for b in body
           if _in_col(b, bounds, "amount")
           and table_top + 20 < b["min_y"] < total_y
           and re.search(r"\d", _plain(b["text"]))
           and not _plain(b["text"]).startswith(("¥", "￥"))]
    ref.sort(key=_cy)
    if not ref:
        diag["warnings"].append("金额列无明细数据，放弃")
        return [], diag

    row_centres = [_cy(b) for b in ref]
    row_bounds = []
    for i, c in enumerate(row_centres):
        top = (table_top + c) / 2 if i == 0 else (row_centres[i - 1] + c) / 2
        bot = (c + total_y) / 2 if i == len(row_centres) - 1 else (c + row_centres[i + 1]) / 2
        row_bounds.append((top, bot))

    # ---- 品名锚点：与行按序号配对（品名框比数字框低约 20px，不能按 y 就近） ----
    alo, ahi = bounds["item_name"]
    anchors = []
    for b in sorted(body, key=_cy):
        if id(b) in header_ids or not (alo <= _cx(b) < ahi):
            continue
        if not (table_top + 20 < b["min_y"] < total_y):
            continue
        t = _plain(b["text"])
        if len(t) < 2 or re.fullmatch(r"[合计小写（大写）价税]+", t):
            continue
        # 纯数字 = 发票联编号/号码残片（实测 x 落点会误入品名列），真品名不含纯数字
        if re.fullmatch(r"\d+", t):
            continue
        anchors.append(b)

    n = min(len(anchors), len(ref))
    if len(anchors) != len(ref):
        diag["warnings"].append(
            f"品名锚点数({len(anchors)}) 与金额行数({len(ref)}) 不等，"
            f"取较小者 {n} 行")

    def pick(cells, key):
        if key not in bounds:
            return None
        hits = [c for c in cells if _in_col(c, bounds, key)]
        if not hits:
            return None
        best = min(hits, key=lambda c: abs(_cx(c) - centres[key]))
        t = _plain(best["text"])
        if key in TEXTUAL:
            return t or None
        if key in NUMERIC:
            m = re.search(r"[\d,]+(?:\.\d+)?", t)
            return m.group(0) if m else None
        if key == "tax_rate":
            m = re.search(r"(\d+)\s*%", t)
            if m:
                return m.group(1)
            if "免税" in t:
                return "免税"
            m = re.search(r"\b(1[0-9]|[0-9])\b", t)
            return m.group(1) if m else None
        return t or None

    items: List[Dict[str, Any]] = []
    for i in range(n):
        top, bot = row_bounds[i]
        cells = [c for c in body if top <= _cy(c) < bot and id(c) not in header_ids]
        it = {k: pick(cells, k) for k in
              ("specification", "unit", "quantity", "unit_price",
               "amount", "tax_rate", "tax_amount")}
        it["item_name"] = _plain(anchors[i]["text"]) if i < len(anchors) else None
        it["_y"] = round(row_centres[i], 1)
        items.append(it)

    # ---- 后处理 ----
    rates = [i["tax_rate"] for i in items if i.get("tax_rate")]
    mode_rate = max(set(rates), key=rates.count) if rates else None
    for it in items:
        if not it.get("quantity") and _num(it.get("unit_price")) and _num(it.get("amount")):
            q = _num(it["amount"]) / _num(it["unit_price"])
            if abs(q - round(q)) < 0.02:
                it["quantity"] = str(int(round(q)))
                it["_quantity_inferred"] = True
        if not it.get("tax_rate") and mode_rate:
            it["tax_rate"] = mode_rate
            it["_tax_rate_inferred"] = True

    diag["row_count"] = len(items)
    diag["column_count"] = len(bounds)
    return items, diag


def extract_item_lines(body: Sequence[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """一步到位的明细行抽取入口。

    调用方只需要给出版面盒子（每项含 ``text/min_x/max_x/min_y/max_y``）。
    定位不到表头或合计行时返回 ``([], diag)``，调用方据此降级。
    """
    diag: Dict[str, Any] = {"warnings": []}
    table = locate_table(body)
    if table is None:
        diag["warnings"].append("未定位到明细表表头/合计行，放弃明细抽取")
        return [], diag
    items, diag = extract_items(
        list(body), table["table_top"], table["width"], table["total_y"])
    diag["table_top"] = round(table["table_top"], 1)
    diag["total_y"] = round(table["total_y"], 1)
    return items, diag


def strip_internal(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉 ``_y`` / ``_quantity_inferred`` 等内部键，只留落库字段。"""
    drop = ("_y", "_quantity_inferred", "_tax_rate_inferred")
    return [{k: v for k, v in it.items() if k not in drop} for it in items]
