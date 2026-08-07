# -*- coding: utf-8 -*-
from odoo import models, api, fields
from odoo.exceptions import ValidationError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    _phone_unique = models.Constraint(
        'UNIQUE(phone)',
        "This phone number is already used by another contact. "
        "Each contact must have a unique phone number."
    )

    @api.constrains('phone')
    def _check_phone_unique(self):
        for partner in self:
            if not partner.phone:
                continue
            normalized = partner.phone.strip()
            duplicate = self.search([
                ('id', '!=', partner.id),
                ('phone', '=', normalized),
            ], limit=1)
            if duplicate:
                raise ValidationError(
                    f"The phone number '{normalized}' is already assigned "
                    f"to contact '{duplicate.name}'. Please use a different "
                    f"number, or open that existing contact instead."
                )

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        """ Extends the default search (by name) to also match on
            phone, so typing a number in the Partner field finds
            the right contact too. """
        domain = list(args or [])
        if name:
            search_domain = ['|', '|',
                              ('name', operator, name),
                              ('email', operator, name),
                              ('phone', operator, name)] + domain
            partners = self.search(search_domain, limit=limit)
            return [(p.id, p.display_name) for p in partners]
        return super().name_search(name=name, domain=domain, operator=operator, limit=limit)