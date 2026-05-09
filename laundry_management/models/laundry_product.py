from odoo import models, fields

class LaundryProduct(models.Model):
    _name = 'laundry.product'
    _description = 'Laundry Product (Cloth Type)'

    name = fields.Char(required=True)
    category = fields.Selection([
        ('wearable', 'Wearable'),
        ('household', 'Household')
    ], default='wearable')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True
    )