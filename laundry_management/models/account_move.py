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
import time

class AccountMove(models.Model):
    _inherit = "account.move"

    pickup_delivery = fields.Char(string="Pickup & Delivery")
    invoice_discount = fields.Float(string="Discount")
    laundry_order_id = fields.Many2one('laundry.order', 'Laundry Ref', tracking=True)
    is_whatsapp_sent = fields.Boolean(string="WhatsApp Sent", default=False)

    # =====================================================================
    # PHONE NUMBER (for Invoicing list view display)
    # =====================================================================
    partner_phone = fields.Char(
        string="Phone",
        related='partner_id.phone',
        store=True,
        readonly=True
    )

    # =====================================================================
    # SERVICE TYPES (grouped with per-service subtotal, for list view display)
    # =====================================================================
    service_types_display = fields.Char(
        string="Service Types",
        compute="_compute_service_types_display",
        store=True,
    )

    @api.depends('invoice_line_ids.service_type_id', 'invoice_line_ids.price_subtotal')
    def _compute_service_types_display(self):
        """
        Groups invoice lines by service_type_id and sums price_subtotal
        (tax-excluded) per group, formatted as:
            "Steam Iron: ₹150.00, Dry Cleaning: ₹86.00"
        Lines with no service_type_id (e.g. section/note lines, or lines
        from extra charges/discounts that don't carry a service type)
        are skipped entirely.
        """
        for move in self:
            groups = {}
            for line in move.invoice_line_ids:
                if not line.service_type_id:
                    continue
                name = line.service_type_id.name
                groups[name] = groups.get(name, 0.0) + line.price_subtotal

            parts = [f"{name}: ₹{amount:,.2f}" for name, amount in sorted(groups.items())]
            move.service_types_display = ', '.join(parts)

    # =====================================================================
    # PER-SERVICE-TYPE AMOUNT COLUMNS (Invoicing list view)
    # =====================================================================
    amount_wash_fold = fields.Monetary(
        string="Wash & Fold", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_steam_iron = fields.Monetary(
        string="Steam Iron", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_wash_iron = fields.Monetary(
        string="Wash & Iron", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_dry_cleaning = fields.Monetary(
        string="Dry Cleaning", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_add_ons = fields.Monetary(
        string="Add-Ons", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_shoe_cleaning = fields.Monetary(
        string="Shoe Cleaning", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )
    amount_roll_pressing = fields.Monetary(
        string="Roll Pressing", compute="_compute_service_type_amounts",
        store=True, currency_field='currency_id'
    )

    # Maps laundry.service.type NAME -> field name on account.move.
    # If you rename a service type in Configuration > Service Types,
    # update the key here to match, or this type's amounts will stop
    # being picked up (they'll just show as 0 for that type going
    # forward, existing stored values won't retroactively break).
    _SERVICE_TYPE_FIELD_MAP = {
        'Wash & Fold': 'amount_wash_fold',
        'Steam Iron': 'amount_steam_iron',
        'Wash & Iron': 'amount_wash_iron',
        'Dry Cleaning': 'amount_dry_cleaning',
        'Add-Ons': 'amount_add_ons',
        'Shoe Cleaning': 'amount_shoe_cleaning',
        'Roll Pressing': 'amount_roll_pressing',
    }

    @api.depends('invoice_line_ids.service_type_id', 'invoice_line_ids.price_subtotal')
    def _compute_service_type_amounts(self):
        field_names = list(self._SERVICE_TYPE_FIELD_MAP.values())
        for move in self:
            totals = {fname: 0.0 for fname in field_names}
            for line in move.invoice_line_ids:
                if not line.service_type_id:
                    continue
                fname = self._SERVICE_TYPE_FIELD_MAP.get(line.service_type_id.name)
                if fname:
                    totals[fname] += line.price_subtotal
            for fname in field_names:
                setattr(move, fname, totals[fname])

    # =====================================================================
    # EXTRA CHARGES / DISCOUNT (from linked laundry.order, for list view)
    # =====================================================================
    extra_charges_amount = fields.Monetary(
        string="Extra Charges",
        compute="_compute_extra_charges_discount",
        store=True,
        currency_field='currency_id',
        help="Sum of 'Other Charges' type extra lines on the linked Laundry Order."
    )
    discount_amount_display = fields.Monetary(
        string="Discount",
        compute="_compute_extra_charges_discount",
        store=True,
        currency_field='currency_id',
        help="Sum of 'Discount' type extra lines on the linked Laundry Order (shown as a positive number)."
    )

    @api.depends(
        'laundry_order_id.extra_line_ids.subtotal',
        'laundry_order_id.extra_line_ids.product_id.product_type',
    )
    def _compute_extra_charges_discount(self):
        for move in self:
            order = move.laundry_order_id
            if not order:
                move.extra_charges_amount = 0.0
                move.discount_amount_display = 0.0
                continue

            charges = sum(
                order.extra_line_ids.filtered(
                    lambda l: l.product_id.product_type == 'charge'
                ).mapped('subtotal')
            )
            discount = sum(
                order.extra_line_ids.filtered(
                    lambda l: l.product_id.product_type == 'discount'
                ).mapped('subtotal')
            )

            move.extra_charges_amount = charges
            move.discount_amount_display = abs(discount)

    # =====================================================================
    # DISPLAY-ONLY GST CALCULATION
    # =====================================================================
    cgst_amount = fields.Monetary(
        string="CGST",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only: 9% CGST calculated on (Untaxed Amount - Discount)."
    )

    sgst_amount = fields.Monetary(
        string="SGST",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only: 9% SGST calculated on (Untaxed Amount - Discount)."
    )

    gst_amount = fields.Monetary(
        string="GST",
        compute="_compute_gst_display",
        store=True,
        currency_field='currency_id',
        help="Display-only: total GST = CGST + SGST."
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

    # =====================================================================
    # SYNC PAYMENT STATUS -> "Payment Received" TRACKER STAGE (seq 10)
    # =====================================================================
    # NOTE: payment_state is itself a stored COMPUTED field, driven by
    # Odoo's core reconciliation logic. Overriding write() does not
    # reliably catch changes to it, because the ORM persists recomputed
    # stored fields through its internal flush/recompute path rather
    # than a normal write({...}) call. Wrapping the compute method
    # itself is the reliable hook: this runs the instant payment_state
    # is actually assigned, no matter how it's later persisted.
    PAYMENT_TRACKER_SEQUENCE = 10  # "Payment Received" stage in laundry.order.tracker

    def _compute_payment_state(self):
        res = super()._compute_payment_state()
        for move in self:
            if move.payment_state in ('paid', 'in_payment') and move.laundry_order_id:
                move.laundry_order_id._update_tracker(self.PAYMENT_TRACKER_SEQUENCE)
        return res

    @api.depends('amount_untaxed', 'invoice_discount')
    def _compute_gst_display(self):
        """
        Taxable base is now the EXACT (unrounded-to-whole-rupee) figure —
        Untaxed Amount - Discount, kept to normal 2-decimal currency
        precision — matching laundry.order._compute_amounts. Only the
        final Grand Total is rounded to the nearest whole Rupee; the
        paise difference introduced by that single rounding step is
        shown in "Rounding Off".
        """
        for move in self:
            taxable_base = round(max(move.amount_untaxed - move.invoice_discount, 0), 2)
            cgst = round(taxable_base * 0.09, 2)
            sgst = round(taxable_base * 0.09, 2)
            gst = round(cgst + sgst, 2)
            total_before_rounding = taxable_base + gst
            final_total = round(total_before_rounding, 0)
            rounding_off = round(final_total - total_before_rounding, 2)

            move.cgst_amount = cgst
            move.sgst_amount = sgst
            move.gst_amount = gst
            move.rounding_off = rounding_off
            move.display_grand_total = final_total

    def generate_payment_qr(self):
        """ Generates a base64 string of a QR code for the invoice total """
        self.ensure_one()
        # Pine Labs merchant VPA — confirmed MCC 7210 with HDFC/Pine Labs
        # (laundry/dry-cleaning services), replacing the earlier
        # unconfirmed vyapar.* VPA/MCC guess.
        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        amount = f"{round(self.display_grand_total, 0):.2f}"

        # Unique transaction reference per generation attempt — required
        # for P2M (merchant) UPI transactions per NPCI spec, and must not
        # be reused across retries for the same invoice.
        tr = f"{self.name.replace('/', '')}-{int(time.time())}"

        # All UPI parameters URL-encoded (payee_name contains spaces,
        # which must not be inserted raw into the upi:// URI). mc/tr
        # added so this payload matches merchant-transaction spec —
        # keep payment_qr_controller.py's pay_page() in sync with the
        # same VPA/mc/tr if/when that file is updated.
        qr_data = (
            "upi://pay"
            f"?pa={quote(upi_id)}"
            f"&pn={quote(payee_name)}"
            f"&am={quote(amount)}"
            f"&cu=INR"
            f"&mc=7210"
            f"&tr={quote(tr)}"
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
        # TODO (flagged, not changed): this token is hardcoded in
        # plaintext. Should be moved to ir.config_parameter and rotated
        # since it has now been shared outside your own systems.
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
        # TODO (flagged, not changed): this overrides EVERY customer's real
        # number with one fixed number. Every invoice currently goes to
        # this number regardless of who the customer is. Remove this line
        # (or gate it behind a debug/sandbox config flag) before this is
        # used with real customers.
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