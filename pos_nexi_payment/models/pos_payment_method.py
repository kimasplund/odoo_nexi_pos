# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare

_logger = logging.getLogger(__name__)

# Constants
MIN_PAYMENT_AMOUNT = 0.01
MAX_PAYMENT_AMOUNT = 999999.99


class PosPaymentMethod(models.Model):
    """Extend pos.payment.method to support NEXI payment terminals."""

    _inherit = 'pos.payment.method'

    # =========================================================================
    # Terminal Selection
    # =========================================================================

    @api.model
    def _get_payment_terminal_selection(self):
        """Add NEXI to the list of available payment terminals."""
        return super()._get_payment_terminal_selection() + [('nexi', 'NEXI')]

    # =========================================================================
    # Fields
    # =========================================================================

    is_nexi_payment = fields.Boolean(
        string='NEXI Payment Terminal',
        help='Enable this to use a NEXI payment terminal for this payment method.',
        default=False,
    )
    
    nexi_terminal_id = fields.Many2one(
        comodel_name='nexi.terminal',
        string='NEXI Terminal',
        help='The NEXI terminal associated with this payment method.',
        domain=[('state', '=', 'active')],
        ondelete='restrict',
    )
    
    # =========================================================================
    # ORM Overrides
    # =========================================================================

    @api.model
    def _load_pos_data_fields(self, config_id):
        """Load NEXI fields in POS session data."""
        params = super()._load_pos_data_fields(config_id)
        params += ['is_nexi_payment']
        return params

    # =========================================================================
    # Constraints
    # =========================================================================
    
    @api.constrains('is_nexi_payment', 'nexi_terminal_id')
    def _check_nexi_terminal(self):
        """Ensure NEXI terminal is set when NEXI payment is enabled."""
        for method in self:
            if method.is_nexi_payment and not method.nexi_terminal_id:
                raise ValidationError(_(
                    'A NEXI terminal must be selected when "NEXI Payment Terminal" is enabled.'
                ))
    
    @api.constrains('is_nexi_payment', 'use_payment_terminal')
    def _check_payment_terminal_conflict(self):
        """Prevent conflicts between NEXI and other payment terminals."""
        for method in self:
            if method.is_nexi_payment and method.use_payment_terminal:
                raise ValidationError(_(
                    'Cannot use both NEXI payment terminal and another payment terminal '
                    'for the same payment method. Please choose one.'
                ))
    
    # =========================================================================
    # Business Methods
    # =========================================================================
    
    def get_payment_terminal(self):
        """Override to return NEXI terminal if configured.
        
        Returns:
            nexi.terminal: The configured NEXI terminal, or None if not a NEXI method.
        """
        self.ensure_one()
        if self.is_nexi_payment and self.nexi_terminal_id:
            return self.nexi_terminal_id
        return super(PosPaymentMethod, self).get_payment_terminal()
    
    def _calculate_payment_amount(self, order) -> float:
        """Calculate the correct payment amount from order data.
        
        CRITICAL SECURITY FIX: Never trust client-provided amounts.
        Always calculate from server-side order data.
        
        Args:
            order: The POS order being paid
            
        Returns:
            The calculated amount to charge
        """
        self.ensure_one()
        
        # Calculate remaining amount from order totals
        # Use amount_total - amount_paid to get remaining balance
        amount_total = order.amount_total or 0.0
        amount_paid = order.amount_paid or 0.0
        
        # Calculate remaining balance
        remaining = amount_total - amount_paid
        
        # Round to 2 decimal places
        return round(remaining, 2)
    
    def _validate_payment_amount(self, amount: float, order=None) -> dict:
        """Validate payment amount against security rules.
        
        Args:
            amount: The amount to validate
            order: Optional order for context
            
        Returns:
            Dict with 'valid' (bool) and 'message' (str on error)
        """
        self.ensure_one()
        
        # Check minimum amount
        if float_compare(amount, MIN_PAYMENT_AMOUNT, precision_digits=2) < 0:
            return {
                'valid': False,
                'message': _('Payment amount must be at least %.2f') % MIN_PAYMENT_AMOUNT
            }
        
        # Check maximum amount
        if float_compare(amount, MAX_PAYMENT_AMOUNT, precision_digits=2) > 0:
            return {
                'valid': False,
                'message': _('Payment amount exceeds maximum allowed (%.2f)') % MAX_PAYMENT_AMOUNT
            }
        
        # Check terminal-specific limits
        terminal = self.nexi_terminal_id
        if terminal:
            if terminal.min_amount and float_compare(amount, terminal.min_amount, precision_digits=2) < 0:
                return {
                    'valid': False,
                    'message': _('Amount %.2f is below the terminal minimum (%.2f).') % (
                        amount, terminal.min_amount
                    )
                }
            
            if terminal.max_amount and float_compare(amount, terminal.max_amount, precision_digits=2) > 0:
                return {
                    'valid': False,
                    'message': _('Amount %.2f exceeds the terminal maximum (%.2f).') % (
                        amount, terminal.max_amount
                    )
                }
        
        # If order provided, validate against order total
        if order:
            calculated = self._calculate_payment_amount(order)
            # Allow small tolerance for rounding
            tolerance = 0.01
            if abs(amount - calculated) > tolerance:
                _logger.warning(
                    'Payment amount mismatch for order %s: requested=%s, calculated=%s',
                    order.name, amount, calculated
                )
                return {
                    'valid': False,
                    'message': _('Payment amount does not match order balance. '
                               'Expected: %.2f, Received: %.2f') % (calculated, amount)
                }
        
        return {'valid': True}
    
    def nexi_payment_request(self, order, client_amount=None):
        """Initiate a payment request from the POS.
        
        CRITICAL SECURITY FIX: Server-side amount calculation
        The client_amount parameter is IGNORED. Amount is always
        calculated from order.amount_total - order.amount_paid.
        
        This prevents:
        - Client-side amount manipulation
        - Payment amount tampering
        - Under/over payment attacks
        
        Args:
            order: The POS order being paid.
            client_amount: IGNORED - kept for API compatibility only
            
        Returns:
            dict: Result containing:
                - success (bool): Whether the request was initiated successfully.
                - transaction_id (int): ID of the created nexi.transaction.
                - external_id (str): External reference for the transaction.
                - message (str): Status message or error description.
        """
        self.ensure_one()
        
        # Security check: Verify user has permission to create payments
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            _logger.warning(
                'User %s attempted to initiate NEXI payment without permission',
                self.env.user.login
            )
            return {
                'success': False,
                'transaction_id': None,
                'external_id': None,
                'message': _('You do not have permission to process NEXI payments.'),
            }
        
        # SECURITY FIX: Calculate amount server-side, ignore client_amount
        amount = self._calculate_payment_amount(order)
        
        _logger.info(
            'NEXI payment request for order %s: calculated_amount=%s (client_amount ignored)',
            order.name, amount
        )
        
        # Validate calculated amount
        if float_compare(amount, 0.0, precision_digits=2) <= 0:
            return {
                'success': False,
                'transaction_id': None,
                'external_id': None,
                'message': _('No remaining balance to pay for this order.'),
            }
        
        # Validate against security rules
        validation = self._validate_payment_amount(amount, order)
        if not validation['valid']:
            return {
                'success': False,
                'transaction_id': None,
                'external_id': None,
                'message': validation['message'],
            }
        
        # Verify terminal is online and ready
        if not self.nexi_terminal_id or self.nexi_terminal_id.state != 'active':
            return {
                'success': False,
                'transaction_id': None,
                'external_id': None,
                'message': _('NEXI terminal is not available. Please check the configuration.'),
            }
        
        try:
            # Rate limiting check
            rate_limit_key = f"payment_{self.nexi_terminal_id.id}"
            recent_payments = self.env['nexi.transaction'].search_count([
                ('terminal_id', '=', self.nexi_terminal_id.terminal_id),
                ('create_date', '>=', fields.Datetime.now() - timedelta(minutes=1)),
            ])
            
            if recent_payments >= 10:
                _logger.warning(
                    'Rate limit exceeded for terminal %s',
                    self.nexi_terminal_id.terminal_id
                )
                return {
                    'success': False,
                    'transaction_id': None,
                    'external_id': None,
                    'message': _('Too many payment attempts. Please wait a moment.'),
                }
            
            # Create NEXI transaction record
            transaction = self.env['nexi.transaction'].create({
                'terminal_id': self.nexi_terminal_id.terminal_id,
                'amount': amount,
                'currency_id': order.currency_id.id or order.company_id.currency_id.id,
                'pos_order_id': order.id,
                'payment_method_id': self.id,
                'state': 'pending',
            })
            
            # Audit log
            transaction._audit_log(
                'payment_request_initiated',
                {
                    'order_id': order.id,
                    'amount': amount,
                    'terminal_id': self.nexi_terminal_id.terminal_id,
                }
            )
            
            # Initiate payment via transaction model
            transaction.action_purchase()
            
            _logger.info(
                'NEXI payment initiated for order %s, transaction %s, external_id %s, amount %s',
                order.name, transaction.id, transaction.external_id, amount
            )
            
            return {
                'success': True,
                'transaction_id': transaction.id,
                'external_id': transaction.external_id,
                'message': _('Payment initiated on terminal. Waiting for customer...'),
            }
            
        except Exception as e:
            _logger.exception('Error initiating NEXI payment for order %s', order.name)
            return {
                'success': False,
                'transaction_id': None,
                'external_id': None,
                'message': _('Error initiating payment: %s') % str(e),
            }
    
    def nexi_cancel_payment(self, external_id=None):
        """Cancel an ongoing payment request.
        
        This method is called when the cashier cancels a payment or when
        the POS session is closed while a payment is pending.
        
        Args:
            external_id (str, optional): The external ID of the transaction to cancel.
                                        If not provided, cancels any pending transaction
                                        for this payment method.
            
        Returns:
            dict: Result containing:
                - success (bool): Whether the cancellation was successful.
                - message (str): Status message.
        """
        self.ensure_one()
        
        # Security check
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            return {
                'success': False,
                'message': _('Permission denied.'),
            }
        
        Transaction = self.env['nexi.transaction']
        
        # Find transaction to cancel
        if external_id:
            # Sanitize external_id
            try:
                external_id = self.env['nexi.transaction']._sanitize_external_id(external_id)
            except ValidationError:
                return {
                    'success': False,
                    'message': _('Invalid external ID format.'),
                }
            
            transaction = Transaction.search([
                ('external_id', '=', external_id),
                ('terminal_id', '=', self.nexi_terminal_id.terminal_id),
            ], limit=1)
        else:
            # Find any pending transaction for this terminal
            transaction = Transaction.search([
                ('terminal_id', '=', self.nexi_terminal_id.terminal_id),
                ('state', 'in', ['pending', 'processing']),
            ], order='create_date desc', limit=1)
        
        if not transaction:
            return {
                'success': True,
                'message': _('No active payment to cancel.'),
            }
        
        try:
            # Cancel the transaction
            transaction.action_cancel()
            
            _logger.info('NEXI payment cancelled: %s', transaction.external_id)
            
            transaction._audit_log(
                'payment_cancelled',
                {'cancelled_by': self.env.user.login}
            )
            
            return {
                'success': True,
                'message': _('Payment cancelled successfully.'),
            }
            
        except Exception as e:
            _logger.exception('Error cancelling NEXI payment %s', transaction.external_id)
            # Still mark as cancelled to prevent stuck transactions
            transaction.write({
                'state': 'cancelled',
                'result_message': _('Cancelled by user due to error: %s') % str(e),
            })
            return {
                'success': True,
                'message': _('Payment marked as cancelled.'),
            }
    
    def nexi_poll_payment_status(self, external_id):
        """Poll the status of a NEXI payment.
        
        Args:
            external_id (str): The external ID of the transaction.
            
        Returns:
            dict: Current status of the payment:
                - state (str): Transaction state (pending, approved, declined, error).
                - transaction_id (int): ID of the nexi.transaction.
                - message (str): Human-readable status message.
        """
        self.ensure_one()
        
        # Sanitize external_id
        try:
            external_id = self.env['nexi.transaction']._sanitize_external_id(external_id)
        except ValidationError:
            return {
                'state': 'error',
                'transaction_id': None,
                'message': _('Invalid external ID format.'),
            }
        
        Transaction = self.env['nexi.transaction']
        transaction = Transaction.search([
            ('external_id', '=', external_id),
        ], limit=1)
        
        if not transaction:
            return {
                'state': 'error',
                'transaction_id': None,
                'message': _('Transaction not found.'),
            }
        
        # Poll for updated status
        try:
            transaction.action_poll_status()
        except Exception as e:
            _logger.exception('Error polling NEXI transaction %s', external_id)
        
        state_messages = {
            'pending': _('Waiting for terminal response...'),
            'processing': _('Processing on terminal...'),
            'awaiting_confirm': _('Awaiting confirmation...'),
            'confirmed': _('Payment confirmed!'),
            'failed': _('Payment failed: %s') % (transaction.result_message or _('Unknown error')),
            'cancelled': _('Payment was cancelled.'),
        }
        
        return {
            'state': transaction.state,
            'transaction_id': transaction.id,
            'message': state_messages.get(transaction.state, _('Unknown status.')),
            # Card details
            'card_scheme': transaction.card_scheme or '',
            'authorization_code': transaction.authorization_code or '',
            'card_number_customer': transaction.card_number_customer or '',
            'card_entry_mode': transaction.card_entry_mode or '',
            'reference': transaction.reference or '',
            'receipt_details_customer': transaction.receipt_details_customer or '',
            'receipt_details_merchant': transaction.receipt_details_merchant or '',
        }
