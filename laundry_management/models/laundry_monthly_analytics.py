# =============================================
# MONTHLY REVENUE ANALYTICS MODEL
# =============================================
# ADD THIS TO YOUR laundry_order.py FILE
# Place it BEFORE the LaundryOrderTracker class definition

from odoo import models, fields, api
from datetime import datetime

class LaundryMonthlyAnalytics(models.TransientModel):
    _name = 'laundry.monthly.analytics'
    _description = 'Laundry Monthly Revenue Analytics'
    _order = 'year desc, month desc'

    year = fields.Integer(string="Year")
    month = fields.Integer(string="Month")  # 1-12
    month_name = fields.Char(
        string="Month",
        compute='_compute_month_name',
        store=True
    )
    year_month = fields.Char(
        string="Period",
        compute='_compute_year_month',
        store=True
    )

    order_count = fields.Integer(string="Total Orders")
    delivered_count = fields.Integer(string="Delivered Orders")
    invoiced_amount = fields.Float(string="Revenue (Invoiced)")
    total_amount = fields.Float(string="Total Amount (₹)")

    # Additional breakdown (optional - for product/service specific view)
    product_id = fields.Many2one('laundry.product', string="Product (Optional)")
    service_type_id = fields.Many2one('laundry.service.type', string="Service (Optional)")

    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=True
    )

    @api.depends('year', 'month')
    def _compute_month_name(self):
        """Convert month number to month name"""
        month_names = {
            1: 'January', 2: 'February', 3: 'March', 4: 'April',
            5: 'May', 6: 'June', 7: 'July', 8: 'August',
            9: 'September', 10: 'October', 11: 'November', 12: 'December'
        }
        for rec in self:
            rec.month_name = month_names.get(rec.month, '')

    @api.depends('year', 'month')
    def _compute_year_month(self):
        """Combine year and month for display"""
        for rec in self:
            rec.year_month = f"{rec.month_name} {rec.year}" if rec.month and rec.year else ""

    def _compute_currency_id(self):  
        for rec in self:
            rec.currency_id = self.env.company.currency_id  

    def action_generate_monthly_analytics(self, year_filter=None, month_filter=None):
        """
        Compute monthly revenue & order data from laundry.order records.
        
        Data includes:
        - Total orders created in the month
        - Delivered orders count
        - Invoiced amount (only from posted/paid invoices)
        - Total amount (all orders, regardless of state)
        """
        self.search([]).unlink()  # Clear previous analytics

        orders = self.env['laundry.order'].search([])

        # Group orders by (year, month)
        monthly_data = {}
        for order in orders:
            if not order.order_date:
                continue

            year = order.order_date.year
            month = order.order_date.month
            key = (year, month)

            if key not in monthly_data:
                monthly_data[key] = {
                    'order_count': 0,
                    'delivered_count': 0,
                    'invoiced_amount': 0.0,
                    'total_amount': 0.0,
                }

            monthly_data[key]['order_count'] += 1
            
            # Count delivered orders
            if order.state == 'delivered':
                monthly_data[key]['delivered_count'] += 1
            
            # Sum invoiced revenue (only from posted invoices)
            if order.invoice_id and order.invoice_id.state in ['posted', 'paid']:
                monthly_data[key]['invoiced_amount'] += order.amount_total
            
            # Total amount (all orders)
            monthly_data[key]['total_amount'] += order.amount_total

        # Create analytics records for display
        for (year, month), data in monthly_data.items():
            if year_filter and year != year_filter:
                continue
            if month_filter and month != month_filter:
                continue

            self.create({
                'year': year,
                'month': month,
                'order_count': data['order_count'],
                'delivered_count': data['delivered_count'],
                'invoiced_amount': data['invoiced_amount'],
                'total_amount': data['total_amount'],
            })

        return {
            'type': 'ir.actions.act_window',
            'name': 'Monthly Revenue Analytics',
            'res_model': 'laundry.monthly.analytics',
            'view_mode': 'graph,pivot,list',
            'target': 'current',
            'context': {'search_default_filter_past_12_months': 1},
        }

    def action_refresh(self):
        """Quick refresh button for the report"""
        return self.action_generate_monthly_analytics()

    def get_current_year_analytics(self):
        """Show only current fiscal year"""
        today = fields.Date.today()
        current_year = today.year
        # Adjust for fiscal year starting April
        if today.month >= 4:
            fy_year = current_year
        else:
            fy_year = current_year - 1
        return self.action_generate_monthly_analytics(year_filter=fy_year)


class LaundryMonthlyProductAnalytics(models.TransientModel):
    """
    Breakdown: Revenue by Product/Service per Month
    Answers: "Which product/service generates most revenue?"
    """
    _name = 'laundry.monthly.product.analytics'
    _description = 'Product-wise Monthly Analytics'
    _order = 'year desc, month desc, revenue desc'

    year = fields.Integer(string="Year")
    month = fields.Integer(string="Month")
    product_id = fields.Many2one('laundry.product', string="Product")
    service_type_id = fields.Many2one('laundry.service.type', string="Service Type")
    
    order_count = fields.Integer(string="Orders")
    quantity = fields.Float(string="Qty/Weight")  # Either count or weight, depending on pricing type
    revenue = fields.Float(string="Revenue (₹)", help="Based on order line subtotal")
    
    currency_id = fields.Many2one(
        'res.currency',
        compute='_compute_currency_id',
        store=True
    )


    def _compute_currency_id(self): 
        for rec in self:
            rec.currency_id = self.env.company.currency_id

    def action_generate_product_analytics(self):
        """
        Generate product-wise breakdown for each month.
        Useful for: "Which product is our top revenue generator?"
        """
        self.search([]).unlink()

        orders = self.env['laundry.order'].search([])

        # Structure: {(year, month, product_id, service_type_id): {...data...}}
        product_monthly = {}

        for order in orders:
            if not order.order_date:
                continue

            year = order.order_date.year
            month = order.order_date.month

            for line in order.order_line_ids:
                key = (year, month, line.product_id.id, line.service_type_id.id)
                
                if key not in product_monthly:
                    product_monthly[key] = {
                        'order_count': 0,
                        'quantity': 0.0,
                        'revenue': 0.0,
                    }

                product_monthly[key]['order_count'] += 1
                qty = line.qty if line.pricing_type == 'per_item' else line.weight
                product_monthly[key]['quantity'] += qty
                product_monthly[key]['revenue'] += line.subtotal

        # Create records
        for (year, month, product_id, service_id), data in product_monthly.items():
            self.create({
                'year': year,
                'month': month,
                'product_id': product_id,
                'service_type_id': service_id,
                'order_count': data['order_count'],
                'quantity': data['quantity'],
                'revenue': data['revenue'],
            })

        return {
            'type': 'ir.actions.act_window',
            'name': 'Product-wise Revenue by Month',
            'res_model': 'laundry.monthly.product.analytics',
            'view_mode': 'pivot,graph,list',
            'target': 'current',
        }