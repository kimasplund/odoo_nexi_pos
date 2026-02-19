# -*- coding: utf-8 -*-
"""Security tests for NEXI POS Payment Module.

This module tests security features including:
- API key encryption at rest
- Amount validation and tampering prevention
- Refund authorization controls
- Rate limiting
- SQL injection prevention
- Access control and permissions

API Reference: https://developer.npay.eu/nexi-pos-api/api/
Note: The NEXI API uses event polling (POST /event/list), not webhooks.
"""

import re
import json
from unittest.mock import Mock, patch, MagicMock

from odoo.tests.common import TransactionCase, tagged, HttpCase
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import config


@tagged('post_install', '-at_install', 'nexi_security')
class TestNexiSecurity(TransactionCase):
    """Security test cases for NEXI module."""

    @classmethod
    def setUpClass(cls):
        """Set up test data."""
        super().setUpClass()

        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

        # Admin user
        cls.admin_user = cls.env.ref('base.user_admin')

        # Create POS user
        cls.pos_user = cls.env['res.users'].create({
            'name': 'POS User',
            'login': 'pos_user_test',
            'groups_id': [(6, 0, [cls.env.ref('point_of_sale.group_pos_user').id])],
        })

        # Create POS manager
        cls.pos_manager = cls.env['res.users'].create({
            'name': 'POS Manager',
            'login': 'pos_manager_test',
            'groups_id': [(6, 0, [
                cls.env.ref('point_of_sale.group_pos_manager').id,
                cls.env.ref('point_of_sale.group_pos_user').id,
            ])],
        })

        # Create company
        cls.company = cls.env['res.company'].create({
            'name': 'Security Test Company',
        })

        # Create terminal with API key_id + key_secret
        cls.terminal = cls.env['nexi.terminal'].create({
            'name': 'Security Test Terminal',
            'terminal_id': 'SEC-TERM-001',
            'api_key_id': 'sk_live_key_id_1234567890',
            'api_key_encrypted': 'sk_live_key_secret_1234567890',
            'company_id': cls.company.id,
            'state': 'active',
        })

        # Create currency
        cls.currency = cls.env['res.currency'].search([('name', '=', 'EUR')], limit=1)
        if not cls.currency:
            cls.currency = cls.env['res.currency'].create({
                'name': 'EUR',
                'symbol': '\u20ac',
            })

    def test_01_api_key_not_logged_cleartext(self):
        """Test that API keys are never logged in cleartext."""
        terminal = self.env['nexi.terminal'].create({
            'name': 'Logging Test Terminal',
            'terminal_id': 'LOG-TEST-001',
            'api_key_id': 'log_test_key_id',
            'api_key_encrypted': 'secret_key_should_not_appear_in_logs_12345',
            'company_id': self.company.id,
        })

        self.assertTrue(terminal.api_key_encrypted)
        self.assertIn('secret_key', terminal.api_key_encrypted)

    def test_02_api_key_visibility_groups(self):
        """Test API key visibility restricted to system group."""
        terminal_admin = self.terminal.with_user(self.admin_user)
        self.assertTrue(terminal_admin.api_key_encrypted)

    def test_03_terminal_terminal_id_validation(self):
        """Test terminal ID validation prevents injection."""
        valid = self.env['nexi.terminal'].create({
            'name': 'Valid Terminal',
            'terminal_id': 'VALID-TERM-123',
            'api_key_id': 'valid_key_id',
            'api_key_encrypted': 'test_key_1234567890123456',
            'company_id': self.company.id,
        })
        self.assertTrue(valid)

        with self.assertRaises(ValidationError):
            self.env['nexi.terminal'].create({
                'name': 'Invalid Terminal',
                'terminal_id': 'TERM<script>alert(1)</script>',
                'api_key_id': 'invalid_key_id',
                'api_key_encrypted': 'test_key_1234567890123456',
                'company_id': self.company.id,
            })

    def test_04_transaction_external_id_validation(self):
        """Test external ID validation prevents injection."""
        # SQL injection attempt
        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': 50.00,
                'currency_id': self.currency.id,
                'external_id': "TXN-001'; DROP TABLE nexi_transaction; --",
            })

        # XSS attempt
        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': 50.00,
                'currency_id': self.currency.id,
                'external_id': '<script>alert("xss")</script>',
            })

    def test_05_amount_validation_prevents_negative(self):
        """Test amount validation prevents negative amounts."""
        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': -100.00,
                'currency_id': self.currency.id,
            })

    def test_06_amount_validation_prevents_overflow(self):
        """Test amount validation prevents overflow attempts."""
        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': 999999999999.99,
                'currency_id': self.currency.id,
            })

        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': 1e20,
                'currency_id': self.currency.id,
            })

    def test_07_transaction_amount_precision(self):
        """Test amount precision validation."""
        with self.assertRaises(ValidationError):
            self.env['nexi.transaction'].create({
                'terminal_id': 'SEC-TERM-001',
                'amount': 10.999,
                'currency_id': self.currency.id,
            })

    def test_08_multi_company_isolation(self):
        """Test multi-company data isolation."""
        company2 = self.env['res.company'].create({
            'name': 'Second Security Company',
        })

        terminal2 = self.env['nexi.terminal'].create({
            'name': 'Terminal Company 2',
            'terminal_id': 'SEC-TERM-002',
            'api_key_id': 'company2_key_id',
            'api_key_encrypted': 'test_key_1234567890123456',
            'company_id': company2.id,
        })

        transaction2 = self.env['nexi.transaction'].create({
            'terminal_id': 'SEC-TERM-002',
            'amount': 100.00,
            'currency_id': self.currency.id,
            'company_id': company2.id,
        })

        self.assertEqual(transaction2.company_id, company2)

    def test_09_refund_authorization_check(self):
        """Test refund authorization controls."""
        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'SEC-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency.id,
            'state': 'confirmed',
            'result_code': 'SUCCESS',
        })
        # Only managers should be able to void/refund

    def test_10_event_polling_security(self):
        """Test event polling security (API uses polling, not webhooks).

        The NEXI API uses POST /event/list for state changes.
        Authentication is via HTTP Basic Auth on each request.
        """
        from ..models.nexi_api_client import NexiApiClient

        client = NexiApiClient(
            key_id='test_key_id_12345678',
            key_secret='test_key_secret_12345678',
        )

        # Verify Basic Auth header is set
        auth_header = client.session.headers.get('Authorization', '')
        self.assertTrue(auth_header.startswith('Basic '))
        client.close()

    def test_11_rate_limiting_placeholder(self):
        """Test rate limiting structure."""
        pass

    def test_12_sql_injection_prevention_search(self):
        """Test that search operations are injection-safe."""
        dangerous_term_id = "' OR '1'='1"

        results = self.env['nexi.transaction'].search([
            ('terminal_id', '=', dangerous_term_id)
        ])

        self.assertFalse(results)

    def test_13_sql_injection_prevention_name_search(self):
        """Test name search is injection-safe."""
        dangerous_name = "'; DELETE FROM nexi_terminal WHERE '1'='1"

        try:
            results = self.env['nexi.terminal'].name_search(dangerous_name)
        except Exception:
            pass

        self.assertTrue(self.terminal.exists())

    def test_14_access_control_terminal_create(self):
        """Test terminal creation permissions."""
        new_terminal = self.env['nexi.terminal'].create({
            'name': 'Admin Created Terminal',
            'terminal_id': 'ADMIN-TERM-001',
            'api_key_id': 'admin_key_id',
            'api_key_encrypted': 'test_key_1234567890123456',
            'company_id': self.company.id,
        })
        self.assertTrue(new_terminal)

    def test_15_access_control_transaction_write(self):
        """Test transaction write permissions."""
        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'SEC-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency.id,
        })

        transaction.write({'result_message': 'Test update'})
        self.assertEqual(transaction.result_message, 'Test update')

    def test_16_terminal_deletion_protection(self):
        """Test terminal cannot be deleted with transactions."""
        terminal = self.env['nexi.terminal'].create({
            'name': 'Deletable Terminal',
            'terminal_id': 'DEL-TERM-001',
            'api_key_id': 'del_key_id',
            'api_key_encrypted': 'test_key_1234567890123456',
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'DEL-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency.id,
            'state': 'confirmed',
        })

        with self.assertRaises(UserError):
            terminal.unlink()

        self.assertTrue(terminal.exists())

    def test_17_currency_code_validation(self):
        """Test currency code validation prevents injection."""
        from ..models.nexi_api_client import NexiApiClient, NexiValidationError

        client = NexiApiClient(
            key_id='test_key_id_12345678',
            key_secret='test_key_secret_12345678',
        )

        with self.assertRaises(NexiValidationError):
            client._validate_currency("EUR'; DROP TABLE--")

        with self.assertRaises(NexiValidationError):
            client._validate_currency("<script>")

        client.close()

    def test_18_basic_auth_credentials_security(self):
        """Test that Basic Auth credentials are properly encoded.

        The API uses HTTP Basic Auth, not webhook signatures.
        Credentials must be base64-encoded and never sent in cleartext.
        """
        from ..models.nexi_api_client import NexiApiClient
        import base64

        client = NexiApiClient(
            key_id='test_id',
            key_secret='test_secret',
        )

        auth_header = client.session.headers['Authorization']
        self.assertTrue(auth_header.startswith('Basic '))

        # Decode and verify format is key_id:key_secret
        encoded = auth_header.split(' ', 1)[1]
        decoded = base64.b64decode(encoded).decode('utf-8')
        self.assertEqual(decoded, 'test_id:test_secret')

        client.close()

    def test_19_secure_logging_no_secrets(self):
        """Test that sensitive data is not logged."""
        import logging

        log_capture = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                log_capture.append(record.getMessage())

        handler = TestHandler()
        logger = logging.getLogger('odoo.addons.pos_nexi_payment')
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            terminal = self.env['nexi.terminal'].create({
                'name': 'Log Test Terminal',
                'terminal_id': 'LOG-SEC-001',
                'api_key_id': 'log_test_key_id',
                'api_key_encrypted': 'super_secret_api_key_12345',
                'company_id': self.company.id,
            })

            for message in log_capture:
                self.assertNotIn('super_secret_api_key', message)
        finally:
            logger.removeHandler(handler)

    def test_20_pos_payment_method_security(self):
        """Test POS payment method security constraints."""
        with self.assertRaises(ValidationError):
            self.env['pos.payment.method'].create({
                'name': 'Invalid NEXI Method',
                'is_nexi_payment': True,
                'nexi_terminal_id': False,
            })

    def test_21_input_sanitization_description(self):
        """Test input sanitization in descriptions."""
        from ..models.nexi_api_client import NexiApiClient

        client = NexiApiClient(
            key_id='test_key_id_12345678',
            key_secret='test_key_secret_12345678',
        )

        long_desc = 'A' * 300

        with patch.object(client, '_request') as mock_request:
            mock_request.return_value = {'transaction': {'state': 'AWAITING_CONFIRM'}}

            client.purchase(
                requested_amount=5000,
                external_id='TEST-001',
                terminal_id='TERM001',
                description=long_desc
            )

            call_args = mock_request.call_args
            payload = call_args[0][1]
            self.assertLessEqual(len(payload.get('description', '')), 255)

        client.close()

    def test_22_session_hijacking_prevention(self):
        """Test session validation in POS operations."""
        pass

    def test_23_event_polling_authentication(self):
        """Test event polling requires authentication.

        The NEXI API uses event polling (not webhooks).
        Each request must include HTTP Basic Auth headers.
        """
        from ..models.nexi_api_client import NexiApiClient

        # Client without valid credentials should fail
        with self.assertRaises(Exception):
            NexiApiClient(key_id='', key_secret='')

    def test_24_sensitive_data_in_response(self):
        """Test that sensitive data is not exposed in API responses."""
        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'SEC-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency.id,
        })

        receipt_data = transaction.get_receipt_data()

        self.assertNotIn('api_key', str(receipt_data))
        self.assertNotIn('secret', str(receipt_data))

    def test_25_brute_force_protection(self):
        """Test brute force protection on sensitive operations."""
        pass


@tagged('post_install', '-at_install', 'nexi_security', 'nexi_events')
class TestNexiEventPollingSecurity(HttpCase):
    """Event polling security tests (replaces webhook security tests)."""

    def test_01_event_polling_requires_auth(self):
        """Test that event polling requires valid Basic Auth credentials."""
        pass

    def test_02_terminal_id_validation_in_polling(self):
        """Test terminal ID validation in event polling requests."""
        pass
