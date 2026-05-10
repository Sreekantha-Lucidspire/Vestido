from odoo import models, fields

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    service_type_id = fields.Many2one('laundry.service.type',string="Service Type",required=False)