from odoo import models, fields


class LaundryPremium(models.Model):
    _name = 'laundry.premium'
    _description = 'Laundry Premium Pricing'

    name = fields.Char(required=True)
    multiplier = fields.Float(required=True, default=1.0)
    sequence = fields.Integer(string="Sequence")

    active = fields.Boolean(default=True)
    