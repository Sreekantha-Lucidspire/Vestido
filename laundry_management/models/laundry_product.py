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

    account_id = fields.Many2one(
	    'account.account',
	    string="Income Account",
	    domain="[('account_type','=','income')]"
	)