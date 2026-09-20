"""Draw the six documents a demo application needs.

Every value is invented. A real receipt carries the buyer's own mailbox and
postal address and a real card statement carries their card number, so none of
those may be built into the product; the layouts here are reproduced, the
contents are not. The images are drawn rather than shipped as files so the
repository stays free of anything that looks like somebody's identity document.

The pixels must survive recognition: the citizen page reads them with the same
engine, so the text is set large, in one column, on a near-white background.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# The practice identity, matching the cards the browser draws in identity.ts.
NAME = "林小竹"
ID_NUMBER = "A123456789"
BIRTH_ROC = "民國90年5月16日"
ADDRESS = "新竹市東區練習路100號"
ACCOUNT = "000-00-000000-0"

CARD = (1800, 1140)
SHEET = (1240, 1750)
INK = (17, 17, 17)
LABEL = (122, 64, 56)
RED = (176, 48, 39)
GREY = (68, 68, 68)
RULE = (230, 185, 180)

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/NotoSansTC-VF.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


class FontMissing(RuntimeError):
    """Raised with the paths tried, so the operator can install or point at one."""


def _font_path() -> str:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise FontMissing(
        "找不到中文字型，請安裝其中一個：" + "、".join(_FONT_CANDIDATES)
    )


_cache: dict[int, ImageFont.FreeTypeFont] = {}


def font(size: int) -> ImageFont.FreeTypeFont:
    if size not in _cache:
        _cache[size] = ImageFont.truetype(_font_path(), size)
    return _cache[size]


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _blank(size: tuple[int, int], tint: tuple[int, int, int] | None = None):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    if tint:
        draw.rectangle((26, 26, size[0] - 26, size[1] - 26), fill=tint)
        draw.rectangle((26, 26, size[0] - 26, size[1] - 26), outline=RED, width=7)
    return image, draw


def _field(draw, x: int, y: int, label: str, value: str) -> tuple[int, int]:
    """Draw `label value` as one run. A wide gutter splits the page into columns
    during recognition and carries the value away from its label."""
    draw.text((x, y), label, font=font(46), fill=LABEL)
    value_x = x + int(draw.textlength(label, font=font(46))) + 34
    draw.text((value_x, y), value, font=font(56), fill=INK)
    return value_x, int(draw.textlength(value, font=font(56)))


def id_front() -> bytes:
    image, draw = _blank(CARD, (253, 244, 244))
    draw.text((150, 105), "中華民國國民身分證", font=font(72), fill=RED)
    draw.rectangle((1300, 250, 1650, 690), fill=(231, 224, 216), outline=(184, 167, 156), width=4)
    draw.text((1330, 410), "樣本", font=font(96), fill=(228, 180, 176))
    _field(draw, 150, 276, "姓名", NAME)
    _, width = _field(draw, 150, 406, "出生年月日", BIRTH_ROC)
    _field(draw, 150 + 330 + width + 60, 406, "性別", "女")
    _field(draw, 150, 536, "發證日期", "民國110年1月2日(竹市)初發")
    _field(draw, 150, 666, "統一編號", ID_NUMBER)
    _footer(draw)
    return _png(image)


def id_back() -> bytes:
    image, draw = _blank(CARD, (253, 244, 244))
    for y in (190, 290, 390, 490, 590, 690, 880):
        draw.line((110, y, CARD[0] - 110, y), fill=RULE, width=3)
    for index, (label, value) in enumerate(
        [("父", "林大竹"), ("母", "吳小梅"), ("配偶", "陳小梅"),
         ("役別", "免役"), ("出生地", "新竹市")]
    ):
        _field(draw, 150, 206 + index * 100, label, value)
    value_x, _ = _field(draw, 150, 706, "住址", "新竹市東區練習路")
    draw.text((value_x, 784), "100號", font=font(56), fill=INK)
    _footer(draw)
    return _png(image)


def _footer(draw) -> None:
    draw.text((150, 946), "練習資料・非正式證件", font=font(46), fill=RED)
    draw.text((150, 1016), "僅用於練習文字辨識，不代表任何人的真實資料。", font=font(38), fill=GREY)


def receipt() -> bytes:
    """A subscription invoice in the shape vendors actually issue."""
    image, draw = _blank(SHEET)

    def rule(y: int) -> None:
        draw.line((90, y, 1150, y), fill=(216, 216, 216), width=2)

    def right(value: str, x: int, y: int, size: int, fill=INK) -> None:
        draw.text((x - draw.textlength(value, font=font(size)), y), value, font=font(size), fill=fill)

    draw.text((90, 78), "Invoice", font=font(62), fill=INK)
    for index, (label, value) in enumerate(
        [("Invoice number", "PRACTICE-0000-0000"),
         ("Date of issue", "September 7, 2026"),
         ("Date due", "September 7, 2026"),
         ("VAT Registration", "Taiwan VAT: 00000000")]
    ):
        draw.text((90, 186 + index * 44), label, font=font(28), fill=(34, 34, 34))
        draw.text((400, 186 + index * 44), value, font=font(28), fill=INK)

    draw.text((90, 424), "Practice Software, PBC", font=font(28), fill=INK)
    for index, line in enumerate(["548 Practice Street", "PMB 00000",
                                  "Practice City, CA 00000", "United States",
                                  "support@example.test"]):
        draw.text((90, 470 + index * 40), line, font=font(28), fill=(51, 51, 51))
    draw.text((640, 424), "Bill to", font=font(28), fill=INK)
    for index, line in enumerate([NAME, ADDRESS, "Taiwan", "practice@example.test"]):
        draw.text((640, 470 + index * 40), line, font=font(28), fill=(51, 51, 51))

    draw.text((90, 752), "TWD 650.00 due September 7, 2026", font=font(44), fill=INK)
    rule(880)
    draw.text((90, 836), "Description", font=font(26), fill=(85, 85, 85))
    right("Qty", 800, 836, 26, (85, 85, 85))
    right("Unit price", 960, 836, 26, (85, 85, 85))
    right("Amount", 1150, 836, 26, (85, 85, 85))
    draw.text((90, 906), "Practice AI Pro", font=font(30), fill=INK)
    draw.text((90, 952), "Sep 7 - Oct 7, 2026", font=font(28), fill=(68, 68, 68))
    right("1", 800, 906, 30)
    right("TWD 650.00", 960, 906, 30)
    right("TWD 650.00", 1150, 906, 30)

    for index, (label, value) in enumerate(
        [("Subtotal", "TWD 650.00"), ("Total excluding tax", "TWD 650.00"),
         ("Total", "TWD 650.00"), ("Amount due", "TWD 650.00")]
    ):
        y = 1070 + index * 56
        rule(y - 20)
        draw.text((640, y), label, font=font(30), fill=(51, 51, 51))
        right(value, 1150, y, 30)
    rule(1330)
    draw.text((90, 1390), "Plan: Monthly subscription", font=font(30), fill=INK)
    draw.text((90, 1440), "Payment method: Practice card", font=font(30), fill=INK)
    rule(1560)
    draw.text((90, 1598), "練習收據・非正式憑證", font=font(34), fill=RED)
    draw.text((90, 1658), "For text recognition practice only. Not an invoice.", font=font(27), fill=GREY)
    draw.text((90, 1700), "不能作為付款、金額或申請資格的證明。", font=font(27), fill=GREY)
    return _png(image)


def _document(title: str, subtitle: str, rows: list[tuple[str, str]], notes: list[str]) -> bytes:
    image, draw = _blank(SHEET)
    draw.rectangle((0, 0, SHEET[0], 150), fill=(22, 62, 51))
    draw.text((90, 40), title, font=font(50), fill=(255, 255, 255))
    draw.text((90, 106), subtitle, font=font(26), fill=(205, 222, 214))
    for index, (label, value) in enumerate(rows):
        y = 250 + index * 96
        draw.text((90, y), label, font=font(30), fill=(90, 90, 90))
        draw.text((90, y + 44), value, font=font(38), fill=INK)
        draw.line((90, y + 92, 1150, y + 92), fill=(224, 224, 224), width=2)
    for index, note in enumerate(notes):
        draw.text((90, 250 + len(rows) * 96 + 60 + index * 48), note, font=font(28), fill=GREY)
    draw.text((90, 1630), "練習文件・非正式憑證", font=font(34), fill=RED)
    draw.text((90, 1690), "不能作為付款、帳戶歸屬或申請資格的證明。", font=font(27), fill=GREY)
    return _png(image)


def payment_proof() -> bytes:
    return _document(
        "PRACTICE CARD STATEMENT", "練習信用卡帳單・非正式憑證",
        [("持卡人 Cardholder", NAME),
         ("卡號 Card number", "**** **** **** 0000"),
         ("交易日期 Transaction date", "2026-09-07"),
         ("商店名稱 Merchant", "PRACTICE SOFTWARE PBC"),
         ("交易金額 Amount", "TWD 650.00"),
         ("授權碼 Authorisation", "000000")],
        ["本頁僅列出與本次申請有關的一筆交易。",
         "其餘交易與餘額不在此頁，承辦仍須核對原始帳單。"],
    )


def bank_account() -> bytes:
    return _document(
        "PRACTICE PASSBOOK", "練習存摺封面・非正式憑證",
        [("戶名 Account name", NAME),
         ("銀行 Bank", "練習商業銀行 練習分行"),
         ("帳號 Account number", ACCOUNT),
         ("開戶日期 Opened", "2020-01-02")],
        ["戶名須與申請人姓名相同，帳號正確性與實際帳戶歸屬仍由承辦確認。"],
    )


def affidavit() -> bytes:
    return _document(
        "PRACTICE AFFIDAVIT", "練習切結書・非正式文件",
        [("申請人 Applicant", NAME),
         ("身分證字號 ID number", ID_NUMBER),
         ("戶籍地址 Registered address", ADDRESS),
         ("簽署日期 Signed", "2026-09-07")],
        ["茲切結本次申請所附文件均為本人所有，且未就同一項目重複受領補助。",
         "本頁為練習用文件，簽名欄為列印文字，非親筆簽名。"],
    )


BUILDERS = {
    "ID_FRONT": id_front,
    "ID_BACK": id_back,
    "RECEIPT": receipt,
    "PAYMENT_PROOF": payment_proof,
    "BANK_ACCOUNT": bank_account,
    "AFFIDAVIT": affidavit,
}


def build(document_type: str) -> bytes:
    return BUILDERS[document_type]()


if __name__ == "__main__":  # Preview them without touching a deployment.
    out = Path("demo-documents")
    out.mkdir(exist_ok=True)
    for name, builder in BUILDERS.items():
        target = out / f"{name.lower()}.png"
        target.write_bytes(builder())
        print(target)
