import io
import qrcode
from qrcode.constants import ERROR_CORRECT_M
from config import settings


class PaymentQrService:
    @staticmethod
    def generate_sbp_qr_bytes(amount_rub: float, order_id_or_comment: str) -> bytes:
        """
        Генерирует изображение QR-кода для оплаты через СБП / банковские приложения РФ.
        Использует стандарт ГОСТ Р 56042-2014 с реквизитами из конфигурации.
        """
        sum_kopecks = int(round(amount_rub * 100))
        phone_digits = "".join(filter(str.isdigit, settings.SBP_PHONE))

        # Формирование строки платежных реквизитов
        qr_payload = (
            f"ST00012|"
            f"Name={settings.SBP_RECIPIENT_NAME}|"
            f"BankName={settings.SBP_BANK}|"
            f"Sum={sum_kopecks}|"
            f"Purpose=Печать в ЦСО-4 {order_id_or_comment}|"
            f"Phone={phone_digits}"
        )

        qr = qrcode.QRCode(
            version=1,
            error_correction=ERROR_CORRECT_M,
            box_size=10,
            border=3,
        )
        qr.add_data(qr_payload)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        return buffer.getvalue()
