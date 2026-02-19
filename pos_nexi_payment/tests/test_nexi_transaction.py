# -*- coding: utf-8 -*-
"""Tests for NEXI Transaction Model.

This module tests the NexiTransaction model including:
- State transitions and lifecycle
- Purchase flow initiation
- Confirmation flow
- Abort/cancellation handling
- Notification processing
- Security validations

API Reference: https://developer.npay.eu/nexi-pos-api/api/
Result codes: SUCCESS, ABORTED, REJECTED, NETWORK_ERROR, NOT_ACCEPTED
"""

import json
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from odoo.tests.common import TransactionCase, tagged
from odoo.exceptions import UserError, ValidationError
from odoo import fields


@tagged('post_install', '-at_install', 'nexi_transaction')
class TestNexiTransaction(TransactionCase):
    """Test cases for NEXI Transaction model."""

    @classmethod
    def setUpClass(cls):
        """Set up test data once for all tests."""
        super().setUpClass()

        # Create test company
        cls.company = cls.env['res.company'].create({
            'name': 'Test Company NEXI',
        })

        # Create test currency
        cls.currency_eur = cls.env['res.currency'].search([
            ('name', '=', 'EUR')
        ], limit=1)
        if not cls.currency_eur:
            cls.currency_eur = cls.env['res.currency'].create({
                'name': 'EUR',
                'symbol': '\u20ac',
                'rounding': 0.01,
            })

        # Create test terminal (now requires api_key_id)
        cls.terminal = cls.env['nexi.terminal'].create({
            'name': 'Test Terminal',
            'terminal_id': 'TEST-TERM-001',
            'api_key_id': 'test_key_id_12345678',
            'api_key_encrypted': 'test_key_secret_12345678',
            'company_id': cls.company.id,
            'state': 'active',
        })

        # Create POS config
        cls.pos_config = cls.env['pos.config'].create({
            'name': 'Test POS Config',
            'company_id': cls.company.id,
        })

        # Create payment method
        cls.payment_method = cls.env['pos.payment.method'].create({
            'name': 'NEXI Test',
            'is_nexi_payment': True,
            'nexi_terminal_id': cls.terminal.id,
        })

        # Create POS session
        cls.session = cls.env['pos.session'].create({
            'config_id': cls.pos_config.id,
            'user_id': cls.env.user.id,
        })

        # Create POS order
        cls.order = cls.env['pos.order'].create({
            'session_id': cls.session.id,
            'company_id': cls.company.id,
            'amount_total': 100.00,
        })

    def setUp(self):
        """Set up for each test method."""
        super().setUp()
        self.Transaction = self.env['nexi.transaction']

    def test_01_create_transaction_basic(self):
        """Test basic transaction creation."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'pos_order_id': self.order.id,
        })

        self.assertTrue(transaction.external_id)
        self.assertTrue(transaction.external_id.startswith('NEXI-'))
        self.assertEqual(transaction.state, 'pending')
        self.assertEqual(transaction.amount, 50.00)
        self.assertEqual(transaction.terminal_id, 'TEST-TERM-001')

    def test_02_create_transaction_with_external_id(self):
        """Test transaction creation with custom external ID."""
        custom_id = 'CUSTOM-TEST-001'
        transaction = self.Transaction.create({
            'external_id': custom_id,
            'terminal_id': 'TEST-TERM-001',
            'amount': 75.00,
            'currency_id': self.currency_eur.id,
        })

        self.assertEqual(transaction.external_id, custom_id)

    def test_03_external_id_unique_constraint(self):
        """Test external ID uniqueness constraint."""
        external_id = 'UNIQUE-TEST-001'

        txn1 = self.Transaction.create({
            'external_id': external_id,
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })
        self.assertTrue(txn1)

        with self.assertRaises(Exception):  # IntegrityError
            self.Transaction.create({
                'external_id': external_id,
                'terminal_id': 'TEST-TERM-001',
                'amount': 60.00,
                'currency_id': self.currency_eur.id,
            })

    def test_04_amount_validation_positive(self):
        """Test amount must be positive."""
        with self.assertRaises(ValidationError):
            self.Transaction.create({
                'terminal_id': 'TEST-TERM-001',
                'amount': 0,
                'currency_id': self.currency_eur.id,
            })

        with self.assertRaises(ValidationError):
            self.Transaction.create({
                'terminal_id': 'TEST-TERM-001',
                'amount': -10.00,
                'currency_id': self.currency_eur.id,
            })

    def test_05_amount_validation_maximum(self):
        """Test amount maximum limit."""
        with self.assertRaises(ValidationError):
            self.Transaction.create({
                'terminal_id': 'TEST-TERM-001',
                'amount': 1000000.00,
                'currency_id': self.currency_eur.id,
            })

    def test_06_state_transitions_allowed(self):
        """Test allowed state transitions."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        self.assertEqual(transaction.state, 'pending')

        transaction.write({'state': 'processing'})
        self.assertEqual(transaction.state, 'processing')

        transaction.write({'state': 'awaiting_confirm'})
        self.assertEqual(transaction.state, 'awaiting_confirm')

        transaction.write({'state': 'confirmed'})
        self.assertEqual(transaction.state, 'confirmed')

    def test_07_compute_terminal_ref(self):
        """Test terminal reference computation."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        self.assertEqual(transaction.terminal_ref, self.terminal)

    def test_08_is_successful_method(self):
        """Test is_successful method."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        self.assertFalse(transaction.is_successful())

        # Mark as confirmed with SUCCESS
        transaction.write({
            'state': 'confirmed',
            'result_code': 'SUCCESS'
        })
        self.assertTrue(transaction.is_successful())

        # Confirmed but with REJECTED code (per API spec, not DECLINED)
        transaction.write({'result_code': 'REJECTED'})
        self.assertFalse(transaction.is_successful())

    def test_09_can_be_confirmed_method(self):
        """Test can_be_confirmed method."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        self.assertFalse(transaction.can_be_confirmed())

        transaction.write({'state': 'processing'})
        self.assertTrue(transaction.can_be_confirmed())

        transaction.write({'state': 'awaiting_confirm'})
        self.assertTrue(transaction.can_be_confirmed())

        transaction.write({'state': 'confirmed'})
        self.assertFalse(transaction.can_be_confirmed())

    def test_10_get_receipt_data(self):
        """Test get_receipt_data method."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        data = transaction.get_receipt_data()

        self.assertEqual(data['external_id'], transaction.external_id)
        self.assertEqual(data['terminal_id'], 'TEST-TERM-001')
        self.assertEqual(data['amount'], 50.00)
        self.assertEqual(data['currency'], 'EUR')
        self.assertEqual(data['state'], 'pending')

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.purchase')
    def test_11_action_purchase_success(self, mock_purchase):
        """Test successful purchase action."""
        mock_purchase.return_value = {
            'state': 'AWAITING_CONFIRM',
            'result_code': 'PENDING',
            'result_description': 'Transaction initiated'
        }

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        result = transaction.action_purchase()

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'awaiting_confirm')
        self.assertIsNotNone(transaction.purchase_request_time)
        mock_purchase.assert_called_once()

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.purchase')
    def test_12_action_purchase_immediate_success(self, mock_purchase):
        """Test purchase action with immediate success (CONFIRMED state)."""
        mock_purchase.return_value = {
            'state': 'CONFIRMED',
            'result_code': 'SUCCESS',
        }

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        with patch.object(transaction, 'action_confirm') as mock_confirm:
            mock_confirm.return_value = True
            transaction.action_purchase()
            mock_confirm.assert_called_once()

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.purchase')
    def test_13_action_purchase_failure(self, mock_purchase):
        """Test purchase action with API failure."""
        from ..models.nexi_api_client import NexiApiError
        mock_purchase.side_effect = NexiApiError('API Error')

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        with self.assertRaises(UserError):
            transaction.action_purchase()

        self.assertEqual(transaction.state, 'failed')
        self.assertEqual(transaction.error_count, 1)
        self.assertIsNotNone(transaction.last_error)

    def test_14_action_purchase_wrong_state(self):
        """Test purchase action with invalid state."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })
        transaction.write({'state': 'processing'})

        with self.assertRaises(UserError):
            transaction.action_purchase()

    def test_15_action_purchase_terminal_not_found(self):
        """Test purchase action with missing terminal."""
        transaction = self.Transaction.create({
            'terminal_id': 'NONEXISTENT-TERM',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        with self.assertRaises(UserError) as cm:
            transaction.action_purchase()

        self.assertIn('not found', str(cm.exception).lower())

    def test_16_action_purchase_terminal_inactive(self):
        """Test purchase action with inactive terminal."""
        self.terminal.write({'state': 'inactive'})

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
        })

        with self.assertRaises(UserError) as cm:
            transaction.action_purchase()

        self.assertIn('not active', str(cm.exception).lower())

        # Restore terminal state
        self.terminal.write({'state': 'active'})

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.confirm')
    def test_17_action_confirm_success(self, mock_confirm):
        """Test successful confirmation."""
        mock_confirm.return_value = {
            'state': 'CONFIRMED',
            'result_code': 'SUCCESS'
        }

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'awaiting_confirm',
        })

        result = transaction.action_confirm()

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'confirmed')
        self.assertEqual(transaction.confirm_attempts, 1)
        self.assertIsNotNone(transaction.confirm_request_time)

    def test_18_action_confirm_wrong_state(self):
        """Test confirm action with invalid state."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'pending',
        })

        with self.assertRaises(UserError):
            transaction.action_confirm()

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.abort')
    def test_19_action_cancel_success(self, mock_abort):
        """Test successful cancellation (uses abort, not cancel)."""
        mock_abort.return_value = {
            'state': 'ABORTED',
            'result_code': 'ABORTED'
        }

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        result = transaction.action_cancel()

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'cancelled')
        self.assertEqual(transaction.result_code, 'ABORTED')

    def test_20_action_cancel_confirmed_transaction(self):
        """Test cannot cancel confirmed transaction."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'confirmed',
        })

        with self.assertRaises(UserError):
            transaction.action_cancel()

    def test_21_action_retry(self):
        """Test retry action creates new transaction."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'failed',
            'pos_order_id': self.order.id,
            'payment_method_id': self.payment_method.id,
        })

        with patch.object(self.Transaction, 'create') as mock_create:
            mock_new = Mock()
            mock_new.id = 999
            mock_new.external_id = 'NEXI-NEW-001'
            mock_create.return_value = mock_new

            result = transaction.action_retry()

            mock_create.assert_called_once()
            call_args = mock_create.call_args[1]
            self.assertEqual(call_args['terminal_id'], 'TEST-TERM-001')
            self.assertEqual(call_args['amount'], 50.00)

    def test_22_action_retry_wrong_state(self):
        """Test retry only works for failed transactions."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'confirmed',
        })

        with self.assertRaises(UserError):
            transaction.action_retry()

    @patch('odoo.addons.pos_nexi_payment.models.nexi_api_client.NexiApiClient.get_transaction')
    def test_23_action_poll_status(self, mock_get_transaction):
        """Test status polling (uses get_transaction, not get_transaction_status)."""
        mock_get_transaction.return_value = {
            'state': 'AWAITING_CONFIRM',
            'result_code': 'PENDING'
        }

        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        result = transaction.action_poll_status()

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'awaiting_confirm')

    def test_24_process_notification_awaiting_confirm(self):
        """Test notification processing - awaiting confirm."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        with patch.object(transaction, 'action_confirm') as mock_confirm:
            mock_confirm.return_value = True

            notification = {
                'state': 'AWAITING_CONFIRM',
                'result_code': 'PENDING',
                'result_description': 'Waiting for confirmation'
            }

            result = transaction.process_notification(notification)

            self.assertTrue(result)
            self.assertEqual(transaction.state, 'awaiting_confirm')
            self.assertTrue(transaction.notification_received)
            mock_confirm.assert_called_once()

    def test_25_process_notification_confirmed(self):
        """Test notification processing - confirmed."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'awaiting_confirm',
        })

        notification = {
            'state': 'CONFIRMED',
            'result_code': 'SUCCESS',
            'result_description': 'Payment successful'
        }

        result = transaction.process_notification(notification)

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'confirmed')
        self.assertEqual(transaction.result_code, 'SUCCESS')

    def test_26_process_notification_failed(self):
        """Test notification processing - failed (REJECTED, not DECLINED)."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        notification = {
            'state': 'FAILED',
            'result_code': 'REJECTED',
            'result_description': 'Card rejected'
        }

        result = transaction.process_notification(notification)

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'failed')
        self.assertEqual(transaction.result_code, 'REJECTED')

    def test_27_process_notification_aborted(self):
        """Test notification processing - aborted (was 'cancelled')."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        notification = {
            'state': 'ABORTED',
            'result_code': 'ABORTED',
            'result_description': 'Aborted by user'
        }

        result = transaction.process_notification(notification)

        self.assertTrue(result)
        self.assertEqual(transaction.state, 'cancelled')

    def test_28_handle_webhook_success(self):
        """Test webhook handler - success case."""
        transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
        })

        payload = {
            'external_id': transaction.external_id,
            'state': 'CONFIRMED',
            'result_code': 'SUCCESS'
        }

        result = self.Transaction.handle_webhook('TEST-TERM-001', payload)

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(transaction.state, 'confirmed')

    def test_29_handle_webhook_missing_external_id(self):
        """Test webhook handler - missing external_id."""
        payload = {'state': 'CONFIRMED'}

        result = self.Transaction.handle_webhook('TEST-TERM-001', payload)

        self.assertEqual(result['status'], 'error')
        self.assertIn('external_id', result['message'])

    def test_30_handle_webhook_transaction_not_found(self):
        """Test webhook handler - transaction not found."""
        payload = {
            'external_id': 'NONEXISTENT-TRANSACTION',
            'state': 'CONFIRMED'
        }

        result = self.Transaction.handle_webhook('TEST-TERM-001', payload)

        self.assertEqual(result['status'], 'error')
        self.assertIn('not found', result['message'])

    def test_31_cron_cleanup_old_transactions(self):
        """Test cleanup cron job."""
        old_transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'cancelled',
        })
        old_transaction.write({'write_date': fields.Datetime.now() - timedelta(days=100)})

        recent_transaction = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'cancelled',
        })

        self.Transaction._cron_cleanup_old_transactions(days=90)

        self.assertFalse(old_transaction.exists())
        self.assertTrue(recent_transaction.exists())

    @patch('odoo.addons.pos_nexi_payment.models.nexi_transaction.NexiTransaction.action_poll_status')
    def test_32_cron_poll_pending_transactions(self, mock_poll):
        """Test poll pending cron job."""
        mock_poll.return_value = True

        stalled = self.Transaction.create({
            'terminal_id': 'TEST-TERM-001',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'state': 'processing',
            'purchase_request_time': fields.Datetime.now() - timedelta(minutes=10),
        })

        self.Transaction._cron_poll_pending_transactions()

        mock_poll.assert_called_once()

    def test_33_multi_company_security(self):
        """Test multi-company security."""
        company2 = self.env['res.company'].create({
            'name': 'Second Test Company',
        })

        terminal2 = self.env['nexi.terminal'].create({
            'name': 'Terminal Company 2',
            'terminal_id': 'TERM-COMPANY-2',
            'api_key_id': 'test_key_id_company2',
            'api_key_encrypted': 'test_key_secret_company2',
            'company_id': company2.id,
            'state': 'active',
        })

        transaction = self.Transaction.create({
            'terminal_id': 'TERM-COMPANY-2',
            'amount': 50.00,
            'currency_id': self.currency_eur.id,
            'company_id': company2.id,
        })

        self.assertEqual(transaction.company_id, company2)
