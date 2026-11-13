# -*- coding: utf-8 -*-
from odoo import models, fields


class ResPartnerWa(models.Model):
    _inherit = 'res.partner'

    wa_unread_count = fields.Integer(string="New WhatsApp", default=0, copy=False)
    wa_last_inbound = fields.Datetime(string="Last WhatsApp reply", copy=False)

    def action_wa_mark_read(self):
        self.sudo().write({'wa_unread_count': 0})
        return True