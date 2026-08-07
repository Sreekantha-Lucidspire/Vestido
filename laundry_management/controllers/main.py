from odoo import http
from odoo.http import request
import base64
import os
import datetime
import zoneinfo

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


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

        if move == 'expired':
            html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Link Expired</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    body { font-family: Arial, sans-serif; text-align:center; padding-top:80px; }
                    h2 { color:#b00020; }
                </style>
            </head>
            <body>
                <h2>This payment link has expired</h2>
                <p>Please contact Vestido Fabwash Studio for a new payment link.</p>
                <p>📞 8296777380</p>
            </body>
            </html>
            """
            return request.make_response(html, headers=[('Content-Type', 'text/html')])

        if not move:
            return request.not_found()

        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(move.display_grand_total, 0):.2f}"
        upi_url = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu=INR"
        qr_image_url = f"/download_qr/{move.id}"

        # Decode the token's timestamp to show the customer when this
        # link expires (48 hours after it was generated), always shown
        # in IST regardless of the server's own system timezone.
        expiry_text = ""
        try:
            b64_id, _sig = token.split('.')
            padding = '=' * (-len(b64_id) % 4)
            raw = base64.urlsafe_b64decode(b64_id + padding)
            _move_id_str, timestamp_str = raw.decode().split(':')
            generated_at = datetime.datetime.fromtimestamp(int(timestamp_str), tz=IST)
            expires_at = generated_at + datetime.timedelta(hours=48)
            expiry_text = expires_at.strftime('%d %b %Y, %I:%M %p')
        except Exception:
            pass

        # Embed logo as base64 directly in the HTML — avoids a separate
        # HTTP request for a static file, which the payment.* nginx
        # config intentionally blocks (only /pay/ and /download_qr/ are
        # allowed through on that subdomain).
        logo_data_uri = ""
        try:
            logo_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                'static', 'src', 'img', 'vestido_logo.png'
            )
            with open(logo_path, 'rb') as f:
                logo_base64 = base64.b64encode(f.read()).decode('utf-8')
            logo_data_uri = f"data:image/png;base64,{logo_base64}"
        except FileNotFoundError:
            pass  # gracefully skip the logo if the file isn't found
        logo_html = (
            f'<img class="logo" src="{logo_data_uri}" alt="Vestido Fabwash Studio"/>'
            if logo_data_uri else ""
        )
        expiry_html = (
            f'<p class="expiry">This link expires on {expiry_text} IST</p>'
            if expiry_text else ""
        )
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
                .expiry {{ font-size:13px; color:#888; margin-top:14px; }}
                a.pay-btn {{
                    display:inline-block; margin-top:20px; padding:14px 32px;
                    background:#0b8043; color:white; text-decoration:none;
                    border-radius:8px; font-size:17px; font-weight:bold;
                }}
            </style>
        </head>
        <body>
            {logo_html}
            <h2>{move.name}</h2>
            <div class="amount">Amount: Rs. {amount}</div>
            <img class="qr" src="{qr_image_url}" alt="Payment QR"/>
            <p>Scan the QR above, or tap below to pay directly:</p>
            <a class="pay-btn" href="{upi_url}">Pay Now</a>
            {expiry_html}
        </body>
        </html>
        """
        return request.make_response(html, headers=[('Content-Type', 'text/html')])