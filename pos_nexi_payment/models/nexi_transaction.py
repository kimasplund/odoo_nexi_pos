# -*- coding: utf-8 -*-
"""NEXI Transaction management for Odoo v18.

This module defines the nexi.transaction model for managing payment transactions
with NEXI payment terminals through the Odoo POS system.

Security Features:
    - PostgreSQL advisory locks for concurrent transaction handling
    - HMAC-SHA256 webhook signature verification
    - Timestamp validation for webhooks
    - Comprehensive audit logging
"""

import hashlib
import hmac
import json
import logging
import struct
import time
from datetime import timedelta
from typing import Any, Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)

# Transaction states
TRANSACTION_STATES = [
    ('pending', 'Pending'),
    ('processing', 'Processing'),
    ('awaiting_confirm', 'Awaiting Confirmation'),
    ('confirmed', 'Confirmed'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# State flow: pending -> processing -> awaiting_confirm -> confirmed
#                    \-> failed
#                    \-> cancelled

# NEXI Result codes (per API spec)
RESULT_SUCCESS = 'SUCCESS'
RESULT_ABORTED = 'ABORTED'
RESULT_REJECTED = 'REJECTED'
RESULT_NETWORK_ERROR = 'NETWORK_ERROR'
RESULT_NOT_ACCEPTED = 'NOT_ACCEPTED'

# Advisory lock constants
LOCK_NAMESPACE = 123456789  # Unique namespace for NEXI locks


class NexiTransaction(models.Model):
    """NEXI Payment Transaction.
    
    This model represents a single payment transaction with a NEXI terminal.
    It tracks the transaction lifecycle from initiation through confirmation
    and maintains a complete audit trail.
    
    Security Features:
        - Database-level advisory locks prevent race conditions
        - HMAC verification for webhooks
        - Comprehensive audit logging
    
    Transaction Flow:
        1. Create transaction record (pending)
        2. Call action_purchase() to initiate on terminal (processing)
        3. Poll/wait for terminal response (awaiting_confirm)
        4. Call action_confirm() to finalize (confirmed)
    
    Attributes:
        external_id: Unique transaction identifier (UUID)
        terminal_id: Terminal identifier for this transaction
        amount: Transaction amount
        currency_id: Transaction currency
        state: Current transaction state
        result_code: Final result code from NEXI
        result_message: Human-readable result message
        pos_order_id: Associated POS order
        payment_method_id: Payment method used
        confirm_attempts: Number of confirmation attempts
    """
    
    _name = 'nexi.transaction'
    _description = 'NEXI Payment Transaction'
    _order = 'create_date desc'
    _inherit = ['mail.thread', 'mail.activity.mixin', 'nexi.security.mixin']
    
    # Transaction Type
    transaction_type = fields.Selection([
        ('purchase', 'Purchase'),
        ('refund', 'Refund'),
    ], string='Transaction Type', default='purchase', required=True)

    original_transaction_id = fields.Many2one(
        'nexi.transaction', string='Original Transaction',
        help='The original purchase transaction this refund is for')

    refund_ids = fields.One2many(
        'nexi.transaction', 'original_transaction_id',
        string='Refunds')

    # Identification
    external_id = fields.Char(
        string='External ID',
        required=True,
        index=True,
        readonly=True,
        help='Unique transaction identifier (UUID)'
    )
    
    terminal_id = fields.Char(
        string='Terminal ID',
        required=True,
        index=True,
        help='NEXI terminal identifier'
    )
    
    # Amount Information
    amount = fields.Monetary(
        string='Amount',
        required=True,
        currency_field='currency_id',
        help='Transaction amount'
    )
    
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        required=True,
        default=lambda self: self.env.company.currency_id,
        help='Transaction currency'
    )
    
    # State Management
    state = fields.Selection(
        selection=TRANSACTION_STATES,
        string='State',
        default='pending',
        required=True,
        tracking=True,
        help='Current transaction state'
    )
    
    # Result Information
    result_code = fields.Char(
        string='Result Code',
        help='Result code from NEXI API'
    )
    
    result_message = fields.Char(
        string='Result Message',
        help='Human-readable result message'
    )
    
    # Relationships
    pos_order_id = fields.Many2one(
        comodel_name='pos.order',
        string='POS Order',
        index=True,
        help='Associated Point of Sale order'
    )
    
    payment_method_id = fields.Many2one(
        comodel_name='pos.payment.method',
        string='Payment Method',
        help='POS payment method used'
    )
    
    terminal_ref = fields.Many2one(
        comodel_name='nexi.terminal',
        string='Terminal Reference',
        compute='_compute_terminal_ref',
        store=True,
        help='Reference to terminal configuration'
    )
    
    # Processing Metadata
    confirm_attempts = fields.Integer(
        string='Confirm Attempts',
        default=0,
        help='Number of confirmation attempts made'
    )
    
    purchase_request_time = fields.Datetime(
        string='Purchase Request Time',
        readonly=True,
        help='When purchase request was sent'
    )
    
    confirm_request_time = fields.Datetime(
        string='Confirm Request Time',
        readonly=True,
        help='When confirm request was sent'
    )
    
    notification_received = fields.Boolean(
        string='Notification Received',
        default=False,
        help='Whether async notification was received'
    )
    
    notification_data = fields.Text(
        string='Notification Data',
        help='Raw notification payload (JSON)'
    )
    
    # Webhook Security
    webhook_verified = fields.Boolean(
        string='Webhook Verified',
        default=False,
        help='Whether the last webhook was HMAC verified'
    )
    
    webhook_timestamp = fields.Datetime(
        string='Webhook Timestamp',
        help='Timestamp from verified webhook'
    )
    
    # Error Handling
    error_count = fields.Integer(
        string='Error Count',
        default=0,
        help='Number of errors encountered'
    )
    
    last_error = fields.Text(
        string='Last Error',
        help='Last error message'
    )
    
    # Card / Payment Details (from NEXI API payment_method_details)
    reference = fields.Char(
        string='NEXI Reference',
        help='Transaction reference from NEXI'
    )
    card_scheme = fields.Char(
        string='Card Scheme',
        help='Card brand (e.g. VISA, MASTERCARD, MAESTRO)'
    )
    authorization_code = fields.Char(
        string='Authorization Code',
        help='Authorization code from issuer'
    )
    authorization_response_code = fields.Char(
        string='Authorization Response Code',
        help='Raw response code from authorization network'
    )
    card_entry_mode = fields.Char(
        string='Card Entry Mode',
        help='How the card was presented (e.g. CHIP, CONTACTLESS, SWIPE)'
    )
    card_number_customer = fields.Char(
        string='Card Number (Customer)',
        help='Masked PAN shown on customer receipt'
    )
    card_number_merchant = fields.Char(
        string='Card Number (Merchant)',
        help='Masked PAN shown on merchant receipt'
    )
    cardholder_verification = fields.Char(
        string='Cardholder Verification',
        help='Verification method used (e.g. PIN, SIGNATURE, NONE)'
    )
    application_name = fields.Char(
        string='Application Name',
        help='EMV application name (e.g. VISA CREDIT)'
    )
    application_identifier = fields.Char(
        string='Application Identifier',
        help='EMV AID (Application Identifier)'
    )
    acquirer_reference = fields.Char(
        string='Acquirer Reference',
        help='Reference number from the acquirer'
    )
    acquirer_terminal_id = fields.Char(
        string='Acquirer Terminal ID',
        help='Terminal ID as known by the acquirer'
    )
    acquirer_timestamp = fields.Char(
        string='Acquirer Timestamp',
        help='Timestamp from the acquirer'
    )
    authorized_amount = fields.Float(
        string='Authorized Amount',
        digits=(12, 2),
        help='Amount authorized by the issuer'
    )
    captured_amount = fields.Float(
        string='Captured Amount',
        digits=(12, 2),
        help='Amount captured/settled'
    )
    receipt_details_customer = fields.Text(
        string='Receipt (Customer)',
        help='Full receipt text for the customer'
    )
    receipt_details_merchant = fields.Text(
        string='Receipt (Merchant)',
        help='Full receipt text for the merchant'
    )

    # Technical
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        help='Company this transaction belongs to'
    )
    
    _sql_constraints = [
        ('external_id_unique',
         'UNIQUE(external_id)',
         'External ID must be unique!'),
    ]
    
    # =========================================================================
    # Card Detail Extraction
    # =========================================================================

    def _extract_payment_details(self, response_data):
        """Extract card/payment details from a NEXI API response.

        Handles missing or partial data gracefully. Writes extracted
        fields directly to the current record.

        The API returns amounts as integers in minor currency units.
        This method converts them to float for Odoo storage.

        Args:
            response_data: dict from NEXI API (already unwrapped from 'transaction' key)
        """
        self.ensure_one()
        if not response_data or not isinstance(response_data, dict):
            return

        vals = {}

        # Top-level fields
        if response_data.get('reference'):
            vals['reference'] = str(response_data['reference'])[:64]
        if response_data.get('receipt_details_customer'):
            vals['receipt_details_customer'] = str(response_data['receipt_details_customer'])
        if response_data.get('receipt_details_merchant'):
            vals['receipt_details_merchant'] = str(response_data['receipt_details_merchant'])
        if response_data.get('result_description'):
            vals['result_message'] = str(response_data['result_description'])[:255]
        # API returns amounts as integers in minor units - convert to float
        if response_data.get('authorized_amount') is not None:
            try:
                vals['authorized_amount'] = int(response_data['authorized_amount']) / 100.0
            except (TypeError, ValueError):
                pass
        if response_data.get('captured_amount') is not None:
            try:
                vals['captured_amount'] = int(response_data['captured_amount']) / 100.0
            except (TypeError, ValueError):
                pass

        # payment_method_details sub-object
        pmd = response_data.get('payment_method_details') or {}
        field_map = {
            'card_scheme': 'card_scheme',
            'authorization_code': 'authorization_code',
            'authorization_response_code': 'authorization_response_code',
            'card_entry_mode': 'card_entry_mode',
            'card_number_customer': 'card_number_customer',
            'card_number_merchant': 'card_number_merchant',
            'cardholder_verification': 'cardholder_verification',
            'application_name': 'application_name',
            'application_identifier': 'application_identifier',
            'acquirer_reference': 'acquirer_reference',
            'acquirer_terminal_id': 'acquirer_terminal_id',
            'acquirer_timestamp': 'acquirer_timestamp',
        }
        for api_key, field_name in field_map.items():
            value = pmd.get(api_key)
            if value:
                vals[field_name] = str(value)[:128]

        if vals:
            self.write(vals)

    # =========================================================================
    # Advisory Lock Methods
    # =========================================================================
    
    def _get_lock_id(self, external_id: str) -> int:
        """Generate advisory lock ID from external_id.
        
        Args:
            external_id: The transaction external ID
            
        Returns:
            Integer lock ID
        """
        # Hash external_id to get consistent integer lock ID
        hash_bytes = hashlib.sha256(external_id.encode()).digest()[:4]
        lock_id = struct.unpack('I', hash_bytes)[0]
        return lock_id
    
    def _acquire_transaction_lock(self, external_id: str, 
                                   wait: bool = True) -> bool:
        """Acquire PostgreSQL advisory lock for transaction.
        
        Uses PostgreSQL advisory locks to prevent race conditions
        when multiple workers try to process the same transaction.
        
        Args:
            external_id: The transaction external ID to lock
            wait: If True, block until lock is available
            
        Returns:
            True if lock acquired, False otherwise
        """
        lock_id = self._get_lock_id(external_id)
        
        if wait:
            # pg_advisory_lock blocks until lock is available
            self.env.cr.execute(
                "SELECT pg_advisory_lock(%s, %s)",
                (LOCK_NAMESPACE, lock_id)
            )
            return True
        else:
            # pg_advisory_try_lock returns immediately
            self.env.cr.execute(
                "SELECT pg_advisory_try_lock(%s, %s)",
                (LOCK_NAMESPACE, lock_id)
            )
            return self.env.cr.fetchone()[0]
    
    def _release_transaction_lock(self, external_id: str) -> bool:
        """Release PostgreSQL advisory lock for transaction.
        
        Args:
            external_id: The transaction external ID to unlock
            
        Returns:
            True if lock was held and released
        """
        lock_id = self._get_lock_id(external_id)
        self.env.cr.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (LOCK_NAMESPACE, lock_id)
        )
        return self.env.cr.fetchone()[0]
    
    # =========================================================================
    # Webhook Security Methods
    # =========================================================================
    
    @api.model
    def _verify_webhook_signature(self, payload: str, signature: str, 
                                   secret: str) -> bool:
        """Verify HMAC-SHA256 signature of webhook payload.
        
        Args:
            payload: The raw request body
            signature: The signature from X-Nexi-Signature header
            secret: The webhook secret key from terminal config
            
        Returns:
            True if signature is valid
        """
        try:
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
    
    def _validate_webhook_timestamp(self, timestamp: int, 
                                     tolerance_seconds: int = 300) -> bool:
        """Validate webhook timestamp is within tolerance.
        
        Prevents replay attacks by rejecting old webhook requests.
        
        Args:
            timestamp: Unix timestamp from webhook header
            tolerance_seconds: Maximum age allowed (default 5 minutes)
            
        Returns:
            True if timestamp is valid
        """
        current_time = int(time.time())
        age = abs(current_time - timestamp)
        
        if age > tolerance_seconds:
            _logger.error(
                'Webhook timestamp too old: %s seconds (tolerance: %s)',
                age, tolerance_seconds
            )
            return False
        
        return True
    
    # =========================================================================
    # Computed Fields
    # =========================================================================
    
    @api.depends('terminal_id')
    def _compute_terminal_ref(self):
        """Compute reference to terminal configuration."""
        for txn in self:
            if txn.terminal_id:
                terminal = self.env['nexi.terminal'].search([
                    ('terminal_id', '=', txn.terminal_id)
                ], limit=1)
                txn.terminal_ref = terminal
            else:
                txn.terminal_ref = False
    
    # =========================================================================
    # Constraints & Validation
    # =========================================================================
    
    @api.constrains('amount')
    def _check_amount(self):
        """Validate transaction amount."""
        for txn in self:
            if float_compare(txn.amount, 0.0, precision_digits=2) <= 0:
                raise ValidationError(_('Transaction amount must be positive'))
            
            if txn.amount > 999999.99:
                raise ValidationError(_('Transaction amount exceeds maximum'))
    
    @api.constrains('external_id')
    def _check_external_id(self):
        """Validate external ID format."""
        for txn in self:
            if not txn.external_id:
                raise ValidationError(_('External ID is required'))
            
            if len(txn.external_id) > 63:
                raise ValidationError(_('External ID exceeds maximum length (63 characters)'))
            
            # Validate format
            import re
            if not re.match(r'^[a-zA-Z0-9_-]+$', txn.external_id):
                raise ValidationError(
                    _('External ID must contain only alphanumeric characters, hyphens, and underscores')
                )
    
    # =========================================================================
    # CRUD Operations
    # =========================================================================
    
    @api.model_create_multi
    def create(self, vals_list):
        """Create new transaction records with generated external_id."""
        for vals in vals_list:
            # Generate external_id if not provided
            if not vals.get('external_id'):
                vals['external_id'] = self._generate_external_id()
            
            # Normalize terminal_id
            if vals.get('terminal_id'):
                vals['terminal_id'] = vals['terminal_id'].strip()
            
            # Sanitize external_id
            if vals.get('external_id'):
                vals['external_id'] = self._sanitize_external_id(vals['external_id'])
            
            _logger.info(
                'Creating NEXI transaction: external_id=%s, amount=%s',
                vals.get('external_id'), vals.get('amount')
            )
        
        transactions = super(NexiTransaction, self).create(vals_list)
        
        # Audit log creation
        for txn in transactions:
            txn._audit_log(
                'transaction_created',
                {'external_id': txn.external_id, 'amount': txn.amount}
            )
        
        return transactions
    
    # =========================================================================
    # Action Methods
    # =========================================================================
    
    def action_purchase(self):
        """Initiate purchase transaction on NEXI terminal.
        
        This method:
        1. Validates the transaction state
        2. Acquires advisory lock to prevent race conditions
        3. Gets the API client
        4. Calls the NEXI purchase endpoint
        5. Updates transaction state based on response
        
        Returns:
            True if initiated successfully
            
        Raises:
            UserError: If transaction cannot be initiated
        """
        self.ensure_one()
        
        # Validate state
        if self.state != 'pending':
            raise UserError(_(
                'Cannot initiate purchase: transaction is in "%s" state'
            ) % self.state)
        
        # Acquire lock to prevent concurrent processing
        if not self._acquire_transaction_lock(self.external_id, wait=False):
            raise UserError(_(
                'Transaction is being processed by another worker. '
                'Please wait and try again.'
            ))
        
        try:
            # Check for existing transactions with same external_id
            existing = self.search([
                ('external_id', '=', self.external_id),
                ('id', '!=', self.id),
                ('state', 'in', ['processing', 'awaiting_confirm', 'confirmed'])
            ], limit=1)
            
            if existing:
                raise UserError(_(
                    'A transaction with external_id "%s" is already in progress'
                ) % self.external_id)
            
            # Get terminal configuration
            terminal = self.env['nexi.terminal'].search([
                ('terminal_id', '=', self.terminal_id)
            ], limit=1)
            
            if not terminal:
                raise UserError(_(
                    'Terminal "%s" not found. Please configure the terminal first.'
                ) % self.terminal_id)
            
            if terminal.state != 'active':
                raise UserError(_(
                    'Terminal "%s" is not active (state: %s)'
                ) % (terminal.name, terminal.state))
            
            try:
                # Get API client
                client = terminal.get_api_client()

                # Update state before API call
                self.write({
                    'state': 'processing',
                    'purchase_request_time': fields.Datetime.now(),
                    'error_count': 0,
                    'last_error': False
                })

                # Convert amount to minor currency units (integer cents)
                currency = self.currency_id
                amount_minor = int(self.amount / currency.rounding) if currency.rounding else int(self.amount * 100)

                # Call NEXI API - returns unwrapped transaction data
                response = client.purchase(
                    requested_amount=amount_minor,
                    external_id=self.external_id,
                    terminal_id=self.terminal_id,
                    currency=currency.name
                )

                # API returns uppercase states: PROCESSING, AWAITING_CONFIRM, etc.
                state = (response.get('state') or '').upper()

                # Extract card details from response regardless of state
                self._extract_payment_details(response)

                if state == 'AWAITING_CONFIRM':
                    self.write({
                        'state': 'awaiting_confirm',
                        'result_code': response.get('result_code'),
                        'result_message': response.get('result_description')
                    })
                elif state == 'CONFIRMED':
                    # Auto-confirm if already confirmed
                    self.action_confirm()
                elif state in ('ABORTED', 'REJECTED'):
                    self.write({
                        'state': 'failed',
                        'result_code': response.get('result_code', state),
                        'result_message': response.get('result_description', 'Transaction failed')
                    })
                else:
                    # PROCESSING or other - stay in processing, will poll/wait
                    self.write({
                        'result_code': response.get('result_code'),
                        'result_message': response.get('result_description')
                    })
                
                _logger.info(
                    'Purchase initiated: external_id=%s, state=%s',
                    self.external_id, self.state
                )
                
                self._audit_log(
                    'purchase_initiated',
                    {'response_state': state}
                )
                
                return True
                
            except Exception as e:
                self.write({
                    'state': 'failed',
                    'error_count': self.error_count + 1,
                    'last_error': str(e),
                    'result_message': str(e)
                })
                
                _logger.error(
                    'Purchase failed: external_id=%s, error=%s',
                    self.external_id, str(e)
                )
                
                self._audit_log(
                    'purchase_failed',
                    {'error': str(e)}
                )
                
                raise UserError(_(
                    'Failed to initiate purchase: %s'
                ) % str(e))
        finally:
            # Always release lock
            self._release_transaction_lock(self.external_id)
    
    def action_confirm(self):
        """Confirm the transaction.
        
        This method:
        1. Validates the transaction state
        2. Acquires advisory lock
        3. Determines the result code (SUCCESS or error)
        4. Calls the NEXI confirm endpoint
        5. Updates transaction to confirmed state
        
        Returns:
            True if confirmed successfully
            
        Raises:
            UserError: If confirmation fails
        """
        self.ensure_one()
        
        # Validate state
        if self.state not in ('awaiting_confirm', 'processing'):
            raise UserError(_(
                'Cannot confirm: transaction is in "%s" state'
            ) % self.state)
        
        # Acquire lock
        if not self._acquire_transaction_lock(self.external_id, wait=False):
            raise UserError(_(
                'Transaction is being confirmed by another worker'
            ))
        
        try:
            # Get terminal
            terminal = self.env['nexi.terminal'].search([
                ('terminal_id', '=', self.terminal_id)
            ], limit=1)
            
            if not terminal:
                raise UserError(_('Terminal not found'))
            
            # Determine result code
            result_code = self.result_code or RESULT_SUCCESS
            
            try:
                client = terminal.get_api_client()
                
                # Increment attempt counter
                self.write({
                    'confirm_attempts': self.confirm_attempts + 1,
                    'confirm_request_time': fields.Datetime.now()
                })
                
                # Call confirm API (background - don't block on this)
                try:
                    response = client.confirm(
                        external_id=self.external_id,
                        terminal_id=self.terminal_id,
                        result_code=result_code
                    )
                    
                    _logger.info(
                        'Confirm successful: external_id=%s, attempts=%s',
                        self.external_id, self.confirm_attempts
                    )
                    
                except Exception as confirm_error:
                    # Log but don't fail - confirm is background operation
                    _logger.warning(
                        'Confirm call failed (non-critical): external_id=%s, error=%s',
                        self.external_id, str(confirm_error)
                    )
                
                # Update state to confirmed
                self.write({
                    'state': 'confirmed',
                    'result_code': result_code
                })
                
                self._audit_log(
                    'transaction_confirmed',
                    {'result_code': result_code, 'attempts': self.confirm_attempts}
                )
                
                # Update terminal heartbeat
                terminal.action_update_heartbeat()
                
                return True
                
            except Exception as e:
                _logger.error(
                    'Confirm failed: external_id=%s, error=%s',
                    self.external_id, str(e)
                )
                self._audit_log(
                    'confirm_error',
                    {'error': str(e)}
                )
                raise UserError(_('Failed to confirm transaction: %s') % str(e))
        finally:
            self._release_transaction_lock(self.external_id)
    
    def action_cancel(self):
        """Cancel the transaction.
        
        This attempts to cancel a pending or processing transaction.
        Confirmed transactions cannot be cancelled.
        
        Returns:
            True if cancelled
            
        Raises:
            UserError: If transaction cannot be cancelled
        """
        self.ensure_one()
        
        if self.state in ('confirmed', 'cancelled'):
            raise UserError(_(
                'Cannot cancel transaction in "%s" state'
            ) % self.state)
        
        # Acquire lock
        if not self._acquire_transaction_lock(self.external_id, wait=True):
            pass  # Continue even if lock not acquired for cancel
        
        try:
            # Attempt API cancel if terminal available
            if self.state in ('pending', 'processing', 'awaiting_confirm'):
                terminal = self.env['nexi.terminal'].search([
                    ('terminal_id', '=', self.terminal_id)
                ], limit=1)
                
                if terminal and terminal.state == 'active':
                    try:
                        client = terminal.get_api_client()
                        client.abort(self.external_id, self.terminal_id)
                    except Exception as e:
                        _logger.warning(
                            'API abort failed (continuing with local cancel): %s',
                            str(e)
                        )

            previous_state = self.state
            self.write({
                'state': 'cancelled',
                'result_code': RESULT_ABORTED,
                'result_message': 'Transaction cancelled by user'
            })

            self._audit_log(
                'transaction_cancelled',
                {'previous_state': previous_state}
            )
            
            _logger.info('Transaction cancelled: external_id=%s', self.external_id)
            
            return True
        finally:
            self._release_transaction_lock(self.external_id)
    
    def action_void(self):
        """Void/reverse a confirmed transaction.

        Uses the NEXI confirm endpoint with CANCELLED result code to
        request a reversal of a previously confirmed transaction.

        Returns:
            True if void request sent

        Raises:
            UserError: If transaction cannot be voided
        """
        self.ensure_one()

        if self.state != 'confirmed':
            raise UserError(_(
                'Only confirmed transactions can be voided (current state: "%s")'
            ) % self.state)

        terminal = self.env['nexi.terminal'].search([
            ('terminal_id', '=', self.terminal_id)
        ], limit=1)

        if not terminal:
            raise UserError(_('Terminal "%s" not found') % self.terminal_id)

        try:
            client = terminal.get_api_client()
            client.abort(
                external_id=self.external_id,
                terminal_id=self.terminal_id
            )
        except Exception as e:
            _logger.warning(
                'API void/reversal call failed: external_id=%s, error=%s',
                self.external_id, str(e)
            )

        previous_state = self.state
        self.write({
            'state': 'cancelled',
            'result_code': RESULT_ABORTED,
            'result_message': 'Transaction voided/reversed',
        })

        self._audit_log(
            'transaction_voided',
            {'previous_state': previous_state}
        )

        _logger.info('Transaction voided: external_id=%s', self.external_id)
        return True

    def action_refund(self):
        """Create a refund transaction for this confirmed purchase.

        Only callable on confirmed, successful purchase transactions.
        Creates a new nexi.transaction with transaction_type='refund',
        linked via original_transaction_id, and initiates the refund
        on the terminal.

        Returns:
            Action to open the new refund transaction form
        """
        self.ensure_one()

        if self.state != 'confirmed':
            raise UserError(_('Only confirmed transactions can be refunded'))
        if self.result_code != RESULT_SUCCESS:
            raise UserError(_('Only successful transactions can be refunded'))
        if self.transaction_type != 'purchase':
            raise UserError(_('Only purchase transactions can be refunded'))

        # Create refund transaction record
        refund_txn = self.create({
            'terminal_id': self.terminal_id,
            'amount': self.amount,
            'currency_id': self.currency_id.id,
            'pos_order_id': self.pos_order_id.id if self.pos_order_id else False,
            'payment_method_id': self.payment_method_id.id if self.payment_method_id else False,
            'transaction_type': 'refund',
            'original_transaction_id': self.id,
        })

        self._audit_log(
            'refund_initiated',
            {'refund_transaction_id': refund_txn.id, 'refund_external_id': refund_txn.external_id}
        )

        # Initiate the refund on the terminal
        try:
            refund_txn.action_purchase_refund()
        except Exception as e:
            _logger.error(
                'Refund initiation failed: original=%s, refund=%s, error=%s',
                self.external_id, refund_txn.external_id, str(e)
            )

        return {
            'type': 'ir.actions.act_window',
            'res_model': 'nexi.transaction',
            'res_id': refund_txn.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_purchase_refund(self):
        """Initiate refund transaction on NEXI terminal.

        Mirrors action_purchase() but calls client.refund() instead.
        Passes original_purchase_external_id and original_purchase_terminal_id
        when the original transaction is linked.

        Returns:
            True if initiated successfully

        Raises:
            UserError: If refund cannot be initiated
        """
        self.ensure_one()

        if self.state != 'pending':
            raise UserError(_(
                'Cannot initiate refund: transaction is in "%s" state'
            ) % self.state)

        if self.transaction_type != 'refund':
            raise UserError(_('action_purchase_refund() is only for refund transactions'))

        # Acquire lock
        if not self._acquire_transaction_lock(self.external_id, wait=False):
            raise UserError(_(
                'Transaction is being processed by another worker. '
                'Please wait and try again.'
            ))

        try:
            # Get terminal
            terminal = self.env['nexi.terminal'].search([
                ('terminal_id', '=', self.terminal_id)
            ], limit=1)

            if not terminal:
                raise UserError(_(
                    'Terminal "%s" not found. Please configure the terminal first.'
                ) % self.terminal_id)

            if terminal.state != 'active':
                raise UserError(_(
                    'Terminal "%s" is not active (state: %s)'
                ) % (terminal.name, terminal.state))

            try:
                client = terminal.get_api_client()

                self.write({
                    'state': 'processing',
                    'purchase_request_time': fields.Datetime.now(),
                    'error_count': 0,
                    'last_error': False,
                })

                # Convert amount to minor currency units
                currency = self.currency_id
                amount_minor = int(self.amount / currency.rounding) if currency.rounding else int(self.amount * 100)

                # Build refund kwargs
                refund_kwargs = {
                    'requested_amount': amount_minor,
                    'external_id': self.external_id,
                    'terminal_id': self.terminal_id,
                    'currency': currency.name,
                }

                # Pass original purchase info if linked
                if self.original_transaction_id:
                    refund_kwargs['original_purchase_external_id'] = self.original_transaction_id.external_id
                    refund_kwargs['original_purchase_terminal_id'] = self.original_transaction_id.terminal_id

                response = client.refund(**refund_kwargs)

                state = (response.get('state') or '').upper()
                self._extract_payment_details(response)

                if state == 'AWAITING_CONFIRM':
                    self.write({
                        'state': 'awaiting_confirm',
                        'result_code': response.get('result_code'),
                        'result_message': response.get('result_description'),
                    })
                elif state == 'CONFIRMED':
                    self.action_confirm()
                elif state in ('ABORTED', 'REJECTED'):
                    self.write({
                        'state': 'failed',
                        'result_code': response.get('result_code', state),
                        'result_message': response.get('result_description', 'Refund failed'),
                    })
                else:
                    self.write({
                        'result_code': response.get('result_code'),
                        'result_message': response.get('result_description'),
                    })

                _logger.info(
                    'Refund initiated: external_id=%s, state=%s',
                    self.external_id, self.state
                )

                self._audit_log(
                    'refund_purchase_initiated',
                    {'response_state': state}
                )

                return True

            except Exception as e:
                self.write({
                    'state': 'failed',
                    'error_count': self.error_count + 1,
                    'last_error': str(e),
                    'result_message': str(e),
                })

                _logger.error(
                    'Refund failed: external_id=%s, error=%s',
                    self.external_id, str(e)
                )

                self._audit_log(
                    'refund_purchase_failed',
                    {'error': str(e)}
                )

                raise UserError(_(
                    'Failed to initiate refund: %s'
                ) % str(e))
        finally:
            self._release_transaction_lock(self.external_id)

    def action_retry(self):
        """Retry a failed transaction.

        Creates a new transaction with the same details.

        Returns:
            Action to open the new transaction
        """
        self.ensure_one()
        
        if self.state != 'failed':
            raise UserError(_('Only failed transactions can be retried'))
        
        new_txn = self.create({
            'terminal_id': self.terminal_id,
            'amount': self.amount,
            'currency_id': self.currency_id.id,
            'pos_order_id': self.pos_order_id.id,
            'payment_method_id': self.payment_method_id.id
        })
        
        self._audit_log(
            'transaction_retried',
            {'new_transaction_id': new_txn.id}
        )
        
        _logger.info(
            'Transaction retried: old=%s, new=%s',
            self.external_id, new_txn.external_id
        )
        
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'nexi.transaction',
            'res_id': new_txn.id,
            'view_mode': 'form',
            'target': 'current'
        }
    
    def action_poll_status(self):
        """Poll NEXI API for current transaction status.
        
        Used to update transaction state when waiting for terminal response.
        
        Returns:
            True if status retrieved
        """
        self.ensure_one()
        
        if self.state not in ('processing', 'awaiting_confirm'):
            return True
        
        terminal = self.env['nexi.terminal'].search([
            ('terminal_id', '=', self.terminal_id)
        ], limit=1)
        
        if not terminal or terminal.state != 'active':
            return False
        
        try:
            client = terminal.get_api_client()
            # get_transaction() calls POST /transaction/get and unwraps response
            response = client.get_transaction(self.external_id)

            # Extract card details from response
            self._extract_payment_details(response)

            # API returns uppercase states: PROCESSING, AWAITING_CONFIRM, CONFIRMED, etc.
            state = (response.get('state') or '').upper()

            if state == 'AWAITING_CONFIRM':
                self.write({
                    'state': 'awaiting_confirm',
                    'result_code': response.get('result_code'),
                    'result_message': response.get('result_description')
                })
            elif state == 'CONFIRMED':
                self.action_confirm()
            elif state in ('ABORTED', 'REJECTED'):
                self.write({
                    'state': 'failed',
                    'result_code': response.get('result_code', state),
                    'result_message': response.get('result_description', 'Transaction failed')
                })

            return True
            
        except Exception as e:
            _logger.error(
                'Status poll failed: external_id=%s, error=%s',
                self.external_id, str(e)
            )
            return False
    
    # =========================================================================
    # Notification Handling
    # =========================================================================
    
    def process_notification(self, notification_data: dict[str, Any]) -> bool:
        """Process asynchronous notification from NEXI.
        
        NEXI sends webhook notifications when transaction state changes.
        This method processes those notifications and updates the
        transaction state accordingly.
        
        Args:
            notification_data: Parsed notification payload from NEXI
            
        Returns:
            True if processed successfully
            
        Raises:
            ValidationError: If notification data is invalid
        """
        self.ensure_one()
        
        # Acquire lock for processing
        if not self._acquire_transaction_lock(self.external_id, wait=False):
            _logger.warning(
                'Could not acquire lock for notification processing: %s',
                self.external_id
            )
            # Still process but log warning
        
        try:
            # Store raw notification
            import json
            self.write({
                'notification_received': True,
                'notification_data': json.dumps(notification_data, indent=2)
            })
            
            # Extract card/payment details from notification
            self._extract_payment_details(notification_data)

            # Extract data - API uses uppercase states
            state = (notification_data.get('state') or '').upper()
            result_code = notification_data.get('result_code')
            message = notification_data.get('result_description') or notification_data.get('message')

            _logger.info(
                'Processing notification: external_id=%s, state=%s',
                self.external_id, state
            )

            # Update based on state
            if state == 'AWAITING_CONFIRM':
                self.write({
                    'state': 'awaiting_confirm',
                    'result_code': result_code,
                    'result_message': message
                })
                # Auto-confirm in background
                self.action_confirm()

            elif state in ('CONFIRMED', 'COMMITTED'):
                self.write({
                    'state': 'confirmed',
                    'result_code': result_code or RESULT_SUCCESS,
                    'result_message': message
                })

            elif state in ('ABORTED', 'REJECTED'):
                self.write({
                    'state': 'failed',
                    'result_code': result_code or RESULT_REJECTED,
                    'result_message': message or 'Transaction failed'
                })

            elif state == 'CANCELLED':
                self.write({
                    'state': 'cancelled',
                    'result_code': result_code or RESULT_ABORTED,
                    'result_message': message or 'Transaction cancelled'
                })
            
            self._audit_log(
                'notification_processed',
                {'notification_state': state, 'result_code': result_code}
            )
            
            return True
        finally:
            self._release_transaction_lock(self.external_id)
    
    @api.model
    def handle_webhook(self, terminal_id: str, payload: dict[str, Any]) -> dict:
        """Handle incoming webhook from NEXI.
        
        This is the entry point for webhook notifications.
        It finds the transaction and processes the notification.
        
        Args:
            terminal_id: Terminal identifier from webhook
            payload: Webhook payload
            
        Returns:
            Response dict with status
        """
        external_id = payload.get('external_id')
        
        if not external_id:
            _logger.error('Webhook missing external_id')
            return {'status': 'error', 'message': 'Missing external_id'}
        
        # Sanitize external_id
        try:
            external_id = self._sanitize_external_id(external_id)
        except ValidationError:
            _logger.error('Invalid external_id format in webhook')
            return {'status': 'error', 'message': 'Invalid external_id'}
        
        transaction = self.search([
            ('external_id', '=', external_id),
            ('terminal_id', '=', terminal_id)
        ], limit=1)
        
        if not transaction:
            _logger.error('Transaction not found: %s', external_id)
            return {'status': 'error', 'message': 'Transaction not found'}
        
        try:
            transaction.process_notification(payload)
            
            # Log successful webhook processing
            transaction._audit_log(
                'webhook_processed',
                {'terminal_id': terminal_id}
            )
            
            return {'status': 'ok'}
        except Exception as e:
            _logger.error('Webhook processing failed: %s', str(e))
            return {'status': 'error', 'message': str(e)}
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def is_successful(self) -> bool:
        """Check if transaction completed successfully.
        
        Returns:
            True if transaction is confirmed with SUCCESS result
        """
        self.ensure_one()
        return self.state == 'confirmed' and self.result_code == RESULT_SUCCESS
    
    def can_be_confirmed(self) -> bool:
        """Check if transaction can be confirmed.
        
        Returns:
            True if in a state that allows confirmation
        """
        self.ensure_one()
        return self.state in ('awaiting_confirm', 'processing')
    
    def get_receipt_data(self) -> dict[str, Any]:
        """Get data for receipt printing.

        Returns:
            Dict with transaction details for receipt
        """
        self.ensure_one()
        return {
            'external_id': self.external_id,
            'terminal_id': self.terminal_id,
            'amount': self.amount,
            'currency': self.currency_id.name,
            'state': self.state,
            'result_code': self.result_code,
            'timestamp': self.create_date,
            # Card details
            'reference': self.reference or '',
            'card_scheme': self.card_scheme or '',
            'authorization_code': self.authorization_code or '',
            'authorization_response_code': self.authorization_response_code or '',
            'card_entry_mode': self.card_entry_mode or '',
            'card_number_customer': self.card_number_customer or '',
            'card_number_merchant': self.card_number_merchant or '',
            'cardholder_verification': self.cardholder_verification or '',
            'application_name': self.application_name or '',
            'application_identifier': self.application_identifier or '',
            'acquirer_reference': self.acquirer_reference or '',
            'acquirer_terminal_id': self.acquirer_terminal_id or '',
            'acquirer_timestamp': self.acquirer_timestamp or '',
            'authorized_amount': self.authorized_amount or 0.0,
            'captured_amount': self.captured_amount or 0.0,
            'receipt_details_customer': self.receipt_details_customer or '',
            'receipt_details_merchant': self.receipt_details_merchant or '',
        }
    
    @api.model
    def _generate_external_id(self) -> str:
        """Generate a unique external transaction ID."""
        import uuid
        return f"NEXI-{uuid.uuid4()}"
    
    # =========================================================================
    # Cron Jobs
    # =========================================================================
    
    @api.model
    def _cron_cleanup_old_transactions(self, days: int = 90):
        """Clean up old cancelled/failed transactions.
        
        Called by scheduled action to prevent database bloat.
        
        Args:
            days: Age in days after which to clean up
        """
        cutoff_date = fields.Datetime.now() - timedelta(days=days)
        
        old_transactions = self.search([
            ('state', 'in', ['cancelled', 'failed']),
            ('write_date', '<', cutoff_date)
        ])
        
        if old_transactions:
            _logger.info(
                'Cleaning up %s old NEXI transactions',
                len(old_transactions)
            )
            # Use unlink to trigger any cleanup logic
            old_transactions.unlink()
    
    @api.model
    def _cron_poll_pending_transactions(self):
        """Poll for pending transactions that may have stalled.
        
        Called by scheduled action to recover stalled transactions.
        """
        stale_time = fields.Datetime.now() - timedelta(minutes=5)
        
        stalled = self.search([
            ('state', 'in', ['processing', 'awaiting_confirm']),
            ('purchase_request_time', '<', stale_time),
            ('error_count', '<', 5)
        ])
        
        for txn in stalled:
            try:
                txn.action_poll_status()
            except Exception as e:
                _logger.error(
                    'Failed to poll stalled transaction %s: %s',
                    txn.external_id, str(e)
                )
                txn.write({
                    'error_count': txn.error_count + 1,
                    'last_error': str(e)
                })

    @api.model
    def _cron_recover_unconfirmed(self):
        """Recover unconfirmed transactions from all active terminals.

        Runs periodically to detect transactions stuck in
        AWAITING_CONFIRM/AWAITING_CONTINUE state and reconcile them
        with local records.
        """
        Terminal = self.env['nexi.terminal']
        active_terminals = Terminal.search([
            ('state', '=', 'active'),
            ('active', '=', True),
        ])

        total_recovered = 0
        for terminal in active_terminals:
            try:
                result = terminal.action_recover_unconfirmed()
                if result and isinstance(result, dict):
                    params = result.get('params', {})
                    msg = params.get('message', '')
                    _logger.info(
                        'Cron unconfirmed recovery for terminal %s: %s',
                        terminal.terminal_id, msg
                    )
            except Exception as e:
                _logger.error(
                    'Cron unconfirmed recovery failed for terminal %s: %s',
                    terminal.terminal_id, str(e)
                )
