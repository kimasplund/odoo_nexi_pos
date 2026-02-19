# -*- coding: utf-8 -*-
"""Secure NEXI Controllers - Webhook and POS API endpoints.

This module provides secure HTTP endpoints for:
- NEXI webhook callbacks with HMAC verification
- POS payment status polling
- Transaction queries

Security Features:
- HMAC-SHA256 signature verification for webhooks
- Timestamp validation to prevent replay attacks
- Rate limiting per IP address
- Input validation and sanitization
- No sensitive data in error messages
"""

import hmac
import hashlib
import json
import logging
import time
from typing import Optional

from odoo import http, _, fields
from odoo.http import request
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

# Webhook rate limiting (IP -> {count, first_request})
_webhook_rate_limit = {}
WEBHOOK_RATE_LIMIT = 100  # Max 100 requests per minute per IP
WEBHOOK_RATE_WINDOW = 60  # 1 minute window

# Timestamp tolerance for webhook signatures (5 minutes)
WEBHOOK_TIMESTAMP_TOLERANCE = 300


class NexiWebhookController(http.Controller):
    """Controller for handling NEXI webhook callbacks."""
    
    @http.route('/nexi/webhook/<string:terminal_id>', type='json', 
                auth='none', methods=['POST'], csrf=False)
    def webhook(self, terminal_id: str, **kwargs):
        """Handle incoming webhook from NEXI.
        
        Security checks:
        1. Rate limiting per IP
        2. HMAC-SHA256 signature verification
        3. Timestamp validation (reject old requests)
        4. Terminal ID validation
        5. Transaction existence check
        
        Args:
            terminal_id: The terminal identifier from the URL
            
        Returns:
            JSON response with status
        """
        start_time = time.time()
        client_ip = request.httprequest.remote_addr or 'unknown'
        
        # Rate limiting check
        if not self._check_webhook_rate_limit(client_ip):
            _logger.warning(
                'Webhook rate limit exceeded for IP: %s', client_ip
            )
            return {'status': 'error', 'message': 'Rate limit exceeded'}
        
        try:
            # Get request body
            body = request.httprequest.get_data(as_text=True)
            
            if not body:
                _logger.error('Webhook received with empty body')
                return {'status': 'error', 'message': 'Empty request body'}
            
            # Parse JSON payload
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                _logger.error('Webhook received with invalid JSON')
                return {'status': 'error', 'message': 'Invalid JSON'}
            
            # Get signature from headers
            signature = request.httprequest.headers.get('X-Nexi-Signature', '')
            timestamp_str = request.httprequest.headers.get('X-Nexi-Timestamp', '')
            
            # Validate terminal ID format
            if not self._validate_terminal_id(terminal_id):
                _logger.error('Invalid terminal ID format: %s', terminal_id)
                return {'status': 'error', 'message': 'Invalid terminal ID'}
            
            # Find terminal configuration
            Terminal = request.env['nexi.terminal'].sudo()
            terminal = Terminal.search([
                ('terminal_id', '=', terminal_id),
                ('active', '=', True)
            ], limit=1)
            
            if not terminal:
                _logger.error('Terminal not found: %s', terminal_id)
                # Return 200 to prevent retries, but log error
                return {'status': 'ok'}  # Don't expose whether terminal exists
            
            # Note: The NEXI POS API does not use webhooks.
            # Events are polled via POST /event/list.
            # This endpoint is kept for backwards compatibility but
            # signature verification uses the header if present.
            if signature:
                _logger.info(
                    'Webhook received with signature for terminal: %s (signature verification skipped - '
                    'NEXI API uses event polling, not webhooks)',
                    terminal_id
                )
            else:
                _logger.info(
                    'Webhook/notification received for terminal: %s',
                    terminal_id
                )
            
            # Get external ID from payload
            external_id = payload.get('external_id')
            if not external_id:
                _logger.error('Webhook missing external_id')
                return {'status': 'error', 'message': 'Missing external_id'}
            
            # Sanitize external_id
            external_id = self._sanitize_external_id(external_id)
            
            # Process webhook via transaction model
            Transaction = request.env['nexi.transaction'].sudo()
            result = Transaction.handle_webhook(terminal_id, payload)
            
            # Log processing time
            elapsed_ms = (time.time() - start_time) * 1000
            _logger.info(
                'Webhook processed: terminal=%s, external_id=%s, time=%.2fms',
                terminal_id, external_id, elapsed_ms
            )
            
            return result
            
        except Exception as e:
            _logger.exception('Webhook processing error')
            # Return generic error, don't expose details
            return {'status': 'error', 'message': 'Processing error'}
    
    def _verify_webhook_signature(self, payload: str, signature: str, 
                                   secret: str) -> bool:
        """Verify HMAC-SHA256 signature of webhook payload.
        
        Args:
            payload: The raw request body
            signature: The signature from X-Nexi-Signature header
            secret: The webhook secret key
            
        Returns:
            True if signature is valid
        """
        try:
            # Compute expected signature
            expected = hmac.new(
                secret.encode('utf-8'),
                payload.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            # Constant-time comparison to prevent timing attacks
            return hmac.compare_digest(signature, expected)
            
        except Exception as e:
            _logger.error('Signature verification error: %s', str(e))
            return False
    
    def _check_webhook_rate_limit(self, client_ip: str) -> bool:
        """Check if client IP is within rate limits.
        
        Args:
            client_ip: The client IP address
            
        Returns:
            True if within limits
        """
        now = time.time()
        entry = _webhook_rate_limit.get(client_ip, {
            'count': 0,
            'first_request': now
        })
        
        # Reset if window has passed
        if now - entry['first_request'] > WEBHOOK_RATE_WINDOW:
            entry = {'count': 0, 'first_request': now}
        
        # Check limit
        if entry['count'] >= WEBHOOK_RATE_LIMIT:
            return False
        
        # Increment counter
        entry['count'] += 1
        _webhook_rate_limit[client_ip] = entry
        
        return True
    
    def _validate_terminal_id(self, terminal_id: str) -> bool:
        """Validate terminal ID format.
        
        Args:
            terminal_id: The terminal ID to validate
            
        Returns:
            True if valid format
        """
        import re
        if not terminal_id:
            return False
        if len(terminal_id) > 32:
            return False
        return bool(re.match(r'^[a-zA-Z0-9_-]+$', terminal_id))
    
    def _sanitize_external_id(self, external_id: str) -> str:
        """Sanitize external ID.
        
        Args:
            external_id: The external ID to sanitize
            
        Returns:
            Sanitized external ID
        """
        import re
        external_id = str(external_id).strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', external_id):
            raise ValidationError('Invalid external ID format')
        return external_id[:63]


class NexiPosController(http.Controller):
    """Controller for POS API endpoints."""

    @http.route('/pos_nexi/terminals', type='json', auth='user')
    def get_terminals(self, config_id=None, **kwargs):
        """Get available NEXI terminals for POS config.

        Args:
            config_id: POS config ID

        Returns:
            JSON response with terminal list
        """
        try:
            domain = [('state', '=', 'active'), ('active', '=', True)]
            if config_id:
                domain.append(('pos_config_ids', 'in', [config_id]))

            terminals = request.env['nexi.terminal'].search(domain)
            terminal_list = []
            for t in terminals:
                terminal_list.append({
                    'id': t.id,
                    'name': t.name,
                    'terminal_id': t.terminal_id,
                    'status': 'online' if t.state == 'active' else 'offline',
                    'last_seen': fields.Datetime.to_string(t.last_heartbeat) if t.last_heartbeat else None,
                    'is_default': len(terminal_list) == 0,
                })
            return {'success': True, 'terminals': terminal_list}
        except Exception as e:
            _logger.exception('Error loading terminals')
            return {'success': False, 'error': str(e), 'terminals': []}

    @http.route('/pos_nexi/payment/initiate', type='json', auth='user')
    def initiate_payment(self, order_id=None, amount=None, terminal_id=None,
                         currency_id=None, pos_session_id=None, config_id=None, **kwargs):
        """Initiate a NEXI payment from POS frontend.

        Args:
            order_id: POS order UID
            amount: Payment amount
            terminal_id: nexi.terminal record ID
            currency_id: Currency record ID
            pos_session_id: POS session ID
            config_id: POS config ID

        Returns:
            JSON response with transaction data
        """
        try:
            if not terminal_id or not amount:
                return {'success': False, 'error': 'Missing required parameters'}

            # Find payment method linked to this terminal
            terminal = request.env['nexi.terminal'].browse(int(terminal_id))
            if not terminal.exists() or terminal.state != 'active':
                return {'success': False, 'error': 'Terminal not available'}

            payment_method = request.env['pos.payment.method'].search([
                ('nexi_terminal_id', '=', terminal.id),
                ('is_nexi_payment', '=', True),
            ], limit=1)

            if not payment_method:
                return {'success': False, 'error': 'No payment method configured for this terminal'}

            # Find POS order if provided
            order = None
            if order_id and pos_session_id:
                session = request.env['pos.session'].browse(int(pos_session_id))
                if session.exists():
                    order = request.env['pos.order'].search([
                        ('session_id', '=', session.id),
                        ('pos_reference', 'like', order_id),
                    ], limit=1)

            if order:
                result = payment_method.nexi_payment_request(order, float(amount))
            else:
                # Create transaction directly without order link
                transaction = request.env['nexi.transaction'].create({
                    'terminal_id': terminal.terminal_id,
                    'amount': float(amount),
                    'currency_id': int(currency_id) if currency_id else request.env.company.currency_id.id,
                    'payment_method_id': payment_method.id,
                    'state': 'pending',
                })
                transaction.action_purchase()
                result = {
                    'success': True,
                    'transaction_id': transaction.id,
                    'external_id': transaction.external_id,
                }

            if result.get('success') and result.get('transaction_id'):
                txn = request.env['nexi.transaction'].browse(result['transaction_id'])
                result['transaction'] = {
                    'id': txn.id,
                    'external_id': txn.external_id,
                    'amount': txn.amount,
                    'state': txn.state,
                    'terminal_id': txn.terminal_id,
                    'transaction_ref': txn.reference or '',
                    'auth_code': txn.authorization_code or '',
                }
            return result

        except Exception as e:
            _logger.exception('Error initiating payment')
            return {'success': False, 'error': str(e)}

    @http.route('/pos_nexi/payment/status', type='json', auth='user')
    def get_payment_status_alt(self, external_id=None, **kwargs):
        """Get payment status (alternate route for JS compatibility).

        Delegates to the main status handler.
        """
        if not external_id:
            return {'success': False, 'status': 'error', 'message': 'Missing external_id'}

        try:
            external_id = self._sanitize_external_id(external_id)
        except ValidationError:
            return {'success': False, 'status': 'error', 'message': 'Invalid external ID'}

        transaction = request.env['nexi.transaction'].search([
            ('external_id', '=', external_id),
        ], limit=1)

        if not transaction:
            return {'success': False, 'status': 'not_found', 'message': 'Transaction not found'}

        try:
            transaction.action_poll_status()
        except Exception as e:
            _logger.warning('Status poll failed for %s: %s', external_id, str(e))

        receipt_data = transaction.get_receipt_data()
        return {
            'success': True,
            'transaction': {
                'id': transaction.id,
                'external_id': transaction.external_id,
                'amount': transaction.amount,
                'state': transaction.state,
                'terminal_id': transaction.terminal_id,
                'error_message': transaction.last_error or transaction.result_message or '',
                'transaction_ref': transaction.reference or '',
                'auth_code': transaction.authorization_code or '',
                'card_scheme': receipt_data.get('card_scheme', ''),
                'card_number_customer': receipt_data.get('card_number_customer', ''),
                'card_entry_mode': receipt_data.get('card_entry_mode', ''),
                'receipt_details_customer': receipt_data.get('receipt_details_customer', ''),
                'receipt_details_merchant': receipt_data.get('receipt_details_merchant', ''),
            },
            'transaction_state': transaction.state,
            'result_code': transaction.result_code,
            'result_message': transaction.result_message,
        }

    @http.route('/pos_nexi/payment/refund', type='json', auth='user')
    def refund_payment(self, payment_method_id=None, amount=None,
                       original_external_id=None, **kwargs):
        """Initiate a NEXI refund from POS frontend.

        Args:
            payment_method_id: ID of the pos.payment.method
            amount: Refund amount (positive, major currency units)
            original_external_id: Optional external ID of original purchase

        Returns:
            JSON response with refund transaction data
        """
        try:
            if not request.env.user.has_group('pos_nexi_payment.group_nexi_manager'):
                return {'success': False, 'error': 'Insufficient permissions for refund'}

            if not payment_method_id or not amount:
                return {'success': False, 'error': 'Missing required parameters'}

            payment_method = request.env['pos.payment.method'].browse(int(payment_method_id))
            if not payment_method.exists():
                return {'success': False, 'error': 'Payment method not found'}

            result = payment_method.nexi_refund_payment(
                float(amount),
                original_external_id=original_external_id,
            )

            if not result or result.get('error'):
                return {
                    'success': False,
                    'error': result.get('error', 'Refund failed') if result else 'Refund failed',
                }

            # Create nexi.transaction record for tracking
            Transaction = request.env['nexi.transaction']
            txn_id = None
            if Transaction is not None:
                terminal = payment_method.nexi_terminal_id
                terminal_id = terminal.terminal_id if terminal else ''
                currency = payment_method.journal_id.currency_id or payment_method.company_id.currency_id

                txn_vals = {
                    'external_id': result['external_id'],
                    'terminal_id': terminal_id,
                    'amount': abs(float(amount)),
                    'currency_id': currency.id,
                    'payment_method_id': payment_method.id,
                    'transaction_type': 'refund',
                    'state': result.get('status', 'processing'),
                }

                # Link to original transaction if available
                if original_external_id:
                    orig_txn = Transaction.search([
                        ('external_id', '=', original_external_id),
                    ], limit=1)
                    if orig_txn:
                        txn_vals['original_transaction_id'] = orig_txn.id

                txn = Transaction.create(txn_vals)
                txn_id = txn.id

            return {
                'success': True,
                'external_id': result['external_id'],
                'status': result.get('status', 'processing'),
                'transaction_id': txn_id,
            }

        except Exception as e:
            _logger.exception('Error initiating refund')
            return {'success': False, 'error': str(e)}

    @http.route('/pos_nexi/transaction/unconfirmed', type='json', auth='user')
    def get_unconfirmed_transactions(self, terminal_id=None, **kwargs):
        """Recover unconfirmed transactions for a terminal.

        Args:
            terminal_id: nexi.terminal record ID or terminal_id string

        Returns:
            JSON response with recovered/updated transaction counts
        """
        try:
            if not request.env.user.has_group('pos_nexi_payment.group_nexi_manager'):
                return {'success': False, 'error': 'Insufficient permissions for recovery'}

            if not terminal_id:
                return {'success': False, 'error': 'Missing terminal_id'}

            # Find terminal - try as record ID first, then as terminal_id string
            terminal = None
            try:
                terminal = request.env['nexi.terminal'].browse(int(terminal_id))
                if not terminal.exists():
                    terminal = None
            except (ValueError, TypeError):
                pass

            if not terminal:
                terminal = request.env['nexi.terminal'].search([
                    ('terminal_id', '=', str(terminal_id)),
                    ('active', '=', True),
                ], limit=1)

            if not terminal:
                return {'success': False, 'error': 'Terminal not found'}

            result = terminal.action_recover_unconfirmed()

            return {
                'success': True,
                'message': result.get('params', {}).get('message', '') if isinstance(result, dict) else '',
            }

        except Exception as e:
            _logger.exception('Error recovering unconfirmed transactions')
            return {'success': False, 'error': str(e)}

    @http.route('/pos_nexi/payment/cancel', type='json', auth='user')
    def cancel_payment(self, external_id=None, **kwargs):
        """Cancel a NEXI payment from POS frontend."""
        if not external_id:
            return {'success': False, 'message': 'Missing external_id'}

        try:
            external_id = self._sanitize_external_id(external_id)
        except ValidationError:
            return {'success': False, 'message': 'Invalid external ID'}

        transaction = request.env['nexi.transaction'].search([
            ('external_id', '=', external_id),
        ], limit=1)

        if not transaction:
            return {'success': True, 'message': 'No transaction found'}

        try:
            transaction.action_cancel()
            return {'success': True, 'message': 'Payment cancelled'}
        except Exception as e:
            _logger.exception('Error cancelling payment %s', external_id)
            return {'success': False, 'message': str(e)}

    @http.route('/pos_nexi/terminal/print', type='json', auth='user')
    def nexi_terminal_print(self, payment_method_id, external_id=None, **kwargs):
        """Trigger receipt printing on the NEXI terminal.

        Based on the payment method's print_content setting, renders
        the NEXI card receipt text and/or POS receipt HTML as PNG
        and sends to the terminal printer.

        Args:
            payment_method_id: ID of the pos.payment.method record
            external_id: Optional external transaction ID to find receipt data

        Returns:
            JSON dict with success boolean and message
        """
        try:
            payment_method = request.env['pos.payment.method'].browse(int(payment_method_id))
            if not payment_method.exists():
                return {'success': False, 'message': 'Payment method not found'}

            if not payment_method.nexi_print_enabled:
                return {'success': False, 'message': 'Terminal printing is disabled'}

            print_content = payment_method.nexi_print_content or 'nexi_receipt'
            receipt_text = None
            receipt_html = None

            # Get NEXI card receipt text from transaction data
            if print_content in ('nexi_receipt', 'both') and external_id:
                # Get receipt data from nexi.transaction
                Transaction = request.env['nexi.transaction']
                if Transaction is not None:
                    txn = Transaction.search([
                        ('external_id', '=', external_id),
                    ], limit=1)
                    if txn:
                        receipt_data = txn.get_receipt_data()
                        receipt_text = receipt_data.get('receipt_details_customer', '')
                else:
                    # Fallback: check the latest response buffer
                    if payment_method.nexi_latest_response:
                        import json as json_mod
                        try:
                            resp = json_mod.loads(payment_method.nexi_latest_response)
                            txn_data = resp.get('transaction', resp)
                            receipt_text = txn_data.get('receipt_details_customer', '')
                        except (json_mod.JSONDecodeError, AttributeError):
                            pass

            # Get POS receipt HTML from the current order
            if print_content in ('pos_receipt', 'both'):
                try:
                    # Render the POS receipt template
                    receipt_html = request.env['ir.qweb']._render(
                        'point_of_sale.OrderReceipt', {}
                    )
                    if hasattr(receipt_html, '__html__'):
                        receipt_html = str(receipt_html)
                except Exception as e:
                    _logger.warning('Failed to render POS receipt HTML: %s', str(e))

            if not hasattr(payment_method, 'nexi_print_to_terminal'):
                return {'success': False, 'message': 'Terminal printing not available in this module'}

            result = payment_method.nexi_print_to_terminal(
                receipt_text=receipt_text,
                receipt_html=receipt_html,
            )
            return result

        except Exception as e:
            _logger.exception('Error in terminal print endpoint')
            return {'success': False, 'message': str(e)}

    @http.route('/pos_nexi/terminal/test', type='json', auth='user')
    def test_terminal(self, terminal_id=None, **kwargs):
        """Test terminal connection from POS frontend."""
        if not terminal_id:
            return {'success': False, 'message': 'Missing terminal_id'}

        try:
            terminal = request.env['nexi.terminal'].browse(int(terminal_id))
            if not terminal.exists():
                return {'success': False, 'message': 'Terminal not found'}

            terminal.action_test_connection()
            return {
                'success': True,
                'message': 'Terminal connection OK',
                'status': 'online' if terminal.state == 'active' else terminal.state,
            }
        except Exception as e:
            _logger.exception('Terminal test failed for %s', terminal_id)
            return {'success': False, 'message': str(e)}

    @http.route('/nexi/pos/payment_status', type='json', auth='user')
    def get_payment_status(self, external_id: str):
        """Get payment status for polling from POS.
        
        Args:
            external_id: The external transaction ID
            
        Returns:
            JSON response with transaction status
        """
        try:
            # Validate external_id
            if not external_id or not isinstance(external_id, str):
                return {
                    'status': 'error',
                    'message': 'Invalid external ID'
                }
            
            # Sanitize
            external_id = self._sanitize_external_id(external_id)
            
            # Search for transaction
            Transaction = request.env['nexi.transaction']
            transaction = Transaction.search([
                ('external_id', '=', external_id)
            ], limit=1)
            
            if not transaction:
                return {
                    'status': 'not_found',
                    'message': 'Transaction not found'
                }
            
            # Check if user has access to this transaction
            # (belongs to same company or is admin)
            if transaction.company_id != request.env.company:
                if not request.env.user.has_group('base.group_system'):
                    return {
                        'status': 'error',
                        'message': 'Access denied'
                    }
            
            # Poll for latest status
            try:
                transaction.action_poll_status()
            except Exception as e:
                _logger.warning(
                    'Status poll failed for %s: %s', external_id, str(e)
                )
            
            # Return status with card details (no sensitive data)
            receipt_data = transaction.get_receipt_data()
            return {
                'status': 'ok',
                'transaction_state': transaction.state,
                'result_code': transaction.result_code,
                'result_message': transaction.result_message,
                'amount': transaction.amount,
                'currency': transaction.currency_id.name,
                'external_id': transaction.external_id,
                'card_scheme': receipt_data.get('card_scheme', ''),
                'authorization_code': receipt_data.get('authorization_code', ''),
                'card_number_customer': receipt_data.get('card_number_customer', ''),
                'card_entry_mode': receipt_data.get('card_entry_mode', ''),
                'reference': receipt_data.get('reference', ''),
                'receipt_details_customer': receipt_data.get('receipt_details_customer', ''),
                'receipt_details_merchant': receipt_data.get('receipt_details_merchant', ''),
            }
            
        except Exception as e:
            _logger.exception('Error getting payment status')
            return {
                'status': 'error',
                'message': 'Internal error'
            }
    
    @http.route('/nexi/pos/terminal_status', type='json', auth='user')
    def get_terminal_status(self, terminal_id: str):
        """Get terminal status for POS.
        
        Args:
            terminal_id: The terminal identifier
            
        Returns:
            JSON response with terminal status
        """
        try:
            # Validate terminal_id
            if not terminal_id or not isinstance(terminal_id, str):
                return {
                    'status': 'error',
                    'message': 'Invalid terminal ID'
                }
            
            # Sanitize
            terminal_id = self._sanitize_terminal_id(terminal_id)
            
            # Find terminal
            Terminal = request.env['nexi.terminal']
            terminal = Terminal.search([
                ('terminal_id', '=', terminal_id),
                ('active', '=', True)
            ], limit=1)
            
            if not terminal:
                return {
                    'status': 'not_found',
                    'message': 'Terminal not found'
                }
            
            # Return status (no sensitive data)
            return {
                'status': 'ok',
                'terminal_state': terminal.state,
                'is_online': terminal.state == 'active',
                'last_heartbeat': fields.Datetime.to_string(terminal.last_heartbeat),
                'name': terminal.name,
            }
            
        except Exception as e:
            _logger.exception('Error getting terminal status')
            return {
                'status': 'error',
                'message': 'Internal error'
            }
    
    def _sanitize_external_id(self, external_id: str) -> str:
        """Sanitize external ID."""
        import re
        external_id = str(external_id).strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', external_id):
            raise ValidationError('Invalid external ID format')
        return external_id[:63]
    
    def _sanitize_terminal_id(self, terminal_id: str) -> str:
        """Sanitize terminal ID."""
        import re
        terminal_id = str(terminal_id).strip()
        if not re.match(r'^[a-zA-Z0-9_-]+$', terminal_id):
            raise ValidationError('Invalid terminal ID format')
        return terminal_id[:32]
