from odoo import models, fields, api,_

class LaundryProduct(models.Model):
    _name = 'laundry.product'
    _description = 'Laundry Product (Cloth Type)'

    name = fields.Char(required=True)
    category = fields.Selection([
        ('wearable', 'Wearable'),
        ('household', 'Household'),
    ], default='wearable')
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True
    )

    product_type = fields.Selection([
        ('service', 'Service'),
        ('charge', 'Charge'),
        ('discount', 'Discount'),
    ], default='service')

    income_account_id = fields.Many2one(
        'account.account',
        string="Income Account",
        domain="[('account_type','=','income')]"
    )

    expense_account_id = fields.Many2one(
        'account.account',
        string="Expense Account",
        domain="[('account_type','=','expense')]"
    )

    @api.onchange('product_type')
    def update_category(self):
        for line in self:
            if line.product_type != 'service':
                line.category = False