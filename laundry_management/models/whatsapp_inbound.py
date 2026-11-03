# -*- coding: utf-8 -*-
from odoo import models, fields


class LaundryWhatsappInbound(models.Model):
    _name = 'laundry.whatsapp.inbound'
    _description = 'WhatsApp inbound message log'
    _order = 'id desc'

    wamid = fields.Char(required=True, index=True)
    phone = fields.Char()
    body = fields.Text()
    order_id = fields.Many2one('laundry.order', ondelete='set null')