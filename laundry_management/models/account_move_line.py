from odoo import models, fields

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    service_type = fields.Selection([
        ('iron', 'Steam Iron'),
        ('wash', 'Wash & Fold'),
        ('wash_iron', 'Wash & Iron'),
        ('dry_clean', 'Dry Cleaning'),
    ], string="Service Type")