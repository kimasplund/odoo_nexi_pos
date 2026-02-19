# -*- coding: utf-8 -*-
"""Integration tests for NEXI POS Payment Module.

This module tests the complete integration flow including:
- Payment method integration
- POS order flow with NEXI payments
- Terminal activation and status checking
- End-of-day reconciliation
- Multi-terminal scenarios

API Reference: https://developer.npay.eu/nexi-pos-api/api/
Note: Terminal has action_activate/deactivate/test_connection (not open_session/close_session).
"""

from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError, ValidationError
from odoo import fields


@tagged('post_install', '-at_install', 'nexi_integration')
class TestNexiPosIntegration(TransactionCase):
    """Integration test cases for NEXI POS workflow."""

    @classmethod
    def setUpClass(cls):
        """Set up test data for integration tests."""
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))

        # Create test company
        cls.company = cls.env['res.company'].create({
            'name': 'Integration Test Company',
        })

        # Get or create currency
        cls.currency_eur = cls.env['res.currency'].search([
            ('name', '=', 'EUR')
        ], limit=1)
        if not cls.currency_eur:
            cls.currency_eur = cls.env['res.currency'].create({
                'name': 'EUR',
                'symbol': '\u20ac',
                'rounding': 0.01,
            })

        # Create main warehouse
        cls.warehouse = cls.env['stock.warehouse'].create({
            'name': 'Test Warehouse',
            'code': 'TEST-WH',
            'company_id': cls.company.id,
        })

        # Create POS journal
        cls.pos_journal = cls.env['account.journal'].create({
            'name': 'POS Journal',
            'code': 'POS',
            'type': 'sale',
            'company_id': cls.company.id,
        })

        # Create cash journal
        cls.cash_journal = cls.env['account.journal'].create({
            'name': 'Cash',
            'code': 'CSH',
            'type': 'cash',
            'company_id': cls.company.id,
        })

        # Create NEXI terminals (with api_key_id for Basic Auth)
        cls.terminal_1 = cls.env['nexi.terminal'].create({
            'name': 'Terminal 1 - Counter',
            'terminal_id': 'INT-TERM-001',
            'api_key_id': 'int_test_key_id_001',
            'api_key_encrypted': 'test_api_key_12345678901234567890',
            'company_id': cls.company.id,
            'state': 'active',
        })

        cls.terminal_2 = cls.env['nexi.terminal'].create({
            'name': 'Terminal 2 - Bar',
            'terminal_id': 'INT-TERM-002',
            'api_key_id': 'int_test_key_id_002',
            'api_key_encrypted': 'test_api_key_09876543210987654321',
            'company_id': cls.company.id,
            'state': 'active',
        })

        # Create POS config
        cls.pos_config = cls.env['pos.config'].create({
            'name': 'Integration Test POS',
            'company_id': cls.company.id,
            'warehouse_id': cls.warehouse.id,
            'journal_id': cls.pos_journal.id,
        })

        # Create payment methods
        cls.cash_method = cls.env['pos.payment.method'].create({
            'name': 'Cash',
            'journal_id': cls.cash_journal.id,
        })

        cls.nexi_method = cls.env['pos.payment.method'].create({
            'name': 'NEXI Card',
            'is_nexi_payment': True,
            'nexi_terminal_id': cls.terminal_1.id,
        })

        # Add payment methods to config
        cls.pos_config.write({
            'payment_method_ids': [(6, 0, [cls.cash_method.id, cls.nexi_method.id])],
        })

        # Create test product
        cls.product = cls.env['product.product'].create({
            'name': 'Test Product',
            'type': 'consu',
            'list_price': 50.00,
            'company_id': cls.company.id,
        })

    def setUp(self):
        """Set up for each test."""
        super().setUp()
        self.session = self.env['pos.session'].create({
            'config_id': self.pos_config.id,
            'user_id': self.env.user.id,
        })

    def test_01_payment_method_configuration(self):
        """Test NEXI payment method is properly configured."""
        self.assertTrue(self.nexi_method.is_nexi_payment)
        self.assertEqual(self.nexi_method.nexi_terminal_id, self.terminal_1)
        self.assertEqual(self.nexi_method.nexi_terminal_id.state, 'active')

    def test_02_session_compute_nexi_terminals(self):
        """Test session computes NEXI terminals correctly."""
        self.assertTrue(self.session.has_nexi_terminal)
        self.assertIn(self.terminal_1, self.session.nexi_terminal_ids)

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.get_terminal_status')
    def test_03_terminal_test_connection(self, mock_status):
        """Test terminal connection check via POST /terminal/status."""
        mock_status.return_value = {
            'connected': True,
            'terminal_id': 'INT-TERM-001'
        }

        result = self.terminal_1.action_test_connection()

        # Should succeed and update terminal state
        self.assertEqual(self.terminal_1.state, 'active')

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.get_terminal_status')
    def test_04_terminal_test_connection_offline(self, mock_status):
        """Test handling offline terminal during connection test."""
        from ..models.nexi_api_client import NexiNetworkError
        mock_status.side_effect = NexiNetworkError('Connection timeout')

        # Should handle error gracefully
        result = self.terminal_1.action_test_connection()
        # Terminal state should reflect error
        self.assertEqual(self.terminal_1.state, 'error')

    def test_05_create_pos_order_with_nexi_payment(self):
        """Test creating POS order with NEXI payment."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
            'amount_total': 100.00,
        })

        self.env['pos.order.line'].create({
            'order_id': order.id,
            'product_id': self.product.id,
            'qty': 2,
            'price_unit': 50.00,
        })

        self.assertTrue(order)
        self.assertEqual(order.amount_total, 100.00)

    @patch('odoo.addons.pos_nexi_payment.models.pos_payment_method.PosPaymentMethod.nexi_payment_request')
    def test_06_add_nexi_payment_to_order(self, mock_payment):
        """Test adding NEXI payment to an order."""
        mock_payment.return_value = {
            'success': True,
            'transaction_id': 123,
            'external_id': 'TEST-TXN-001',
            'message': 'Payment initiated'
        }

        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
            'amount_total': 100.00,
        })

        result = order.add_nexi_payment(
            amount=100.00,
            payment_method_id=self.nexi_method.id
        )

        mock_payment.assert_called_once()
        self.assertTrue(result['success'])

    def test_07_add_nexi_payment_wrong_state(self):
        """Test cannot add payment to paid order."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
            'amount_total': 100.00,
            'state': 'paid',
        })

        result = order.add_nexi_payment(amount=100.00)

        self.assertFalse(result['success'])
        self.assertIn('completed order', result['message'].lower())

    def test_08_add_nexi_payment_invalid_amount(self):
        """Test payment validation for invalid amounts."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
            'amount_total': 100.00,
        })

        result = order.add_nexi_payment(amount=0)
        self.assertFalse(result['success'])

        result = order.add_nexi_payment(amount=-10)
        self.assertFalse(result['success'])

    def test_09_add_nexi_payment_no_method(self):
        """Test payment fails without NEXI method."""
        config_no_nexi = self.env['pos.config'].create({
            'name': 'No NEXI Config',
            'company_id': self.company.id,
            'payment_method_ids': [(6, 0, [self.cash_method.id])],
        })

        session = self.env['pos.session'].create({
            'config_id': config_no_nexi.id,
            'user_id': self.env.user.id,
        })

        order = self.env['pos.order'].create({
            'session_id': session.id,
            'company_id': self.company.id,
            'amount_total': 100.00,
        })

        result = order.add_nexi_payment(amount=100.00)

        self.assertFalse(result['success'])
        self.assertIn('no nexi', result['message'].lower())

    def test_10_validate_nexi_payments_pending(self):
        """Test validation detects pending payments."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'pending',
        })

        result = order.validate_nexi_payments()

        self.assertFalse(result['valid'])
        self.assertTrue(result['pending'])

    def test_11_validate_nexi_payments_failed(self):
        """Test validation detects failed payments."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'failed',
            'result_message': 'Card rejected',
        })

        result = order.validate_nexi_payments()

        self.assertFalse(result['valid'])
        self.assertTrue(result['failed'])

    def test_12_terminal_deactivation(self):
        """Test deactivating a NEXI terminal."""
        # Create a terminal we can modify
        test_terminal = self.env['nexi.terminal'].create({
            'name': 'Deactivation Test',
            'terminal_id': 'DEACT-TERM-001',
            'api_key_id': 'deact_key_id',
            'api_key_encrypted': 'deact_key_secret',
            'company_id': self.company.id,
            'state': 'active',
        })

        test_terminal.action_deactivate()
        self.assertEqual(test_terminal.state, 'inactive')

    def test_13_cannot_close_session_with_pending(self):
        """Test cannot close session with pending NEXI payments."""
        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'state': 'pending',
        })

        with self.assertRaises(UserError) as cm:
            self.session.action_pos_session_closing_control()

        self.assertIn('pending', str(cm.exception).lower())

    def test_14_get_nexi_reconciliation_data(self):
        """Test reconciliation data generation."""
        self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'state': 'confirmed',
        })

        self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'cancelled',
        })

        data = self.session.get_nexi_reconciliation_data()

        self.assertIn('by_terminal', data)
        self.assertIn('totals', data)
        self.assertEqual(data['totals']['count'], 2)

    def test_15_cancel_pending_nexi_payments(self):
        """Test cancelling pending payments on order."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        txn1 = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'pending',
        })

        txn2 = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'pending',
        })

        result = order.cancel_pending_nexi_payments()

        self.assertEqual(result['cancelled'], 2)
        self.assertEqual(txn1.state, 'cancelled')
        self.assertEqual(txn2.state, 'cancelled')

    def test_16_multi_terminal_support(self):
        """Test using multiple terminals in same session."""
        nexi_method_2 = self.env['pos.payment.method'].create({
            'name': 'NEXI Card - Bar',
            'is_nexi_payment': True,
            'nexi_terminal_id': self.terminal_2.id,
        })

        self.pos_config.write({
            'payment_method_ids': [(4, nexi_method_2.id)],
        })

        self.session.invalidate_cache()

        self.assertIn(self.terminal_1, self.session.nexi_terminal_ids)
        self.assertIn(self.terminal_2, self.session.nexi_terminal_ids)

    def test_17_payment_linking_to_transaction(self):
        """Test POS payment links to NEXI transaction."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'confirmed',
        })

        payment = self.env['pos.payment'].create({
            'pos_order_id': order.id,
            'payment_method_id': self.nexi_method.id,
            'amount': 100.00,
            'nexi_transaction_id': transaction.id,
        })

        self.assertEqual(payment.nexi_transaction_id, transaction)
        self.assertEqual(payment.nexi_external_id, transaction.external_id)
        self.assertTrue(payment.is_nexi_payment)

    def test_18_nexi_payment_count_computation(self):
        """Test NEXI payment count computation."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        self.assertEqual(order.nexi_payment_count, 0)

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
        })

        payment = self.env['pos.payment'].create({
            'pos_order_id': order.id,
            'payment_method_id': self.nexi_method.id,
            'amount': 100.00,
            'nexi_transaction_id': transaction.id,
        })

        order.invalidate_cache()
        self.assertEqual(order.nexi_payment_count, 1)

    def test_19_has_pending_nexi_payments_computation(self):
        """Test pending payments flag computation."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        self.assertFalse(order.has_pending_nexi_payments)

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'pending',
        })

        order.invalidate_cache()
        self.assertTrue(order.has_pending_nexi_payments)

    def test_20_action_pos_order_paid_with_pending(self):
        """Test cannot mark order paid with pending NEXI payments."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'pending',
        })

        with self.assertRaises(UserError) as cm:
            order.action_pos_order_paid()

        self.assertIn('pending', str(cm.exception).lower())

    def test_21_action_pos_order_paid_with_failed(self):
        """Test cannot mark order paid with failed NEXI payments."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        transaction = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
            'state': 'failed',
            'result_message': 'Card rejected',
        })

        with self.assertRaises(UserError) as cm:
            order.action_pos_order_paid()

        self.assertIn('failed', str(cm.exception).lower())

    def test_22_view_nexi_transactions_action(self):
        """Test view transactions action returns correct window."""
        order = self.env['pos.order'].create({
            'session_id': self.session.id,
            'company_id': self.company.id,
        })

        txn1 = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
        })

        txn2 = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': order.id,
        })

        action = order.action_view_nexi_transactions()

        self.assertEqual(action['res_model'], 'nexi.transaction')
        self.assertIn(order.id, action['domain'][0][2])

    def test_23_terminal_company_validation(self):
        """Test terminal and POS config company must match."""
        other_company = self.env['res.company'].create({
            'name': 'Other Company',
        })

        other_terminal = self.env['nexi.terminal'].create({
            'name': 'Other Terminal',
            'terminal_id': 'OTHER-TERM-001',
            'api_key_id': 'other_key_id',
            'api_key_encrypted': 'test_key_1234567890123456',
            'company_id': other_company.id,
            'state': 'active',
        })

        with self.assertRaises(ValidationError):
            self.pos_config.write({
                'payment_method_ids': [(0, 0, {
                    'name': 'Invalid Cross-Company',
                    'is_nexi_payment': True,
                    'nexi_terminal_id': other_terminal.id,
                })],
            })

    def test_24_payment_method_conflict_check(self):
        """Test cannot use NEXI and another terminal simultaneously."""
        with self.assertRaises(ValidationError):
            self.env['pos.payment.method'].create({
                'name': 'Conflicting Method',
                'is_nexi_payment': True,
                'nexi_terminal_id': self.terminal_1.id,
                'use_payment_terminal': 'adyen',
            })

    def test_25_get_nexi_transactions_filtering(self):
        """Test getting filtered NEXI transactions for session."""
        txn_confirmed = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 100.00,
            'currency_id': self.currency_eur.id,
            'state': 'confirmed',
        })

        txn_pending = self.env['nexi.transaction'].create({
            'terminal_id': 'INT-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'pending',
        })

        all_txns = self.session.get_nexi_transactions()
        self.assertEqual(len(all_txns), 2)

        confirmed = self.session.get_nexi_transactions([
            ('state', '=', 'confirmed')
        ])
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0], txn_confirmed)
