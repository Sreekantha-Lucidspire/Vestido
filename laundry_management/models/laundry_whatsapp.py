# -*- coding: utf-8 -*-
import logging
import requests

from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com/v21.0"

# Template name + language code exactly as shown in WhatsApp Manager.
#   'doc': True  -> the template has a DOCUMENT (PDF) header.
#   'name': None -> template not created/approved in Meta yet.
WA_TEMPLATES = {
    'enquiry':       {'name': 'customer_enquiry_received_',   'lang': 'en', 'doc': False},
    'picked_up':     {'name': None,                           'lang': 'en', 'doc': False},
    'proforma':      {'name': 'order_verification_proforma_',  'lang': 'en', 'doc': True},
    'work_started':  {'name': None,                           'lang': 'en', 'doc': False},
    'final_invoice': {'name': 'order_final_invoice',          'lang': 'en', 'doc': True},
    'out_delivery':  {'name': None,                           'lang': 'en', 'doc': False},
    'delivered':     {'name': 'order_delivered',              'lang': 'en', 'doc': False},
    'payment':       {'name': 'payment_received',             'lang': 'en', 'doc': False},
    'feedback':      {'name': 'feedback_requested',           'lang': 'en', 'doc': False},
}


class LaundryOrderWhatsapp(models.Model):
    _inherit = 'laundry.order'

    # ------------------------------------------------------------------
    # Core helpers
    # ------------------------------------------------------------------
    @api.model
    def _wa_clean_phone(self, phone):
        digits = ''.join(c for c in (phone or '') if c.isdigit())
        return '91' + digits if len(digits) == 10 else digits

    @staticmethod
    def _wa_clean_param(value):
        # Template variables can't contain newlines/tabs or 4+ consecutive spaces
        if value is None or value is False or value == '':
            value = '-'
        return ' '.join(str(value).split())[:1000]

    @api.model
    def _wa_upload_pdf(self, pdf_content, filename):
        icp = self.env['ir.config_parameter'].sudo()
        token = icp.get_param('laundry.wa_access_token')
        phone_id = icp.get_param('laundry.wa_phone_number_id')
        if not token or not phone_id:
            raise UserError(_("WhatsApp credentials are not configured."))
        try:
            r = requests.post(
                f"{GRAPH_URL}/{phone_id}/media",
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp"},
                files={"file": (filename, pdf_content, "application/pdf")},
                timeout=30)
        except requests.RequestException as e:
            raise UserError(_("Could not reach WhatsApp: %s") % e)
        if r.status_code != 200:
            raise UserError(_("WhatsApp media upload failed: %s") % r.text)
        return r.json()["id"]

    @api.model
    def _send_whatsapp_template(self, to_phone, template_key, body_params=None, document=None):
        """Send an approved template. template_key is a key of WA_TEMPLATES.
        document = (media_id, filename) for templates with a PDF header."""
        tpl = WA_TEMPLATES[template_key]
        if not tpl['name']:
            raise UserError(_(
                "The WhatsApp template for '%s' has not been created in Meta yet. "
                "Create and get it approved, then set its name in WA_TEMPLATES."
            ) % template_key)

        icp = self.env['ir.config_parameter'].sudo()
        token = icp.get_param('laundry.wa_access_token')
        phone_id = icp.get_param('laundry.wa_phone_number_id')
        if not token or not phone_id:
            raise UserError(_("WhatsApp credentials are not configured."))

        # While laundry.wa_test_number is set, EVERY message goes there.
        to = self._wa_clean_phone(icp.get_param('laundry.wa_test_number') or to_phone)
        if not to:
            raise UserError(_("Customer phone number is missing."))

        components = []
        if tpl['doc']:
            if not document:
                raise UserError(_("Template %s needs a PDF attachment.") % tpl['name'])
            components.append({"type": "header", "parameters": [
                {"type": "document",
                 "document": {"id": document[0], "filename": document[1]}}]})
        if body_params:
            components.append({"type": "body", "parameters": [
                {"type": "text", "text": self._wa_clean_param(p)} for p in body_params]})

        template = {"name": tpl['name'], "language": {"code": tpl['lang']}}
        if components:
            template["components"] = components
        payload = {"messaging_product": "whatsapp", "to": to,
                   "type": "template", "template": template}

        try:
            r = requests.post(
                f"{GRAPH_URL}/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"}, json=payload, timeout=20)
            data = r.json()
        except requests.RequestException as e:
            raise UserError(_("Could not reach WhatsApp: %s") % e)
        except ValueError:
            raise UserError(_("Unexpected response from WhatsApp: %s") % r.text)

        if r.status_code != 200:
            err = data.get('error', {})
            details = (err.get('error_data') or {}).get('details', '')
            _logger.error("WhatsApp error (%s): %s", tpl['name'], data)
            raise UserError(_("WhatsApp failed for template '%s' (%s): %s %s") % (
                tpl['name'], err.get('code'), err.get('message'), details))
        return data['messages'][0]['id']

    def _wa_money(self, amount):
        return f"₹{round(amount or 0.0, 0):,.2f}"

    def _wa_today(self):
        return fields.Datetime.context_timestamp(
            self.with_context(tz='Asia/Kolkata'), fields.Datetime.now()
        ).strftime('%d-%m-%Y')

    def _wa_mark_sent(self, label):
        self.whatsapp_status = f"✅ {label} sent via WhatsApp"
        self.whatsapp_sent_datetime = fields.Datetime.now()
        self.whatsapp_sent_by = self.env.user
        self.message_post(body=f"📩 WhatsApp sent: {label}",
                          subtype_xmlid="mail.mt_comment")

    def _wa_invoice_name(self):
        return self.invoice_id.name if self.invoice_id else 'N/A'

    # ------------------------------------------------------------------
    # Stage 1 - Enquiry received (template has no variables)
    # ------------------------------------------------------------------
    def action_send_enquiry_received_whatsapp(self):
        self.ensure_one()
        self._send_whatsapp_template(self.partner_id.phone, 'enquiry')
        self._wa_mark_sent("Enquiry acknowledgment")

    # ------------------------------------------------------------------
    # Stage 3 - Picked up   (template to create: order_picked_up)
    # {{1}} pickup date/time, {{2}} agent name
    # ------------------------------------------------------------------
    def action_send_picked_up_whatsapp(self):
        self.ensure_one()
        tracker = self.tracker_ids.filtered(lambda t: t.sequence == 3)
        pickup = tracker.stage_datetime
        pickup_date = (
            fields.Datetime.context_timestamp(
                self.with_context(tz='Asia/Kolkata'), pickup).strftime('%d-%b %H:%M')
            if pickup else "Not recorded")
        agent = tracker.staff_id.name or "Our Pickup Agent"
        self._send_whatsapp_template(
            self.partner_id.phone, 'picked_up',
            [pickup_date, agent])
        self._wa_mark_sent("Picked Up confirmation")

    # ------------------------------------------------------------------
    # Stage 5 - Verification + Proforma (PDF header)
    # {{1}} order, {{2}} garments, {{3}} notes, {{4}} estimated bill
    # ------------------------------------------------------------------
    def action_send_proforma_whatsapp(self):
        self.ensure_one()
        total_qty = sum(line.qty for line in self.order_line_ids)
        notes = self.remarks or "-"

        pdf_content, _fmt = self.env['ir.actions.report']._render_qweb_pdf(
            'laundry_management.action_laundry_quotation', res_ids=self.id)
        filename = f"Proforma_{self.name.replace('/', '_')}.pdf"
        media_id = self._wa_upload_pdf(pdf_content, filename)

        self._send_whatsapp_template(
            self.partner_id.phone, 'proforma',
            [self.name, total_qty, notes, self._wa_money(self.amount_total)],
            document=(media_id, filename))

        self._wa_mark_sent("Proforma Invoice")
        self._update_tracker(5)
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'laundry.order',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'current',
        }

    # ------------------------------------------------------------------
    # Stage 6 - Work started   (template to create: order_work_started)
    # {{1}} order
    # ------------------------------------------------------------------
    def action_send_work_started_whatsapp(self):
        self.ensure_one()
        self._send_whatsapp_template(
            self.partner_id.phone, 'work_started',
            [self.name])
        self._wa_mark_sent("Work Started")

    # ------------------------------------------------------------------
    # Stage 8 - Out for delivery  (template to create: order_out_for_delivery)
    # {{1}} order, {{2}} invoice, {{3}} agent
    # ------------------------------------------------------------------
    def action_send_out_for_delivery_whatsapp(self):
        self.ensure_one()
        tracker = self.tracker_ids.filtered(lambda t: t.sequence == 8)
        agent = tracker.staff_id.name or "Our Delivery Partner"
        self._send_whatsapp_template(
            self.partner_id.phone, 'out_delivery',
            [self.name, self._wa_invoice_name(), agent])
        self._wa_mark_sent("Out for Delivery")

    # ------------------------------------------------------------------
    # Stage 9 - Delivered
    # {{1}} order, {{2}} invoice, {{3}} delivered on, {{4}} grand total
    # ------------------------------------------------------------------
    def action_send_delivered_whatsapp(self):
        for rec in self:
            rec._send_whatsapp_template(
                rec.partner_id.phone, 'delivered',
                [rec.name, rec._wa_invoice_name(),
                 rec._wa_today(), rec._wa_money(rec.grand_total)])
            rec._wa_mark_sent("Order Delivered")
        return True

    # ------------------------------------------------------------------
    # Stage 10 - Payment received
    # {{1}} order, {{2}} invoice, {{3}} amount received, {{4}} date
    # ------------------------------------------------------------------
    def action_send_payment_received_whatsapp(self):
        self.ensure_one()
        self._send_whatsapp_template(
            self.partner_id.phone, 'payment',
            [self.name, self._wa_invoice_name(),
             self._wa_money(self.grand_total), self._wa_today()])
        self._wa_mark_sent("Payment Received")

    # ------------------------------------------------------------------
    # Stage 11 - Feedback
    # {{1}} order, {{2}} invoice
    # ------------------------------------------------------------------
    def action_send_feedback_whatsapp(self):
        self.ensure_one()
        self._send_whatsapp_template(
            self.partner_id.phone, 'feedback',
            [self.name, self._wa_invoice_name()])
        self._wa_mark_sent("Feedback Request")

    # Hidden/unused button - overridden so no old token remains in use.
    def action_send_received_whatsapp(self):
        raise UserError(_("This notification is no longer used."))


class AccountMoveWhatsapp(models.Model):
    _inherit = 'account.move'

    # ------------------------------------------------------------------
    # Stage 7 - Final invoice (PDF header)
    # {{1}} order, {{2}} invoice, {{3}} invoice amount
    # ------------------------------------------------------------------
    def action_send_invoice_whatsapp(self):
        self.ensure_one()
        if self.state != 'posted':
            raise UserError(_("Post the invoice before sending it on WhatsApp."))
        Order = self.env['laundry.order']

        pdf_content, _fmt = self.env['ir.actions.report']._render_qweb_pdf(
            'account.account_invoices', res_ids=self.id)
        filename = f"{self.name.replace('/', '_')}.pdf"
        media_id = Order._wa_upload_pdf(pdf_content, filename)

        Order._send_whatsapp_template(
            self.partner_id.phone, 'final_invoice',
            [self.laundry_order_id.name or 'N/A', self.name,
             f"₹{round(self.display_grand_total, 0):,.2f}"],
            document=(media_id, filename))

        self.is_whatsapp_sent = True
        self.message_post(body="✅ Invoice PDF sent via WhatsApp.")
        if self.laundry_order_id:
            self.laundry_order_id.invoice_whatsapp_shared = True
            self.laundry_order_id._update_tracker(7)


class LaundryOrderTrackerWhatsapp(models.Model):
    _inherit = 'laundry.order.tracker'

    INVOICE_STAGE_SEQUENCE = 7

    def write(self, vals):
        if vals.get('wa_sent') is True or vals.get('completed') is True:
            for tracker in self:
                if tracker.sequence != self.INVOICE_STAGE_SEQUENCE:
                    continue
                order = tracker.order_id
                invoice = order.invoice_id if order else False

                if invoice and invoice.is_whatsapp_sent:
                    continue  # invoice already sent, nothing to ask for

                if not invoice:
                    raise UserError(_(
                        "Thank you for completing the work! 🙏\n\n"
                        "Before this stage can be marked, please:\n"
                        "1. Open the order and click \"Create Invoice\".\n"
                        "2. Open the invoice and confirm it.\n"
                        "3. Click \"Send via WhatsApp\" on the invoice page.\n\n"
                        "This stage will be ticked automatically once the invoice is sent."
                    ))
                raise UserError(_(
                    "The invoice %s is ready. 🙏\n\n"
                    "Please open it, confirm it if it is still in draft, and click "
                    "\"Send via WhatsApp\" on the invoice page.\n\n"
                    "This stage will be ticked automatically once the invoice is sent."
                ) % invoice.name)
        return super().write(vals)