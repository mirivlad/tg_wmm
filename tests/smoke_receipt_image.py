from PIL import Image,ImageDraw,ImageFont
import qrcode,io
from wmm.receipts import recognize

qrtext = "t=20261028T1330&s=180.00&fn=000110000105&i=1234567890&fp=123456&n=1"
im = Image.new("RGB", (950,1100), "white")
draw = ImageDraw.Draw(im)
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 36)
lines = ["МАГАЗИН", "МОЛОКО 120,00", "ХЛЕБ 60,00", "ИТОГО 180,00", "СПАСИБО ЗА ПОКУПКУ"]
for idx, line in enumerate(lines):
    draw.text((45, 30 + idx*78), line, fill="black", font=font)
qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8,border=4)
qr.add_data(qrtext)
qr.make(fit=True)
stamp = qr.make_image(fill_color="black",back_color="white").convert("RGB")
without_qr = io.BytesIO()
im.save(without_qr, format="PNG")
no_qr = recognize(without_qr.getvalue(), "Europe/Moscow")
print("ocr-only total_minor", no_qr.total_minor, "items", len(no_qr.items))
assert no_qr.total_minor == 18000
im.paste(stamp,(100,530))
stream = io.BytesIO()
im.save(stream, format="PNG")
got = recognize(stream.getvalue(), "Europe/Moscow")
print("fiscal",got.fiscal,"total_minor",got.total_minor,"items", [(x.name,x.amount_minor) for x in got.items], "sum_match", got.items_match_total)
assert got.fiscal and got.total_minor == 18000
assert got.occurred_at.startswith("2026-10-28T10:30")
