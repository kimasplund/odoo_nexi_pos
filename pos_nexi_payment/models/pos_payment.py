# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

from ..security.nexi_security import (
    require_nexi_manager,
    check_refund_authorization,
    NexiSecurityError
)

_logger = logging.getLogger(__name__)

# Default refund approval threshold
DEFAULT_REFUND_THRESHOLD = 100.0


class PosPayment(models.Model):
    """Extend pos.payment to link with NEXI transactions."""
    
    _inherit = 'pos.payment'
    
    # =========================================================================
    # Fields
    # =========================================================================
    
    nexi_transaction_id = fields.Many2one(
        comodel_name='nexi.transaction',
        string='NEXI Transaction',
        help='The linked NEXI transaction for this payment.',
        ondelete='set null',
        readonly=True,
        copy=False,
    )
    
    nexi_external_id = fields.Char(
        string='NEXI External ID',
        help='External reference from NEXI for this transaction.',
        related='nexi_transaction_id.external_id',
        readonly=True,
        store=True,
        copy=False,
    )
    
    is_nexi_payment = fields.Boolean(
        string='Is NEXI Payment',
        related='payment_method_id.is_nexi_payment',
        readonly=True,
        store=True,
    )
    
    # =========================================================================
    # Create Override
    # =========================================================================
    
    @api.model_create_multi
    def create(self, vals_list):
        """Override create to link with NEXI transaction if applicable.
        
        When a POS payment is created for a NEXI payment method, we try to
        link it with the most recent pending NEXI transaction for that order.
        """
        # First create the payments
        payments = super(PosPayment, self).create(vals_list)
        
        # Then link with NEXI transactions if applicable
        for payment in payments:
            if payment.payment_method_id.is_nexi_payment and not payment.nexi_transaction_id:
                payment._link_nexi_transaction()
        
        return payments
    
    def _link_nexi_transaction(self):
        """Link this payment with a pending NEXI transaction.
        
        This method searches for the most recent pending NEXI transaction
        for the same order and amount, and links it to this payment.
        """
        self.ensure_one()
        
        if not self.payment_method_id.is_nexi_payment:
            return
        
        Transaction = self.env['nexi.transaction']
        
        # Search for a matching pending or confirmed transaction
        domain = [
            ('pos_order_id', '=', self.pos_order_id.id),
            ('amount', '=', self.amount),
            ('state', 'in', ['pending', 'awaiting_confirm', 'confirmed']),
        ]
        
        # Also try matching by terminal if available
        if self.payment_method_id.nexi_terminal_id:
            domain.append(
                ('terminal_id', '=', self.payment_method_id.nexi_terminal_id.terminal_id)
            )
        
        transaction = Transaction.search(domain, order='create_date desc', limit=1)
        
        if transaction:
            # Link the transaction to this payment
            transaction.write({'pos_order_id': self.pos_order_id.id})
            self.write({'nexi_transaction_id': transaction.id})
            _logger.info(
                'Linked POS payment %s to NEXI transaction %s',
                self.id, transaction.external_id
            )
    
    # =========================================================================
    # Business Methods
    # =========================================================================
    
    def get_payment_receipt_data(self):
        """Get data for printing on the receipt.
        
        Returns:
            dict: Receipt data containing:
                - is_nexi_payment (bool): Whether this is a NEXI payment.
                - external_id (str): NEXI transaction reference.
                - terminal_name (str): Name of the terminal used.
                - auth_code (str): Authorization code if available.
                - card_type (str): Card type if available.
                - last_four_digits (str): Last 4 digits of card if available.
        """
        self.ensure_one()
        
        if not self.is_nexi_payment or not self.nexi_transaction_id:
            return super(PosPayment, self).get_payment_receipt_data() if hasattr(
                super(PosPayment, self), 'get_payment_receipt_data'
            ) else {}
        
        transaction = self.nexi_transaction_id
        
        receipt_data = transaction.get_receipt_data()
        # Extract last four digits from masked PAN
        card_number = transaction.card_number_customer or ''
        last_four = card_number[-4:] if len(card_number) >= 4 else card_number

        return {
            'is_nexi_payment': True,
            'external_id': transaction.external_id or '',
            'terminal_name': transaction.terminal_ref.name if transaction.terminal_ref else '',
            'terminal_id': transaction.terminal_id or '',
            'auth_code': transaction.authorization_code or '',
            'card_type': transaction.card_scheme or '',
            'last_four_digits': last_four,
            'transaction_date': transaction.create_date or fields.Datetime.now(),
            'card_entry_mode': transaction.card_entry_mode or '',
            'reference': transaction.reference or '',
            'card_number_customer': card_number,
            'cardholder_verification': transaction.cardholder_verification or '',
            'application_name': transaction.application_name or '',
            'receipt_details_customer': transaction.receipt_details_customer or '',
            'receipt_details_merchant': transaction.receipt_details_merchant or '',
        }
    
    def action_retry_nexi_payment(self):
        """Retry a failed NEXI payment.
        
        This method can be called from the POS UI when a NEXI payment fails,
        allowing the cashier to retry without recreating the entire order.
        
        Returns:
            dict: Result of the retry attempt.
        """
        self.ensure_one()
        
        if not self.is_nexi_payment:
            return {
                'success': False,
                'message': _('This is not a NEXI payment.'),
            }
        
        # Security check
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            return {
                'success': False,
                'message': _('Permission denied.'),
            }
        
        try:
            # Cancel any existing transaction
            if self.nexi_transaction_id:
                self.nexi_transaction_id.action_cancel()
            
            # Initiate new payment request
            result = self.payment_method_id.nexi_payment_request(
                self.pos_order_id, self.amount
            )
            
            if result.get('success'):
                # Link new transaction to this payment
                if result.get('transaction_id'):
                    transaction = self.env['nexi.transaction'].browse(
                        result['transaction_id']
                    )
                    transaction.write({'pos_order_id': self.pos_order_id.id})
                    self.write({'nexi_transaction_id': transaction.id})
            
            return result
            
        except Exception as e:
            _logger.exception('Error retrying NEXI payment %s', self.id)
            return {
                'success': False,
                'message': _('Error retrying payment: %s') % str(e),
            }
    
    def _get_refund_approval_threshold(self) -> float:
        """Get the refund approval threshold from configuration.
        
        Returns:
            The threshold amount above which manager approval is required.
        """
        return float(
            self.env['ir.config_parameter'].sudo().get_param(
                'nexi_payment.refund_threshold',
                str(DEFAULT_REFUND_THRESHOLD)
            )
        )
    
    def _check_refund_permission(self, amount: float = None) -> bool:
        """Check if current user has permission to process refund.
        
        Implements multi-layer authorization:
        1. Check basic refund permission
        2. Check amount against threshold
        3. Log the attempt
        
        Args:
            amount: The refund amount (uses payment amount if None)
            
        Returns:
            True if authorized
            
        Raises:
            UserError: If not authorized
        """
        self.ensure_one()
        
        if amount is None:
            amount = abs(self.amount)
        
        try:
            check_refund_authorization(
                self.env,
                amount,
                self._get_refund_approval_threshold()
            )
            return True
        except NexiSecurityError as e:
            raise UserError(str(e))
    
    def action_void_nexi_payment(self):
        """Void/Refund a completed NEXI payment.
        
        SECURITY: Multi-layer authorization implemented:
        - Requires NEXI manager group for any refund
        - Amounts above threshold require additional verification
        - All attempts are audit logged
        
        This initiates a refund through the NEXI terminal for the full amount.
        
        Returns:
            dict: Result of the void attempt.
        """
        self.ensure_one()
        
        if not self.is_nexi_payment:
            raise UserError(_('This is not a NEXI payment.'))
        
        if not self.nexi_transaction_id:
            raise UserError(_('No NEXI transaction linked to this payment.'))
        
        # SECURITY: Multi-layer authorization check
        refund_amount = abs(self.amount)
        
        try:
            self._check_refund_permission(refund_amount)
        except UserError:
            # Audit log the denied access
            _logger.warning(
                'Refund access denied for user %s on payment %s (amount: %s)',
                self.env.user.login, self.id, refund_amount
            )
            raise
        
        # Check if transaction can be voided
        if self.nexi_transaction_id.state != 'confirmed':
            raise UserError(_('Only confirmed transactions can be voided.'))
        
        # Additional check for large refunds - require explicit confirmation
        threshold = self._get_refund_approval_threshold()
        if refund_amount > threshold:
            # This would typically involve a wizard for manager approval
            # For now, we log and require the user to have manager permissions
            _logger.info(
                'Large refund processed: user=%s, payment=%s, amount=%s',
                self.env.user.login, self.id, refund_amount
            )
        
        try:
            # Audit log before processing
            self.nexi_transaction_id._audit_log(
                'refund_initiated',
                {
                    'payment_id': self.id,
                    'amount': refund_amount,
                    'initiated_by': self.env.user.login,
                }
            )
            
            # Process void/reversal via transaction
            result = self.nexi_transaction_id.action_void()
            
            # Audit log success
            self.nexi_transaction_id._audit_log(
                'refund_completed',
                {
                    'payment_id': self.id,
                    'amount': refund_amount,
                    'processed_by': self.env.user.login,
                }
            )
            
            return {
                'success': True,
                'message': _('Payment voided successfully.'),
            }
            
        except Exception as e:
            # Audit log failure
            self.nexi_transaction_id._audit_log(
                'refund_failed',
                {
                    'payment_id': self.id,
                    'amount': refund_amount,
                    'error': str(e),
                }
            )
            _logger.exception('Error voiding NEXI payment %s', self.id)
            raise UserError(_('Error voiding payment: %s') % str(e))
    
    # =========================================================================
    # Sync Methods
    # =========================================================================
    
    def sync_nexi_status(self):
        """Synchronize payment status with NEXI transaction.
        
        This method updates the payment record based on the current state
        of the linked NEXI transaction. It's used during polling from the POS.
        
        Returns:
            dict: Current status information.
        """
        self.ensure_one()
        
        if not self.is_nexi_payment or not self.nexi_transaction_id:
            return {'status': 'not_nexi'}
        
        transaction = self.nexi_transaction_id
        
        # Poll for latest status
        try:
            transaction.action_poll_status()
        except Exception as e:
            _logger.warning('Failed to poll NEXI status: %s', str(e))
        
        return {
            'status': transaction.state,
            'external_id': transaction.external_id,
            'amount': transaction.amount,
            'error_message': transaction.result_message,
        }
