# -*- coding: utf-8 -*-
"""NEXI POS Payment Constants.

This module defines constants used throughout the NEXI payment integration,
following the pattern of official Odoo payment modules.
"""

# API Configuration
API_VERSION = 'v1'
API_BASE_URL = 'https://api.npay.eu/pos/v1'

# Request timeouts (in seconds)
REQUEST_TIMEOUT = 30
WEBHOOK_TIMEOUT = 5

# Security
WEBHOOK_AGE_TOLERANCE = 5 * 60  # 5 minutes (seconds)
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY = 0.5  # seconds

# Transaction states
TRANSACTION_STATES = [
    ('pending', 'Pending'),
    ('processing', 'Processing'),
    ('awaiting_confirm', 'Awaiting Confirmation'),
    ('confirmed', 'Confirmed'),
    ('failed', 'Failed'),
    ('cancelled', 'Cancelled'),
]

# Terminal states
TERMINAL_STATES = [
    ('inactive', 'Inactive'),
    ('active', 'Active'),
    ('error', 'Error'),
]

# NEXI Result codes (per API spec)
RESULT_SUCCESS = 'SUCCESS'
RESULT_ABORTED = 'ABORTED'
RESULT_REJECTED = 'REJECTED'
RESULT_NETWORK_ERROR = 'NETWORK_ERROR'
RESULT_NOT_ACCEPTED = 'NOT_ACCEPTED'

# Status mapping from NEXI result_code to Odoo state
STATUS_MAPPING = {
    'SUCCESS': 'confirmed',
    'ABORTED': 'cancelled',
    'REJECTED': 'failed',
    'NETWORK_ERROR': 'failed',
    'NOT_ACCEPTED': 'failed',
}

# Supported currencies
DEFAULT_CURRENCY = 'EUR'
SUPPORTED_CURRENCIES = ['EUR']

# Transaction type mapping
TRANSACTION_TYPES = [
    ('purchase', 'Purchase'),
    ('refund', 'Refund'),
    ('void', 'Void'),
]

# Amount limits (in EUR)
MIN_TRANSACTION_AMOUNT = 0.01
MAX_TRANSACTION_AMOUNT = 999999.99

# External ID constraints
EXTERNAL_ID_MAX_LENGTH = 63
EXTERNAL_ID_PATTERN = r'^[a-zA-Z0-9_-]+$'

# Advisory lock constants
LOCK_NAMESPACE = 123456789  # Unique namespace for NEXI locks

# Rate limiting
MAX_PAYMENT_ATTEMPTS_PER_MINUTE = 10
MAX_WEBHOOK_CALLS_PER_MINUTE = 100

# Cron intervals
CLEANUP_INTERVAL_DAYS = 90
POLLING_INTERVAL_MINUTES = 1
