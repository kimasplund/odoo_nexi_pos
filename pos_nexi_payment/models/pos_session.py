# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PosSession(models.Model):
    """Extend pos.session to manage NEXI terminal lifecycle."""
    
    _inherit = 'pos.session'
    
    # =========================================================================
    # Fields
    # =========================================================================
    
    nexi_terminal_ids = fields.Many2many(
        comodel_name='nexi.terminal',
        string='NEXI Terminals',
        compute='_compute_nexi_terminal_ids',
        store=True,
        help='NEXI terminals available in this session.',
    )
    
    has_nexi_terminal = fields.Boolean(
        string='Has NEXI Terminal',
        compute='_compute_has_nexi_terminal',
        store=True,
    )
    
    active_nexi_transactions_count = fields.Integer(
        string='Active NEXI Transactions',
        compute='_compute_active_nexi_transactions',
    )
    
    # =========================================================================
    # Compute Methods
    # =========================================================================
    
    @api.depends('payment_method_ids.nexi_terminal_id')
    def _compute_nexi_terminal_ids(self):
        """Compute NEXI terminals available in this session."""
        for session in self:
            terminals = session.payment_method_ids.mapped('nexi_terminal_id')
            session.nexi_terminal_ids = terminals
    
    @api.depends('nexi_terminal_ids')
    def _compute_has_nexi_terminal(self):
        """Check if session has any NEXI terminals."""
        for session in self:
            session.has_nexi_terminal = bool(session.nexi_terminal_ids)
    
    def _compute_active_nexi_transactions(self):
        """Count active (pending) NEXI transactions in this session."""
        for session in self:
            terminal_ids = session.nexi_terminal_ids.mapped('terminal_id')
            session.active_nexi_transactions_count = self.env['nexi.transaction'].search_count([
                ('terminal_id', 'in', terminal_ids),
                ('state', 'in', ('pending', 'processing', 'awaiting_confirm')),
            ])
    
    # =========================================================================
    # Business Methods
    # =========================================================================
    
    def open_nexi_terminal(self):
        """Open/check NEXI terminals at session start.
        
        This method is called when a POS session is opened. It:
        1. Verifies all configured NEXI terminals are online
        2. Opens connections to the terminals
        3. Updates terminal status
        
        Returns:
            dict: Result for each terminal:
                - terminal_id (int): Terminal ID
                - name (str): Terminal name
                - success (bool): Whether the terminal was successfully opened
                - message (str): Status message
        """
        self.ensure_one()
        
        if not self.nexi_terminal_ids:
            return {'terminals': [], 'all_ready': True}
        
        results = []
        all_ready = True
        
        for terminal in self.nexi_terminal_ids:
            try:
                if terminal.state == 'active':
                    # Terminal already active - test connection
                    terminal.action_test_connection()
                    terminal.action_update_heartbeat()
                    results.append({
                        'terminal_id': terminal.id,
                        'name': terminal.name,
                        'success': True,
                        'message': _('Terminal ready.'),
                    })
                    _logger.info('NEXI terminal %s opened successfully for session %s',
                               terminal.name, self.name)
                else:
                    # Try to activate the terminal
                    try:
                        terminal.action_activate()
                        results.append({
                            'terminal_id': terminal.id,
                            'name': terminal.name,
                            'success': True,
                            'message': _('Terminal activated.'),
                        })
                    except Exception as activate_error:
                        results.append({
                            'terminal_id': terminal.id,
                            'name': terminal.name,
                            'success': False,
                            'message': _('Terminal is not active (state: %s)') % terminal.state,
                        })
                        all_ready = False

            except Exception as e:
                _logger.exception('Error opening NEXI terminal %s', terminal.name)
                results.append({
                    'terminal_id': terminal.id,
                    'name': terminal.name,
                    'success': False,
                    'message': _('Error: %s') % str(e),
                })
                all_ready = False
        
        return {
            'terminals': results,
            'all_ready': all_ready,
        }
    
    def close_nexi_terminal(self):
        """Close NEXI terminals at session end.
        
        This method is called when a POS session is closed. It:
        1. Checks for any pending transactions
        2. Attempts to cancel pending transactions
        3. Closes connections to the terminals
        4. Performs end-of-session reconciliation
        
        Returns:
            dict: Result for each terminal:
                - terminal_id (int): Terminal ID
                - name (str): Terminal name
                - success (bool): Whether the terminal was successfully closed
                - message (str): Status message
        """
        self.ensure_one()
        
        if not self.nexi_terminal_ids:
            return {'terminals': [], 'all_closed': True}
        
        results = []
        all_closed = True
        
        # First, check for pending transactions
        terminal_ids = self.nexi_terminal_ids.mapped('terminal_id')
        pending_transactions = self.env['nexi.transaction'].search([
            ('terminal_id', 'in', terminal_ids),
            ('state', 'in', ('pending', 'processing', 'awaiting_confirm')),
        ])
        
        if pending_transactions:
            _logger.warning(
                'Found %s pending NEXI transactions when closing session %s',
                len(pending_transactions), self.name
            )
            
            # Attempt to cancel pending transactions
            for transaction in pending_transactions:
                try:
                    transaction.action_cancel()
                    _logger.info('Cancelled pending transaction %s', transaction.external_id)
                except Exception as e:
                    _logger.exception('Error cancelling transaction %s', transaction.external_id)
                    # Still mark as cancelled to prevent stuck transactions
                    transaction.write({
                        'state': 'cancelled',
                        'last_error': _('Session closed while pending: %s') % str(e),
                    })

        # Now deactivate each terminal
        for terminal in self.nexi_terminal_ids:
            try:
                terminal.action_deactivate()
                results.append({
                    'terminal_id': terminal.id,
                    'name': terminal.name,
                    'success': True,
                    'message': _('Terminal closed successfully.'),
                })
                _logger.info('NEXI terminal %s closed for session %s',
                           terminal.name, self.name)

            except Exception as e:
                _logger.exception('Error closing NEXI terminal %s', terminal.name)
                results.append({
                    'terminal_id': terminal.id,
                    'name': terminal.name,
                    'success': False,
                    'message': _('Error: %s') % str(e),
                })
                all_closed = False
        
        return {
            'terminals': results,
            'all_closed': all_closed,
        }
    
    def get_nexi_transactions(self, domain=None):
        """Get all NEXI transactions for this session.
        
        Args:
            domain (list, optional): Additional search domain filters.
            
        Returns:
            RecordSet: NEXI transaction records for this session.
        """
        self.ensure_one()
        
        if not self.nexi_terminal_ids:
            return self.env['nexi.transaction']

        terminal_ids = self.nexi_terminal_ids.mapped('terminal_id')
        base_domain = [
            ('terminal_id', 'in', terminal_ids),
        ]
        
        if domain:
            base_domain.extend(domain)
        
        return self.env['nexi.transaction'].search(base_domain, order='create_date desc')
    
    def get_nexi_reconciliation_data(self):
        """Get reconciliation data for NEXI terminals in this session.
        
        This method returns data needed for end-of-day reconciliation,
        including transaction totals by state.
        
        Returns:
            dict: Reconciliation data:
                - by_terminal (dict): Data grouped by terminal.
                - totals (dict): Overall totals.
        """
        self.ensure_one()
        
        if not self.nexi_terminal_ids:
            return {
                'by_terminal': {},
                'totals': {
                    'count': 0,
                    'approved_amount': 0.0,
                    'refunded_amount': 0.0,
                },
            }
        
        by_terminal = {}
        totals = {
            'count': 0,
            'approved_count': 0,
            'approved_amount': 0.0,
            'refunded_count': 0,
            'refunded_amount': 0.0,
            'pending_count': 0,
            'pending_amount': 0.0,
        }
        
        for terminal in self.nexi_terminal_ids:
            transactions = self.env['nexi.transaction'].search([
                ('terminal_id', '=', terminal.terminal_id),
                ('create_date', '>=', self.start_at),
            ])

            confirmed = transactions.filtered(lambda t: t.state == 'confirmed')
            cancelled = transactions.filtered(lambda t: t.state == 'cancelled')
            pending = transactions.filtered(lambda t: t.state in ('pending', 'processing', 'awaiting_confirm'))

            terminal_data = {
                'terminal_name': terminal.name,
                'terminal_code': terminal.terminal_id,
                'total_transactions': len(transactions),
                'approved_count': len(confirmed),
                'approved_amount': sum(confirmed.mapped('amount')),
                'refunded_count': len(cancelled),
                'refunded_amount': sum(cancelled.mapped('amount')),
                'pending_count': len(pending),
                'pending_amount': sum(pending.mapped('amount')),
            }
            
            by_terminal[terminal.id] = terminal_data
            
            totals['count'] += len(transactions)
            totals['approved_count'] += len(confirmed)
            totals['approved_amount'] += terminal_data['approved_amount']
            totals['refunded_count'] += len(cancelled)
            totals['refunded_amount'] += terminal_data['refunded_amount']
            totals['pending_count'] += len(pending)
            totals['pending_amount'] += terminal_data['pending_amount']
        
        return {
            'by_terminal': by_terminal,
            'totals': totals,
        }
    
    # =========================================================================
    # Override Methods
    # =========================================================================
    
    def action_pos_session_open(self):
        """Override to open NEXI terminals when session opens."""
        result = super(PosSession, self).action_pos_session_open()
        
        for session in self:
            if session.nexi_terminal_ids:
                open_result = session.open_nexi_terminal()
                
                if not open_result.get('all_ready'):
                    failed = [t for t in open_result.get('terminals', []) if not t['success']]
                    failed_names = ', '.join([t['name'] for t in failed])
                    _logger.warning(
                        'Some NEXI terminals failed to open for session %s: %s',
                        session.name, failed_names
                    )
                    # Note: We don't raise an error here to allow the session to open
                    # The POS frontend can check terminal status and show warnings
        
        return result
    
    def action_pos_session_closing_control(self):
        """Override to check NEXI terminals before allowing session close."""
        for session in self:
            if session.nexi_terminal_ids:
                # Check for pending transactions
                terminal_ids = session.nexi_terminal_ids.mapped('terminal_id')
                pending_count = self.env['nexi.transaction'].search_count([
                    ('terminal_id', 'in', terminal_ids),
                    ('state', 'in', ('pending', 'processing', 'awaiting_confirm')),
                ])
                
                if pending_count:
                    raise UserError(_(
                        'Cannot close session. There are %s pending NEXI transactions. '
                        'Please complete or cancel all terminal transactions before closing.'
                    ) % pending_count)
        
        return super(PosSession, self).action_pos_session_closing_control()
    
    def action_pos_session_close(self):
        """Override to close NEXI terminals when session closes."""
        for session in self:
            if session.nexi_terminal_ids:
                close_result = session.close_nexi_terminal()
                
                if not close_result.get('all_closed'):
                    failed = [t for t in close_result.get('terminals', []) if not t['success']]
                    failed_names = ', '.join([t['name'] for t in failed])
                    _logger.error(
                        'Some NEXI terminals failed to close for session %s: %s',
                        session.name, failed_names
                    )
                    # Note: We still proceed with session close even if terminal close fails
                    # The terminal may need manual intervention
        
        return super(PosSession, self).action_pos_session_close()
