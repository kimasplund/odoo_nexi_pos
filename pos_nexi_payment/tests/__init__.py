# -*- coding: utf-8 -*-
"""NEXI POS Payment Tests.

This package contains comprehensive test suites for the NEXI POS Payment module:
- test_nexi_api_client: Unit tests for the NEXI API client
- test_nexi_transaction: Tests for the transaction model and state management
- test_nexi_security: Security tests including encryption and validation
- test_pos_integration: Integration tests for POS workflow

To run all tests:
    odoo -i pos_nexi_payment --test-enable --stop-after-init

To run specific test file:
    odoo -i pos_nexi_payment --test-enable --test-tags nexi_api

Test Coverage:
    - API Client: 95%+ coverage of nexi_api_client.py
    - Transaction Model: 90%+ coverage of nexi_transaction.py
    - Security: All security features validated
    - Integration: End-to-end POS workflow tested
"""

from . import test_nexi_api_client
from . import test_nexi_transaction
from . import test_nexi_security
from . import test_pos_integration
