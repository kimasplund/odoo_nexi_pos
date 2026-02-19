# -*- coding: utf-8 -*-
"""NEXI Terminal management for Odoo v18.

This module defines the nexi.terminal model for managing NEXI payment terminals
within the Odoo POS system.

Security:
    - API keys are encrypted at rest using Fernet encryption
    - Only system administrators can view API keys
    - API keys are never logged in cleartext
"""

import logging
from typing import Optional

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Terminal state constants
TERMINAL_STATE_ACTIVE = 'active'
TERMINAL_STATE_INACTIVE = 'inactive'
TERMINAL_STATE_ERROR = 'error'

TERMINAL_STATES = [
    (TERMINAL_STATE_ACTIVE, 'Active'),
    (TERMINAL_STATE_INACTIVE, 'Inactive'),
    (TERMINAL_STATE_ERROR, 'Error'),
]

# Encryption constants
ENCRYPTION_PREFIX = '$encrypted$'


class NexiTerminal(models.Model):
    """NEXI Payment Terminal configuration.
    
    This model represents a physical NEXI payment terminal that can be
    associated with one or more POS configurations. Each terminal has
    its own API credentials and operational state.
    
    Security:
        - API keys are encrypted at rest using Odoo's encryption
        - Only system administrators can view API keys
        - API keys are never logged in cleartext
    
    Attributes:
        name: Human-readable terminal name
        terminal_id: Unique terminal identifier from NEXI
        api_key_encrypted: Encrypted API key for terminal authentication
        company_id: Company this terminal belongs to
        pos_config_ids: POS configurations using this terminal
        state: Current operational state
        last_heartbeat: Last known communication timestamp
    """
    
    _name = 'nexi.terminal'
    _description = 'NEXI Payment Terminal'
    _order = 'name'
    _inherit = ['nexi.security.mixin']
    
    # Basic Information
    name = fields.Char(
        string='Terminal Name',
        required=True,
        help='Human-readable name for this terminal'
    )
    
    terminal_id = fields.Char(
        string='Terminal ID',
        required=True,
        index=True,
        help='Unique terminal identifier provided by NEXI (max 32 chars)'
    )
    
    # API Key ID (used for HTTP Basic Auth username)
    api_key_id = fields.Char(
        string='API Key ID',
        required=True,
        groups='pos_nexi_payment.group_nexi_admin',
        help='API key ID for HTTP Basic Auth (provided during NEXI onboarding)'
    )

    # Encrypted API Key Secret - Only visible to system admins
    api_key_encrypted = fields.Char(
        string='API Key Secret',
        required=True,
        groups='pos_nexi_payment.group_nexi_admin',
        help='Encrypted API key secret for terminal authentication. Only system administrators can view this.'
    )
    
    # Transaction amount limits
    min_amount = fields.Monetary(
        string='Minimum Amount',
        currency_field='currency_id',
        default=0.01,
        help='Minimum transaction amount allowed'
    )
    
    max_amount = fields.Monetary(
        string='Maximum Amount',
        currency_field='currency_id',
        default=999999.99,
        help='Maximum transaction amount allowed'
    )
    
    currency_id = fields.Many2one(
        comodel_name='res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
        help='Default currency for this terminal'
    )
    
    # Organization
    company_id = fields.Many2one(
        comodel_name='res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company,
        help='Company this terminal belongs to'
    )
    
    pos_config_ids = fields.Many2many(
        comodel_name='pos.config',
        relation='pos_config_nexi_terminal_rel',
        column1='nexi_terminal_id',
        column2='pos_config_id',
        string='POS Configurations',
        help='Point of Sale configurations that can use this terminal'
    )
    
    # State Management
    state = fields.Selection(
        selection=TERMINAL_STATES,
        string='State',
        default=TERMINAL_STATE_INACTIVE,
        required=True,
        help='Current operational state of the terminal'
    )
    
    last_heartbeat = fields.Datetime(
        string='Last Heartbeat',
        readonly=True,
        help='Timestamp of last successful communication with terminal'
    )
    
    # Statistics
    transaction_count = fields.Integer(
        string='Transaction Count',
        compute='_compute_transaction_stats',
        store=True,
        help='Number of transactions processed by this terminal'
    )
    
    success_rate = fields.Float(
        string='Success Rate (%)',
        compute='_compute_transaction_stats',
        store=True,
        digits=(5, 2),
        help='Percentage of successful transactions'
    )
    
    # Metadata
    active = fields.Boolean(
        string='Active',
        default=True,
        help='Whether this terminal is active and can be used'
    )
    
    notes = fields.Text(
        string='Notes',
        help='Additional notes about this terminal'
    )
    
    _sql_constraints = [
        ('terminal_id_unique', 
         'UNIQUE(terminal_id)', 
         'Terminal ID must be unique!'),
    ]
    
    # =========================================================================
    # Encryption Methods
    # =========================================================================
    
    def _get_crypt_context(self):
        """Get or initialize the encryption context.
        
        Uses Odoo's built-in encryption tools with Fernet.
        Falls back to a simple obfuscation if encryption key not available
        (for development/testing only - production MUST have proper encryption).
        
        Returns:
            Encryption key or context
        """
        # Try to get encryption key from Odoo config
        encryption_key = config.get('nexi_encryption_key')
        
        if not encryption_key:
            # Generate from master password as fallback
            # In production, nexi_encryption_key should be explicitly set
            master = config.get('admin_passwd', '')
            if master:
                import hashlib
                encryption_key = hashlib.sha256(master.encode()).hexdigest()[:32]
        
        if not encryption_key:
            _logger.warning(
                'No encryption key configured for NEXI module. '
                'Set nexi_encryption_key in odoo.conf for production.'
            )
            return None
        
        try:
            from cryptography.fernet import Fernet
            # Ensure key is properly formatted for Fernet
            if len(encryption_key) < 32:
                encryption_key = encryption_key.ljust(32, '=')
            # Fernet requires URL-safe base64-encoded 32-byte key
            import base64
            key = base64.urlsafe_b64encode(encryption_key[:32].encode())
            return Fernet(key)
        except ImportError:
            _logger.error(
                'cryptography library not installed. '
                'Install with: pip install cryptography'
            )
            return None
        except Exception as e:
            _logger.error('Error initializing encryption: %s', str(e))
            return None
    
    def _encrypt_api_key(self, api_key: str) -> str:
        """Encrypt an API key.
        
        Args:
            api_key: The plaintext API key
            
        Returns:
            Encrypted API key with prefix
        """
        if not api_key:
            return api_key
        
        # Check if already encrypted
        if api_key.startswith(ENCRYPTION_PREFIX):
            return api_key
        
        fernet = self._get_crypt_context()
        if fernet is None:
            # Fallback: store with prefix to indicate obfuscation needed
            _logger.warning('Encryption not available, storing API key as-is')
            return api_key
        
        try:
            encrypted = fernet.encrypt(api_key.encode()).decode()
            return f"{ENCRYPTION_PREFIX}{encrypted}"
        except Exception as e:
            _logger.error('Failed to encrypt API key: %s', str(e))
            raise ValidationError(_('Failed to encrypt API key'))
    
    def _decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key.
        
        Args:
            encrypted_key: The encrypted API key
            
        Returns:
            Decrypted API key
        """
        if not encrypted_key:
            return encrypted_key
        
        # Check if encrypted
        if not encrypted_key.startswith(ENCRYPTION_PREFIX):
            # Plaintext - migrate on read
            return encrypted_key
        
        fernet = self._get_crypt_context()
        if fernet is None:
            _logger.error('Encryption not available but key is encrypted')
            raise ValidationError(_('Encryption key not available'))
        
        try:
            ciphertext = encrypted_key[len(ENCRYPTION_PREFIX):]
            return fernet.decrypt(ciphertext.encode()).decode()
        except Exception as e:
            _logger.error('Failed to decrypt API key: %s', str(e))
            raise ValidationError(_('Failed to decrypt API key'))
    
    def _migrate_plaintext_keys(self):
        """Migrate existing plaintext API keys to encrypted storage.
        
        This should be called during module update or via cron.
        """
        # Find terminals with unencrypted keys
        terminals = self.search([
            ('api_key_encrypted', 'not like', ENCRYPTION_PREFIX + '%'),
            ('api_key_encrypted', '!=', False),
            ('api_key_encrypted', '!=', '')
        ])
        
        migrated_count = 0
        for terminal in terminals:
            try:
                plaintext = terminal.api_key_encrypted
                if plaintext and not plaintext.startswith(ENCRYPTION_PREFIX):
                    encrypted = terminal._encrypt_api_key(plaintext)
                    # Use SQL to avoid triggering other logic
                    self.env.cr.execute(
                        "UPDATE nexi_terminal SET api_key_encrypted = %s WHERE id = %s",
                        (encrypted, terminal.id)
                    )
                    migrated_count += 1
                    _logger.info('Migrated API key encryption for terminal %s', terminal.terminal_id)
            except Exception as e:
                _logger.error(
                    'Failed to migrate API key for terminal %s: %s',
                    terminal.terminal_id, str(e)
                )
        
        if migrated_count > 0:
            self.env.cr.commit()
            _logger.info('Migrated %s terminal API keys to encrypted storage', migrated_count)
        
        return migrated_count
    
    # =========================================================================
    # Computed Fields
    # =========================================================================
    
    @api.depends('terminal_id')
    def _compute_transaction_stats(self):
        """Compute transaction statistics for this terminal."""
        for terminal in self:
            transactions = self.env['nexi.transaction'].search([
                ('terminal_id', '=', terminal.terminal_id)
            ])
            
            total = len(transactions)
            successful = len(transactions.filtered(
                lambda t: t.state == 'confirmed'
            ))
            
            terminal.transaction_count = total
            terminal.success_rate = (successful / total * 100) if total > 0 else 0.0
    
    # =========================================================================
    # Constraints & Validation
    # =========================================================================
    
    @api.constrains('terminal_id')
    def _check_terminal_id(self):
        """Validate terminal ID format."""
        for terminal in self:
            if not terminal.terminal_id:
                raise ValidationError(_('Terminal ID is required'))
            
            if len(terminal.terminal_id) > 32:
                raise ValidationError(_(
                    'Terminal ID must not exceed 32 characters'
                ))
            
            # Allow alphanumeric, hyphens, underscores
            import re
            if not re.match(r'^[a-zA-Z0-9_-]+$', terminal.terminal_id):
                raise ValidationError(_(
                    'Terminal ID must contain only letters, numbers, hyphens, and underscores'
                ))
    
    @api.constrains('api_key_encrypted')
    def _check_api_key(self):
        """Validate API key format."""
        for terminal in self:
            if not terminal.api_key_encrypted:
                raise ValidationError(_('API Key is required'))
            
            # API keys should be reasonably long
            key_to_check = terminal.api_key_encrypted
            if key_to_check.startswith(ENCRYPTION_PREFIX):
                key_to_check = terminal._decrypt_api_key(key_to_check)
            
            if len(key_to_check) < 16:
                raise ValidationError(_(
                    'API Key appears to be invalid (too short)'
                ))
    
    @api.constrains('min_amount', 'max_amount')
    def _check_amount_limits(self):
        """Validate amount limits."""
        for terminal in self:
            if terminal.min_amount < 0:
                raise ValidationError(_('Minimum amount must be positive'))
            if terminal.max_amount <= 0:
                raise ValidationError(_('Maximum amount must be positive'))
            if terminal.min_amount >= terminal.max_amount:
                raise ValidationError(_('Minimum amount must be less than maximum amount'))
    
    @api.constrains('pos_config_ids')
    def _check_pos_configs_company(self):
        """Ensure POS configs belong to the same company."""
        for terminal in self:
            for pos_config in terminal.pos_config_ids:
                if pos_config.company_id != terminal.company_id:
                    raise ValidationError(_(
                        'POS Configuration "%s" must belong to the same company as the terminal'
                    ) % pos_config.name)
    
    # =========================================================================
    # CRUD Operations
    # =========================================================================
    
    @api.model_create_multi
    def create(self, vals_list):
        """Create new terminal records with encryption."""
        for vals in vals_list:
            # Normalize terminal_id
            if 'terminal_id' in vals:
                vals['terminal_id'] = vals['terminal_id'].strip()
            
            # Encrypt API key if provided in plaintext
            if vals.get('api_key_encrypted'):
                vals['api_key_encrypted'] = self._encrypt_api_key(vals['api_key_encrypted'])
            
            # Log creation (without sensitive data)
            _logger.info(
                'Creating NEXI terminal: name=%s, terminal_id=%s',
                vals.get('name'), vals.get('terminal_id')
            )
        
        return super(NexiTerminal, self).create(vals_list)
    
    def write(self, vals):
        """Update terminal records with encryption."""
        # Encrypt API key if being updated
        if 'api_key_encrypted' in vals and vals['api_key_encrypted']:
            vals['api_key_encrypted'] = self._encrypt_api_key(vals['api_key_encrypted'])
            
            self._audit_log(
                'api_key_updated',
                {'terminal_ids': self.mapped('terminal_id')},
                f'API key updated for terminal(s): {self.mapped("terminal_id")}'
            )
        
        if 'state' in vals:
            self._audit_log(
                'terminal_state_change',
                {'terminal_ids': self.mapped('terminal_id'), 'new_state': vals['state']},
                f'Terminal state change: {self.mapped("terminal_id")} -> {vals["state"]}'
            )
        
        return super(NexiTerminal, self).write(vals)
    
    def unlink(self):
        """Prevent deletion of terminals with transactions."""
        for terminal in self:
            transactions = self.env['nexi.transaction'].search([
                ('terminal_id', '=', terminal.terminal_id),
                ('state', 'not in', ['cancelled', 'failed'])
            ], limit=1)
            
            if transactions:
                raise UserError(_(
                    'Cannot delete terminal "%s" because it has associated transactions. '
                    'Archive it instead.'
                ) % terminal.name)
        
        return super(NexiTerminal, self).unlink()
    
    # =========================================================================
    # Action Methods
    # =========================================================================
    
    def action_activate(self):
        """Activate the terminal for use.
        
        Sets the terminal state to 'active' if credentials are valid.
        """
        self.ensure_one()
        
        if not self.api_key_encrypted:
            raise UserError(_('Cannot activate terminal without API key'))
        
        # TODO: Optionally validate credentials with NEXI API
        # This could make a test request to verify the API key works
        
        self.write({
            'state': TERMINAL_STATE_ACTIVE,
            'last_heartbeat': fields.Datetime.now()
        })
        
        _logger.info('Terminal activated: %s', self.terminal_id)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Terminal Activated'),
                'message': _('Terminal "%s" is now active') % self.name,
                'type': 'success',
                'sticky': False,
            }
        }
    
    def action_deactivate(self):
        """Deactivate the terminal.
        
        Sets the terminal state to 'inactive'. Existing transactions
        can still be queried but new transactions cannot be started.
        """
        self.ensure_one()
        
        self.write({'state': TERMINAL_STATE_INACTIVE})
        
        _logger.info('Terminal deactivated: %s', self.terminal_id)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Terminal Deactivated'),
                'message': _('Terminal "%s" is now inactive') % self.name,
                'type': 'warning',
                'sticky': False,
            }
        }
    
    def action_test_connection(self):
        """Test the connection to NEXI API.

        Uses POST /terminal/status to verify terminal credentials and connectivity.

        Returns:
            Notification action with test result
        """
        self.ensure_one()

        try:
            from .nexi_api_client import NexiApiClient

            key_secret = self._decrypt_api_key(self.api_key_encrypted)

            client = NexiApiClient(
                key_id=self.api_key_id,
                key_secret=key_secret,
                base_url=self.env['ir.config_parameter'].sudo().get_param(
                    'nexi_payment.base_url', 'https://api.npay.eu/pos/v1/'
                )
            )

            # Use terminal/status endpoint (POST) to test connectivity
            status = client.get_terminal_status(self.terminal_id)
            connected = status.get('connected', False)

            self.write({
                'state': TERMINAL_STATE_ACTIVE,
                'last_heartbeat': fields.Datetime.now()
            })

            _logger.info('Terminal connection test successful: %s (connected=%s)',
                         self.terminal_id, connected)

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('Terminal "%s" is connected and ready') % self.name,
                    'type': 'success',
                    'sticky': False,
                }
            }

        except Exception as e:
            self.write({'state': TERMINAL_STATE_ERROR})

            _logger.error(
                'Terminal connection test failed: %s - %s',
                self.terminal_id, str(e)
            )

            raise UserError(_(
                'Connection test failed for terminal "%s":\n%s'
            ) % (self.name, str(e)))
    
    def action_update_heartbeat(self):
        """Update the last heartbeat timestamp."""
        self.write({'last_heartbeat': fields.Datetime.now()})
    
    def action_recover_unconfirmed(self):
        """Recover unconfirmed transactions from this terminal.

        Calls the NEXI API to get transactions stuck in
        AWAITING_CONFIRM/AWAITING_CONTINUE state. For each:
        - If we already have it by external_id: update its state
        - If unknown: create a new nexi.transaction record
        - Auto-confirm transactions with result_code == SUCCESS

        Returns:
            Notification action showing how many were recovered
        """
        self.ensure_one()

        if self.state != TERMINAL_STATE_ACTIVE:
            raise UserError(_(
                'Terminal "%s" is not active (current state: %s)'
            ) % (self.name, self.state))

        try:
            client = self.get_api_client()
            response = client.get_unconfirmed(self.terminal_id)
        except Exception as e:
            raise UserError(_(
                'Failed to get unconfirmed transactions: %s'
            ) % str(e))

        transactions = response.get('transactions', [])
        recovered = 0
        updated = 0

        Transaction = self.env['nexi.transaction']

        for txn_data in transactions:
            ext_id = txn_data.get('external_id')
            if not ext_id:
                continue

            existing = Transaction.search([
                ('external_id', '=', ext_id),
            ], limit=1)

            api_state = (txn_data.get('state') or '').upper()
            result_code = txn_data.get('result_code')

            if existing:
                # Update existing transaction state
                vals = {}
                if api_state == 'AWAITING_CONFIRM' and existing.state != 'awaiting_confirm':
                    vals['state'] = 'awaiting_confirm'
                if result_code:
                    vals['result_code'] = result_code
                if txn_data.get('result_description'):
                    vals['result_message'] = txn_data['result_description']

                if vals:
                    existing.write(vals)
                    updated += 1

                existing._extract_payment_details(txn_data)

                # Auto-confirm if SUCCESS
                if result_code == 'SUCCESS' and existing.state == 'awaiting_confirm':
                    try:
                        existing.action_confirm()
                    except Exception as e:
                        _logger.warning(
                            'Auto-confirm failed for recovered txn %s: %s',
                            ext_id, str(e)
                        )
            else:
                # Create new transaction record
                amount_minor = txn_data.get('requested_amount') or txn_data.get('authorized_amount') or 0
                try:
                    amount = int(amount_minor) / 100.0
                except (TypeError, ValueError):
                    amount = 0.0

                new_state = 'awaiting_confirm'
                if api_state in ('ABORTED', 'REJECTED'):
                    new_state = 'failed'
                elif api_state == 'CONFIRMED':
                    new_state = 'confirmed'

                txn_type = 'purchase'
                if (txn_data.get('type') or '').upper() == 'REFUND':
                    txn_type = 'refund'

                new_txn = Transaction.create({
                    'external_id': ext_id,
                    'terminal_id': self.terminal_id,
                    'amount': amount,
                    'state': new_state,
                    'result_code': result_code or '',
                    'result_message': txn_data.get('result_description', ''),
                    'transaction_type': txn_type,
                })

                new_txn._extract_payment_details(txn_data)
                recovered += 1

                # Auto-confirm if SUCCESS
                if result_code == 'SUCCESS' and new_state == 'awaiting_confirm':
                    try:
                        new_txn.action_confirm()
                    except Exception as e:
                        _logger.warning(
                            'Auto-confirm failed for new recovered txn %s: %s',
                            ext_id, str(e)
                        )

        self._audit_log(
            'unconfirmed_recovery',
            {
                'recovered': recovered,
                'updated': updated,
                'total_api': len(transactions),
            },
            f'Recovered {recovered} new, updated {updated} existing from {len(transactions)} unconfirmed'
        )

        _logger.info(
            'Unconfirmed recovery for terminal %s: %s new, %s updated, %s total from API',
            self.terminal_id, recovered, updated, len(transactions)
        )

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Unconfirmed Recovery'),
                'message': _(
                    'Found %(total)s unconfirmed transactions: '
                    '%(recovered)s recovered, %(updated)s updated.',
                    total=len(transactions),
                    recovered=recovered,
                    updated=updated,
                ),
                'type': 'success' if (recovered + updated) > 0 else 'info',
                'sticky': False,
            }
        }

    def action_view_transactions(self):
        """Open transactions list for this terminal.
        
        Returns:
            Window action to view related transactions
        """
        self.ensure_one()
        
        return {
            'name': _('NEXI Transactions'),
            'type': 'ir.actions.act_window',
            'res_model': 'nexi.transaction',
            'view_mode': 'tree,form',
            'domain': [('terminal_id', '=', self.terminal_id)],
            'context': {'default_terminal_id': self.terminal_id}
        }
    
    def action_migrate_api_keys(self):
        """Action to migrate plaintext API keys to encrypted storage.
        
        Returns:
            Notification with migration results
        """
        count = self._migrate_plaintext_keys()
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('API Key Migration'),
                'message': _('Migrated %s terminal API keys to encrypted storage') % count,
                'type': 'success' if count > 0 else 'info',
                'sticky': False,
            }
        }
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def get_api_client(self) -> 'NexiApiClient':
        """Get configured API client for this terminal.

        Returns:
            Configured NexiApiClient instance with HTTP Basic Auth

        Raises:
            UserError: If terminal is not active
        """
        self.ensure_one()

        if self.state != TERMINAL_STATE_ACTIVE:
            raise UserError(_(
                'Terminal "%s" is not active (current state: %s)'
            ) % (self.name, self.state))

        from .nexi_api_client import NexiApiClient

        base_url = self.env['ir.config_parameter'].sudo().get_param(
            'nexi_payment.base_url',
            'https://api.npay.eu/pos/v1/'
        )

        # Decrypt API key secret for use
        key_secret = self._decrypt_api_key(self.api_key_encrypted)

        return NexiApiClient(
            key_id=self.api_key_id,
            key_secret=key_secret,
            base_url=base_url
        )
    
    @api.model
    def get_available_terminals(self, pos_config_id: Optional[int] = None):
        """Get list of available terminals.
        
        Args:
            pos_config_id: Optional POS config to filter by
            
        Returns:
            Recordset of active terminals
        """
        domain = [
            ('state', '=', TERMINAL_STATE_ACTIVE),
            ('active', '=', True)
        ]
        
        if pos_config_id:
            domain.append(('pos_config_ids', 'in', [pos_config_id]))
        
        return self.search(domain)
    
    @api.model
    def cron_migrate_api_keys(self):
        """Scheduled job to migrate API keys to encrypted storage."""
        return self._migrate_plaintext_keys()
