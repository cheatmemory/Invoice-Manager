"""合计行三元组（金额 / 税额 / 税率）的版面抽取与仲裁。

回归的是「多行明细发票抽不出金额、税额、税率」那个缺陷。它的成因是
_extract_fields_layout 拿「货物或应税劳务、服务名称」那一格的 min_y 当表头
带下界，而同一视觉行的其他表头格（规格/单位/数量/单价/金/额/税率/税/额）
在多行票上基线偏高、min_y 更小，被整段挡在池子外面，_column_span 找不到
任何表头盒子，三列一起失效。

下面的坐标照搬 vat_0058 的版面：表头带最高的是「税率」(min_y=465)，而
「货物…名称」已经是 483，差 18px。
"""
from app.services.ocr_service import FieldExtractor

WIDTH = 2006.0
HEIGHT = 1166.0


def box(text: str, min_x: float, min_y: float,
        width: float, height: float = 40.0) -> dict:
    return {
        "text": text,
        "min_x": min_x,
        "max_x": min_x + width,
        "min_y": min_y,
        "max_y": min_y + height,
        "center_x": min_x + width / 2,
        "center_y": min_y + height / 2,
    }


# 表头带：规格型号 / 单位 / 数量 / 单价 / 金 / 额 / 税率 / 税 / 额
# 注意「税率」的 min_y 比「货物…名称」小 18px —— 这正是缺陷的触发条件。
_HEADER = [
    # 这一格决定 table_top（_extract_fields_layout 用它当表头带的起点）
    box("货物或应税劳务、服务名称", 247, 483, 373),
    box("规格型号", 711, 480, 138),
    box("单位", 916, 478, 66),
    box("数量", 1042, 474, 93),
    box("单价", 1222, 472, 91),
    box("金", 1430, 474, 32),
    box("额", 1504, 470, 43),
    box("税率", 1625, 465, 71),
    box("税", 1774, 467, 40),
    box("额", 1850, 465, 33),
]

# 明细行（金额列 / 税率列 / 税额列各取一个代表值）
_DETAIL = [
    box("17%", 1644, 518, 50),
    box("11247.86", 1497, 520, 115),
    box("1912.14", 1848, 518, 98),
    box("5606.84", 1510, 557, 104),
    box("953.16", 1857, 553, 89),
    box("1130.77", 1510, 589, 103),
    box("192.23", 1861, 588, 83),
]

# 合计行
_TOTAL = [
    box("合", 336, 836, 30),
    box("计", 505, 830, 39),
    box("¥17985.47", 1421, 805, 200),
    box("¥3057.53", 1781, 801, 174),
    box("¥21043.00", 1625, 862, 208),
    box("（小写）", 1493, 865, 99),
    box("价税合计（大写）", 305, 889, 263),
]

_LINES = _HEADER + _DETAIL + _TOTAL
_TEXT = "货物或应税劳务、服务名称\n17%\n11247.86\n¥17985.47\n¥3057.53\n价税合计（大写）\n发票"


def test_layout_reads_all_three_columns():
    """表头格基线高于「货物…名称」时，三列仍要读得出来（vat_0058）。"""
    layout = FieldExtractor()._extract_fields_layout(_TEXT, list(_LINES))
    assert layout["amount"] == "17985.47"
    assert layout["tax_amount"] == "3057.53"
    assert layout["tax_rate"] == "17"


def test_extract_fields_end_to_end():
    """走完整生产路径，金额 + 税额 = 价税合计。"""
    fields = FieldExtractor().extract_fields(_TEXT, [dict(b) for b in _LINES])
    assert float(fields["amount"]) + float(fields["tax_amount"]) == (
        round(float(fields["total_with_tax"]), 2)
    )
    # 抽取出「17」，出口必须补成票面写法「17%」，否则与 LLM 的「17%」比对不上
    assert fields["tax_rate"] == "17%"


def test_regex_path_tax_rate_gets_percent():
    """正则路径（没有版面坐标）抽到的税率同样要带百分号。

    PATTERNS_CN 的 tax_rate 写成 ``(\\d+%?|免税)``，百分号可选，所以 OCR 把
    「11%」读成「11」时它照样匹配、照样返回不带 % 的值。
    """
    fields = FieldExtractor().extract_fields("税率：11\n税额：1.00", None)
    assert fields["tax_rate"] == "11%"


def test_derived_tax_rate_gets_percent():
    """税率格读不出内容、由 税额/金额 反推出来时，也要带上百分号。

    反推分支内部写的是不带 % 的整数，补 % 由 extract_fields 的出口统一完成。
    """
    # 与上面的版面相同，只把税率格（"17%"）抽走，逼出反推分支
    lines = [b for b in _HEADER + _DETAIL + _TOTAL if b["text"] != "17%"]
    fields = FieldExtractor().extract_fields(_TEXT, [dict(b) for b in lines])
    assert fields["tax_rate"] == "17%"


def test_reconcile_prefers_layout_when_regex_is_wrong():
    """合计行的数字次序被 OCR 打乱时，以自洽的版面值为准（vat_0059）。"""
    fields = {"amount": "290.45", "tax_amount": "290.45",
              "total_with_tax": "1999.00", "tax_rate": None}
    layout = {"amount": "1708.55", "tax_amount": "290.45",
              "total_with_tax": "1999.00"}
    FieldExtractor._reconcile_totals(fields, layout)
    assert fields["amount"] == "1708.55"
    assert fields["tax_amount"] == "290.45"


def test_reconcile_keeps_value_when_layout_inconsistent():
    """版面自己凑不齐时不做越权修改，保留原有结果。"""
    fields = {"amount": "100.00", "tax_amount": "5.00",
              "total_with_tax": "120.00"}
    layout = {"amount": "90.00", "tax_amount": "5.00",
              "total_with_tax": "120.00"}
    FieldExtractor._reconcile_totals(fields, layout)
    assert fields["amount"] == "100.00"


def test_tax_rate_derived_when_cell_unreadable():
    """税率格读成噪声时，用 税额/金额 反推法定税率。"""
    fields = {"amount": "1708.55", "tax_amount": "290.45",
              "total_with_tax": "1999.00", "tax_rate": None}
    layout = {"amount": "1708.55", "tax_amount": "290.45",
              "total_with_tax": "1999.00"}
    FieldExtractor._reconcile_totals(fields, layout)
    assert fields["tax_rate"] == "17"


def test_no_tax_rate_guessed_for_off_beat_ratio():
    """比例对不上任何法定税率时留空，不编数字。"""
    fields = {"amount": "1000.00", "tax_amount": "166.60",
              "total_with_tax": "1166.60", "tax_rate": None}
    layout = {"amount": "1000.00", "tax_amount": "166.60",
              "total_with_tax": "1166.60"}
    FieldExtractor._reconcile_totals(fields, layout)
    assert fields["tax_rate"] is None
