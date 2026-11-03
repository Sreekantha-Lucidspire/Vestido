# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class WhatsappWebhook(http.Controller):

    @http.route('/whatsapp/webhook', type='http', auth='public',
                methods=['GET'], csrf=False)
    def verify(self, **kw):
        args = request.httprequest.args
        token = request.env['ir.config_parameter'].sudo().get_param('laundry.wa_verify_token') or ''
        given = args.get('hub.verify_token') or ''
        if args.get('hub.mode') == 'subscribe' and token and hmac.compare_digest(given, token):
            return request.make_response(
                args.get('hub.challenge', ''), headers=[('Content-Type', 'text/plain')])
        return request.make_response('Forbidden', status=403)

    @http.route('/whatsapp/webhook', type='http', auth='public',
                methods=['POST'], csrf=False)
    def receive(self, **kw):
        raw = request.httprequest.get_data()
        secret = request.env['ir.config_parameter'].sudo().get_param('laundry.wa_app_secret') or ''
        sig = request.httprequest.headers.get('X-Hub-Signature-256', '')
        expected = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(sig, expected):
            return request.make_response('Invalid signature', status=403)
        try:
            request.env['laundry.order'].sudo()._wa_handle_webhook(json.loads(raw))
        except Exception:
            _logger.exception("WhatsApp webhook processing failed")
        return request.make_response('OK', status=200)