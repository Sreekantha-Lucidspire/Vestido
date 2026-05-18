from odoo import models, fields, api

class AccountMove(models.Model):
    _inherit = "account.move"

    pickup_delivery = fields.Char(string="Pickup & Delivery")
    invoice_discount = fields.Float(string="Discount")
    laundry_order_id = fields.Many2one('laundry.order','Laundry Ref',tracking=True)

    @api.depends('invoice_line_ids.price_subtotal', 'invoice_discount')
    def _compute_amount(self):
        super()._compute_amount()

        for move in self:
            move.amount_untaxed = max(move.amount_untaxed - move.invoice_discount, 0)
            move.amount_total = move.amount_untaxed + move.amount_tax

