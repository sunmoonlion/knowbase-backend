"""真实文件表轴投影：关系、共享字符串、稀疏列、公式与坏输入边界。"""

import io
import zipfile

import pytest

from app.domain.table_format import MAIN, MAX_XML, table_formats
from app.domain.user_data import UserDataError

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook(*, target="worksheets/custom.xml", sheet=None, shared=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="' + MAIN[1:-1] + '" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="A &amp; B" r:id="r7"/></sheets></workbook>',
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="r7" Target="'
            + target
            + '"/></Relationships>',
        )
        z.writestr(
            "xl/worksheets/custom.xml",
            sheet
            or '<worksheet xmlns="'
            + MAIN[1:-1]
            + '"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>科目</t></is></c>'
            '<c r="C1" t="n"><v>2024</v></c></row>'
            '<row r="2"><c r="A2" t="s"><v>0</v></c>'
            '<c r="C2"><v>987654321</v></c></row>'
            '<row r="3"><c r="A3"><f>SECRET()</f><v>77</v></c></row>'
            '<row r="4"><c r="A4"><v>1234</v></c></row>'
            "</sheetData></worksheet>",
        )
        z.writestr(
            "xl/sharedStrings.xml",
            shared or '<sst xmlns="' + MAIN[1:-1] + '"><si><t>收入</t></si></sst>',
        )
    return buffer.getvalue()


def test_csv_axes_keep_quoted_labels_but_not_values():
    (shape,) = table_formats(
        "text/csv; charset=utf-8",
        '\ufeff项目,"2023,元",2024\n收入,9001,9002\n123,888,999\n'.encode(),
    )
    assert shape.columns == ("项目", "2023,元", "2024")
    assert shape.rows == ("收入",)
    assert "9001" not in repr(shape)


def test_xlsx_uses_relationships_and_decodes_labels_without_data_or_formulas():
    (shape,) = table_formats(XLSX, workbook())
    assert shape.name == "A & B"
    assert shape.columns == ("科目", "", "2024")
    assert shape.rows == ("收入",)
    assert "987654321" not in repr(shape)
    assert "SECRET" not in repr(shape)


@pytest.mark.parametrize(
    "content",
    [
        b"PK\x03\x04broken",
        workbook(target="../../outside.xml"),
        workbook(shared="x" * (MAX_XML + 1)),
        workbook(shared='<!DOCTYPE a [<!ENTITY x "payload">]><a>&x;</a>'),
    ],
)
def test_malformed_or_unbounded_workbooks_are_explicitly_refused(content):
    with pytest.raises(UserDataError, match="format") as exc:
        table_formats(XLSX, content)
    assert exc.value.code == "format_unreadable"


@pytest.mark.parametrize(
    "media",
    ["application/pdf", "application/vnd.ms-excel", "text/plain", "text/markdown"],
)
def test_non_tables_never_mislabel_body_as_header(media):
    assert table_formats(media, b"confidential first paragraph") == ()


def test_projection_is_bounded_by_incubation_contract():
    content = (",".join(["a" * 250] * 201) + "\n" + "item,999\n" * 600).encode()
    (shape,) = table_formats("text/csv", content)
    assert len(shape.columns) == 200 and len(shape.rows) == 500
    assert len(shape.columns[0]) == 200


def test_csv_in_gb18030_is_read_like_utf8():
    # 账 186：国内常见导出是 GBK/GB18030，读不出就整次挑数据失败
    (shape,) = table_formats(
        "text/csv", "项目,2023年,2024年\n营业收入,1,2\n".encode("gb18030")
    )
    assert shape.columns == ("项目", "2023年", "2024年")
    assert shape.rows == ("营业收入",)


def test_xlsx_cells_without_a_reference_take_the_next_column():
    # OOXML 允许省略 r：按出现顺序是下一列
    sheet = (
        '<worksheet xmlns="' + MAIN[1:-1] + '"><sheetData>'
        '<row><c t="inlineStr"><is><t>科目</t></is></c>'
        '<c t="inlineStr"><is><t>2023</t></is></c>'
        '<c r="D1" t="inlineStr"><is><t>2025</t></is></c>'
        '<c t="inlineStr"><is><t>2026</t></is></c></row>'
        '<row><c t="inlineStr"><is><t>收入</t></is></c><c><v>5</v></c></row>'
        "</sheetData></worksheet>"
    )
    (shape,) = table_formats(XLSX, workbook(sheet=sheet))
    assert shape.columns == ("科目", "2023", "", "2025", "2026")
    assert shape.rows == ("收入",)


def test_xlsx_with_an_invalid_reference_is_still_refused():
    sheet = (
        '<worksheet xmlns="' + MAIN[1:-1] + '"><sheetData>'
        '<row><c r="1A"><v>1</v></c></row></sheetData></worksheet>'
    )
    with pytest.raises(UserDataError, match="format"):
        table_formats(XLSX, workbook(sheet=sheet))
