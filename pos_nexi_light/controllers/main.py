# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import json
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)


class PosNexiController(http.Controller):
    """Controller for handling NEXI webhook notifications.
    
    NEXI sends notifications to this endpoint when:
    - A payment is completed on the terminal
    - A payment fails
    - The terminal status changes
    """

    @http.route('/pos_nexi_light/notification/<int:payment_method_id>/<string:token>',
                type='json', methods=['POST'],
                auth='public', csrf=False, save_session=False)
    def notification(self, payment_method_id, token):
        """Process notification from NEXI.

        Args:
            payment_method_id: The ID of the payment method (terminal)
            token: Webhook security token for verification

        Returns:
            '[accepted]' to acknowledge receipt
        """
        # Get request data
        try:
            data = request.get_json_data()
        except Exception:
            _logger.warning('NEXI notification: Invalid JSON received')
            return '[accepted]'  # Acknowledge to stop retries

        _logger.info('NEXI notification received for payment method %s',
                     payment_method_id)

        # Find the payment method
        payment_method = request.env['pos.payment.method'].sudo().browse(payment_method_id)
        if not payment_method.exists() or payment_method.use_payment_terminal != 'nexi':
            _logger.warning('NEXI notification: Payment method %s not found or not NEXI',
                          payment_method_id)
            return '[accepted]'

        # Verify webhook token
        if not token or not payment_method.nexi_webhook_token or token != payment_method.nexi_webhook_token:
            _logger.warning('NEXI notification: Invalid token for payment method %s',
                          payment_method_id)
            return '[accepted]'
        
        # Extract transaction info
        external_id = data.get('external_id')
        state = data.get('state')
        result_code = data.get('result_code')

        if not external_id:
            _logger.warning('NEXI notification: Missing external_id')
            return '[accepted]'

        # Extract card/payment details from notification
        pmd = data.get('payment_method_details') or {}
        card_details = {}
        for key in ('card_scheme', 'authorization_code', 'authorization_response_code',
                     'card_entry_mode', 'card_number_customer', 'card_number_merchant',
                     'cardholder_verification', 'application_name', 'application_identifier',
                     'acquirer_reference', 'acquirer_terminal_id', 'acquirer_timestamp'):
            if pmd.get(key):
                card_details[key] = pmd[key]

        # Prepare response data for POS
        notification_data = {
            'external_id': external_id,
            'state': state,
            'result_code': result_code,
            'timestamp': datetime.now().isoformat(),
            'reference': data.get('reference', ''),
            'receipt_details_customer': data.get('receipt_details_customer', ''),
            'receipt_details_merchant': data.get('receipt_details_merchant', ''),
            'authorized_amount': data.get('authorized_amount'),
            'captured_amount': data.get('captured_amount'),
            **card_details,
            'raw_data': data,
        }
        
        # Store the notification (for POS to pick up)
        payment_method.write({
            'nexi_latest_response': json.dumps(notification_data),
        })
        
        # Update terminal status
        if state in ('awaiting_confirm', 'confirmed'):
            payment_method.write({
                'nexi_terminal_status': 'online',
            })
        elif state == 'failed':
            payment_method.write({
                'nexi_terminal_status': 'error',
            })
        
        # Notify the POS session
        # Find active POS sessions using this payment method
        config_ids = request.env['pos.config'].sudo().search([
            ('payment_method_ids', 'in', payment_method.id),
            ('has_active_session', '=', True),
        ])
        
        for config in config_ids:
            config._notify('NEXI_LATEST_RESPONSE', {
                'config_id': config.id,
                'payment_method_id': payment_method.id,
                'external_id': external_id,
                'state': state,
                'result_code': result_code,
                'card_scheme': card_details.get('card_scheme', ''),
                'authorization_code': card_details.get('authorization_code', ''),
                'card_number_customer': card_details.get('card_number_customer', ''),
                'card_entry_mode': card_details.get('card_entry_mode', ''),
                'reference': data.get('reference', ''),
            })
        
        _logger.info('NEXI notification processed for transaction %s, state: %s',
                     external_id, state)
        
        return '[accepted]'

    @http.route('/pos_nexi_light/test', type='http', methods=['GET'], auth='public')
    def test_endpoint(self):
        """Simple test endpoint to verify connectivity."""
        return "NEXI POS Integration is running!"
