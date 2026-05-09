from odoo import models, fields, api


class LaundryTag(models.Model):
    _name = 'laundry.tag'
    _description = 'Laundry Tag'

    name = fields.Char(default='New', copy=False)
    order_id = fields.Many2one('laundry.order', required=True)
    order_line_id = fields.Many2one('laundry.order.line')

    partner_id = fields.Many2one(
        'res.partner',
        related='order_id.partner_id',
        store=True
    )

    barcode = fields.Char()
    state = fields.Selection([
        ('draft', 'Draft'),
        ('washing', 'Washing'),
        ('ready', 'Ready'),
        ('delivered', 'Delivered')
    ], default='draft')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals.get('name') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('laundry.tag') or 'New'
        return super().create(vals_list)