from odoo import models, fields

class LaundryServiceType(models.Model):
    _name = 'laundry.service.type'
    _description = 'Laundry Service Type'

    name = fields.Char(required=True)
    code = fields.Char(required=True) 
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True
    )
    hsn = fields.Char(string="HSN")
    income_account_id = fields.Many2one(
	    'account.account',
	    string="Income Account",
	    domain="[('account_type','=','income')]"
	)

    operation_type = fields.Selection([
	    ('wash', 'Wash'),
	    ('iron', 'Iron'),
	    ('wash_iron', 'Wash + Iron'),
	    ('dry_clean', 'Dry Clean'),
	], required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _sql_constraints = [
	    ('unique_code', 'unique(code)', 'Service code must be unique!')
	]