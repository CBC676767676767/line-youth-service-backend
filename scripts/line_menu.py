"""Render and validate the local LINE simulator's six-area menu assets, entirely offline."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
MENU_PATH = ROOT / "app/data/line-rich-menu.json"
IMAGE_PATH = ROOT / "app/data/line-rich-menu.png"
ACTIONS = dict(zip(
    ("start", "rules", "documents", "tracking", "safety", "menu"),
    ("補助預檢", "補助規則", "備件清單", "申請進度", "安全提醒", "服務選單"), strict=True,
))
IMAGE_ALT = "青年補助服務六格選單：補助預檢、補助規則、備件清單、申請進度、安全提醒、服務選單。"
INTEGER = {"type": "integer", "minimum": 0}
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["size", "selected", "name", "chatBarText", "areas"],
    "properties": {
        "size": {"type": "object", "additionalProperties": False, "required": ["width", "height"],
                 "properties": {"width": {"const": 2500}, "height": {"enum": [843, 1686]}}},
        "selected": {"type": "boolean"},
        "name": {"type": "string", "pattern": r"^youth-service-v[1-9][0-9]*$", "maxLength": 80},
        "chatBarText": {"type": "string", "minLength": 1, "maxLength": 14},
        "areas": {"type": "array", "minItems": 6, "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["bounds", "action"],
            "properties": {
                "bounds": {"type": "object", "additionalProperties": False,
                           "required": ["x", "y", "width", "height"],
                           "properties": {"x": INTEGER, "y": INTEGER,
                                          "width": {"type": "integer", "minimum": 1},
                                          "height": {"type": "integer", "minimum": 1}}},
                "action": {"type": "object", "additionalProperties": False,
                           "required": ["type", "label", "data"],
                           "properties": {"type": {"const": "postback"},
                                          "label": {"type": "string", "maxLength": 20},
                                          "data": {"enum": ["youth:" + key for key in ACTIONS]}}},
            },
        }},
    },
}


class MenuValidationError(Exception):
    """Stable asset diagnostics, without echoing untrusted file contents."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def validate(menu_path=MENU_PATH, image_path=IMAGE_PATH):
    try:
        payload = json.loads(menu_path.read_text(encoding="utf-8"))
        if list(Draft202012Validator(SCHEMA).iter_errors(payload)):
            raise MenuValidationError("Menu does not satisfy the six-area versioned schema.")
        width, height = payload["size"]["width"], payload["size"]["height"]
        rectangles, seen, covered = [], set(), 0
        for area in payload["areas"]:
            bounds, action = area["bounds"], area["action"]
            x, y, w, h = (bounds[key] for key in ("x", "y", "width", "height"))
            key = action["data"].removeprefix("youth:")
            if key in seen or action["label"] != ACTIONS[key]:
                raise MenuValidationError("Menu must contain each labelled service action exactly once.")
            seen.add(key)
            if x + w > width or y + h > height:
                raise MenuValidationError("Menu area extends beyond the image.")
            if any(x < a + c and a < x + w and y < b + d and b < y + h for a, b, c, d in rectangles):
                raise MenuValidationError("Menu areas overlap.")
            rectangles.append((x, y, w, h))
            covered += w * h
        if covered != width * height:
            raise MenuValidationError("Menu areas must cover the full image without gaps.")
        image_data = image_path.read_bytes()
        if len(image_data) > 1_000_000:
            raise MenuValidationError("Menu PNG exceeds 1 MB.")
        with Image.open(io.BytesIO(image_data)) as image:
            if image.format != "PNG" or image.size != (width, height) or image.mode not in ("RGB", "RGBA"):
                raise MenuValidationError("Menu PNG format or dimensions do not match the payload.")
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        raise MenuValidationError("Could not read valid menu JSON and PNG assets.") from None
    digest = hashlib.sha256(canonical(payload) + b"\0" + image_data).hexdigest()
    return payload, image_data, digest


def render_image(font_path, output=IMAGE_PATH):
    """Generate original typography/geometry artwork; no font is redistributed."""
    cream, green, muted, line = "#F8F5E9", "#214B3C", "#5D7166", "#D8DFCE"
    image = Image.new("RGB", (2500, 1686), cream)
    draw = ImageDraw.Draw(image)
    title = ImageFont.truetype(str(font_path), 108)
    body = ImageFont.truetype(str(font_path), 49)
    number = ImageFont.truetype(str(font_path), 62)
    descriptions = ["先看適合哪些補助", "核對條件與申請期限", "整理送件所需資料",
                    "查看案件與下一步", "保護帳號及個人資料", "回到青年服務入口"]
    for index, label in enumerate(ACTIONS.values()):
        column, row = index % 3, index // 3
        x, y = (0, 833, 1666)[column], row * 843
        right = (833, 1666, 2500)[column]
        background = green if index == 0 else ("#EEF2E7" if index in (2, 4) else cream)
        foreground = cream if index == 0 else green
        secondary = "#D5E2CF" if index == 0 else muted
        draw.rectangle((x, y, right, y + 843), fill=background)
        draw.text((x + 66, y + 61), f"0{index + 1}", font=number, fill=secondary)
        # Each tile has a recognizable geometric symbol with a consistent heavy stroke.
        cx, cy = x + 415, y + 281
        if index == 0:
            draw.rounded_rectangle((cx - 95, cy - 100, cx + 95, cy + 115), radius=18,
                                   outline=foreground, width=13)
            draw.line([(cx - 50, cy + 8), (cx - 12, cy + 48), (cx + 59, cy - 35)],
                      fill=foreground, width=17)
        elif index == 1:
            draw.rectangle((cx - 106, cy - 91, cx + 106, cy + 111), outline=foreground, width=12)
            draw.line((cx, cy - 91, cx, cy + 111), fill=foreground, width=12)
            for shift in (-43, 8, 59):
                draw.line((cx - 77, cy + shift, cx - 26, cy + shift), fill=foreground, width=9)
                draw.line((cx + 27, cy + shift, cx + 78, cy + shift), fill=foreground, width=9)
        elif index == 2:
            draw.rounded_rectangle((cx - 102, cy - 89, cx + 102, cy + 115), radius=15,
                                   outline=foreground, width=12)
            for shift in (-43, 13, 69):
                draw.line([(cx - 69, cy + shift), (cx - 54, cy + shift + 15),
                           (cx - 30, cy + shift - 13)], fill=foreground, width=10)
                draw.line((cx - 5, cy + shift, cx + 65, cy + shift), fill=foreground, width=10)
        elif index == 3:
            draw.ellipse((cx - 105, cy - 99, cx + 105, cy + 111), outline=foreground, width=13)
            draw.line([(cx, cy - 61), (cx, cy + 7), (cx + 53, cy + 41)], fill=foreground, width=13)
        elif index == 4:
            draw.line([(cx, cy - 110), (cx + 106, cy - 66), (cx + 92, cy + 52),
                       (cx, cy + 129), (cx - 92, cy + 52), (cx - 106, cy - 66),
                       (cx, cy - 110)], fill=foreground, width=13, joint="curve")
            draw.line([(cx - 43, cy + 2), (cx - 9, cy + 36), (cx + 51, cy - 35)],
                      fill=foreground, width=14)
        else:
            for dx, dy in ((-95, -87), (22, -87), (-95, 30), (22, 30)):
                draw.rounded_rectangle((cx + dx, cy + dy, cx + dx + 78, cy + dy + 78),
                                       radius=10, fill=foreground)
        draw.text((cx, y + 459), label, font=title, fill=foreground, anchor="mm")
        draw.text((cx, y + 576), descriptions[index], font=body, fill=secondary, anchor="mm")
        draw.line((x + 67, y + 712, right - 67, y + 712), fill=secondary, width=2)
        draw.text((x + 67, y + 747), "青年補助服務", font=ImageFont.truetype(str(font_path), 31), fill=secondary)
    for x in (833, 1666):
        draw.line((x, 0, x, 1686), fill=line, width=3)
    draw.line((0, 843, 2500, 843), fill=line, width=3)
    image.save(output, "PNG", optimize=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("validate", help="Validate bundled assets without network access")
    render = subparsers.add_parser("render", help="Regenerate the local menu image")
    render.add_argument("--font", type=Path, required=True, help="An installed CJK font; never uploaded")
    args = parser.parse_args(argv)
    try:
        if args.command == "render":
            render_image(args.font)
        payload, image, digest = validate()
        result = {"status": "valid", "mode": "offline", "name": payload["name"],
                  "contentHash": digest, "imageBytes": len(image), "size": payload["size"],
                  "areas": len(payload["areas"]), "imageAlt": IMAGE_ALT, "networkRequests": 0}
        # ASCII JSON keeps the CLI parseable on legacy Windows console encodings.
        print(json.dumps(result))
        return 0
    except MenuValidationError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "message": "Unable to render or read local menu assets."}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
