"""Prompts for LLM-based invoice parsing."""

import json

# JSON Schema definition for strict output format
INVOICE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "invoice_number": {
            "type": ["string", "null"],
            "description": "发票号码，通常为8-20位数字"
        },
        "invoice_code": {
            "type": ["string", "null"],
            "description": "发票代码，通常为10-12位数字（如有）"
        },
        "issue_date": {
            "type": ["string", "null"],
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "开票日期，格式必须为YYYY-MM-DD"
        },
        "buyer_name": {
            "type": ["string", "null"],
            "description": "购买方名称（公司全称）"
        },
        "buyer_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "购买方纳税人识别号，15-20位字母数字"
        },
        "seller_name": {
            "type": ["string", "null"],
            "description": "销售方名称（公司全称）"
        },
        "seller_tax_id": {
            "type": ["string", "null"],
            "pattern": "^[A-Z0-9]{15,20}$",
            "description": "销售方纳税人识别号，15-20位字母数字"
        },
        "item_name": {
            "type": ["string", "null"],
            "description": "项目名称/货物名称，如有类别标记(*类别*商品)则保留"
        },
        "total_with_tax": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "价税合计金额，纯数字不含货币符号"
        },
        "amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "金额（不含税），纯数字"
        },
        "tax_amount": {
            "type": ["string", "null"],
            "pattern": "^\\d+(\\.\\d{1,2})?$",
            "description": "税额，纯数字（免税发票返回\"0\"）"
        },
        "tax_rate": {
            "type": ["string", "null"],
            "description": "税率，如\"6%\"、\"13%\"、\"免税\""
        },
        # 明细行：一张发票可含多行货物。上游的 item_name 仍是单值（无明细
        # 时的兜底展示），这里另开 items 数组承载逐行数据。
        "items": {
            "type": ["array", "null"],
            "description": (
                "明细行数组，每行一个对象，顺序与票面从上到下一致。"
                "单行发票也返回长度为 1 的数组；确实读不到明细行才返回 null。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "item_name": {
                        "type": ["string", "null"],
                        "description": "货物或应税劳务、服务名称"
                    },
                    "specification": {
                        "type": ["string", "null"],
                        "description": "规格型号"
                    },
                    "unit": {
                        "type": ["string", "null"],
                        "description": "计量单位，如\"个\"\"吨\"\"服务\""
                    },
                    "quantity": {
                        "type": ["string", "null"],
                        "description": "数量，纯数字"
                    },
                    "unit_price": {
                        "type": ["string", "null"],
                        "description": (
                            "单价，纯数字。票面上发票印 6 位小数"
                            "（如 11247.863248），必须原样保留，不许四舍五入"
                        )
                    },
                    "amount": {
                        "type": ["string", "null"],
                        "description": "该行金额（不含税），纯数字"
                    },
                    "tax_rate": {
                        "type": ["string", "null"],
                        "description": "该行税率，如\"6%\"、\"免税\""
                    },
                    "tax_amount": {
                        "type": ["string", "null"],
                        "description": "该行税额，纯数字"
                    }
                },
                # 明细行允许带额外键：不同票种列数不一，
                # 且落实阶段只取上面 8 个字段，多余的忽略即可。
                "additionalProperties": True
            }
        }
    },
    "required": [
        "invoice_number", "invoice_code", "issue_date",
        "buyer_name", "buyer_tax_id", "seller_name", "seller_tax_id",
        "item_name", "total_with_tax", "amount", "tax_amount", "tax_rate"
    ],
    "additionalProperties": False
}

# Required fields list for validation
REQUIRED_FIELDS = list(INVOICE_JSON_SCHEMA["required"])

# Vision-based system prompt (for direct image analysis)
INVOICE_VISION_SYSTEM_PROMPT = """你是一个专业的中国发票信息提取助手。
你必须严格按照JSON Schema格式返回结果，无法识别的字段返回null。
只返回JSON对象，不要包含任何其他文字、解释或markdown代码块标记。"""

# Build field descriptions for the prompt
_field_descriptions = json.dumps(
    {k: v["description"] for k, v in INVOICE_JSON_SCHEMA["properties"].items()},
    ensure_ascii=False,
    indent=2
)
# 提示词里写死「12 个字段」会在加字段后说谎，改成跟着 required 走
_required_count = len(INVOICE_JSON_SCHEMA["required"])

# Vision-based extraction prompt (for direct image analysis)
INVOICE_VISION_PROMPT = f"""请分析这张中国发票图片，提取发票信息。

## 输出格式要求（必须严格遵守）

返回一个JSON对象，必须包含以下{_required_count}个字段（不能多也不能少）：
{_field_descriptions}

字段类型规则：
- 所有字段值必须是 string 或 null（数值也用字符串表示）
- 日期格式必须是 YYYY-MM-DD
- 金额字段仅包含数字和小数点
- 税率字段格式如 \"6%\"、\"13%\" 或 \"免税\"
- 不能返回空字符串，用 null 表示缺失

## 购买方与销售方识别规则（最重要）

根据标签文字识别，不要依赖位置：
- 购买方 = 标注为"购买方"、"购方"、"购货单位"的区域
- 销售方 = 标注为"销售方"、"销方"、"销货单位"的区域
- 销售方区域通常还有"开票人"、"收款人"、"复核"等字样

中国发票格式说明：
- 标准增值税发票：购买方在左上，销售方在左下
- 电子普通发票：购买方在上部，销售方在底部（价税合计下方）
- 全电发票：购买方和销售方可能左右并排

## 字段提取规则

1. invoice_number: 查找"发票号码"标签后的数字
2. invoice_code: 查找"发票代码"标签后的数字（如无则返回null）
3. issue_date: 查找"开票日期"，转换为YYYY-MM-DD格式
4. buyer_name/seller_name: 查找"名称"标签后的公司全称
5. buyer_tax_id/seller_tax_id: 查找"纳税人识别号"后的15-20位编码
6. item_name: 查找商品行中的项目名称（可能有*分类*前缀）
7. total_with_tax: 查找"价税合计"或"小写"金额，只返回数字
8. amount: 查找"金额"或"合计"行的不含税金额
9. tax_amount: 查找"税额"，免税发票返回"0"
10. tax_rate: 查找"税率"列的值，如"6%"、"免税"

11. items（明细行数组，自上而下对应票面上的每一行货物）:
    - 勾选发票、专票、普通发票通常有多行明细；只有一行时也返回长度为 1 的数组
    - 每行按票面的「货物或应税劳务、服务名称 / 规格型号 / 单位 / 数量 / 单价 / 金额 / 税率 / 税额」逐列填写
    - 左下角"发票联""记账凭证"之类的编号数字不是明细，不要填进来
    - 合计行、价税合计行不算明细行
    - 单价务必照抄票面小数位，票面印几位就写几位（常见 6 位，如 11247.863248），
      不要约简成 11247.86，否则数量会被反推错

## 数据清洗规则

- 金额字段去除¥、￥、$、逗号，只保留数字和小数点
- 日期统一转换为YYYY-MM-DD
- 无法识别的字段返回null，不要猜测

请直接返回JSON对象："""
