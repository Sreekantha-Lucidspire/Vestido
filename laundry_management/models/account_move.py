# -*- coding: utf-8 -*-
from odoo import models, fields, api
import requests
import json
import base64
import qrcode
from io import BytesIO


class AccountMove(models.Model):
    _inherit = "account.move"

    pickup_delivery = fields.Char(string="Pickup & Delivery")
    invoice_discount = fields.Float(string="Discount")
    laundry_order_id = fields.Many2one('laundry.order', 'Laundry Ref', tracking=True)
    is_whatsapp_sent = fields.Boolean(string="WhatsApp Sent", default=False)

    # =====================================================================
    # DISPLAY-ONLY GST CALCULATION
    # =====================================================================
    # These fields are calculated purely for display on the invoice form
    # and printed report. They do NOT touch amount_untaxed / amount_tax /
    # amount_total, so they never affect journal entries, Amount Due,
    # payment reconciliation, or GST filing/tax reports. If those need to
    # reflect real GST, actual tax records must be attached to invoice
    # lines instead (a completely separate mechanism from this).
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
        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(self.display_grand_total, 0):.2f}"
        qr_data = f"upi://pay?pa={upi_id}&pn={payee_name}&am={amount}&cu=INR"

        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(qr_data)
        qr.make(fit=True)

        img = qr.make_image(fill='black', back_color='white')
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode('utf-8')

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