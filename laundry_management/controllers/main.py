from odoo import http
from odoo.http import request
from urllib.parse import quote
import base64
import os
import datetime
import zoneinfo
import uuid

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

        upi_id = "vestidofabwash@okhdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(move.display_grand_total, 0):.2f}"
        invoice_date = move.invoice_date.strftime('%d %b %Y') if move.invoice_date else ""

        txn_ref = uuid.uuid4().hex[:12]
        # Standardized NPCI parameters (mode=02 for static/B2C QR intent)
        upi_url = (
            f"upi://pay?pa={quote(upi_id)}"
            f"&pn={quote(payee_name)}"
            f"&tr={txn_ref}"
            f"&tn={quote('Payment for ' + move.name)}"
            f"&am={amount}"
            f"&cu=INR"
            f"&mode=02"
            f"&purpose=00"
        )
        qr_image_url = f"/download_qr/{move.id}"

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
            pass

        logo_html = (
            f'<img class="logo" src="{logo_data_uri}" alt="Vestido Fabwash Studio"/>'
            if logo_data_uri else ""
        )
        invoice_date_html = (
            f'<div class="invoice-date">Invoice Date: {invoice_date}</div>'
            if invoice_date else ""
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
                body {{ font-family: Arial, sans-serif; text-align:center; padding: 30px 15px; background-color: #f9f9f9; color: #333; }}
                .card {{ max-width: 400px; margin: 0 auto; background: #fff; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
                img.logo {{ width:120px; margin-bottom:15px; }}
                img.qr {{ width:210px; height:210px; border: 1px solid #eee; border-radius: 8px; padding: 5px; }}
                .invoice-date {{ font-size:14px; color:#666; margin-bottom:8px; }}
                .amount {{ font-size:22px; color:#111; margin: 15px 0; font-weight:bold; }}
                .expiry {{ font-size:12px; color:#888; margin-top:18px; }}
                a.pay-btn {{
                    display:block; width: 100%; box-sizing: border-box; margin-top:15px; padding:14px;
                    background:#0b8043; color:white; text-decoration:none;
                    border-radius:8px; font-size:16px; font-weight:bold;
                }}
                .upi-box {{
                    margin-top: 15px; background: #f1f3f4; padding: 10px; border-radius: 8px;
                    display: flex; align-items: center; justify-content: space-between; font-size: 13px;
                }}
                .copy-btn {{
                    background: #1a73e8; color: white; border: none; padding: 6px 12px;
                    border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 12px;
                }}
                .note {{ font-size: 12px; color: #666; margin-top: 12px; background: #fff8e1; padding: 8px; border-radius: 6px; border: 1px solid #ffe082; }}
            </style>
        </head>
        <body>
            <div class="card">
                {logo_html}
                <h2 style="margin: 0 0 5px 0;">{move.name}</h2>
                {invoice_date_html}
                <div class="amount">Amount: ₹{amount}</div>
                <img class="qr" src="{qr_image_url}" alt="Payment QR"/>
                
                <a class="pay-btn" href="{upi_url}">Pay Now via UPI App</a>

                <div class="upi-box">
                    <span><strong>UPI ID:</strong> {upi_id}</span>
                    <button class="copy-btn" onclick="copyUPI('{upi_id}')" id="copyBtn">Copy ID</button>
                </div>

                <div class="note">
                    <strong>Note:</strong> If Google Pay shows a gallery warning, click <strong>Copy ID</strong> above and pay directly via <em>Pay UPI ID</em> in your app.
                </div>

                {expiry_html}
            </div>

            <script>
                function copyUPI(upi) {{
                    navigator.clipboard.writeText(upi).then(function() {{
                        var btn = document.getElementById('copyBtn');
                        btn.innerText = 'Copied!';
                        btn.style.background = '#2e7d32';
                        setTimeout(function() {{
                            btn.innerText = 'Copy ID';
                            btn.style.background = '#1a73e8';
                        }}, 3000);
                    }});
                }}
            </script>
        </body>
        </html>
        """
        return request.make_response(html, headers=[('Content-Type', 'text/html')])