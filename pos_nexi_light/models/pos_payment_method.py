# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import logging
import requests
import json
import uuid
import time
from datetime import datetime, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError, AccessDenied
from odoo.tools import float_round

_logger = logging.getLogger(__name__)

# API Configuration
NEXI_API_BASE_PROD = 'https://api.npay.eu/pos/v1'
NEXI_API_BASE_TEST = 'https://api-test.npay.eu/pos/v1'
REQUEST_TIMEOUT = 30

# Valid NEXI result codes
RESULT_SUCCESS = 'SUCCESS'
RESULT_ABORTED = 'ABORTED'


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    # ==========================================================================
    # Terminal Selection
    # ==========================================================================

    @api.model
    def _get_payment_terminal_selection(self):
        """Add NEXI to the list of available payment terminals."""
        return super()._get_payment_terminal_selection() + [('nexi', 'NEXI')]

    # ==========================================================================
    # NEXI Configuration Fields
    # ==========================================================================

    # Terminal identification
    nexi_terminal_id = fields.Char(
        string='Terminal ID',
        help='NEXI terminal identifier (e.g., TERM001). This is the unique identifier for your physical terminal.',
        copy=False,
    )

    # API credentials - HTTP Basic Auth requires key_id + key_secret
    nexi_api_key_id = fields.Char(
        string='API Key ID',
        help='Your NEXI API key ID for Basic Auth. Provided during onboarding.',
        copy=False,
        groups='point_of_sale.group_pos_manager',
    )

    nexi_api_key_secret = fields.Char(
        string='API Key Secret',
        help='Your NEXI API key secret for Basic Auth. Keep this secure.',
        copy=False,
        groups='point_of_sale.group_pos_manager',
    )

    # Webhook security token (auto-generated, used to verify notification callbacks)
    nexi_webhook_token = fields.Char(
        string='Webhook Token',
        copy=False,
        groups='point_of_sale.group_pos_manager',
        help='Secret token for verifying webhook/notification callbacks. Auto-generated on save.',
    )

    # Environment settings
    nexi_test_mode = fields.Boolean(
        string='Test Mode',
        help='Run transactions in the test environment. No real charges will be made.',
        default=True,
    )

    # Terminal receipt printing settings
    nexi_print_enabled = fields.Boolean(
        string='Print Receipt on Terminal',
        help='Automatically print receipt on the NEXI terminal after a successful payment.',
        default=False,
    )

    nexi_print_content = fields.Selection([
        ('nexi_receipt', 'NEXI Card Receipt'),
        ('pos_receipt', 'Full POS Receipt'),
        ('both', 'Both'),
    ],
        string='Terminal Print Content',
        default='nexi_receipt',
        help='What to print on the NEXI terminal receipt printer.',
    )

    # Terminal status (updated via polling)
    nexi_terminal_status = fields.Selection([
        ('unknown', 'Unknown'),
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('busy', 'Busy'),
        ('error', 'Error'),
    ],
        string='Terminal Status',
        default='unknown',
        readonly=True,
        help='Current status of the terminal (updated via polling)',
    )
    nexi_last_heartbeat = fields.Datetime(
        string='Last Heartbeat',
        readonly=True,
        help='Last time the terminal was seen online',
    )

    # Latest response buffer (for async status updates)
    nexi_latest_response = fields.Char(
        string='Latest Response',
        copy=False,
        groups='point_of_sale.group_pos_manager',
        help='Buffer for the latest response from NEXI',
    )

    # ==========================================================================
    # Constraints
    # ==========================================================================

    @api.constrains('nexi_terminal_id', 'use_payment_terminal')
    def _check_nexi_terminal_id(self):
        """Ensure terminal ID is unique across all payment methods."""
        for method in self:
            if method.use_payment_terminal != 'nexi' or not method.nexi_terminal_id:
                continue

            existing = self.search([
                ('id', '!=', method.id),
                ('nexi_terminal_id', '=', method.nexi_terminal_id),
                ('use_payment_terminal', '=', 'nexi'),
            ], limit=1)

            if existing:
                raise ValidationError(_(
                    'Terminal %(terminal)s is already used on payment method %(method)s.',
                    terminal=method.nexi_terminal_id,
                    method=existing.display_name
                ))

    @api.constrains('nexi_api_key_id', 'nexi_api_key_secret', 'use_payment_terminal')
    def _check_nexi_credentials(self):
        """Ensure API credentials are provided when using NEXI."""
        for method in self:
            if method.use_payment_terminal != 'nexi':
                continue
            if not method.nexi_api_key_id or not method.nexi_api_key_secret:
                raise ValidationError(_(
                    'API Key ID and API Key Secret are both required for NEXI payment method %(method)s.',
                    method=method.display_name
                ))

    @api.constrains('use_payment_terminal')
    def _ensure_nexi_webhook_token(self):
        """Auto-generate webhook token for NEXI payment methods."""
        for method in self:
            if method.use_payment_terminal == 'nexi' and not method.nexi_webhook_token:
                method.nexi_webhook_token = uuid.uuid4().hex

    # ==========================================================================
    # ORM Overrides
    # ==========================================================================

    @api.model
    def _load_pos_data_fields(self, config_id):
        """Load NEXI fields in POS session data."""
        params = super()._load_pos_data_fields(config_id)
        params += [
            'nexi_terminal_id',
            'nexi_test_mode',
            'nexi_terminal_status',
            'nexi_print_enabled',
            'nexi_print_content',
        ]
        return params

    def _is_write_forbidden(self, fields):
        """Allow modification of response field even when POS session is open."""
        whitelisted_fields = {'nexi_latest_response', 'nexi_terminal_status', 'nexi_last_heartbeat'}
        return super()._is_write_forbidden(fields - whitelisted_fields)

    # ==========================================================================
    # NEXI API Methods
    # ==========================================================================

    def _nexi_build_auth_header(self):
        """Build HTTP Basic Auth header from key_id and key_secret.

        Returns:
            Base64-encoded Basic Auth header value
        """
        self.ensure_one()
        credentials = f"{self.nexi_api_key_id}:{self.nexi_api_key_secret}"
        encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
        return f"Basic {encoded}"

    def nexi_make_request(self, endpoint, payload=None):
        """Make an authenticated POST request to the NEXI API.

        All NEXI API endpoints use POST method.

        Args:
            endpoint: API endpoint path (e.g., 'transaction/purchase')
            payload: Request payload as dict

        Returns:
            API response as dict

        Raises:
            UserError: If the request fails
        """
        self.ensure_one()

        if not self.nexi_api_key_id or not self.nexi_api_key_secret:
            raise UserError(_('NEXI API credentials are not configured for payment method %s', self.display_name))

        # Build full URL
        base_url = NEXI_API_BASE_TEST if self.nexi_test_mode else NEXI_API_BASE_PROD
        url = f"{base_url}/{endpoint}"

        # Prepare headers with HTTP Basic Auth
        headers = {
            'Authorization': self._nexi_build_auth_header(),
            'Content-Type': 'application/json',
            'User-Agent': 'OdooPOS/18.0 (nexi-payment-integration)',
        }

        try:
            _logger.info('NEXI API Request to %s: %s', endpoint, payload)

            response = requests.post(
                url=url,
                headers=headers,
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )

            # Parse response
            try:
                result = response.json()
            except json.JSONDecodeError:
                result = {'error': {'description': response.text}}

            _logger.info('NEXI API Response from %s: %s', endpoint, result)

            # Check for HTTP errors
            if not response.ok:
                error_obj = result.get('error', {})
                if isinstance(error_obj, dict):
                    error_msg = error_obj.get('description', response.text)
                else:
                    error_msg = str(error_obj)
                raise UserError(_(
                    'NEXI API Error (%(status)s): %(message)s',
                    status=response.status_code,
                    message=error_msg
                ))

            return result

        except requests.exceptions.Timeout:
            _logger.exception('NEXI API Timeout')
            raise UserError(_('Connection to NEXI timed out. Please try again.'))
        except requests.exceptions.ConnectionError:
            _logger.exception('NEXI API Connection Error')
            raise UserError(_('Could not connect to NEXI. Please check your internet connection.'))
        except Exception as e:
            if isinstance(e, UserError):
                raise
            _logger.exception('NEXI API Error')
            raise UserError(_('Error communicating with NEXI: %s', str(e)))

    # ==========================================================================
    # Card Detail Extraction
    # ==========================================================================

    def _extract_nexi_card_details(self, result):
        """Extract card/payment details from a NEXI API response dict.

        The API wraps transaction data in a 'transaction' key.
        Card details are in the 'payment_method_details' sub-object.
        Receipt text is at the transaction level.

        Args:
            result: dict from NEXI API response (already unwrapped from transaction key)

        Returns:
            dict with card detail fields (only non-empty values)
        """
        if not result or not isinstance(result, dict):
            return {}

        details = {}
        # Top-level transaction fields
        for key in ('reference', 'receipt_details_customer', 'receipt_details_merchant',
                     'authorized_amount', 'captured_amount', 'result_description'):
            if result.get(key):
                details[key] = result[key]

        # payment_method_details sub-object
        pmd = result.get('payment_method_details') or {}
        for key in ('card_scheme', 'authorization_code', 'authorization_response_code',
                     'card_entry_mode', 'card_number_customer', 'card_number_merchant',
                     'cardholder_verification', 'application_name', 'application_identifier',
                     'acquirer_reference', 'acquirer_terminal_id', 'acquirer_timestamp'):
            if pmd.get(key):
                details[key] = pmd[key]

        return details

    def _unwrap_transaction(self, result):
        """Unwrap transaction data from API response envelope.

        The NEXI API wraps transaction responses in {"transaction": {...}}.

        Args:
            result: Raw API response dict

        Returns:
            Transaction data dict
        """
        if not result or not isinstance(result, dict):
            return result or {}
        return result.get('transaction', result)

    # ==========================================================================
    # Payment Flow Methods
    # ==========================================================================

    def nexi_create_payment(self, amount, external_id=None):
        """Create a new payment request on the terminal.

        Args:
            amount: Payment amount (in major currency units, e.g., 99.99)
            external_id: Optional external transaction ID (generated if not provided)

        Returns:
            Dict with transaction details including external_id
        """
        self.ensure_one()

        if not external_id:
            external_id = str(uuid.uuid4())

        # Convert amount to minor currency units (integer cents)
        currency = self.journal_id.currency_id or self.company_id.currency_id
        amount_minor = int(float_round(amount / currency.rounding, precision_rounding=1))

        payload = {
            'external_id': external_id,
            'terminal_id': self.nexi_terminal_id,
            'requested_amount': amount_minor,
            'currency': currency.name,
            'options': {
                'wait_seconds': 60,
            }
        }

        # Clear stale response buffer before new payment
        self.write({
            'nexi_latest_response': False,
        })

        result = self.nexi_make_request('transaction/purchase', payload)

        # Unwrap transaction from response envelope
        txn_data = self._unwrap_transaction(result)

        # Update terminal status
        self.write({
            'nexi_terminal_status': 'busy',
        })

        card_details = self._extract_nexi_card_details(txn_data)
        return {
            'external_id': external_id,
            'status': (txn_data.get('state') or 'unknown').lower(),
            'result': txn_data,
            **card_details,
        }

    def nexi_refund_payment(self, amount, original_external_id=None):
        """Create a refund request on the terminal.

        Mirrors nexi_create_payment() but calls transaction/refund endpoint.

        Args:
            amount: Refund amount (positive, in major currency units, e.g., 99.99)
            original_external_id: Optional external ID of the original purchase

        Returns:
            Dict with transaction details including external_id
        """
        self.ensure_one()

        external_id = str(uuid.uuid4())

        # Convert amount to minor currency units (integer cents)
        currency = self.journal_id.currency_id or self.company_id.currency_id
        amount_minor = int(float_round(abs(amount) / currency.rounding, precision_rounding=1))

        payload = {
            'external_id': external_id,
            'terminal_id': self.nexi_terminal_id,
            'requested_amount': amount_minor,
            'currency': currency.name,
            'options': {
                'wait_seconds': 60,
            }
        }

        if original_external_id:
            payload['original_purchase_external_id'] = original_external_id

        # Clear stale response buffer
        self.write({
            'nexi_latest_response': False,
        })

        result = self.nexi_make_request('transaction/refund', payload)

        # Unwrap transaction from response envelope
        txn_data = self._unwrap_transaction(result)

        self.write({
            'nexi_terminal_status': 'busy',
        })

        card_details = self._extract_nexi_card_details(txn_data)
        return {
            'external_id': external_id,
            'status': (txn_data.get('state') or 'unknown').lower(),
            'result': txn_data,
            **card_details,
        }

    def nexi_check_status(self, external_id):
        """Check the status of a pending transaction.

        Uses POST /transaction/get per API spec.

        Args:
            external_id: The external transaction ID

        Returns:
            Dict with current transaction status
        """
        self.ensure_one()

        payload = {
            'external_id': external_id,
        }

        result = self.nexi_make_request('transaction/get', payload)
        txn_data = self._unwrap_transaction(result)

        return {
            'external_id': external_id,
            'status': (txn_data.get('state') or 'unknown').lower(),
            'result': txn_data,
        }

    def nexi_confirm_payment(self, external_id, result_code='SUCCESS'):
        """Confirm a completed transaction.

        Args:
            external_id: The external transaction ID
            result_code: The result code (SUCCESS for successful payments)

        Returns:
            Dict with confirmation result
        """
        self.ensure_one()

        payload = {
            'external_id': external_id,
            'terminal_id': self.nexi_terminal_id,
            'result_code': result_code,
        }

        result = self.nexi_make_request('transaction/confirm', payload)
        txn_data = self._unwrap_transaction(result)

        # Update terminal status back to online
        self.write({
            'nexi_terminal_status': 'online',
            'nexi_last_heartbeat': fields.Datetime.now(),
        })

        card_details = self._extract_nexi_card_details(txn_data)
        return {
            'external_id': external_id,
            'status': 'confirmed',
            'result': txn_data,
            **card_details,
        }

    def nexi_cancel_payment(self, external_id):
        """Cancel/abort a pending transaction.

        Uses transaction/confirm with result_code=ABORTED per API spec.
        The NEXI API has no separate cancel endpoint.

        Args:
            external_id: The external transaction ID

        Returns:
            Dict with cancellation result
        """
        self.ensure_one()

        payload = {
            'external_id': external_id,
            'terminal_id': self.nexi_terminal_id,
            'result_code': RESULT_ABORTED,
        }

        result = self.nexi_make_request('transaction/confirm', payload)
        txn_data = self._unwrap_transaction(result)

        # Update terminal status
        self.write({
            'nexi_terminal_status': 'online',
        })

        return {
            'external_id': external_id,
            'status': 'cancelled',
            'result': txn_data,
        }

    # ==========================================================================
    # POS Integration Methods
    # ==========================================================================

    def get_latest_nexi_status(self):
        """Get the latest status from NEXI (called by POS frontend).

        Returns:
            Dict with latest response data or False
        """
        self.ensure_one()

        if not self.env.su and not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessDenied()

        if not self.nexi_latest_response:
            return False

        try:
            return json.loads(self.nexi_latest_response)
        except json.JSONDecodeError:
            return False

    def action_nexi_test_connection(self):
        """Test the connection to NEXI API.

        Uses POST /terminal/status to verify connectivity.

        Returns:
            Action to show success/error message
        """
        self.ensure_one()

        try:
            result = self.nexi_make_request('terminal/status', {
                'terminal_id': self.nexi_terminal_id
            })

            # Parse terminal status from response
            status_data = result.get('status', result)
            connected = status_data.get('connected', False)

            if connected:
                self.write({
                    'nexi_terminal_status': 'online',
                    'nexi_last_heartbeat': fields.Datetime.now(),
                })

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('Successfully connected to NEXI terminal.'),
                    'type': 'success',
                    'sticky': False,
                }
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Failed'),
                    'message': str(e),
                    'type': 'danger',
                    'sticky': True,
                }
            }

    # ==========================================================================
    # Terminal Receipt Printing
    # ==========================================================================

    def nexi_print_to_terminal(self, receipt_text=None, receipt_html=None):
        """Print receipt content on the NEXI terminal's receipt printer.

        Renders the appropriate content as PNG and sends it via the
        POST /terminal/print API endpoint.

        Args:
            receipt_text: Plain text receipt content (for nexi_receipt mode)
            receipt_html: HTML receipt content (for pos_receipt mode)

        Returns:
            dict with success status and message
        """
        self.ensure_one()

        if not self.nexi_print_enabled:
            return {'success': False, 'message': 'Terminal printing is disabled'}

        if not self.nexi_terminal_id:
            return {'success': False, 'message': 'Terminal ID not configured'}

        from . import nexi_receipt_renderer as renderer

        print_content = self.nexi_print_content or 'nexi_receipt'
        results = []

        try:
            # Render and print NEXI card receipt (text)
            if print_content in ('nexi_receipt', 'both') and receipt_text:
                png_b64 = renderer.render_text_receipt(receipt_text)
                if png_b64:
                    self._nexi_send_print_fragments(png_b64)
                    results.append('nexi_receipt')

            # Render and print full POS receipt (HTML)
            if print_content in ('pos_receipt', 'both') and receipt_html:
                png_b64 = renderer.render_html_receipt(receipt_html)
                if png_b64:
                    self._nexi_send_print_fragments(png_b64)
                    results.append('pos_receipt')

            if results:
                return {'success': True, 'message': f"Printed: {', '.join(results)}"}
            else:
                return {'success': False, 'message': 'No content to print'}

        except Exception as e:
            _logger.warning('NEXI terminal print failed for %s: %s',
                           self.nexi_terminal_id, str(e))
            return {'success': False, 'message': str(e)}

    def _nexi_send_print_fragments(self, contents_base64):
        """Send print content to the terminal, splitting into fragments if needed.

        Args:
            contents_base64: Base64-encoded PNG data
        """
        from . import nexi_receipt_renderer as renderer

        fragments = renderer.split_fragments(contents_base64)
        for chunk, is_continuation in fragments:
            payload = {
                'terminal_id': self.nexi_terminal_id,
                'contents': chunk,
                'format': 'PNG',
            }
            if is_continuation:
                payload['continue'] = True

            self.nexi_make_request('terminal/print', payload)
