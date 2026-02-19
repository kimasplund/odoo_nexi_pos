# -*- coding: utf-8 -*-
"""Tests for pos_nexi_light module.

Tests the simple NEXI POS module which extends pos.payment.method
with NEXI terminal configuration fields and API integration.

Authentication: HTTP Basic Auth with nexi_api_key_id + nexi_api_key_secret
"""

from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


class TestPosNexi(TransactionCase):
    """Test suite for POS NEXI module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.PosPaymentMethod = cls.env['pos.payment.method']

    def test_01_module_installed(self):
        """Test that the module is installed."""
        module = self.env['ir.module.module'].search([
            ('name', '=', 'pos_nexi_light')
        ], limit=1)
        self.assertTrue(module)
        self.assertEqual(module.state, 'installed')

    def test_02_fields_exist(self):
        """Test that NEXI fields exist on pos.payment.method."""
        pm = self.PosPaymentMethod
        self.assertIn('nexi_terminal_id', pm._fields)
        self.assertIn('nexi_api_key_id', pm._fields)
        self.assertIn('nexi_api_key_secret', pm._fields)
        self.assertIn('nexi_test_mode', pm._fields)
        self.assertIn('nexi_terminal_status', pm._fields)
        # nexi_webhook_url was removed - API uses event polling

    def test_03_terminal_selection(self):
        """Test that NEXI appears in terminal selection."""
        selection = self.PosPaymentMethod._get_payment_terminal_selection()
        codes = [code for code, name in selection]
        self.assertIn('nexi', codes)

    def test_04_create_payment_method(self):
        """Test creating a NEXI payment method with key_id + key_secret."""
        pm = self.PosPaymentMethod.create({
            'name': 'Test NEXI',
            'use_payment_terminal': 'nexi',
            'nexi_terminal_id': 'TEST-001',
            'nexi_api_key_id': 'test_key_id',
            'nexi_api_key_secret': 'test_key_secret',
            'nexi_test_mode': True,
        })
        self.assertTrue(pm)
        self.assertEqual(pm.nexi_terminal_id, 'TEST-001')
        self.assertEqual(pm.nexi_api_key_id, 'test_key_id')
        self.assertEqual(pm.nexi_api_key_secret, 'test_key_secret')

    def test_05_duplicate_terminal_id_blocked(self):
        """Test that duplicate terminal IDs are blocked."""
        self.PosPaymentMethod.create({
            'name': 'Terminal 1',
            'use_payment_terminal': 'nexi',
            'nexi_terminal_id': 'DUPLICATE-TEST',
            'nexi_api_key_id': 'key_id_1',
            'nexi_api_key_secret': 'key_secret_1',
        })

        with self.assertRaises(ValidationError):
            self.PosPaymentMethod.create({
                'name': 'Terminal 2',
                'use_payment_terminal': 'nexi',
                'nexi_terminal_id': 'DUPLICATE-TEST',
                'nexi_api_key_id': 'key_id_2',
                'nexi_api_key_secret': 'key_secret_2',
            })

    def test_06_missing_credentials_blocked(self):
        """Test that missing API credentials are blocked."""
        # Missing both key_id and key_secret
        with self.assertRaises(ValidationError):
            self.PosPaymentMethod.create({
                'name': 'No Credentials',
                'use_payment_terminal': 'nexi',
                'nexi_terminal_id': 'NO-CRED-001',
                # nexi_api_key_id missing
                # nexi_api_key_secret missing
            })

    def test_07_missing_key_secret_blocked(self):
        """Test that missing key_secret is blocked."""
        with self.assertRaises(ValidationError):
            self.PosPaymentMethod.create({
                'name': 'No Secret',
                'use_payment_terminal': 'nexi',
                'nexi_terminal_id': 'NO-SEC-001',
                'nexi_api_key_id': 'only_id_provided',
                # nexi_api_key_secret missing
            })
