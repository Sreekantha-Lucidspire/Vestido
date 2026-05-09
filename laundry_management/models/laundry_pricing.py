from odoo import models, fields, api, _

class LaundryPricing(models.Model):
    _name = 'laundry.pricing'
    _description = 'Laundry Pricing'

    product_id = fields.Many2one('laundry.product', required=True)
    name = fields.Char(string="Name",compute="_update_name")
    service_type = fields.Selection([
        ('iron', 'Steam Iron'),
        ('wash', 'Wash & Fold'),
        ('wash_iron', 'Wash & Iron'),
        ('dry_clean', 'Dry Cleaning')
    ], required=True)

    pricing_type = fields.Selection([
        ('per_item', 'Per Item'),
        ('per_kg', 'Per KG')
    ], required=False,default="per_item")

    price = fields.Float(required=True)

    @api.depends('product_id', 'price', 'service_type')
    def _update_name(self):
        for line in self:
            if line.product_id and line.price:
                line.name = f"{line.product_id.name} - {line.price}"
            else:
                line.name = False

# class LaundryPricing(models.Model):
#     _name = 'laundry.pricing'
#     _description = 'Laundry Pricing'

#     product_id = fields.Many2one('laundry.product', required=True)
#     service_type = fields.Selection([
#         ('wash', 'Wash'),
#         ('wash_iron', 'Wash + Iron'),
#         ('iron', 'Iron'),
#     ], required=True)

#     normal_price = fields.Float(required=True)
#     premium_price = fields.Float(required=True)

#     initial_offer_price = fields.Float()

#     _sql_constraints = [
#         ('unique_product_service',
#          'unique(product_id, service_type)',
#          'Pricing already defined for this product and service!')
#     ]