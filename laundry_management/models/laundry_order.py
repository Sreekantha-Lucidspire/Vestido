# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
import requests
import json
import logging
import base64
import qrcode
from io import BytesIO
from urllib.parse import quote

_logger = logging.getLogger(__name__)

class LaundryOrder(models.Model):
    _name = 'laundry.order'
    _description = 'Laundry Order'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'id desc'

    name = fields.Char(default='New', copy=False)
    partner_id = fields.Many2one('res.partner', required=True)
    email = fields.Char(string="Email", related="partner_id.email")
    phone = fields.Char(string="Phone", related="partner_id.phone")
    order_date = fields.Datetime(default=fields.Datetime.now)
    delivery_date = fields.Datetime(string="Delivery Date")
    remarks = fields.Text(string="Remarks")

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True
    )
    user_id = fields.Many2one(
        'res.users',
        string="Responsible",
        default=lambda self: self.env.user,
        tracking=True
    )
    active = fields.Boolean(default=True)
    grand_total = fields.Monetary(
        string="Grand Total",
        compute="_compute_grand_total",
        store=True
    )
    document_ids = fields.One2many(
        'laundry.order.document',
        'order_id',
        string="Images"
    )
    tracker_ids = fields.One2many(
        'laundry.order.tracker',
        'order_id',
        string='Tracker'
    )

    order_line_ids = fields.One2many('laundry.order.line', 'order_id')
    extra_line_ids = fields.One2many('laundry.order.extra.line', 'order_id', string="Extra Charges")

    total_weight = fields.Float(compute='_compute_total_weight', store=True)

    # amount_total is computed by _compute_amounts, which includes GST
    # (18%) applied on (Items + Other Charges - Discount), matching the
    # rule used in the Proforma/Invoice QWeb reports. Every intermediate
    # figure (Items, Discount, Other Charges) is rounded to the nearest
    # rupee BEFORE the taxable base is built, exactly like the report.
    amount_total = fields.Float(
        string="Grand Total",
        compute='_compute_amounts',
        store=True
    )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('received', 'Received'),
        ('washing', 'Washing'),
        ('ironing', 'Ironing'),
        ('ready', 'Ready'),
        ('delivered', 'Delivered'),
    ], default='draft')

    invoice_id = fields.Many2one('account.move')
    invoice_date = fields.Date(
        string="Invoice Date",
        related='invoice_id.invoice_date',
        store=True,
        readonly=True
    )

    tag_type = fields.Selection([('order', 'Per Order'), ('line', 'Per Line')], default='order')

    tag_ids = fields.One2many('laundry.tag', 'order_id')
    tag_count = fields.Integer(compute='_compute_tag_count')

    has_wash = fields.Boolean(compute='_compute_service_flags')
    has_iron = fields.Boolean(compute='_compute_service_flags')

    # ---- GST Summary fields (mirror the PDF report labels exactly) ----
    amount_untaxed = fields.Float(
        string="Subtotal",
        compute='_compute_amounts',
        store=True,
        help="Items + Other Charges - Discount, before GST (matches 'Subtotal' on the invoice)."
    )
    amount_tax = fields.Float(
        string="GST",
        compute='_compute_amounts',
        store=True,
        help="18% GST calculated on the Subtotal."
    )
    rounding_off = fields.Float(
        string="Rounding Off",
        compute='_compute_amounts',
        store=True,
        help="Paise adjustment applied to reach a whole-rupee Grand Total."
    )

    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=True
    )
    whatsapp_status = fields.Text(
        string="WhatsApp Status",
        readonly=True,
        copy=False,
        tracking=True
    )
    whatsapp_sent_datetime = fields.Datetime(
        string="WhatsApp Sent Time",
        readonly=True,
        copy=False
    )
    whatsapp_sent_by = fields.Many2one(
        'res.users',
        string="WhatsApp Sent By",
        readonly=True,
        copy=False
    )
    invoice_whatsapp_shared = fields.Boolean(string="WhatsApp Invoice Shared", default=False)

    # =========================================================================
    # INVOICE LOCK PROTECTION
    # =========================================================================
    # Fields that represent the "composition" of the order — once an
    # invoice exists, these should not be editable from the order form.
    # 'state' and 'invoice_id' are intentionally excluded so the existing
    # status-bar workflow (Receive/Deliver) and WhatsApp tracking fields
    # keep working normally after invoicing.
    _INVOICE_LOCKED_FIELDS = {
        'partner_id', 'order_date', 'delivery_date', 'tag_type', 'remarks',
        'order_line_ids', 'extra_line_ids',
    }

    def write(self, vals):
        if not self.env.context.get('allow_invoiced_write'):
            locked = self._INVOICE_LOCKED_FIELDS & set(vals.keys())
            if locked:
                for order in self:
                    if order.invoice_id:
                        raise UserError(_(
                            "This order (%s) has already been invoiced (%s). "
                            "Please contact the administrator before making any changes."
                        ) % (order.name, order.invoice_id.name))
        return super().write(vals)

    @api.onchange('order_line_ids', 'extra_line_ids')
    def _onchange_lines_invoice_lock(self):
        """
        Pops the 'contact administrator' warning STRICTLY the moment the
        user clicks 'Add a line' on an already-invoiced order — not on
        every edit to an existing line. A freshly added, not-yet-saved
        row has a virtual NewId (not a real database int id), which is
        how we distinguish "a new line was just added" from "an existing
        line's field was edited". This is a UI-level heads-up only; the
        write() override above is what actually blocks the save if the
        user proceeds anyway (covers edits/deletes too).
        """
        if not self.invoice_id:
            return

        new_order_lines = self.order_line_ids.filtered(lambda l: not isinstance(l.id, int))
        new_extra_lines = self.extra_line_ids.filtered(lambda l: not isinstance(l.id, int))

        if new_order_lines or new_extra_lines:
            return {
                'warning': {
                    'title': "Order Already Invoiced",
                    'message': (
                        "This order (%s) has already been invoiced (%s). "
                        "Please contact the administrator before making any changes."
                    ) % (self.name, self.invoice_id.name),
                }
            }

    @api.onchange('order_line_ids', 'extra_line_ids')
    def _onchange_lines_recompute_percent_discount(self):
        """
        Keeps any Discount line whose Discount Type = 'Percent' in sync
        with whatever it's a percentage OF (Items + Other Charges).

        Without this, a percent discount line only recalculates its own
        price_unit when a field ON THAT SAME LINE changes (see
        LaundryOrderExtraLine._onchange_discount_percent). Editing an
        Item line's qty/weight/price, or another Extra Charges line
        (e.g. "Other Charges"), does not touch the Discount line's own
        fields, so its price_unit was silently going stale (e.g. Other
        Charges 100 -> 200 but the 10% discount amount stayed frozen at
        the value calculated against the old base).

        This order-level onchange watches both order_line_ids and
        extra_line_ids as a whole, so any change to Items OR Extra
        Charges re-triggers the base recalculation for every percent-mode
        discount line, using the exact same _get_discount_base() /
        formula the line itself already uses. Pure UI-side recompute —
        no other logic, fields, or save-time behavior is changed.
        """
        for line in self.extra_line_ids:
            if line.product_id and line.product_id.product_type == 'discount' \
               and line.discount_mode == 'percent':
                base = line._get_discount_base()
                line.price_unit = -round(base * (line.discount_percent or 0.0) / 100.0, 2)

    @api.depends('order_line_ids.subtotal', 'order_line_ids.price_tax', 'extra_line_ids.subtotal')
    def _compute_amounts(self):
        """
        Subtotal is now the EXACT (unrounded-to-whole-rupee) taxable
        base — Items + Other Charges - Discount, kept to normal 2-decimal
        currency precision. Only the final Grand Total is rounded to the
        nearest whole Rupee (as before); the paise difference introduced
        by that single rounding step is shown in "Rounding Off".

            Subtotal      = Items + Other Charges - Discount   (exact, 2-decimal;
                             NOT rounded to the nearest whole rupee)
            GST            = round(Subtotal * 18%, 2)
            Grand Total    = round(Subtotal + GST, 0)
            Rounding Off   = Grand Total - (Subtotal + GST)   [paise difference]
        """
        for order in self:
            # Items (pre-tax) subtotal, summed from order lines (line.subtotal is
            # already tax-excluded because order line _compute_tax uses
            # taxes['total_excluded']).
            items_subtotal = sum(order.order_line_ids.mapped('subtotal'))

            # Split extra lines into charges vs discounts (mirrors report logic)
            discount_total = sum(
                order.extra_line_ids.filtered(
                    lambda l: l.product_id.product_type == 'discount'
                ).mapped('subtotal')
            )
            other_charges_total = sum(
                order.extra_line_ids.filtered(
                    lambda l: l.product_id.product_type == 'charge'
                ).mapped('subtotal')
            )

            # Subtotal (taxable base) = Items + Other Charges - Discount,
            # kept to 2-decimal currency precision — NOT rounded to the
            # nearest whole rupee (that whole-rupee rounding now happens
            # only once, at the very end, on the Grand Total).
            taxable_base = round(items_subtotal + other_charges_total - abs(discount_total), 2)

            # GST (18%) applied on the exact taxable base, kept to 2 decimals
            gst = round(taxable_base * 0.18, 2)

            # Grand Total = Subtotal + GST, then rounded to the nearest rupee
            total_before_rounding = taxable_base + gst
            final_total = round(total_before_rounding, 0)

            # Rounding Off = the paise difference introduced by that final rounding
            rounding_off = round(final_total - total_before_rounding, 2)

            order.amount_untaxed = taxable_base   # Subtotal (no GST) — exact, unrounded
            order.amount_tax = gst                # GST
            order.rounding_off = rounding_off     # Rounding Off
            order.amount_total = final_total      # Grand Total

    def generate_payment_qr(self):
        """ Generates a base64 string of a QR code for the invoice total """
        self.ensure_one()
        # Using the Pine Labs merchant VPA (verified as "Vestido Fabwash
        # Studio" — a proper business account) instead of the earlier
        # personal-account VPA (vestidofabwash@okhdfcbank), which was
        # the root cause of the "Pay Now" deep link failing after PIN
        # entry — confirmed via both Google Pay and PhonePe resolving
        # that VPA to a personal account holder name rather than the
        # business name.
        upi_id = "pinelabs.STQ4596522@hdfcbank"
        payee_name = "Vestido Fabwash Studio"
        # NOTE: laundry.order has no display_grand_total field (that only
        # exists on account.move). Use this model's own Grand Total
        # (amount_total / grand_total, both computed from the same
        # _compute_amounts) instead.
        amount = f"{round(self.amount_total, 0):.2f}"

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

    # =========================================================================
    # WHATSAPP & PROFORMA INTEGRATION (SPLIT OPERATIONS)
    # =========================================================================
    def action_print_proforma_invoice(self):
        """ Purely downloads/previews the Proforma Invoice report. """
        self.ensure_one()
        return self.env.ref('laundry_management.action_laundry_quotation').report_action(self)

    def action_send_proforma_whatsapp(self):
        """ Renders the layout to PDF and delivers it via Meta APIs """
        self.ensure_one()
        
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"
        
        recipient_phone = "+919686570381" # Sandbox test number
        
        # --- Data Preparation ---
        # Qty number from sum of order lines
        total_qty = sum(line.qty for line in self.order_line_ids)
        
        # Condition notes from the tracker remarks (Stage 5)
        tracker = self.tracker_ids.filtered(lambda t: t.sequence == 5)
        # condition_notes = tracker.remarks if tracker and tracker.remarks else "Verified and inspected."
        condition_notes = self.remarks if self.remarks else "Verified and inspected."

        message_body = (
            f"Hello {self.partner_id.name},\n\n"
            f"Your order has been inspected and verified at our studio. Here's a summary:\n\n"
            f"📦 *Order ID:* {self.name}\n"
            f"🧺 *Total Garments:* {total_qty}\n"
            f"📋 *Notes:* {condition_notes}\n\n"
            f"💰 *Proforma Invoice:* ₹{round(self.amount_total, 0):,.2f}\n\n"
            f"If you need to reach us:\n8296777380\n\n"
            f"Upon completion of the order, we will be sharing the final invoice with you for making the payment.\n\n"
            f"— Team Vestido Fabwash Studio"
        )
        
        report_template = 'laundry_management.action_laundry_quotation'
        
        # Generate PDF
        pdf_content, report_format = self.env['ir.actions.report']._render_qweb_pdf(report_template, res_ids=self.id)
        filename = f"Proforma_{self.name.replace('/', '_')}.pdf"
            
        # Upload Media
        upload_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
        files = {"file": (filename, pdf_content, "application/pdf")}
        data = {"messaging_product": "whatsapp"}
        
        upload_response = requests.post(upload_url, headers=headers, data=data, files=files, timeout=10)
        upload_result = upload_response.json()

        if upload_response.status_code == 200 and "id" in upload_result:
            media_id = upload_result["id"]
            
            # Send Message
            send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
            send_payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "document",
                "document": {"id": media_id, "filename": filename, "caption": message_body}
            }
            
            requests.post(send_url, headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}, data=json.dumps(send_payload), timeout=10)
        
        self.whatsapp_sent_datetime = fields.Datetime.now()
        self.whatsapp_sent_by = self.env.user
        self.whatsapp_status = f"✅ Proforma Invoice successfully sent to {recipient_phone}"
        self._update_tracker(5)
        self.message_post(
            body=f"📄 WhatsApp Sent: Proforma Invoice\n📦 Order: {self.name}\n📱 To: {recipient_phone}",
            message_type="notification",
            subtype_xmlid="mail.mt_comment"
        )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'laundry.order',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_send_out_for_delivery_whatsapp(self):
        self.ensure_one()
        
        # Ensure this token is refreshed and valid
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD" 
        PHONE_NUMBER_ID = "1126838393847539"
        
        # Fetch tracker sequence 8
        delivery_tracker = self.tracker_ids.filtered(lambda t: t.sequence == 8)
        
        # Use the name of the staff assigned to this tracker stage
        agent_name = delivery_tracker.staff_id.name if delivery_tracker.staff_id else "Our Delivery Partner"
        
        message_body = (
            f"Hello {self.partner_id.name},\n\n"
            f"Your order is on its way! 🚚\n\n"
            f"📦 *Order ID:* {self.name}\n"
            f"🧾 *Invoice ID:* {self.invoice_id.name if self.invoice_id else 'N/A'}\n"
            f"👤 *Delivery Agent:* {agent_name}\n\n"
            f"If you need to reach us:\n8296777380\n\n"
            f"— Team Vestido Fabwash Studio"
        )
        
        # API Payload
        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": "+919686570381", # For testing
            "type": "text",
            "text": {"body": message_body}
        }
        
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        
        response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if response.status_code == 200:
            self.message_post(body=f"🚚 Out for Delivery WhatsApp sent. Agent: {agent_name}")
        else:
            _logger.error(f"WhatsApp API Error: {response.text}")
            raise UserError("Failed to send WhatsApp. Please check your Access Token.")

    def trigger_whatsapp_for_tracker(self, sequence):
        self.ensure_one()
        
        # Mapping sequences to their specific action methods
        if sequence == 1:
            self.action_send_enquiry_received_whatsapp()
        elif sequence == 3:  # Matches the sequence of your Picked Up stage
            self.action_send_picked_up_whatsapp()
        elif sequence == 5:
            self.action_send_proforma_whatsapp() # Triggers your existing proforma method
        elif sequence == 6: 
            self.action_send_work_started_whatsapp()
        elif sequence == 8:  # Added: Out for Delivery
            self.action_send_out_for_delivery_whatsapp()
        elif sequence == 9:
            self.action_send_delivered_whatsapp()
        elif sequence == 10: # Payment Received
            self.action_send_payment_received_whatsapp()
        elif sequence == 11: # Feedback Requested
            self.action_send_feedback_whatsapp()

    # =========================================================================
    # EXTANT CORE OPERATIONS
    # =========================================================================
    def action_print_invoice_bill(self):
        self.ensure_one()
        return self.env.ref('laundry_management.action_laundry_invoice_bill').report_action(self)

    def action_tracker(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Operations Tracker',
            'res_model': 'laundry.order.tracker',
            'view_mode': 'list,form',
            'domain': [('order_id', '=', self.id)],
            'context': {'default_order_id': self.id},
            'target': 'current',
        }

    @api.depends('amount_total')
    def _compute_grand_total(self):
        for rec in self:
            rec.grand_total = rec.amount_total 

    @api.depends('company_id')
    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = rec.company_id.currency_id

    @api.depends('order_line_ids.service_type_id')
    def _compute_service_flags(self):
        for rec in self:
            has_wash = False
            has_iron = False
            for line in rec.order_line_ids:
                op = line.service_type_id.operation_type
                if op in ['wash', 'wash_iron', 'dry_clean']:
                    has_wash = True
                if op in ['iron', 'wash_iron']:
                    has_iron = True
            rec.has_wash = has_wash
            rec.has_iron = has_iron

    def _compute_tag_count(self):
        for rec in self:
            rec.tag_count = len(rec.tag_ids)

    def action_generate_tags(self):
        for rec in self:
            if rec.tag_ids:
                raise ValidationError("Tags already generated. You can only print them.")
            if rec.tag_type == 'order':
                self.env['laundry.tag'].create({
                    'order_id': rec.id,
                    'barcode': rec.name,
                })
            elif rec.tag_type == 'line':
                for line in rec.order_line_ids:
                    self.env['laundry.tag'].create({
                        'order_id': rec.id,
                        'order_line_id': line.id,
                        'barcode': f"{rec.name}-{line.id}",
                    })

    def action_regenerate_tags(self):
        for rec in self:
            rec.tag_ids.unlink()

    def action_print_tags(self):
        return self.env.ref('laundry_management.action_laundry_tag_30mm').report_action(self.tag_ids)

    def action_print_receipt_80mm(self):
        return self.env.ref('laundry_management.action_laundry_receipt_80mm').report_action(self)

    def action_view_tags(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Tags',
            'res_model': 'laundry.tag',
            'view_mode': 'list,form',
            'domain': [('order_id', '=', self.id)],
        }

    def _update_tracker(self, sequence_no):
        tracker = self.tracker_ids.filtered(lambda t: t.sequence == sequence_no)
        if tracker and not tracker.completed:
            tracker.write({
                'completed': True,
                'stage_datetime': fields.Datetime.now(), # Automatically captures current time
                'staff_id': self.env.user.id,
            })

        if tracker:
            tracker.write({
                'completed': True,
                'stage_datetime': fields.Datetime.now(),
                'staff_id': self.env.user.id,
            })

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                today = fields.Date.today()
                year = today.year % 100

                if today.month >= 4:
                    fy_start = year
                    fy_end = year + 1
                else:
                    fy_start = year - 1
                    fy_end = year

                fiscal_year = f"{fy_start:02d}-{fy_end:02d}"
                month_code = f"{today.month:02d}"
                seq = self.env['ir.sequence'].next_by_code('laundry.order') or '00001'

                vals['name'] = f"VFS/{fiscal_year}/{month_code}/{seq}"

        orders = super().create(vals_list)

        for order in orders:

            stages = [
                (1, "Customer Enquiry Received", False,
                 "Acknowledge receipt; set expectations on response time"),

                (2, "Enquiry Allocated to Field Staff", False,
                 "Notify customer of pickup agent name and ETA. No WhatsApp message configured"),

                (3, "Order Picked Up", False,
                 "Confirm pickup with order ID; share photo if required"),

                (4, "Order Received at Studio", False,
                 "Internal checkpoint — no customer comms needed. No WhatsApp message configured"),

                (5, "Order Verification & Proforma Invoice Shared", False,
                 "Share garment checklist, condition notes, proforma invoice"),

                (6, "Work Approved & Started", False,
                 "Confirm customer approval; share expected completion date"),

                (7, "Work Completed — Invoice Raised", False,
                 "Please generate the invoice and use the 'Send via WhatsApp' feature on the invoice page to complete this order; request delivery slot"),

                (8, "Order Out for Delivery", False,
                 "Share agent name, live tracking link if available"),

                (9, "Order Delivered", False,
                 "Confirm delivery; share payment link if pending"),

                (10, "Payment Received", False,
                 "Acknowledge payment; share receipt"),

                (11, "Feedback Requested", False,
                 "Request Google/WhatsApp review; thank customer"),
            ]

            for seq, stage, wa, remarks in stages:
                self.env['laundry.order.tracker'].create({
                    'order_id': order.id,
                    'sequence': seq,
                    'stage': stage,
                    'wa_sent': wa,
                    'remarks': remarks,
                })

        return orders

    @api.onchange('total_weight')
    def _onchange_total_weight(self):
        if self.total_weight and self.total_weight < 4:
            return {
                'warning': {
                     'title': "Minimum Weight",
                     'message': "Minimum recommended weight is 4 KG, you can still proceed."                
                }
            }

    @api.depends('order_line_ids.weight')
    def _compute_total_weight(self):
        for rec in self:
            rec.total_weight = sum(line.weight for line in rec.order_line_ids)

    def action_received(self):
        for rec in self:
            rec.state = 'received'
            rec._update_tracker(4)

            rec.message_post(
                body="📦 Order marked as Received.",
                subtype_xmlid="mail.mt_comment"
            )
        return True

    def action_send_enquiry_received_whatsapp(self):
        self.ensure_one()
        # Ensure you use your valid credentials/constants
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"
        
        recipient_phone = self.partner_id.phone
        if not recipient_phone:
            raise UserError("Customer phone number is missing.")
            
        # Meta Sandbox Bypass if needed, otherwise clean the number
        recipient_phone = "+919686570381" 
        customer_name = self.partner_id.name or "Valued Customer"
        message_body = (
            f"Hello {customer_name} 👋\n\n"
            "Thank you for reaching out to *Vestido Fabwash Studio*!\n\n"
            "We have received your laundry enquiry and our team will get back to you shortly.\n\n"
            "Team Vestido Fabwash Studio"
        )
        
        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "text",
            "text": {"body": message_body}
        }

        response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if response.status_code == 200:
            self.message_post(body="✅ Enquiry acknowledgment sent via WhatsApp.")
        else:
            _logger.error(f"WhatsApp Error: {response.text}")
            raise UserError(f"Failed to send WhatsApp: {response.text}")

    def action_send_picked_up_whatsapp(self):
        self.ensure_one()
        # Fetch the specific tracker record for "Order Picked Up" (Sequence 3)
        tracker = self.tracker_ids.filtered(lambda t: t.sequence == 3)
        
        # Pull data from the form fields
        pickup_date = tracker.stage_datetime.strftime('%d-%b %H:%M') if tracker.stage_datetime else "Not recorded"
        agent_name = tracker.staff_id.name or "Our Pickup Agent"

        # Meta API Config
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"
        recipient_phone = "+919686570381" # Sandbox test number

        message_body = (
            f"Hello {self.partner_id.name},\n\n"
            f"Your garments have been picked up successfully! ✅\n\n"
            f"🗓️ *Picked up on:* {pickup_date}\n"
            f"👤 *Agent:* {agent_name}\n"
            f"*Reference:* Your order ID will be shared upon Order Verification.\n\n"
            f"We'll send you a detailed verification update once your order reaches our studio. Thank you for choosing Vestido Fabwash Studio!\n\n"
            f"— Team Vestido Fabwash Studio"
        )

        # Standard API request logic
        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone,
            "type": "text",
            "text": {"body": message_body}
        }

        response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if response.status_code == 200:
            self.message_post(body="📦 WhatsApp Sent: Picked Up confirmation.")
        else:
            raise UserError(f"WhatsApp Error: {response.text}")

    def action_send_received_whatsapp(self):
        for rec in self:

            ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
            PHONE_NUMBER_ID = "1126838393847539"

            recipient_phone = rec.partner_id.phone
            if not recipient_phone:
                raise UserError("Customer phone number is missing.")

            recipient_phone = "+919686570381"

            message_body = (
                f"Hello {rec.partner_id.name},\n\n"
                f"We have successfully received your laundry order!\n\n"
                f"📦 Order ID: {rec.name}\n"
                f"💰 Grand Total: ₹ {round(rec.grand_total, 0):,.2f}\n\n"
                f"Thank you for choosing Vestido Fabwash Studio!"
            ) 

            send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

            headers = {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }

            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "text",
                "text": {"body": message_body}
            }

            response = requests.post(
                send_url,
                headers=headers,
                data=json.dumps(payload),
                timeout=10
            )

            result = response.json()

            if response.status_code == 200:
                rec.whatsapp_status = (
                    f"✅ Order Received notification sent to {recipient_phone}"
                )
                rec.whatsapp_sent_datetime = fields.Datetime.now()
                rec.whatsapp_sent_by = self.env.user

                rec.message_post(
                    body=(
                        f"📩 WhatsApp Sent: Order Received<br/>"
                        f"📦 Order: {rec.name}<br/>"
                        f"📱 To: {recipient_phone}"
                    ),
                    subtype_xmlid="mail.mt_comment"
                )
            else:
                error_msg = result.get('error', {}).get('message', 'Unknown Error')
                raise UserError(error_msg)

        return True

    def action_send_work_started_whatsapp(self):
        self.ensure_one()

        # Replaced with your working Sandbox Tokens
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"

        recipient_phone = self.partner_id.phone
        if not recipient_phone:
            raise UserError("Customer phone number is missing.")

        recipient_phone = "+919686570381"   # Sandbox test number

        customer_name = self.partner_id.name or "Customer"

        # Formatted exactly to your specifications
        message_body = (
            f"Hello {customer_name},\n\n"
            f"We have received your confirmation — work has begun on your order! 🧵✨\n\n"
            f"📦 *Order ID:* {self.name}\n\n"
            f"We will notify you as soon as your garments are ready for delivery.\n\n"
            f"— Team Vestido Fabwash Studio"
        )

        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone,
            "type": "text",
            "text": {
                "body": message_body
            }
        }

        response = requests.post(
            send_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=10
        )

        result = response.json()

        if response.status_code == 200:
            self.message_post(
                body="🧵 WhatsApp Sent: Work Started",
                subtype_xmlid="mail.mt_comment"
            )
        else:
            error_msg = result.get('error', {}).get('message', 'Unknown Error')
            raise UserError(f"Failed to send WhatsApp: {error_msg}")

    def action_washing(self):
        for rec in self:
            if not rec.tag_ids:
                raise UserError("Generate Tags before proceeding with this action")
            if not rec.has_wash:
                raise ValidationError("No washing required for this order.")
            rec.state = 'washing'

    def action_ironing(self):
        for rec in self:
            if not rec.tag_ids:
                raise UserError("Generate Tags before proceeding with this action")
            if not rec.has_iron:
                raise ValidationError("No ironing required for this order.")
            rec.state = 'ironing'

    def action_ready(self):
        for rec in self:
            if rec.has_wash and rec.has_iron:
                if rec.state != 'ironing':
                    raise ValidationError("Complete ironing before marking ready.")
            elif rec.has_wash and not rec.has_iron:
                if rec.state != 'washing':
                    raise ValidationError("Complete washing before marking ready.")
            elif rec.has_iron and not rec.has_wash:
                if rec.state != 'ironing':
                    raise ValidationError("Complete ironing before marking ready.")
            rec.state = 'ready'

    def action_delivered(self):
        for rec in self:
            rec.state = 'delivered'

            rec._update_tracker(9)

            rec.message_post(
                body="🚚 Order marked as Delivered.",
                subtype_xmlid="mail.mt_comment"
            )

        return True

    def action_send_delivered_whatsapp(self):
        for rec in self:
            ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
            PHONE_NUMBER_ID = "1126838393847539"

            recipient_phone = "+919686570381" # Test number

            # Format current date for the message
            delivery_date = fields.Datetime.context_timestamp(self.with_context(tz='Asia/Kolkata'), fields.Datetime.now()).strftime('%d-%m-%Y')

            message_body = (
                f"Hello {rec.partner_id.name},\n\n"
                f"Your order has been delivered successfully! ✅\n\n"
                f"📦 *Order ID:* {rec.name}\n"
                f"🧾 *Invoice ID:* {rec.invoice_id.name if rec.invoice_id else 'N/A'}\n"
                f"🗓️ *Delivered on:* {delivery_date}\n"
                f"💰 Grand Total: ₹ {round(rec.grand_total, 0):,.2f}\n\n"
                f"Please confirm once payment is made. Thank you for trusting Vestido Fabwash! 🙏\n\n"
                f"— Team Vestido Fabwash Studio"
            )

            send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
            headers = {
                "Authorization": f"Bearer {ACCESS_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "text",
                "text": {"body": message_body}
            }

            response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
            result = response.json()

            if response.status_code == 200:
                rec.whatsapp_status = f"✅ Order Delivered notification sent to {recipient_phone}"
                rec.whatsapp_sent_datetime = fields.Datetime.now()
                rec.whatsapp_sent_by = self.env.user
                rec.message_post(
                    body=f"🚚 WhatsApp Sent: Order Delivered<br/>📦 Order: {rec.name}<br/>📱 To: {recipient_phone}",
                    subtype_xmlid="mail.mt_comment"
                )
            else:
                error_msg = result.get('error', {}).get('message', 'Unknown Error')
                raise UserError(error_msg)
        return True

    def action_send_payment_received_whatsapp(self):
        self.ensure_one()
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"
        recipient_phone = "+919686570381" # Test number

        payment_date = fields.Datetime.context_timestamp(self.with_context(tz='Asia/Kolkata'), fields.Datetime.now()).strftime('%d-%m-%Y')

        message_body = (
            f"Hello {self.partner_id.name},\n\n"
            f"We have received your payment. Thank you! 🙏\n\n"
            f"📦 *Order ID:* {self.name}\n"
            f"🧾 *Invoice ID:* {self.invoice_id.name if self.invoice_id else 'N/A'}\n"
            f"✅ *Amount Received:* ₹{round(self.grand_total, 0):,.2f}\n"
            f"📅 *Date:* {payment_date}\n\n"
            f"Your payment receipt has been recorded. We look forward to serving you again!\n\n"
            f"— Team Vestido Fabwash Studio"
        )

        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "text",
            "text": {"body": message_body}
        }
        
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        
        response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if response.status_code == 200:
            self.message_post(body=f"💰 Payment Received WhatsApp sent for {self.name}")
        else:
            raise UserError(f"Failed to send WhatsApp: {response.text}")

    def action_send_feedback_whatsapp(self):
        self.ensure_one()
        ACCESS_TOKEN = "EAAYONoZCXnFsBRhp12GEFTCl4TzJ1wgDNhAE4O5Q1ie4BNMmWwyYMPsrjiRcSdJvYkT2ftqsBHaZCRaWHZCaKNvkZB17mok4jtCzngzudpzHMaatR5I1ZCLTPHG4ZC0hsR9pX9jwDlQfG2wEYBdD5acWcDOcMl4Y7P14KK28vgp7gEPLZBrBZC1hUMkdvrYl3XiHj5K7xHtXQAbvC9l6BlNZAZCkdLmbv5hTI3IuWIPZBTRSh4ZAWIOaT95HULk212ekoHxY1KBZA9f9fUdA7x2qIlczDwITltgZDZD"
        PHONE_NUMBER_ID = "1126838393847539"
        recipient_phone = "+919686570381" # Test number
        google_review_link = "https://www.google.com/maps/place/Vestido+Fabwash+Studio/@12.8717514,77.5428859,17z/data=!3m1!4b1!4m6!3m5!1s0x3bae413df543c0fd:0x1aea1f2916cd29d2!8m2!3d12.8717514!4d77.5428859!16s%2Fg%2F11z2d4jgx5?entry=ttu&g_ep=EgoyMDI2MDYwMy4xIKXMDSoASAFQAw%3D%3D"

        message_body = (
            f"Hello {self.partner_id.name},\n\n"
            f"We hope your garments are looking their best! 😊\n\n"
            f"Your feedback means the world to us. Could you spare 2 minutes to share your experience?\n\n"
            f"⭐ *Google Review:* {google_review_link}\n\n"
            f"📦 *Order ID:* {self.name}\n"
            f"🧾 *Invoice ID:* {self.invoice_id.name if self.invoice_id else 'N/A'}\n\n"
            f"Thank you for choosing *Vestido Fabwash Studio* — see you next time!\n\n"
            f"— Team Vestido Fabwash Studio"
        )

        send_url = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": recipient_phone,
            "type": "text",
            "text": {"body": message_body}
        }
        
        headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}
        
        response = requests.post(send_url, headers=headers, data=json.dumps(payload), timeout=10)
        
        if response.status_code == 200:
            self.message_post(body=f"⭐ Feedback Request WhatsApp sent for {self.name}")
        else:
            _logger.error(f"WhatsApp API Error: {response.text}")
            raise UserError(f"Failed to send WhatsApp: {response.json().get('error', {}).get('message', 'Unknown Error')}")

    def get_dashboard_data(self):
        result = {}
        orders = self.search([])
        for order in orders:
            for line in order.order_line_ids:
                key = (line.product_id.id, line.service_type_id.id)
                if key not in result:
                    result[key] = {
                        'product_id': line.product_id.id,
                        'service_type_id': line.service_type_id.id,
                        'pending': 0,
                        'progress': 0,
                        'done': 0,
                    }
                state = order.state
                if state in ['draft', 'received']:
                    bucket = 'pending'
                elif state in ['washing', 'ironing']:
                    bucket = 'progress'
                else:
                    bucket = 'done'
                result[key][bucket] += 1
        return list(result.values())

    def _get_or_create_product(self, laundry_product, service_type_id=None):
        Product = self.env['product.product']
        default_code = f"LAUNDRY_{laundry_product.id}"
        if service_type_id:
            default_code += f"_{service_type_id.id}"

        product = Product.search([('default_code', '=', default_code)], limit=1)
        if product:
            return product

        if laundry_product.product_type == 'discount':
            account = laundry_product.expense_account_id
        elif laundry_product.product_type == 'charge':
            account = laundry_product.income_account_id
        else:
            account = service_type_id.income_account_id

        if not account:
            raise ValidationError(f"Please configure income account for service '{service_type_id.name}'")

        product_name = f"{laundry_product.name}"
        product = Product.create({
            'name': product_name,
            'type': 'service',
            'default_code': default_code,
            'list_price': 0.0,
            'property_account_income_id': account.id,
        })
        return product

    def action_create_invoice(self):
        for rec in self:
            if not rec.order_line_ids:
                continue

            journal = self.env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', rec.company_id.id)
            ], limit=1)

            if not journal:
                raise ValidationError("Please configure a Sales Journal.")

            invoice_lines = []
            for line in rec.order_line_ids:
                product = self._get_or_create_product(line.product_id, line.service_type_id)
                qty = line.qty if line.pricing_type == 'per_item' else line.weight

                invoice_lines.append((0, 0, {
                     'product_id': product.id,
                     'name': f"{line.product_id.name}",
                     'quantity': qty,
                     'price_unit': line.unit_price,
                     'account_id': line.service_type_id.income_account_id.id,
                     'tax_ids': [(6, 0, line.tax_ids.ids)],
                     'service_type_id': line.service_type_id.id,
                     'pricing_type': line.pricing_type,
                     'weight': line.weight,
                     'premium_id': line.premium_id.id,
                     'premium_multiplier': line.premium_multiplier,
                }))

            for extra in rec.extra_line_ids:
                product = self._get_or_create_product(extra.product_id)
                invoice_lines.append((0, 0, {
                    'product_id': product.id,
                    'name': extra.name,
                    'quantity': extra.quantity,
                    'price_unit': extra.price_unit,
                    'tax_ids': [(6, 0, extra.tax_ids.ids)],
                }))

            invoice = self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': rec.partner_id.id,
                'journal_id': journal.id,
                'invoice_line_ids': invoice_lines,
                'invoice_origin': rec.name,
                'laundry_order_id': rec.id
            })
            rec.invoice_id = invoice.id

    def action_view_invoice(self):
        self.ensure_one()
        if not self.invoice_id:
            return
        return {
            'type': 'ir.actions.act_window',
            'name': 'Invoice',
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.invoice_id.id,
            'target': 'current',
        }


# -----------------------------
# ORDER LINE MODEL
# -----------------------------
class LaundryOrderLine(models.Model):
    _name = 'laundry.order.line'
    _description = 'Laundry Order Line'

    order_id = fields.Many2one('laundry.order', ondelete='cascade')
    product_id = fields.Many2one('laundry.product', required=True)
    service_type_id = fields.Many2one('laundry.service.type', required=True)
    pricing_id = fields.Many2one('laundry.pricing', readonly=True)

    pricing_type = fields.Selection([
        ('per_item', 'Per Item'),
        ('per_kg', 'Per KG')
    ], required=True)

    qty = fields.Integer()
    weight = fields.Float()
    currency_id = fields.Many2one(
        'res.currency',
        related='order_id.currency_id',
        store=True,
        readonly=True
    )

    unit_price = fields.Monetary(string="Unit Price", currency_field='currency_id')
    premium_id = fields.Many2one('laundry.premium', string="Premium Type", default=lambda self: self.env['laundry.premium'].search([('sequence', '=', 0)], limit=1))
    premium_multiplier = fields.Float(related='premium_id.multiplier')

    tax_ids = fields.Many2many(
        'account.tax',
        string="Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
        default=lambda self: self.env['account.tax'].search([
            ('amount', '=', 18.0), 
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
    )

    subtotal = fields.Monetary(compute='_compute_tax', store=True, currency_field='currency_id')
    price_tax = fields.Monetary(compute='_compute_tax', store=True, currency_field='currency_id')
    price_total = fields.Monetary(string="Total Price", compute='_compute_tax', store=True, currency_field='currency_id')

    def write(self, vals):
        if not self.env.context.get('allow_invoiced_write'):
            for line in self:
                if line.order_id.invoice_id:
                    raise UserError(_(
                        "Order %s has already been invoiced. "
                        "Please contact the administrator before making any changes."
                    ) % line.order_id.name)
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('allow_invoiced_write'):
            for line in self:
                if line.order_id.invoice_id:
                    raise UserError(_(
                        "Order %s has already been invoiced. "
                        "Please contact the administrator before making any changes."
                    ) % line.order_id.name)
        return super().unlink()

    @api.onchange('premium_id')
    def _onchange_premium_invoice_lock(self):
        """
        Pops the 'contact administrator' warning the moment the user
        touches the first field (Premium Type) of a new line on an
        already-invoiced order. premium_id has a default value, so the
        web client includes it in the very first onchange call fired
        right after 'Add a line' is clicked — this is the earliest
        possible point a warning can be shown, since Odoo's client does
        not call the server on the click itself, only once the row's
        first field is touched. The write() override above is what
        actually blocks the save if the user proceeds anyway.
        """
        for line in self:
            if line.order_id and line.order_id.invoice_id:
                return {
                    'warning': {
                        'title': "Order Already Invoiced",
                        'message': (
                            "This order (%s) has already been invoiced (%s). "
                            "Please contact the administrator before making any changes."
                        ) % (line.order_id.name, line.order_id.invoice_id.name),
                    }
                }

    @api.onchange('product_id', 'service_type_id')
    def fetch_pricing_type(self):
        for line in self:
            if line.product_id and line.service_type_id:
                pricing = self.env['laundry.pricing'].search([
                    ('product_id', '=', line.product_id.id),
                    ('service_type_id', '=', line.service_type_id.id)
                ], limit=1)
                if pricing:
                    line.pricing_type = pricing.pricing_type

    @api.onchange('product_id', 'service_type_id', 'pricing_type', 'premium_id')
    def _onchange_price(self):
        for rec in self:
            pricing = self.env['laundry.pricing'].search([
                ('product_id', '=', rec.product_id.id),
                ('service_type_id', '=', rec.service_type_id.id),
                ('pricing_type', '=', rec.pricing_type)
            ], limit=1)
            base_price = pricing.price if pricing else 0.0
            
            # Clean compound price protection math
            if rec.premium_id:
                rec.unit_price = base_price * rec.premium_id.multiplier
            else:
                rec.unit_price = base_price

    @api.onchange('qty', 'weight')
    def _onchange_qty_weight_zero_price_warning(self):
        """
        Pops a single confirmation dialog when the user enters a quantity
        or weight for a line whose computed unit price is still zero
        (e.g. no matching laundry.pricing record was found). This is a
        pure UI warning — it does not block or alter saving, and does
        not touch unit_price, pricing lookups, or any other logic.
        """
        for line in self:
            entered_qty = line.qty if line.pricing_type == 'per_item' else line.weight
            if entered_qty and line.unit_price == 0.0:
                return {
                    'warning': {
                        'title': "Unit Price is Zero",
                        'message': (
                            f"Unit price for '{line.product_id.name}' is ₹0.00. "
                            "Please check pricing configuration before saving."
                        ),
                    }
                }

    @api.constrains('unit_price', 'qty', 'weight')
    def _check_zero_unit_price(self):
        """
        Safety-net popup shown on Save if any line still has unit_price
        of 0 despite a quantity/weight being entered (covers cases where
        the onchange above did not fire, e.g. bulk/imported rows). Raises
        one combined error listing all affected products, so the user
        sees exactly one popup instead of one per line.
        """
        zero_price_lines = self.filtered(
            lambda l: l.unit_price == 0.0 and (l.qty if l.pricing_type == 'per_item' else l.weight)
        )
        if zero_price_lines:
            names = ", ".join(zero_price_lines.mapped('product_id.name'))
            raise ValidationError(
                f"Unit price is ₹0.00 for: {names}. Please check pricing before saving."
            )

    @api.depends('qty', 'weight', 'unit_price', 'tax_ids', 'pricing_type')
    def _compute_tax(self):
        for line in self:
            qty = line.qty if line.pricing_type == 'per_item' else line.weight
            price = line.unit_price

            if not line.tax_ids:
                line.subtotal = qty * price
                line.price_tax = 0.0
                line.price_total = line.subtotal
                continue

            taxes = line.tax_ids.compute_all(
                price,
                currency=line.order_id.currency_id,
                quantity=qty,
                product=None,
                partner=line.order_id.partner_id
            )

            line.subtotal = taxes['total_excluded']
            line.price_tax = taxes['total_included'] - taxes['total_excluded']
            line.price_total = taxes['total_included']


# -----------------------------
# DASHBOARD MODEL
# -----------------------------
class LaundryDashboard(models.TransientModel):
    _name = 'laundry.dashboard'
    _description = 'Laundry Dashboard'

    product_id = fields.Many2one('laundry.product', string="Product")
    service_type_id = fields.Many2one('laundry.service.type', string="Service Type")
    pending = fields.Integer()
    progress = fields.Integer()
    done = fields.Integer()

    def action_open_dashboard(self):
        data = self.env['laundry.order'].get_dashboard_data()
        self.search([]).unlink()
        self.create(data)

        return {
            'type': 'ir.actions.act_window',
            'name': 'Laundry Dashboard',
            'res_model': 'laundry.dashboard',
            'view_mode': 'kanban,list',
            'target': 'current',
        }


# -----------------------------
# DOCUMENT/IMAGE MODEL
# -----------------------------
class LaundryOrderDocument(models.Model):
    _name = 'laundry.order.document'
    _description = 'Laundry Order Image'

    order_id = fields.Many2one('laundry.order', string="Order", ondelete='cascade')
    name = fields.Char(string="Image Name")
    file = fields.Binary(string="Upload File", attachment=True)
    filename = fields.Char(string="Filename")


# -----------------------------
# EXTRA CHARGES MODEL
# -----------------------------
class LaundryOrderExtraLine(models.Model):
    _name = 'laundry.order.extra.line'
    _description = 'Laundry Extra Charges'

    order_id = fields.Many2one('laundry.order', ondelete='cascade')
    currency_id = fields.Many2one('res.currency', related='order_id.currency_id')
    product_id = fields.Many2one('laundry.product', required=True, domain="[('product_type','in',['charge','discount'])]")
    name = fields.Char()
    quantity = fields.Float(default=1)
    price_unit = fields.Monetary(currency_field='currency_id')

    # ---- Percentage-based discount support ----
    # Plain boolean, computed on this same model, so the list view's
    # invisible= expressions don't need to dot into product_id.product_type
    # (a cross-relation path that isn't reliably prefetched for row-level
    # invisible evaluation inside a one2many sub-list, and can silently
    # evaluate to "hidden" -> an unclickable cell).
    is_discount_line = fields.Boolean(
        compute='_compute_is_discount_line',
        help="True when product_id is a Discount-type product."
    )
    discount_mode = fields.Selection([
        ('amount', 'Amount (₹)'),
        ('percent', 'Percent (%)'),
    ], string="Discount Type", default='amount',
       help="Only relevant for lines using a Discount product. "
            "'Percent' auto-calculates the rupee price_unit from "
            "the % entered, applied on Items + Other Charges.")
    discount_percent = fields.Float(
        string="Discount %",
        help="Used only when Discount Type = Percent. Applied on the "
             "order's Items + Other Charges (pre-GST), matching the "
             "taxable-base logic in laundry.order._compute_amounts."
    )
    # --------------------------------------------

    tax_ids = fields.Many2many(
        'account.tax',
        string="Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
        default=lambda self: self.env['account.tax'].search([
            ('amount', '=', 18.0),
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
    )
    subtotal = fields.Monetary(compute='_compute_total', store=True, currency_field='currency_id')

    def write(self, vals):
        if not self.env.context.get('allow_invoiced_write'):
            for line in self:
                if line.order_id.invoice_id:
                    raise UserError(_(
                        "Order %s has already been invoiced. "
                        "Please contact the administrator before making any changes."
                    ) % line.order_id.name)
        return super().write(vals)

    def unlink(self):
        if not self.env.context.get('allow_invoiced_write'):
            for line in self:
                if line.order_id.invoice_id:
                    raise UserError(_(
                        "Order %s has already been invoiced. "
                        "Please contact the administrator before making any changes."
                    ) % line.order_id.name)
        return super().unlink()

    @api.onchange('quantity')
    def _onchange_quantity_invoice_lock(self):
        """
        Pops the 'contact administrator' warning the moment the user
        touches the first field of a new Extra Charges line on an
        already-invoiced order. quantity has default=1, so it is
        included in the very first onchange call fired right after
        'Add a line' is clicked. The write() override above is what
        actually blocks the save if the user proceeds anyway.
        """
        for line in self:
            if line.order_id and line.order_id.invoice_id:
                return {
                    'warning': {
                        'title': "Order Already Invoiced",
                        'message': (
                            "Order %s has already been invoiced. "
                            "Please contact the administrator before making any changes."
                        ) % line.order_id.name,
                    }
                }

    @api.depends('product_id.product_type')
    def _compute_is_discount_line(self):
        for rec in self:
            rec.is_discount_line = rec.product_id.product_type == 'discount'

    @api.onchange('product_id')
    def _onchange_product_id(self):
        for rec in self:
            if not rec.product_id:
                continue
            rec.name = rec.product_id.name
            if rec.product_id.product_type == 'discount':
                rec.price_unit = -abs(rec.price_unit or 0)
                # Force a default so the Discount Type cell is never
                # left blank (the field-level default='amount' only
                # applies on brand-new records, not when product_id
                # is set via this onchange).
                if not rec.discount_mode:
                    rec.discount_mode = 'amount'
            else:
                # Not a discount line -> reset the % fields so they
                # don't linger/confuse if the product is switched later.
                rec.discount_mode = 'amount'
                rec.discount_percent = 0.0
            # Self-heal: make sure 18% GST is attached even if this row
            # existed before the default above was added, or if tax_ids
            # was cleared for any reason.
            if not rec.tax_ids:
                gst_18 = self.env['account.tax'].search([
                    ('amount', '=', 18.0),
                    ('type_tax_use', '=', 'sale'),
                    ('company_id', '=', self.env.company.id)
                ], limit=1)
                if gst_18:
                    rec.tax_ids = [(6, 0, gst_18.ids)]

    @api.onchange('price_unit')
    def _onchange_price_unit(self):
        for rec in self:
            if rec.product_id and rec.product_id.product_type == 'discount' and rec.discount_mode == 'amount':
                rec.price_unit = -abs(rec.price_unit)

    @api.onchange('discount_mode', 'discount_percent')
    def _onchange_discount_percent(self):
        """ When in Percent mode, auto-derive price_unit (always
            negative, matching the existing discount convention) from
            discount_percent applied to the order's taxable base
            (Items + Other Charges, excluding discount lines). """
        for rec in self:
            if rec.product_id and rec.product_id.product_type == 'discount' and rec.discount_mode == 'percent':
                base = rec._get_discount_base()
                rec.price_unit = -round(base * (rec.discount_percent or 0.0) / 100.0, 2)

    def _get_discount_base(self):
        """ Base on which % discount is calculated: Items + Other
            Charges (pre-GST), matching laundry.order._compute_amounts
            so the discount % behaves consistently with the invoice
            totals shown elsewhere. """
        self.ensure_one()
        order = self.order_id
        items_subtotal = round(sum(order.order_line_ids.mapped('subtotal')), 0)
        other_charges = round(sum(
            order.extra_line_ids.filtered(
                lambda l: l.product_id.product_type == 'charge'
            ).mapped('subtotal')
        ), 0)
        return items_subtotal + other_charges

    @api.depends('quantity', 'price_unit', 'tax_ids')
    def _compute_total(self):
        for rec in self:
            if not rec.tax_ids:
                rec.subtotal = rec.quantity * rec.price_unit
                continue
            
            taxes = rec.tax_ids.compute_all(
                rec.price_unit,
                currency=rec.order_id.currency_id,
                quantity=rec.quantity,
                product=None,
                partner=rec.order_id.partner_id
            )
            rec.subtotal = taxes['total_excluded']


class LaundryOrderTracker(models.Model):
    _name = 'laundry.order.tracker'
    _description = 'Laundry Order Tracker'
    _order = 'sequence'
    _rec_name = 'stage'

    order_id = fields.Many2one(
        'laundry.order',
        ondelete='cascade'
    )

    sequence = fields.Integer(
        string="#",
        required=True
    )

    stage = fields.Char(
        string="Stage",
        required=True
    )

    stage_datetime = fields.Datetime(
        string="Date & Time"
    )

    wa_sent = fields.Boolean(
        string="WhatsApp Required"
    )

    staff_id = fields.Many2one(
        'res.users',
        string="Staff"
    )

    remarks = fields.Text(
        string="Remarks"
    )

    completed = fields.Boolean(
        string="Completed",
        default=False
    )

    DELIVERED_STAGE_SEQUENCE = 9

    def write(self, vals):
        # 1. Identify which records are changing wa_sent from False to True
        trackers_to_trigger = self.env['laundry.order.tracker']
        if vals.get('wa_sent') is True:
            trackers_to_trigger = self.filtered(lambda t: not t.wa_sent)

        # 2. NEW: identify which records are the "Order Delivered" stage
        #    flipping completed from False to True, so we can push that
        #    onto the parent order's state after the write succeeds.
        trackers_delivered_to_sync = self.env['laundry.order.tracker']
        if vals.get('completed') is True:
            trackers_delivered_to_sync = self.filtered(
                lambda t: not t.completed and t.sequence == self.DELIVERED_STAGE_SEQUENCE
            )

        # 3. Perform the standard write operation
        res = super(LaundryOrderTracker, self).write(vals)

        # 4. Trigger the WhatsApp method for the filtered records
        for tracker in trackers_to_trigger:
            tracker.order_id.trigger_whatsapp_for_tracker(tracker.sequence)

        # 5. NEW: sync the parent order's state to 'delivered' when the
        #    "Order Delivered" tracker line is checked off directly from
        #    the Operations Tracker (not via the order form's Deliver
        #    button, which already does this the other way around).
        #    Calling order_id.write() directly (not action_delivered())
        #    avoids re-entering this same write() via _update_tracker.
        for tracker in trackers_delivered_to_sync:
            if tracker.order_id and tracker.order_id.state != 'delivered':
                tracker.order_id.write({'state': 'delivered'})
                tracker.order_id.message_post(
                    body="🚚 Order marked as Delivered (via Operations Tracker).",
                    subtype_xmlid="mail.mt_comment"
                )

        return res