# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
import json
import base64
import qrcode
from io import BytesIO
import hashlib
import hmac
from urllib.parse import quote

class AccountMove(models.Model):
    _inherit = "account.move"

    pickup_delivery = fields.Char(string="Pickup & Delivery")
    invoice_discount = fields.Float(string="Discount")
    laundry_order_id = fields.Many2one('laundry.order', 'Laundry Ref', tracking=True)
    is_whatsapp_sent = fields.Boolean(string="WhatsApp Sent", default=False)

    # =====================================================================
    # DISPLAY-ONLY GST CALCULATION
    # =====================================================================
    gst_amount = fields.Monetary(
        string="GST",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only: 18% calculated on (Untaxed Amount - Discount). "
             "Not posted to accounting."
    )
    rounding_off = fields.Monetary(
        string="Rounding Off",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only: paise adjustment to reach a whole-rupee "
             "display Grand Total. Not posted to accounting."
    )
    display_grand_total = fields.Monetary(
        string="Grand Total",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only Grand Total = Subtotal + GST, rounded to the "
             "nearest rupee. This is NOT the same as Amount Due, which "
             "remains based on the invoice's real accounting total."
    )

    @api.depends('amount_untaxed', 'invoice_discount')
    def _compute_gst_display(self):
        for move in self:
            taxable_base = round(max(move.amount_untaxed - move.invoice_discount, 0), 0)
            gst = round(taxable_base * 0.18, 2)
            total_before_rounding = taxable_base + gst
            final_total = round(total_before_rounding, 0)
            rounding_off = round(final_total - total_before_rounding, 2)

            move.gst_amount = gst
            move.rounding_off = rounding_off
            move.display_grand_total = final_total

    def generate_payment_qr(self):
        """ Generates a base64 string of a QR code for the invoice total """
        self.ensure_one()
        # Using the Pine Labs merchant VPA (verified as "Vestido Fabwash
        # Studio" — a proper business account) instead of the earlier
        # personal-account VPA, which was the root cause of the
        # "Pay Now" deep link failing after PIN entry.
        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(self.display_grand_total, 0):.2f}"

        # FIX: URL-encode every UPI parameter (matches the fix applied in
        # payment_qr_controller.py's pay_page()). payee_name contains
        # spaces which were previously inserted raw into the upi:// URI;
        # encoding them keeps this QR consistent with the "Pay Now" link
        # and avoids any chance of a scanning app misparsing the string.
        qr_data = (
            "upi://pay"
            f"?pa={quote(upi_id)}"
            f"&pn={quote(payee_name)}"
            f"&am={quote(amount)}"
            f"&cu=INR"
            f"&tn={quote('Payment for ' + self.name)}"
        )

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_data)
        qr.make(fit=True)

        img = qr.make_image(fill='black', back_color='white')
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode('utf-8')

    def action_download_payment_qr(self):
        """ Opens the standalone QR PNG for download via the controller """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/download_qr/{self.id}',
            'target': 'self',
        }

    def action_open_pay_page(self):
        self.ensure_one()
        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'laundry.payment_base_url', 'https://payment.vestidofabwash.com'
        )
        return {
            'type': 'ir.actions.act_url',
            'url': f'{base_url}/pay/{self._get_pay_token()}',
            'target': 'new',
        }

    def _get_pay_token(self):
        """ Builds a signed token encoding this invoice's ID and the
            current timestamp, so the payment URL can't be tampered
            with or guessed sequentially, and expires after 48 hours """
        self.ensure_one()
        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        timestamp = int(fields.Datetime.now().timestamp())
        raw = f"{self.id}:{timestamp}".encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()[:16]
        token = base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + sig
        return token

    @api.model
    def _get_move_from_pay_token(self, token):
        """ Reverses _get_pay_token: verifies the signature and returns
            the matching move. Returns False if invalid/tampered, or
            the string 'expired' if the signature is valid but the
            48-hour window has passed. """
        TOKEN_VALIDITY_SECONDS = 48 * 60 * 60  # 48 hours

        try:
            b64_id, sig = token.split('.')
            padding = '=' * (-len(b64_id) % 4)
            raw = base64.urlsafe_b64decode(b64_id + padding)
            move_id_str, timestamp_str = raw.decode().split(':')
            move_id = int(move_id_str)
            timestamp = int(timestamp_str)
        except Exception:
            return False

        secret = self.env['ir.config_parameter'].sudo().get_param('database.secret')
        expected_sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected_sig):
            return False

        current_timestamp = int(fields.Datetime.now().timestamp())
        if current_timestamp - timestamp > TOKEN_VALIDITY_SECONDS:
            return 'expired'

        move = self.sudo().browse(move_id)
        return move if move.exists() else False

    def action_send_invoice_whatsapp(self):
        """ Generates the invoice PDF and sends it via Meta WhatsApp API """
        self.ensure_one()

        # 1. SETUP CONFIGURATION
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"

        recipient_phone = self.partner_id.phone
        if not recipient_phone:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': 'The customer does not have a phone number configured.',
                    'type': 'danger',
                    'sticky': False,
                }
            }

        recipient_phone = ''.join(c for c in recipient_phone if c.isdigit())

        # =========================================================================
        # STEP 6: BYPASS FOR META SANDBOX ALLOWED LIST
        # =========================================================================
        recipient_phone = "+919686570381"

        if self.state == 'posted':

            # 2. GENERATE PDF FROM ODOO (Using the correct Report Action ID)
            report_template = 'account.account_invoices'
            pdf_content, report_format = self.env['ir.actions.report']._render_qweb_pdf(
                report_template,
                res_ids=self.id
            )
            filename = f"{self.name.replace('/', '_')}.pdf"

            # 3. UPLOAD PDF TO META WHATSAPP API
            upload_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/media"
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            data = {"messaging_product": "whatsapp"}
            files = {"file": (filename, pdf_content, "application/pdf")}

            try:
                upload_response = requests.post(upload_url, headers=headers, data=data, files=files, timeout=10)
                upload_result = upload_response.json()
            except Exception as e:
                self.message_post(body=f"❌ WhatsApp Upload Connection Timeout: {str(e)}")
                return False

            # 4. GET MEDIA_ID & SEND MESSAGE
            if upload_response.status_code == 200 and "id" in upload_result:
                media_id = upload_result["id"]

                send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
                send_headers = {
                    "Authorization": f"Bearer {ACCESS_TOKEN}",
                    "Content-Type": "application/json"
                }

                # --- NEW: SEND TEXT MESSAGE FIRST ---
                message_body = (
                    f"Hello {self.partner_id.name},\n\n"
                    f"Your garments are ready! 🎉\n\n"
                    f"📦 *Order ID:* {self.laundry_order_id.name or 'N/A'}\n"
                    f"🧾 *Invoice ID:* {self.name}\n"
                    f"💰 *Invoice Amount:* ₹{round(self.display_grand_total, 0):,.2f}\n\n"
                    f"— Team Vestido Fabwash Studio"
                )
                text_payload = {
                    "messaging_product": "whatsapp",
                    "to": recipient_phone,
                    "type": "text",
                    "text": {"body": message_body}
                }
                requests.post(send_url, headers=send_headers, data=json.dumps(text_payload))

                # --- EXISTING: SEND DOCUMENT ---
                doc_payload = {
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient_phone,
                    "type": "document",
                    "document": {
                        "id": media_id,
                        "filename": filename
                    }
                }

                try:
                    send_response = requests.post(send_url, headers=send_headers, data=json.dumps(doc_payload), timeout=10)
                    send_result = send_response.json()
                except Exception as e:
                    self.message_post(body=f"❌ WhatsApp Send Connection Timeout: {str(e)}")
                    return False

                if send_response.status_code == 200:
                    self.is_whatsapp_sent = True
                    self.message_post(body=f"✅ Invoice PDF and message successfully sent via WhatsApp.")
                    if self.laundry_order_id:
                        self.laundry_order_id.invoice_whatsapp_shared = True
                else:
                    self.message_post(body=f"❌ Failed to send WhatsApp. Error: {send_result.get('error', {}).get('message')}")
            else:
                self.message_post(body=f"❌ Failed to upload Invoice to Meta. Error: {upload_result.get('error', {}).get('message')}")