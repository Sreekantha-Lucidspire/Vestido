# -*- coding: utf-8 -*-
import logging

from markupsafe import Markup

from odoo import models, api

_logger = logging.getLogger(__name__)


class LaundryOrderWhatsappNotify(models.Model):
    _inherit = 'laundry.order'

    @api.model
    def _wa_inbox_partners(self, order):
        """Who gets the WhatsApp reply in the chat icon and as a pop-up."""
        partners = self.env['res.partner']
        if order and order.user_id:
            partners |= order.user_id.partner_id
        users = self.env['res.users'].sudo().search([('share', '=', False)])
        for user in users:
            if user.has_group('laundry_management.group_laundry_manager'):
                partners |= user.partner_id
        return partners

    @api.model
    def _wa_push_popup(self, order, who, wa_phone, text):
        """On-screen pop-up for everyone who should see this reply."""
        message = f"{who} (+{wa_phone})"
        if order:
            message += f" - {order.name}"
        message += f": {(text or '')[:120]}"
        for partner in self._wa_inbox_partners(order):
            self.env['bus.bus'].sudo()._sendone(
                partner, 'simple_notification', {
                    'title': "New WhatsApp message",
                    'message': message,
                    'type': 'info',
                    'sticky': True,
                })

    @api.model
    def _wa_store_inbound(self, wamid, wa_phone, profile_name, text):
        partner = self._wa_find_partner(wa_phone)
        order = self.env['laundry.order']
        if partner:
            order = self.sudo().with_context(active_test=False).search(
                [('partner_id', '=', partner.id)], order='id desc', limit=1)
        self.env['laundry.whatsapp.inbound'].sudo().create({
            'wamid': wamid, 'phone': wa_phone, 'body': text,
            'order_id': order.id or False})
        if not partner:
            _logger.warning("WhatsApp reply from %s has no matching customer", wa_phone)
            return

        who = profile_name or partner.name

        # 1) Contact page: the full conversation for this customer (silent note).
        order_ref = Markup(" (latest order %s)") % order.name if order else Markup("")
        partner.sudo().message_post(
            body=Markup("📩 <b>WhatsApp reply</b> from %s (+%s)%s:<br/>%s") % (
                who, wa_phone, order_ref, text),
            message_type='comment', subtype_xmlid='mail.mt_note',
            author_id=partner.id)

        # 2) Order page: notifies the responsible user and managers (chat icon).
        if order:
            order.sudo().message_post(
                body=Markup("📩 <b>WhatsApp reply</b> from %s (+%s):<br/>%s") % (
                    who, wa_phone, text),
                message_type='comment', subtype_xmlid='mail.mt_comment',
                author_id=partner.id,
                partner_ids=self._wa_inbox_partners(order).ids)

        # 3) On-screen pop-up.
        self._wa_push_popup(order, who, wa_phone, text)