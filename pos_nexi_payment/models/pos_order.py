# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


class PosOrder(models.Model):
    """Extend pos.order to support NEXI payment integration."""
    
    _inherit = 'pos.order'
    
    # =========================================================================
    # Fields
    # =========================================================================
    
    nexi_transaction_ids = fields.One2many(
        comodel_name='nexi.transaction',
        inverse_name='pos_order_id',
        string='NEXI Transactions',
        help='All NEXI transactions associated with this order.',
        readonly=True,
        copy=False,
    )
    
    nexi_payment_count = fields.Integer(
        string='NEXI Payment Count',
        compute='_compute_nexi_payment_count',
        store=True,
    )
    
    has_pending_nexi_payments = fields.Boolean(
        string='Has Pending NEXI Payments',
        compute='_compute_has_pending_nexi_payments',
        store=True,
    )
    
    # =========================================================================
    # Compute Methods
    # =========================================================================
    
    @api.depends('payment_ids.nexi_transaction_id')
    def _compute_nexi_payment_count(self):
        """Count the number of NEXI payments on this order."""
        for order in self:
            order.nexi_payment_count = len(
                order.payment_ids.filtered('nexi_transaction_id')
            )
    
    @api.depends('nexi_transaction_ids.state')
    def _compute_has_pending_nexi_payments(self):
        """Check if there are any pending NEXI transactions."""
        for order in self:
            order.has_pending_nexi_payments = bool(
                order.nexi_transaction_ids.filtered(lambda t: t.state == 'pending')
            )
    
    # =========================================================================
    # Business Methods
    # =========================================================================
    
    def add_nexi_payment(self, amount, payment_method_id=None, context=None):
        """Add a NEXI payment to this order.
        
        This method is called from the POS frontend when the cashier selects
        NEXI as the payment method. It initiates the NEXI payment flow.
        
        Args:
            amount (float): The payment amount.
            payment_method_id (int, optional): ID of the NEXI payment method to use.
                                            If not provided, uses the first NEXI
                                            method available in the session.
            context (dict, optional): Additional context.
            
        Returns:
            dict: Result of the payment initiation:
                - success (bool): Whether initiation succeeded.
                - payment_id (int): ID of created pos.payment (if applicable).
                - transaction_id (int): ID of created nexi.transaction.
                - external_id (str): External reference for the transaction.
                - message (str): Status message.
        """
        self.ensure_one()
        
        # Security check
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            return {
                'success': False,
                'message': _('Permission denied.'),
            }
        
        # Validate order state
        if self.state in ['paid', 'done', 'invoiced']:
            return {
                'success': False,
                'message': _('Cannot add payment to a completed order.'),
            }
        
        # Validate amount
        if amount <= 0:
            return {
                'success': False,
                'message': _('Payment amount must be greater than zero.'),
            }
        
        # Determine payment method
        PaymentMethod = self.env['pos.payment.method']
        if payment_method_id:
            payment_method = PaymentMethod.browse(payment_method_id)
        else:
            # Find first NEXI payment method available in this session
            payment_method = self.session_id.payment_method_ids.filtered(
                lambda m: m.is_nexi_payment and m.nexi_terminal_id
            )[:1]
        
        if not payment_method or not payment_method.is_nexi_payment:
            return {
                'success': False,
                'message': _('No NEXI payment method available.'),
            }
        
        # Check terminal availability
        if payment_method.nexi_terminal_id.state != 'active':
            return {
                'success': False,
                'message': _('NEXI terminal is currently offline.'),
            }
        
        try:
            # Initiate NEXI payment
            result = payment_method.nexi_payment_request(self, amount)
            
            if result.get('success'):
                # Create the POS payment record (will be linked to transaction)
                payment_vals = {
                    'pos_order_id': self.id,
                    'payment_method_id': payment_method.id,
                    'amount': amount,
                    'payment_date': fields.Datetime.now(),
                }
                payment = self.env['pos.payment'].create(payment_vals)
                
                # Link transaction to payment
                if result.get('transaction_id'):
                    transaction = self.env['nexi.transaction'].browse(
                        result['transaction_id']
                    )
                    payment.write({'nexi_transaction_id': transaction.id})
                
                _logger.info(
                    'Added NEXI payment %.2f to order %s (payment_id=%s, transaction_id=%s)',
                    amount, self.name, payment.id, result.get('transaction_id')
                )
                
                result['payment_id'] = payment.id
                return result
            else:
                return result
                
        except Exception as e:
            _logger.exception('Error adding NEXI payment to order %s', self.name)
            return {
                'success': False,
                'message': _('Error adding payment: %s') % str(e),
            }
    
    def validate_nexi_payments(self):
        """Ensure all NEXI payments on this order are confirmed.
        
        This method checks the status of all linked NEXI transactions and
        returns whether all payments are complete and approved.
        
        Returns:
            dict: Validation result:
                - valid (bool): True if all NEXI payments are approved.
                - pending (list): IDs of pending transactions.
                - failed (list): IDs of failed/declined transactions.
                - message (str): Human-readable status message.
        """
        self.ensure_one()
        
        nexi_payments = self.payment_ids.filtered('nexi_transaction_id')
        
        if not nexi_payments:
            return {
                'valid': True,
                'pending': [],
                'failed': [],
                'message': _('No NEXI payments to validate.'),
            }
        
        pending = []
        failed = []
        approved = []
        
        for payment in nexi_payments:
            transaction = payment.nexi_transaction_id
            
            # Poll for latest status
            try:
                transaction.action_poll_status()
            except Exception as e:
                _logger.warning('Failed to poll NEXI transaction %s: %s',
                              transaction.external_id, str(e))

            if transaction.state == 'confirmed':
                approved.append(transaction.id)
            elif transaction.state in ('pending', 'processing', 'awaiting_confirm'):
                pending.append(transaction.id)
            else:
                failed.append({
                    'transaction_id': transaction.id,
                    'state': transaction.state,
                    'error': transaction.last_error,
                })
        
        if pending:
            return {
                'valid': False,
                'pending': pending,
                'failed': failed,
                'message': _('Some NEXI payments are still pending terminal approval.'),
            }
        
        if failed:
            error_details = ', '.join([
                '%s (%s)' % (f['state'], f['error'] or 'Unknown error')
                for f in failed
            ])
            return {
                'valid': False,
                'pending': [],
                'failed': [f['transaction_id'] for f in failed],
                'message': _('Some NEXI payments failed: %s') % error_details,
            }
        
        return {
            'valid': True,
            'pending': [],
            'failed': [],
            'message': _('All NEXI payments validated successfully.'),
        }
    
    # =========================================================================
    # Override Methods
    # =========================================================================
    
    def action_pos_order_paid(self):
        """Override to check NEXI payment status before marking order as paid.
        
        This ensures that all NEXI payments are confirmed before the order
        can be marked as paid. If any payments are still pending, it will
        prevent the order from being marked as paid.
        """
        for order in self:
            # Check if there are any pending NEXI payments
            pending_transactions = order.nexi_transaction_ids.filtered(
                lambda t: t.state == 'pending'
            )
            
            if pending_transactions:
                # Poll for latest status
                for transaction in pending_transactions:
                    try:
                        transaction.action_poll_status()
                    except Exception as e:
                        _logger.warning('Failed to poll NEXI transaction: %s', str(e))
                
                # Re-check after polling
                still_pending = order.nexi_transaction_ids.filtered(
                    lambda t: t.state == 'pending'
                )
                
                if still_pending:
                    raise UserError(_(
                        'Cannot mark order as paid. Some NEXI payments are still pending '
                        'terminal approval. Please wait for the terminal to complete '
                        'the transaction or cancel the payment.'
                    ))
            
            # Check for failed NEXI payments
            failed_transactions = order.nexi_transaction_ids.filtered(
                lambda t: t.state in ['failed', 'cancelled']
            )

            if failed_transactions:
                # Get list of failed transactions with details
                failed_details = '\n'.join([
                    '- %s: %s' % (
                        t.external_id or 'Unknown',
                        t.last_error or t.result_message or _('Transaction %s') % t.state
                    )
                    for t in failed_transactions
                ])
                
                raise UserError(_(
                    'Cannot mark order as paid. The following NEXI payments failed:\n%s\n\n'
                    'Please remove the failed payments and try again, or retry the payment.'
                ) % failed_details)
        
        return super(PosOrder, self).action_pos_order_paid()
    
    def action_pos_order_invoice(self):
        """Override to ensure NEXI payments are settled before invoicing."""
        for order in self:
            if order.nexi_transaction_ids.filtered(lambda t: t.state == 'pending'):
                raise UserError(_(
                    'Cannot create invoice while NEXI payments are pending. '
                    'Please wait for all terminal transactions to complete.'
                ))
        return super(PosOrder, self).action_pos_order_invoice()
    
    # =========================================================================
    # Action Methods
    # =========================================================================
    
    def action_view_nexi_transactions(self):
        """Open a view showing all NEXI transactions for this order."""
        self.ensure_one()
        
        action = {
            'type': 'ir.actions.act_window',
            'name': _('NEXI Transactions'),
            'res_model': 'nexi.transaction',
            'view_mode': 'tree,form',
            'domain': [('pos_order_id', '=', self.id)],
            'context': {'default_pos_order_id': self.id},
        }
        
        if len(self.nexi_transaction_ids) == 1:
            action.update({
                'view_mode': 'form',
                'res_id': self.nexi_transaction_ids.id,
            })
        
        return action
    
    def cancel_pending_nexi_payments(self):
        """Cancel all pending NEXI payments on this order.
        
        This is useful when the cashier needs to cancel the order or
        switch to a different payment method.
        
        Returns:
            dict: Result of the cancellation:
                - cancelled (int): Number of transactions cancelled.
                - message (str): Status message.
        """
        self.ensure_one()
        
        pending = self.nexi_transaction_ids.filtered(lambda t: t.state == 'pending')
        
        if not pending:
            return {
                'cancelled': 0,
                'message': _('No pending NEXI payments to cancel.'),
            }
        
        cancelled_count = 0
        errors = []
        
        for transaction in pending:
            try:
                transaction.action_cancel()
                cancelled_count += 1
            except Exception as e:
                _logger.exception('Error cancelling NEXI transaction %s', transaction.external_id)
                errors.append(str(e))
                # Still mark as cancelled to prevent stuck transactions
                transaction.write({
                    'state': 'cancelled',
                    'last_error': _('Cancelled due to error: %s') % str(e),
                })
                cancelled_count += 1
        
        if errors:
            return {
                'cancelled': cancelled_count,
                'message': _('Cancelled %s transaction(s). Errors: %s') % (
                    cancelled_count, ', '.join(errors)
                ),
            }
        
        return {
            'cancelled': cancelled_count,
            'message': _('Successfully cancelled %s NEXI payment(s).') % cancelled_count,
        }
