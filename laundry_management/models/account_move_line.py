from odoo import models, fields, api

class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    service_type_id = fields.Many2one(
        'laundry.service.type',
        string="Service Type"
    )

    premium_id = fields.Many2one(
        'laundry.premium',
        string="Premium Type"
    )

    premium_multiplier = fields.Float(string="Premium Multiplier")

    pricing_type = fields.Selection([
        ('per_item', 'Per Item'),
        ('per_kg', 'Per KG')
    ], string="Pricing Type")

    weight = fields.Float(string="Weight")

    price_tax = fields.Monetary(
        string="Price Tax",
        compute="_compute_custom_amounts",
        store=True,
        currency_field='currency_id'
    )

    @api.depends('price_total', 'price_subtotal')
    def _compute_custom_amounts(self):
        for line in self:
            # Using price_subtotal (the standard Odoo field for price_unit * qty)
            line.price_tax = line.price_total - line.price_subtotal