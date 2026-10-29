"""上传时识别描述（SDD 0044 第 2.3 节、0049 第六节）：按文件名、首页文字猜，
规则带版本号。"""

from __future__ import annotations

from datetime import date

from app.domain.recognition import RULES_VERSION, recognize
from app.infrastructure.external.first_page import read_first_page

RESEARCH_PAGE = """证券研究报告 | 公司深度
中信证券研究部
贵州茅台（600519）首次覆盖：高端白酒龙头
投资评级：买入（首次）
分析师：张三 执业证书编号：S1010520010001
分析师：李四
2026年10月27日
核心观点：上调盈利预测，维持高端定位。
"""


def test_research_report_dimensions_come_from_the_first_page():
    found = recognize("茅台深度报告.pdf", RESEARCH_PAGE, "pdf")
    d = found.description
    assert found.rules_version == RULES_VERSION and found.read_first_page
    assert d.status == "unconfirmed"  # 识别出来的一律待确认
    assert d.subject == "600519.SH"
    assert d.doc_type == "research_company"
    assert d.disclosed_on == date(2026, 10, 27)
    assert d.language == "zh"
    assert d.attributes == {
        "institution": "中信证券",
        "analysts": ["张三", "李四"],
        "rating": "买入",
        "forecast": ["initiation", "estimate_change"],
        "market": "cn",
        "depth": "deep",
    }
    assert "attributes.institution" in found.fields and "subject" in found.fields


def test_research_types_by_title():
    assert recognize("宏观周报.pdf", "证券研究报告").description.doc_type == (
        "research_macro"
    )
    weekly = recognize("银行行业周报.pdf", "证券研究报告").description
    assert weekly.doc_type == "research_industry"
    assert weekly.industry == "银行"
    assert weekly.attributes["frequency"] == "weekly"
    # 认得是研报、认不出细类也没有主体：其他报告
    assert recognize("报告.pdf", "研究所 出品").description.doc_type == (
        "research_other"
    )


def test_disclosures_by_title_follow_0049_section_four():
    annual = recognize("贵州茅台：2025年年度报告摘要.pdf").description
    assert annual.doc_type == "annual_report"
    assert (annual.period_start, annual.period_end) == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )
    assert annual.edition == "summary" and annual.disclosed_on is None
    half = recognize("2026年半年度报告.pdf").description
    assert half.doc_type == "semiannual_report"
    assert half.period_end == date(2026, 6, 30)
    # 关于年报的临时公告不是年报
    reply = recognize("关于2025年年度报告问询函的回复公告.pdf").description
    assert reply.doc_type == "interim_announcement"
    assert "supervision" in reply.topics
    letter = recognize("关于对某公司的问询函.pdf").description
    assert letter.doc_type == "regulatory_letter"
    assert recognize("首次公开发行股票招股说明书.pdf").description.doc_type == (
        "prospectus"
    )
    dividend = recognize("2025年年度权益分派实施公告（000001.SZ）.pdf").description
    assert dividend.subject == "000001.SZ"
    assert "dividend" in dividend.topics


def test_topics_are_multi_and_codes_infer_the_exchange():
    found = recognize("关于股东减持及重大资产重组的进展公告(证券代码：688001).pdf")
    assert set(found.description.topics) == {"equity", "restructuring"}
    assert found.description.subject == "688001.SH"
    assert recognize("x（300750）.pdf").description.subject == "300750.SZ"
    assert recognize("腾讯 00700.HK 点评.pdf", "研究报告").description.subject == (
        "00700.HK"
    )


def test_file_name_alone_guesses_little_and_never_the_language():
    found = recognize("revenue.csv", "", "csv")
    assert found.description.doc_type == "other_table"
    assert found.description.language is None
    assert not found.read_first_page
    assert recognize("notes.md", "", "markdown").description.doc_type == "other_text"
    assert recognize("scan.pdf", "", "pdf").description.doc_type is None


def test_the_log_records_the_rules_version_and_values():
    log = recognize("2025年年度报告.pdf").as_log()
    assert log["rules_version"] == RULES_VERSION
    assert log["values"]["doc_type"] == "annual_report"
    assert "doc_type" in log["fields"]


def _pdf(text: str) -> bytes:
    """一页、一行 Helvetica 文字的最小 PDF（xref 偏移现算）。"""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def test_first_page_of_a_pdf_and_of_text():
    pdf = _pdf("Equity Research 600519.SH Rating Buy")
    assert "Equity Research" in read_first_page("application/pdf", pdf)
    assert read_first_page("text/plain; charset=utf-8", "第一行\n第二行".encode()) == (
        "第一行\n第二行"
    )
    # 读不出不报错：识别只是建议
    assert read_first_page("application/pdf", b"%PDF-broken") == ""
    assert read_first_page("application/vnd.ms-excel", b"\xd0\xcf") == ""
