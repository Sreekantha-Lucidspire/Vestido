from odoo import http
from odoo.http import request
import base64


class PaymentQRController(http.Controller):

    @http.route('/download_qr/<int:move_id>', type='http', auth='public')
    def download_qr(self, move_id, **kwargs):
        move = request.env['account.move'].sudo().browse(move_id)
        if not move.exists():
            return request.not_found()
        qr_base64 = move.generate_payment_qr()
        qr_bytes = base64.b64decode(qr_base64)
        filename = f"{move.name.replace('/', '_')}_payment_qr.png"
        return request.make_response(
            qr_bytes,
            headers=[
                ('Content-Type', 'image/png'),
                ('Content-Disposition', f'inline; filename="{filename}"'),
            ]
        )

    @http.route('/pay/<string:token>', type='http', auth='public')
    def pay_page(self, token, **kwargs):
        move = request.env['account.move']._get_move_from_pay_token(token)
        if not move:
            return request.not_found()
        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(move.display_grand_total, 0):.2f}"
        upi_url = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu=INR"
        qr_image_url = f"/download_qr/{move.id}"
        logo_url = "/laundry_management/static/src/img/vestido_logo.png"
        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Pay {move.name}</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: Arial, sans-serif; text-align:center; padding-top:50px; }}
                img.logo {{ width:120px; margin-bottom:20px; }}
                img.qr {{ width:220px; height:220px; }}
                .amount {{ font-size:18px; color:#333; margin-bottom:20px; }}
                a.pay-btn {{
                    display:inline-block; margin-top:20px; padding:14px 32px;
                    background:#0b8043; color:white; text-decoration:none;
                    border-radius:8px; font-size:17px; font-weight:bold;
                }}
            </style>
        </head>
        <body>
            <img class="logo" src="{logo_url}" alt="Vestido Fabwash Studio"/>
            <h2>{move.name}</h2>
            <div class="amount">Amount: Rs. {amount}</div>
            <img class="qr" src="{qr_image_url}" alt="Payment QR"/>
            <p>Scan the QR above, or tap below to pay directly:</p>
            <a class="pay-btn" href="{upi_url}">Pay Now</a>
        </body>
        </html>
        """
        return request.make_response(html, headers=[('Content-Type', 'text/html')])