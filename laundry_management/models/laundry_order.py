from odoo import models, fields, api
from odoo.exceptions import ValidationError,UserError


class LaundryOrder(models.Model):
    _name = 'laundry.order'
    _description = 'Laundry Order'

    name = fields.Char(default='New', copy=False)
    partner_id = fields.Many2one('res.partner', required=True)
    email = fields.Char(string="Email",related="partner_id.email")
    phone = fields.Char(string="Phone",related="partner_id.phone")
    order_date = fields.Datetime(default=fields.Datetime.now)
    delivery_date = fields.Datetime(string="Delivery Date")
    remarks = fields.Text(string="Remarks")

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True
    )
    user_id = fields.Many2one(
        'res.users',
        string="Responsible",
        default=lambda self: self.env.user,
        tracking=True
    )
    active = fields.Boolean(default=True)
    discount = fields.Monetary(string="Discount")
    grand_total = fields.Monetary(
        string="Grand Total",
        compute="_compute_grand_total",
        store=True
    )
    document_ids = fields.One2many(
        'laundry.order.document',
        'order_id',
        string="Images"
    )

    order_line_ids = fields.One2many(
        'laundry.order.line', 'order_id')

    total_weight = fields.Float(compute='_compute_total_weight', store=True)
    amount_total = fields.Float(compute='_compute_total', store=True)

    state = fields.Selection([
        ('draft', 'Draft'),
        ('received', 'Received'),
        ('washing', 'Washing'),
        ('ironing', 'Ironing'),
        ('ready', 'Ready'),
        ('delivered', 'Delivered'),
    ], default='draft')

    invoice_id = fields.Many2one('account.move')

    tag_type = fields.Selection([('order', 'Per Order'),('line', 'Per Line')], default='order')

    tag_ids = fields.One2many('laundry.tag', 'order_id')
    tag_count = fields.Integer(compute='_compute_tag_count')

    has_wash = fields.Boolean(compute='_compute_service_flags')
    has_iron = fields.Boolean(compute='_compute_service_flags')

    amount_untaxed = fields.Float( string="Untaxed Amount",compute='_compute_amounts', store=True)
    amount_tax = fields.Float( string="Tax Amount",compute='_compute_amounts', store=True)
    amount_total = fields.Float( string="Total",compute='_compute_amounts', store=True)

    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=True
    )


    @api.depends('order_line_ids.subtotal', 'order_line_ids.price_tax','discount')
    def _compute_amounts(self):
        for order in self:
            order.amount_untaxed = sum(order.order_line_ids.mapped('subtotal'))
            order.amount_tax = sum(order.order_line_ids.mapped('price_tax'))
            order.amount_total = order.amount_untaxed + order.amount_tax
            
    def action_print_invoice_bill(self):
        """ Method to print the 80mm Invoice Bill report """
        self.ensure_one()
        return self.env.ref('laundry_management.action_laundry_invoice_bill').report_action(self)
        

    @api.depends('amount_total', 'discount')
    def _compute_grand_total(self):
        for rec in self:
            rec.grand_total = rec.amount_total - rec.discount

    @api.depends('company_id')
    def _compute_currency_id(self):
        for rec in self:
            rec.currency_id = rec.company_id.currency_id


    @api.depends('order_line_ids.service_type_id')
    def _compute_service_flags(self):
        for rec in self:
            has_wash = False
            has_iron = False

            for line in rec.order_line_ids:
                op = line.service_type_id.operation_type

                if op in ['wash', 'wash_iron', 'dry_clean']:
                    has_wash = True

                if op in ['iron', 'wash_iron']:
                    has_iron = True

            rec.has_wash = has_wash
            rec.has_iron = has_iron



    def _compute_tag_count(self):
        for rec in self:
            rec.tag_count = len(rec.tag_ids)

    def action_generate_tags(self):
        for rec in self:

            if rec.tag_ids:
                raise ValidationError("Tags already generated. You can only print them.")

            # Clear old tags
            # rec.tag_ids.unlink()

            if rec.tag_type == 'order':
                self.env['laundry.tag'].create({
                    'order_id': rec.id,
                    'barcode': rec.name,
                })

            elif rec.tag_type == 'line':
                for line in rec.order_line_ids:
                    self.env['laundry.tag'].create({
                        'order_id': rec.id,
                        'order_line_id': line.id,
                        'barcode': f"{rec.name}-{line.id}",
                    })
    def action_regenerate_tags(self):
        for rec in self:
            rec.tag_ids.unlink()

    def action_print_tags(self):
        return self.env.ref(
            'laundry_management.action_laundry_tag_30mm'
        ).report_action(self.tag_ids)

    def action_print_receipt_80mm(self):
        return self.env.ref(
            'laundry_management.action_laundry_receipt_80mm'
        ).report_action(self)

    def action_view_tags(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Tags',
            'res_model': 'laundry.tag',
            'view_mode': 'list,form',
            'domain': [('order_id', '=', self.id)],
        }


    # 🔹 Auto sequence
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('laundry.order') or 'New'
        return super().create(vals_list)

    # 🔹 Compute total
    @api.depends('order_line_ids.subtotal')
    def _compute_total(self):
        for rec in self:
            rec.amount_total = sum(line.subtotal for line in rec.order_line_ids)


    # 🔹 5KG validation
    #@api.constrains('total_weight')
    #def _check_min_weight(self):
    #    for rec in self:
    #        if rec.total_weight and rec.total_weight < 5:
    #           raise ValidationError("Minimum 5 KG required")
    @api.onchange('total_weight')
    def _onchange_total_weight(self):
        if self.total_weight and self.total_weight < 4:
            return {
                'warning': {
                     'title': "Minimum Weight",
                     'message': "Minimum recommended weight is 4 KG, you can still proceed."                
                }
            }

    @api.depends('order_line_ids.weight')
    def _compute_total_weight(self):
        for rec in self:
            rec.total_weight = sum(line.weight for line in rec.order_line_ids)


    def action_received(self):
        for rec in self:
            rec.state = 'received'

    def action_washing(self):
        for rec in self:
            if not rec.tag_ids:
                raise UserError("Generate Tags before proceeding with this action")
            if not rec.has_wash:
                raise ValidationError("No washing required for this order.")

            rec.state = 'washing'

    def action_ironing(self):
        for rec in self:
            if not rec.tag_ids:
                raise UserError("Generate Tags before proceeding with this action")
            if not rec.has_iron:
                raise ValidationError("No ironing required for this order.")

            rec.state = 'ironing'

    def action_ready(self):
        for rec in self:

            # CASE 1: Wash + Iron → must complete ironing
            if rec.has_wash and rec.has_iron:
                if rec.state != 'ironing':
                    raise ValidationError("Complete ironing before marking ready.")

            # CASE 2: Only Wash
            elif rec.has_wash and not rec.has_iron:
                if rec.state != 'washing':
                    raise ValidationError("Complete washing before marking ready.")

            # CASE 3: Only Iron
            elif rec.has_iron and not rec.has_wash:
                if rec.state != 'ironing':
                    raise ValidationError("Complete ironing before marking ready.")

            rec.state = 'ready'

    def action_delivered(self):
        self.state = 'delivered'


    def get_dashboard_data(self):

        result = {}

        orders = self.search([])

        for order in orders:
            for line in order.order_line_ids:

                key = (line.product_id.id, line.service_type_id.id)

                if key not in result:
                    result[key] = {
                        'product_id': line.product_id.id,
                        'service_type_id': line.service_type_id.id,
                        'pending': 0,
                        'progress': 0,
                        'done': 0,
                    }

                state = order.state

                if state in ['draft', 'received']:
                    bucket = 'pending'
                elif state in ['washing', 'ironing']:
                    bucket = 'progress'
                else:
                    bucket = 'done'

                result[key][bucket] += 1

        return list(result.values())


    def _get_or_create_product(self,laundry_product,service_type_id):

        Product = self.env['product.product']

        # 🔹 Unique product code
        default_code = (
            f"LAUNDRY_"
            f"{laundry_product.id}_"
            f"{service_type_id.id}"
        )

        # 🔹 Try existing product
        product = Product.search([
            ('default_code', '=', default_code)
        ], limit=1)

        if product:
            return product

        # 🔹 Validate income account
        account = (service_type_id.income_account_id)

        if not account:
            raise ValidationError(
                f"Please configure income account "
                f"for service '{service_type_id.name}'"
            )

        # 🔹 Product Name
        product_name = (f"{laundry_product.name}")

        # 🔹 Create product
        product = Product.create({
            'name': product_name,
            'type': 'service',
            'default_code': default_code,
            'list_price': 0.0,
            'property_account_income_id': account.id,
        })

        return product

    def action_create_invoice(self):
        for rec in self:

            if not rec.order_line_ids:
                continue

            # 🔹 Get Sales Journal
            journal = self.env['account.journal'].search([
                ('type', '=', 'sale'),
                ('company_id', '=', rec.company_id.id)
            ], limit=1)

            if not journal:
                raise ValidationError("Please configure a Sales Journal.")

            invoice_lines = []

            for line in rec.order_line_ids:

                # 🔹 Get or create product dynamically
                product = self._get_or_create_product(
                    line.product_id,
                    line.service_type_id
                )

                # 🔹 Quantity logic
                qty = line.qty if line.pricing_type == 'per_item' else line.weight

                # 🔹 Auto naming with service type
                invoice_lines.append((0, 0, {
                     'product_id': product.id,
                     'name': f"{line.product_id.name}",
                     'quantity': qty,
                     'price_unit': line.unit_price,
                     'account_id': line.service_type_id.income_account_id.id,
                     'tax_ids': [(6, 0, line.tax_ids.ids)],

                     # CUSTOM VALUES
                     'service_type_id': line.service_type_id.id,
                     'pricing_type': line.pricing_type,
                     'weight': line.weight,
                     'premium_id': line.premium_id.id,
                }))

            # 🔹 Create Invoice
            invoice = self.env['account.move'].create({
                'move_type': 'out_invoice',
                'partner_id': rec.partner_id.id,
                'journal_id': journal.id,
                'invoice_line_ids': invoice_lines,
                'invoice_origin': rec.name,
                'laundry_order_id' : rec.id
            })

            rec.invoice_id = invoice.id

    def action_view_invoice(self):

        self.ensure_one()

        if not self.invoice_id:
            return

        return {
            'type': 'ir.actions.act_window',
            'name': 'Invoice',
            'res_model': 'account.move',
            'view_mode': 'form',

            # 🔥 IMPORTANT
            'res_id': self.invoice_id.id,

            'target': 'current',
        }


# -----------------------------
# ORDER LINE
# -----------------------------
class LaundryOrderLine(models.Model):
    _name = 'laundry.order.line'
    _description = 'Laundry Order Line'

    order_id = fields.Many2one('laundry.order', ondelete='cascade')

    product_id = fields.Many2one('laundry.product', required=True)

    service_type_id = fields.Many2one('laundry.service.type',required=True)
    pricing_id = fields.Many2one('laundry.pricing',readonly=True)

    pricing_type = fields.Selection([
        ('per_item', 'Per Item'),
        ('per_kg', 'Per KG')
    ], required=True)

    qty = fields.Integer()
    weight = fields.Float()
    currency_id = fields.Many2one(
        'res.currency',
        related='order_id.currency_id',
        store=True,
        readonly=True
    )

    unit_price = fields.Monetary(string="Unit Price",currency_field='currency_id')
    premium_id = fields.Many2one('laundry.premium',string="Premium Type",default=lambda self: self.env['laundry.premium'].search([('sequence', '=', 0)], limit=1))
    premium_multiplier = fields.Float(related='premium_id.multiplier')

    tax_ids = fields.Many2many(
        'account.tax',
        string="Taxes",
        domain="[('type_tax_use', '=', 'sale')]",
        # This lambda searches for the 18% tax record automatically
        default=lambda self: self.env['account.tax'].search([
            ('amount', '=', 18.0), 
            ('type_tax_use', '=', 'sale'),
            ('company_id', '=', self.env.company.id)
        ], limit=1)
    )

    subtotal = fields.Monetary(compute='_compute_tax', store=True,currency_field='currency_id')
    price_tax = fields.Monetary(compute='_compute_tax', store=True,currency_field='currency_id')
    price_total = fields.Monetary( string="Total Price",compute='_compute_tax', store=True,currency_field='currency_id')

    @api.depends('qty','weight','unit_price','tax_ids','pricing_type','premium_id','premium_id.multiplier')
    def _compute_tax(self):

        for line in self:

            # 🔹 Quantity logic
            qty = (
                line.qty
                if line.pricing_type == 'per_item'
                else line.weight
            )

            # 🔹 Base price
            price = line.unit_price

            # 🔹 Apply premium multiplier
            if line.premium_id:
                price *= line.premium_id.multiplier

            # 🔹 No taxes
            if not line.tax_ids:

                line.subtotal = qty * price
                line.price_tax = 0.0
                line.price_total = line.subtotal

                continue

            # 🔹 Tax calculation
            taxes = line.tax_ids.compute_all(
                price,
                currency=line.order_id.currency_id,
                quantity=qty,
                product=None,
                partner=line.order_id.partner_id
            )

            # 🔹 Final values
            line.subtotal = taxes['total_excluded']

            line.price_tax = (
                taxes['total_included']
                - taxes['total_excluded']
            )

            line.price_total = taxes['total_included']

    @api.onchange('product_id','service_type_id')
    def fetch_pricing_type(self):
        for line in self:
            if line.product_id and line.service_type_id:
                pricing = self.env['laundry.pricing'].search([
                ('product_id', '=', line.product_id.id),
                ('service_type_id', '=', line.service_type_id)], limit=1)
                if pricing:
                    line.pricing_type = pricing.pricing_type

    # 🔹 Auto price fetch
    @api.onchange('product_id', 'service_type_id', 'pricing_type', 'premium_id')
    def _onchange_price(self):
        for rec in self:
            pricing = self.env['laundry.pricing'].search([
                ('product_id', '=', rec.product_id.id),
                ('service_type_id', '=', rec.service_type_id),
                ('pricing_type', '=', rec.pricing_type)
            ], limit=1)

            rec.unit_price = pricing.price if pricing else 0.0

    # 🔹 subtotal calc
    @api.depends('qty', 'weight', 'unit_price', 'pricing_type', 'premium_id', 'premium_id.multiplier')
    def _compute_subtotal(self):
        for rec in self:

            # 🔹 Step 1: Base subtotal
            if rec.pricing_type == 'per_item':
                subtotal = rec.qty * rec.unit_price
            else:
                subtotal = rec.weight * rec.unit_price

            # 🔹 Step 2: Apply premium
            if rec.premium_id:
                subtotal *= rec.premium_id.multiplier

            # 🔹 Step 3: Assign
            rec.subtotal = subtotal


class LaundryDashboard(models.TransientModel):
    _name = 'laundry.dashboard'
    _description = 'Laundry Dashboard'

    product_id = fields.Many2one('laundry.product', string="Product")
    service_type_id = fields.Many2one('laundry.service.type',string="Service Type")
    pending = fields.Integer()
    progress = fields.Integer()
    done = fields.Integer()

    def action_open_dashboard(self):

        data = self.env['laundry.order'].get_dashboard_data()

        self.search([]).unlink()

        self.create(data)

        return {
            'type': 'ir.actions.act_window',
            'name': 'Laundry Dashboard',
            'res_model': 'laundry.dashboard',
            'view_mode': 'kanban,list',
            'target': 'current',
        }
class LaundryOrderDocument(models.Model):
    _name = 'laundry.order.document'
    _description = 'Laundry Order Image'

    order_id = fields.Many2one(
        'laundry.order',
        string="Order",
        ondelete='cascade'
    )

    name = fields.Char(string="Image Name")

    file = fields.Binary(
        string="Upload File",
        attachment=True
    )

    filename = fields.Char(string="Filename")