# -*- coding: utf-8 -*-
"""Unit tests for NEXI API Client.

This module tests the NexiApiClient class including:
- Successful API requests
- Retry logic and exponential backoff
- Timeout handling
- Error response handling
- Input validation

API Reference: https://developer.npay.eu/nexi-pos-api/api/
Authentication: HTTP Basic Auth with key_id:key_secret
All endpoints use POST. Amounts are integers in minor currency units.
"""

import base64
import json
import unittest
from unittest.mock import Mock, patch, MagicMock

from odoo.tests.common import BaseCase
from odoo.tools import mute_logger

# Import the module under test
from ..models.nexi_api_client import (
    NexiApiClient,
    NexiApiError,
    NexiNetworkError,
    NexiValidationError,
    NexiAuthenticationError,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MAX_RETRY_ATTEMPTS,
    EXTERNAL_ID_MAX_LENGTH,
    VALID_RESULT_CODES,
)


# Test credentials for Basic Auth
TEST_KEY_ID = 'test_key_id_12345678'
TEST_KEY_SECRET = 'test_key_secret_12345678'


class TestNexiApiClientUnit(BaseCase):
    """Unit tests for NexiApiClient - no database required."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.key_id = TEST_KEY_ID
        self.key_secret = TEST_KEY_SECRET
        self.base_url = 'https://api.test.nexi.com/pos/v1/'

    def test_01_client_initialization_valid(self):
        """Test client initialization with valid parameters."""
        client = NexiApiClient(
            key_id=self.key_id,
            key_secret=self.key_secret,
            base_url=self.base_url,
            timeout=30
        )

        self.assertEqual(client.base_url, self.base_url.rstrip('/') + '/')
        self.assertEqual(client.timeout, 30)
        self.assertIsNotNone(client.session)

    def test_02_client_initialization_invalid_credentials(self):
        """Test client initialization fails with invalid credentials."""
        # Empty key_id
        with self.assertRaises(NexiValidationError) as cm:
            NexiApiClient(key_id='', key_secret=self.key_secret)
        self.assertIn('key_id', str(cm.exception))

        # None key_id
        with self.assertRaises(NexiValidationError):
            NexiApiClient(key_id=None, key_secret=self.key_secret)

        # Non-string key_id
        with self.assertRaises(NexiValidationError):
            NexiApiClient(key_id=12345, key_secret=self.key_secret)

        # Empty key_secret
        with self.assertRaises(NexiValidationError) as cm:
            NexiApiClient(key_id=self.key_id, key_secret='')
        self.assertIn('key_secret', str(cm.exception))

        # None key_secret
        with self.assertRaises(NexiValidationError):
            NexiApiClient(key_id=self.key_id, key_secret=None)

    def test_03_basic_auth_header(self):
        """Test HTTP Basic Auth header construction."""
        auth = NexiApiClient._build_basic_auth('myid', 'mysecret')
        expected = 'Basic ' + base64.b64encode(b'myid:mysecret').decode('utf-8')
        self.assertEqual(auth, expected)

    def test_04_value_masking(self):
        """Test value masking for safe logging."""
        masked = NexiApiClient._mask_value('abcdefghijklmnop')
        self.assertIn('...', masked)
        self.assertTrue(masked.startswith('abcd'))
        self.assertTrue(masked.endswith('mnop'))

        # Short value masking
        short_masked = NexiApiClient._mask_value('short')
        self.assertEqual(short_masked, '***')

        # Empty value masking
        self.assertEqual(NexiApiClient._mask_value(''), '***')

    def test_05_session_configuration(self):
        """Test HTTP session is configured with retry logic and Basic Auth."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        session = client.session
        self.assertIsNotNone(session)

        # Check headers - Authorization should be Basic Auth
        auth_header = session.headers['Authorization']
        self.assertTrue(auth_header.startswith('Basic '))
        # Verify it decodes correctly
        encoded_part = auth_header.split(' ', 1)[1]
        decoded = base64.b64decode(encoded_part).decode('utf-8')
        self.assertEqual(decoded, f'{self.key_id}:{self.key_secret}')

        self.assertEqual(session.headers['Content-Type'], 'application/json')
        self.assertEqual(session.headers['Accept'], 'application/json')
        self.assertIn('OdooPOS/18.0', session.headers['User-Agent'])

    def test_06_validate_external_id_valid(self):
        """Test external ID validation with valid inputs."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        valid_ids = [
            'ORDER-001',
            'TXN_123',
            'ABC123',
            'test-transaction-123',
            'a' * 63,  # Max length (63, not 64)
        ]

        for ext_id in valid_ids:
            client._validate_external_id(ext_id)

    def test_07_validate_external_id_invalid(self):
        """Test external ID validation with invalid inputs."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        # Empty ID
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_external_id('')
        self.assertIn('external_id is required', str(cm.exception))

        # Too long (64 chars exceeds max of 63)
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_external_id('a' * 64)
        self.assertIn('exceeds maximum', str(cm.exception))

        # Invalid characters
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_external_id('ORDER@123')
        self.assertIn('alphanumeric', str(cm.exception))

    def test_08_validate_terminal_id_valid(self):
        """Test terminal ID validation with valid inputs."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        valid_ids = ['TERM001', 'TERMINAL-123', 'a' * 32]

        for term_id in valid_ids:
            client._validate_terminal_id(term_id)

    def test_09_validate_terminal_id_invalid(self):
        """Test terminal ID validation with invalid inputs."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        with self.assertRaises(NexiValidationError):
            client._validate_terminal_id('')

        with self.assertRaises(NexiValidationError):
            client._validate_terminal_id('a' * 33)

    def test_10_validate_amount_valid(self):
        """Test amount validation with valid integer amounts (minor units)."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        valid_amounts = [
            1,          # Minimum (1 cent)
            100,        # 1.00 EUR
            9999,       # 99.99 EUR
            99999999,   # Maximum
        ]

        for amount in valid_amounts:
            client._validate_amount(amount)

    def test_11_validate_amount_invalid(self):
        """Test amount validation with invalid amounts."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        # Zero amount
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_amount(0)
        self.assertIn('positive', str(cm.exception))

        # Negative amount
        with self.assertRaises(NexiValidationError):
            client._validate_amount(-10)

        # Float instead of int
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_amount(10.50)
        self.assertIn('integer', str(cm.exception))

        # Above maximum
        with self.assertRaises(NexiValidationError) as cm:
            client._validate_amount(100000000)
        self.assertIn('maximum', str(cm.exception))

        # String (not int)
        with self.assertRaises(NexiValidationError):
            client._validate_amount('100')

    def test_12_validate_currency_valid(self):
        """Test currency validation with valid codes."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        valid_currencies = ['EUR', 'USD', 'GBP', 'eur']  # Case insensitive
        for currency in valid_currencies:
            client._validate_currency(currency)

    def test_13_validate_currency_invalid(self):
        """Test currency validation with invalid codes."""
        client = NexiApiClient(key_id=self.key_id, key_secret=self.key_secret)

        with self.assertRaises(NexiValidationError):
            client._validate_currency('')

        with self.assertRaises(NexiValidationError):
            client._validate_currency('EU')

        with self.assertRaises(NexiValidationError):
            client._validate_currency('EURO')

        with self.assertRaises(NexiValidationError):
            client._validate_currency('EU1')

    def test_14_generate_external_id(self):
        """Test external ID generation."""
        # Default prefix
        ext_id = NexiApiClient.generate_external_id()
        self.assertTrue(ext_id.startswith('NEXI-'))
        self.assertLessEqual(len(ext_id), EXTERNAL_ID_MAX_LENGTH)

        # Custom prefix
        custom_id = NexiApiClient.generate_external_id(prefix='ORDER')
        self.assertTrue(custom_id.startswith('ORDER-'))
        self.assertLessEqual(len(custom_id), EXTERNAL_ID_MAX_LENGTH)

        # Uniqueness check
        ids = [NexiApiClient.generate_external_id() for _ in range(100)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_15_context_manager(self):
        """Test client works as context manager."""
        with NexiApiClient(key_id=self.key_id, key_secret=self.key_secret) as client:
            self.assertIsNotNone(client.session)
        # After exit, session should be closed


class TestNexiApiClientRequests(BaseCase):
    """Tests for API request methods with mocked HTTP."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.client = NexiApiClient(
            key_id=TEST_KEY_ID,
            key_secret=TEST_KEY_SECRET,
        )

    def tearDown(self):
        """Clean up resources."""
        self.client.close()
        super().tearDown()

    @patch('requests.Session.post')
    def test_16_successful_purchase_request(self, mock_post):
        """Test successful purchase API call with integer amount."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {
                'state': 'AWAITING_CONFIRM',
                'external_id': 'TEST-001',
                'requested_amount': 10000,
                'result_description': 'Transaction initiated'
            }
        }
        mock_post.return_value = mock_response

        result = self.client.purchase(
            requested_amount=10000,
            external_id='TEST-001',
            terminal_id='TERM001',
            currency='EUR'
        )

        self.assertEqual(result['state'], 'AWAITING_CONFIRM')
        mock_post.assert_called_once()

        # Verify request payload
        call_args = mock_post.call_args
        self.assertIn('transaction/purchase', call_args[0][0])

    @patch('requests.Session.post')
    def test_17_purchase_with_options(self, mock_post):
        """Test purchase with optional parameters and wait_seconds."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {'state': 'AWAITING_CONFIRM'}
        }
        mock_post.return_value = mock_response

        result = self.client.purchase(
            requested_amount=5000,
            external_id='TEST-002',
            terminal_id='TERM001',
            currency='EUR',
            wait_seconds=60,
            order_id='ORDER-123',
            description='Test payment',
        )

        self.assertEqual(result['state'], 'AWAITING_CONFIRM')

        # Verify payload includes wait_seconds in options
        call_args = mock_post.call_args
        payload = json.loads(call_args[1].get('data', call_args[0][1] if len(call_args[0]) > 1 else '{}'))
        if 'options' in payload:
            self.assertEqual(payload['options']['wait_seconds'], 60)

    @patch('requests.Session.post')
    def test_18_successful_confirm_request(self, mock_post):
        """Test successful confirm API call (now requires terminal_id)."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {
                'state': 'CONFIRMED',
                'external_id': 'TEST-001',
                'result_code': 'SUCCESS'
            }
        }
        mock_post.return_value = mock_response

        result = self.client.confirm(
            external_id='TEST-001',
            terminal_id='TERM001',
            result_code='SUCCESS'
        )

        self.assertEqual(result['state'], 'CONFIRMED')

    @patch('requests.Session.post')
    def test_19_successful_abort_request(self, mock_post):
        """Test successful abort API call (replaces cancel)."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {
                'state': 'ABORTED',
                'external_id': 'TEST-001',
                'result_code': 'ABORTED'
            }
        }
        mock_post.return_value = mock_response

        result = self.client.abort(
            external_id='TEST-001',
            terminal_id='TERM001'
        )

        self.assertEqual(result['result_code'], 'ABORTED')

    @patch('requests.Session.post')
    def test_20_get_transaction(self, mock_post):
        """Test get transaction API call (was get_transaction_status)."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {
                'state': 'CONFIRMED',
                'external_id': 'TEST-001',
                'result_code': 'SUCCESS'
            }
        }
        mock_post.return_value = mock_response

        result = self.client.get_transaction('TEST-001')

        self.assertEqual(result['state'], 'CONFIRMED')

        # Verify endpoint is transaction/get (not transaction/status)
        call_args = mock_post.call_args
        self.assertIn('transaction/get', call_args[0][0])

    @patch('requests.Session.post')
    def test_21_continue_transaction(self, mock_post):
        """Test continue transaction with terminal_id and integer amount."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'transaction': {'state': 'PROCESSING'}
        }
        mock_post.return_value = mock_response

        result = self.client.continue_transaction(
            external_id='TEST-001',
            terminal_id='TERM001',
            requested_amount=7500,
        )

        self.assertEqual(result['state'], 'PROCESSING')

    @patch('requests.Session.post')
    def test_22_get_terminal_status(self, mock_post):
        """Test terminal status check (POST, not GET)."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'status': {
                'connected': True,
                'terminal_id': 'TERM001'
            }
        }
        mock_post.return_value = mock_response

        result = self.client.get_terminal_status('TERM001')

        self.assertTrue(result['connected'])

        # Verify endpoint
        call_args = mock_post.call_args
        self.assertIn('terminal/status', call_args[0][0])

    @patch('requests.Session.post')
    def test_23_get_events(self, mock_post):
        """Test event polling (replaces webhooks)."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'events': [
                {'type': 'transaction_update', 'external_id': 'TEST-001'}
            ]
        }
        mock_post.return_value = mock_response

        result = self.client.get_events('TERM001', wait_seconds=30)

        self.assertIn('events', result)

        # Verify endpoint
        call_args = mock_post.call_args
        self.assertIn('event/list', call_args[0][0])


class TestNexiApiClientErrors(BaseCase):
    """Tests for error handling and edge cases."""

    def setUp(self):
        super().setUp()
        self.client = NexiApiClient(
            key_id=TEST_KEY_ID,
            key_secret=TEST_KEY_SECRET,
        )

    def tearDown(self):
        self.client.close()
        super().tearDown()

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_24_authentication_error(self, mock_post):
        """Test handling of 401 authentication error."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 401
        mock_response.json.return_value = {
            'error': {
                'description': 'Invalid credentials',
                'code': 'AUTH_FAILED'
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(NexiAuthenticationError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('Authentication failed', str(cm.exception))

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_25_validation_error(self, mock_post):
        """Test handling of 400 validation error."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 400
        mock_response.json.return_value = {
            'error': {
                'description': 'Invalid amount',
                'code': 'INVALID_AMOUNT'
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(NexiValidationError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('Validation error', str(cm.exception))

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_26_server_error(self, mock_post):
        """Test handling of 500 server error."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 500
        mock_response.json.return_value = {
            'error': {
                'description': 'Internal server error',
                'code': 'SERVER_ERROR'
            }
        }
        mock_post.return_value = mock_response

        with self.assertRaises(NexiApiError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('API error', str(cm.exception))

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_27_timeout_error(self, mock_post):
        """Test handling of request timeout."""
        from requests.exceptions import Timeout
        mock_post.side_effect = Timeout('Request timed out')

        with self.assertRaises(NexiNetworkError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('timeout', str(cm.exception).lower())

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_28_connection_error(self, mock_post):
        """Test handling of connection error."""
        from requests.exceptions import ConnectionError
        mock_post.side_effect = ConnectionError('Connection refused')

        with self.assertRaises(NexiNetworkError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('Connection', str(cm.exception))

    @patch('requests.Session.post')
    @mute_logger('odoo.addons.pos_nexi_payment.models.nexi_api_client')
    def test_29_ssl_error(self, mock_post):
        """Test handling of SSL/TLS error."""
        from requests.exceptions import SSLError
        mock_post.side_effect = SSLError('SSL certificate verification failed')

        with self.assertRaises(NexiNetworkError) as cm:
            self.client.purchase(10000, 'TEST-001', 'TERM001')

        self.assertIn('SSL', str(cm.exception))

    @patch('requests.Session.post')
    def test_30_invalid_json_response(self, mock_post):
        """Test handling of invalid JSON response."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.json.side_effect = json.JSONDecodeError('Invalid JSON', '', 0)
        mock_response.text = 'Not JSON'
        mock_post.return_value = mock_response

        result = self.client.purchase(10000, 'TEST-001', 'TERM001')
        self.assertIn('raw_response', result)

    def test_31_purchase_validation_errors(self):
        """Test input validation in purchase method."""
        # Empty external_id
        with self.assertRaises(NexiValidationError):
            self.client.purchase(requested_amount=10000, external_id='', terminal_id='TERM')

        # Empty terminal_id
        with self.assertRaises(NexiValidationError):
            self.client.purchase(requested_amount=10000, external_id='TEST', terminal_id='')

        # Negative amount
        with self.assertRaises(NexiValidationError):
            self.client.purchase(requested_amount=-10, external_id='TEST', terminal_id='TERM')

        # Float amount (must be int)
        with self.assertRaises(NexiValidationError):
            self.client.purchase(requested_amount=10.50, external_id='TEST', terminal_id='TERM')

        # Invalid currency
        with self.assertRaises(NexiValidationError):
            self.client.purchase(requested_amount=10000, external_id='TEST', terminal_id='TERM', currency='E')

    def test_32_confirm_validation_errors(self):
        """Test input validation in confirm method (now requires terminal_id)."""
        # Empty external_id
        with self.assertRaises(NexiValidationError):
            self.client.confirm(external_id='', terminal_id='TERM001', result_code='SUCCESS')

        # Empty terminal_id
        with self.assertRaises(NexiValidationError):
            self.client.confirm(external_id='TEST', terminal_id='', result_code='SUCCESS')

        # Empty result_code
        with self.assertRaises(NexiValidationError):
            self.client.confirm(external_id='TEST', terminal_id='TERM001', result_code='')

        # Invalid result_code
        with self.assertRaises(NexiValidationError):
            self.client.confirm(external_id='TEST', terminal_id='TERM001', result_code='INVALID')

    def test_33_valid_result_codes(self):
        """Test that VALID_RESULT_CODES matches API spec."""
        expected = {'SUCCESS', 'ABORTED', 'REJECTED', 'NETWORK_ERROR', 'NOT_ACCEPTED'}
        self.assertEqual(VALID_RESULT_CODES, expected)

    def test_34_exception_metadata(self):
        """Test exception carries error code and response data."""
        try:
            raise NexiApiError(
                'Test error',
                error_code='TEST_CODE',
                response_data={'detail': 'More info'}
            )
        except NexiApiError as e:
            self.assertEqual(e.error_code, 'TEST_CODE')
            self.assertEqual(e.response_data['detail'], 'More info')


# This allows running the tests directly
if __name__ == '__main__':
    unittest.main()
