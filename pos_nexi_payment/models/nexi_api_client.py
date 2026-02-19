# -*- coding: utf-8 -*-
"""NEXI API Client for Odoo v18.

This module provides a Python client for interacting with the NEXI POS API.
It handles authentication, request formatting, retry logic, and error handling.

API Reference: https://developer.npay.eu/nexi-pos-api/api/

Authentication: HTTP Basic Auth with key_id:key_secret
All endpoints use POST method.
Amounts are integers in minor currency units (e.g., cents).
"""

import base64
import json
import logging
import re
import time
import uuid
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

_logger = logging.getLogger(__name__)

# API Constants
DEFAULT_BASE_URL = "https://api.npay.eu/pos/v1/"
DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRY_ATTEMPTS = 3
BACKOFF_FACTOR = 0.5

# Validation Constants
MIN_AMOUNT = 1  # Minimum amount in minor units (1 cent)
MAX_AMOUNT = 99999999  # Maximum amount in minor units
EXTERNAL_ID_MAX_LENGTH = 63
TERMINAL_ID_MAX_LENGTH = 32
CURRENCY_CODE_LENGTH = 3

# Valid NEXI result codes for transaction/confirm
VALID_RESULT_CODES = {'SUCCESS', 'ABORTED', 'REJECTED', 'NETWORK_ERROR', 'NOT_ACCEPTED'}

# HTTP Status codes that trigger retry
RETRY_STATUS_CODES = [429, 500, 502, 503, 504]


class NexiApiError(Exception):
    """Base exception for NEXI API errors."""

    def __init__(self, message: str, error_code: Optional[str] = None,
                 response_data: Optional[dict] = None):
        super().__init__(message)
        self.error_code = error_code
        self.response_data = response_data or {}


class NexiNetworkError(NexiApiError):
    """Exception for network-related errors (timeouts, connection issues)."""
    pass


class NexiValidationError(NexiApiError):
    """Exception for validation errors (invalid parameters, bad requests)."""
    pass


class NexiAuthenticationError(NexiApiError):
    """Exception for authentication/authorization errors."""
    pass


class NexiApiClient:
    """Client for NEXI POS API integration.

    This class handles all communication with the NEXI payment terminal API,
    including authentication, request formatting, retry logic, and error handling.

    Authentication uses HTTP Basic Auth with key_id and key_secret.
    All endpoints use POST. Amounts are integers in minor currency units.

    Usage:
        client = NexiApiClient(key_id='your_key_id', key_secret='your_key_secret')
        result = client.purchase(
            requested_amount=9999, external_id='TXN001',
            terminal_id='TERM001', currency='EUR'
        )

    Attributes:
        base_url: The base URL for the NEXI API
        timeout: Request timeout in seconds
        session: Configured requests.Session with retry logic
    """

    def __init__(self, key_id: str, key_secret: str,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: int = DEFAULT_TIMEOUT):
        """Initialize the NEXI API client.

        Args:
            key_id: The API key ID for Basic Auth (required)
            key_secret: The API key secret for Basic Auth (required)
            base_url: The base URL for the API (defaults to production)
            timeout: Request timeout in seconds

        Raises:
            NexiValidationError: If credentials are empty or invalid
        """
        if not key_id or not isinstance(key_id, str):
            raise NexiValidationError("key_id is required and must be a string")
        if not key_secret or not isinstance(key_secret, str):
            raise NexiValidationError("key_secret is required and must be a string")

        self._key_id = key_id
        self._key_secret = key_secret
        self._auth_header = self._build_basic_auth(key_id, key_secret)
        self.base_url = base_url.rstrip('/') + '/'
        self.timeout = timeout
        self.session = self._configure_session()

        _logger.info("NEXI API client initialized (key_id: %s, base_url: %s)",
                     self._mask_value(key_id), self.base_url)

    @staticmethod
    def _build_basic_auth(key_id: str, key_secret: str) -> str:
        """Build HTTP Basic Auth header value.

        Args:
            key_id: The API key ID
            key_secret: The API key secret

        Returns:
            'Basic <base64(key_id:key_secret)>' header value
        """
        credentials = f"{key_id}:{key_secret}"
        encoded = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
        return f"Basic {encoded}"

    @staticmethod
    def _mask_value(value: str) -> str:
        """Mask a sensitive value for safe logging.

        Args:
            value: The value to mask

        Returns:
            Masked version showing only first 4 and last 4 characters
        """
        if not value or len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"

    def _configure_session(self) -> requests.Session:
        """Configure requests session with retry logic.

        Returns:
            Configured requests.Session instance
        """
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=MAX_RETRY_ATTEMPTS,
            backoff_factor=BACKOFF_FACTOR,
            status_forcelist=RETRY_STATUS_CODES,
            allowed_methods=["POST"],
            raise_on_status=False
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=10
        )

        session.mount("https://", adapter)
        session.mount("http://", adapter)

        # Set default headers with Basic Auth
        session.headers.update({
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OdooPOS/18.0 (nexi-payment-integration)"
        })

        return session

    def _validate_external_id(self, external_id: str) -> None:
        """Validate external transaction ID.

        Args:
            external_id: The external ID to validate

        Raises:
            NexiValidationError: If validation fails
        """
        if not external_id:
            raise NexiValidationError("external_id is required")

        if len(external_id) > EXTERNAL_ID_MAX_LENGTH:
            raise NexiValidationError(
                f"external_id exceeds maximum length of {EXTERNAL_ID_MAX_LENGTH}"
            )

        # Allow alphanumeric, hyphens, underscores
        if not re.match(r'^[a-zA-Z0-9_-]+$', external_id):
            raise NexiValidationError(
                "external_id must contain only alphanumeric characters, hyphens, and underscores"
            )

    def _validate_terminal_id(self, terminal_id: str) -> None:
        """Validate terminal ID.

        Args:
            terminal_id: The terminal ID to validate

        Raises:
            NexiValidationError: If validation fails
        """
        if not terminal_id:
            raise NexiValidationError("terminal_id is required")

        if len(terminal_id) > TERMINAL_ID_MAX_LENGTH:
            raise NexiValidationError(
                f"terminal_id exceeds maximum length of {TERMINAL_ID_MAX_LENGTH}"
            )

    def _validate_amount(self, amount: int) -> None:
        """Validate transaction amount in minor currency units.

        Args:
            amount: The amount in minor units (e.g., cents)

        Raises:
            NexiValidationError: If validation fails
        """
        if amount is None:
            raise NexiValidationError("amount is required")

        if not isinstance(amount, int):
            raise NexiValidationError("amount must be an integer (minor currency units)")

        if amount <= 0:
            raise NexiValidationError("amount must be positive")

        if amount < MIN_AMOUNT:
            raise NexiValidationError(f"amount must be at least {MIN_AMOUNT}")

        if amount > MAX_AMOUNT:
            raise NexiValidationError(f"amount exceeds maximum of {MAX_AMOUNT}")

    def _validate_currency(self, currency: str) -> None:
        """Validate currency code.

        Args:
            currency: The currency code to validate

        Raises:
            NexiValidationError: If validation fails
        """
        if not currency:
            raise NexiValidationError("currency is required")

        if len(currency) != CURRENCY_CODE_LENGTH:
            raise NexiValidationError("currency must be a 3-letter ISO code")

        if not currency.isalpha():
            raise NexiValidationError("currency must contain only letters")

    def _request(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Make an HTTP POST request to the NEXI API.

        All NEXI API endpoints use POST method.

        Args:
            endpoint: API endpoint path (e.g., 'transaction/purchase')
            payload: Request payload as dictionary

        Returns:
            Parsed JSON response as dictionary (raw, not unwrapped)

        Raises:
            NexiNetworkError: For network/timeout errors
            NexiAuthenticationError: For 401/403 errors
            NexiValidationError: For 400/422 validation errors
            NexiApiError: For other API errors
        """
        url = urljoin(self.base_url, endpoint.lstrip('/'))

        # Validate payload can be serialized
        try:
            json_payload = json.dumps(payload, separators=(',', ':'))
        except (TypeError, ValueError) as e:
            raise NexiValidationError(f"Invalid JSON payload: {str(e)}")

        # Log request (with sensitive data masked)
        safe_payload = payload.copy()
        _logger.debug("NEXI API request: POST %s - Payload: %s", url, safe_payload)

        start_time = time.time()

        try:
            response = self.session.post(
                url,
                data=json_payload,
                timeout=self.timeout,
                verify=True
            )

            elapsed_ms = (time.time() - start_time) * 1000
            _logger.debug("NEXI API response: %s in %.2fms",
                         response.status_code, elapsed_ms)

        except requests.exceptions.SSLError as e:
            _logger.error("NEXI API SSL error: %s", str(e))
            raise NexiNetworkError(f"SSL/TLS error: {str(e)}")

        except requests.exceptions.Timeout as e:
            _logger.error("NEXI API timeout after %ss", self.timeout)
            raise NexiNetworkError(f"Request timeout after {self.timeout} seconds")

        except requests.exceptions.ConnectionError as e:
            _logger.error("NEXI API connection error: %s", str(e))
            raise NexiNetworkError(f"Connection error: {str(e)}")

        except requests.exceptions.RequestException as e:
            _logger.error("NEXI API request error: %s", str(e))
            raise NexiNetworkError(f"Request failed: {str(e)}")

        # Parse response
        try:
            response_data = response.json()
        except json.JSONDecodeError:
            response_data = {"raw_response": response.text}

        # Handle error status codes
        if not response.ok:
            error_obj = response_data.get('error', {})
            if isinstance(error_obj, dict):
                error_message = error_obj.get('description', 'Unknown error')
                error_code = error_obj.get('code', f"HTTP_{response.status_code}")
            else:
                error_message = str(error_obj)
                error_code = f"HTTP_{response.status_code}"

            _logger.error("NEXI API error: %s (code: %s, status: %s)",
                         error_message, error_code, response.status_code)

            if response.status_code in (401, 403):
                raise NexiAuthenticationError(
                    f"Authentication failed: {error_message}",
                    error_code=error_code,
                    response_data=response_data
                )
            elif response.status_code in (400, 422):
                raise NexiValidationError(
                    f"Validation error: {error_message}",
                    error_code=error_code,
                    response_data=response_data
                )
            else:
                raise NexiApiError(
                    f"API error ({response.status_code}): {error_message}",
                    error_code=error_code,
                    response_data=response_data
                )

        _logger.debug("NEXI API success response: %s", response_data)
        return response_data

    def purchase(self, requested_amount: int, external_id: str, terminal_id: str,
                 currency: str = 'EUR', wait_seconds: int = 25,
                 **options) -> dict[str, Any]:
        """Initiate a purchase transaction.

        Calls POST /transaction/purchase to start a payment transaction
        on the NEXI terminal.

        Args:
            requested_amount: Amount in minor currency units (e.g., 9999 = 99.99 EUR)
            external_id: Unique transaction identifier (max 63 chars)
            terminal_id: Terminal identifier (max 32 chars)
            currency: 3-letter currency code (default: EUR)
            wait_seconds: Long-polling wait time 0-180 (default: 25)
            **options: Additional optional parameters:
                - order_id: Reference to merchant order
                - description: Transaction description

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_external_id(external_id)
        self._validate_terminal_id(terminal_id)
        self._validate_amount(requested_amount)
        self._validate_currency(currency)

        # Build payload per API spec
        payload = {
            "external_id": external_id,
            "terminal_id": terminal_id,
            "requested_amount": requested_amount,
            "currency": currency.upper(),
            "options": {
                "wait_seconds": max(0, min(180, wait_seconds))
            }
        }

        # Add optional parameters
        valid_options = {'order_id', 'description'}
        for key, value in options.items():
            if key in valid_options and value is not None:
                if isinstance(value, str):
                    value = value.strip()[:255]
                payload[key] = value

        _logger.info("NEXI purchase: external_id=%s, amount=%s %s, terminal=%s",
                    external_id, requested_amount, currency, terminal_id)

        response = self._request("transaction/purchase", payload)

        # Unwrap transaction from response envelope
        return response.get('transaction', response)

    def confirm(self, external_id: str, terminal_id: str,
                result_code: str = 'SUCCESS') -> dict[str, Any]:
        """Confirm a transaction.

        Calls POST /transaction/confirm to finalize a transaction
        after reaching AWAITING_CONFIRM state.

        Args:
            external_id: The transaction's external identifier
            terminal_id: The terminal ID (required by API)
            result_code: Result code - SUCCESS, ABORTED, REJECTED, etc.

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_external_id(external_id)
        self._validate_terminal_id(terminal_id)

        if not result_code:
            raise NexiValidationError("result_code is required")

        result_code = result_code.upper()
        if result_code not in VALID_RESULT_CODES:
            raise NexiValidationError(
                f"Invalid result_code '{result_code}'. "
                f"Must be one of: {', '.join(sorted(VALID_RESULT_CODES))}"
            )

        payload = {
            "external_id": external_id,
            "terminal_id": terminal_id,
            "result_code": result_code
        }

        _logger.info("NEXI confirm: external_id=%s, result_code=%s",
                    external_id, result_code)

        response = self._request("transaction/confirm", payload)
        return response.get('transaction', response)

    def abort(self, external_id: str, terminal_id: str) -> dict[str, Any]:
        """Abort a pending transaction.

        Uses POST /transaction/confirm with result_code=ABORTED.
        The NEXI API has no separate cancel endpoint.

        Args:
            external_id: The transaction's external identifier
            terminal_id: The terminal ID

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        _logger.info("NEXI abort: external_id=%s", external_id)
        return self.confirm(external_id, terminal_id, result_code='ABORTED')

    def continue_transaction(self, external_id: str, terminal_id: str,
                           requested_amount: Optional[int] = None,
                           **options) -> dict[str, Any]:
        """Continue an interrupted transaction.

        Calls POST /transaction/continue to resume a transaction
        that was previously interrupted.

        Args:
            external_id: The transaction's external identifier
            terminal_id: The terminal ID
            requested_amount: Optional new amount in minor units
            **options: Additional optional parameters

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_external_id(external_id)
        self._validate_terminal_id(terminal_id)

        payload = {
            "external_id": external_id,
            "terminal_id": terminal_id
        }

        if requested_amount is not None:
            self._validate_amount(requested_amount)
            payload["requested_amount"] = requested_amount

        # Add optional parameters
        valid_options = {'currency', 'order_id', 'description'}
        for key, value in options.items():
            if key in valid_options and value is not None:
                if key == 'currency':
                    self._validate_currency(value)
                    value = value.upper()
                payload[key] = value

        _logger.info("NEXI continue: external_id=%s", external_id)

        response = self._request("transaction/continue", payload)
        return response.get('transaction', response)

    def get_transaction(self, external_id: str) -> dict[str, Any]:
        """Get transaction details.

        Calls POST /transaction/get to retrieve current state
        and details of a transaction.

        Args:
            external_id: The transaction's external identifier

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_external_id(external_id)

        payload = {
            "external_id": external_id
        }

        _logger.debug("NEXI get_transaction: external_id=%s", external_id)

        response = self._request("transaction/get", payload)
        return response.get('transaction', response)

    def get_terminal_status(self, terminal_id: str) -> dict[str, Any]:
        """Get terminal status.

        Calls POST /terminal/status to check terminal connectivity.

        Args:
            terminal_id: The terminal identifier

        Returns:
            Status data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_terminal_id(terminal_id)

        payload = {
            "terminal_id": terminal_id
        }

        _logger.debug("NEXI terminal_status: terminal_id=%s", terminal_id)

        response = self._request("terminal/status", payload)
        return response.get('status', response)

    def print_receipt(self, terminal_id: str, contents_base64: str,
                      is_continuation: bool = False) -> dict[str, Any]:
        """Print content on the terminal's receipt printer.

        Calls POST /terminal/print to queue a print job.
        Content must be a base64-encoded PNG image.

        Args:
            terminal_id: The terminal identifier
            contents_base64: Base64-encoded PNG image data (max 262143 chars)
            is_continuation: True if more fragments follow (multi-page)

        Returns:
            Response dict with 'queued' boolean
        """
        self._validate_terminal_id(terminal_id)

        if not contents_base64 or len(contents_base64) > 262143:
            raise NexiValidationError("contents must be 1-262143 characters")

        payload = {
            "terminal_id": terminal_id,
            "contents": contents_base64,
            "format": "PNG",
        }
        if is_continuation:
            payload["continue"] = True

        return self._request("terminal/print", payload)

    def refund(self, requested_amount: int, external_id: str, terminal_id: str,
               currency: str = 'EUR', original_purchase_external_id: str = None,
               original_purchase_terminal_id: str = None,
               customer_not_present: bool = False,
               wait_seconds: int = 25, **options) -> dict[str, Any]:
        """Initiate a refund transaction.

        Calls POST /transaction/refund to start a refund on the terminal.
        The customer must present the same card used for the original purchase
        (unless customer_not_present=True).

        Args:
            requested_amount: Amount in minor currency units (e.g., 9999 = 99.99 EUR)
            external_id: NEW unique transaction identifier for the refund (max 63 chars)
            terminal_id: Terminal identifier (max 32 chars)
            currency: 3-letter currency code (default: EUR)
            original_purchase_external_id: External ID of the original purchase (optional)
            original_purchase_terminal_id: Terminal ID of the original purchase (optional)
            customer_not_present: If True, skip card presentation (default: False)
            wait_seconds: Long-polling wait time 0-180 (default: 25)
            **options: Additional optional parameters

        Returns:
            Transaction data dict (unwrapped from API response)

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_external_id(external_id)
        self._validate_terminal_id(terminal_id)
        self._validate_amount(requested_amount)
        self._validate_currency(currency)

        payload = {
            "external_id": external_id,
            "terminal_id": terminal_id,
            "requested_amount": requested_amount,
            "currency": currency.upper(),
            "options": {
                "wait_seconds": max(0, min(180, wait_seconds))
            }
        }

        if original_purchase_external_id:
            payload["original_purchase_external_id"] = original_purchase_external_id
        if original_purchase_terminal_id:
            payload["original_purchase_terminal_id"] = original_purchase_terminal_id
        if customer_not_present:
            payload["customer_not_present"] = True

        # Add optional parameters
        valid_options = {'order_id', 'description'}
        for key, value in options.items():
            if key in valid_options and value is not None:
                if isinstance(value, str):
                    value = value.strip()[:255]
                payload[key] = value

        _logger.info("NEXI refund: external_id=%s, amount=%s %s, terminal=%s",
                    external_id, requested_amount, currency, terminal_id)

        response = self._request("transaction/refund", payload)

        # Unwrap transaction from response envelope
        return response.get('transaction', response)

    def get_unconfirmed(self, terminal_id: str) -> dict[str, Any]:
        """Get unconfirmed transactions for a terminal.

        Calls POST /transaction/unconfirmed.
        Meant for recovery of lost transaction identifiers.

        Args:
            terminal_id: The terminal identifier

        Returns:
            Response dict with 'transactions' list

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_terminal_id(terminal_id)

        payload = {"terminal_id": terminal_id}

        _logger.info("NEXI get_unconfirmed: terminal_id=%s", terminal_id)

        return self._request("transaction/unconfirmed", payload)

    def get_events(self, terminal_id: str,
                   wait_seconds: int = 25) -> dict[str, Any]:
        """Poll for terminal events.

        Calls POST /event/list to retrieve pending events.
        This is how the API communicates state changes (no webhooks).

        Args:
            terminal_id: The terminal identifier
            wait_seconds: Long-polling wait time 0-180 (default: 25)

        Returns:
            Events data dict from API response

        Raises:
            NexiValidationError: If parameters are invalid
            NexiApiError: If API call fails
        """
        self._validate_terminal_id(terminal_id)

        payload = {
            "terminal_id": terminal_id,
            "options": {
                "wait_seconds": max(0, min(180, wait_seconds))
            }
        }

        _logger.debug("NEXI get_events: terminal_id=%s", terminal_id)

        return self._request("event/list", payload)

    @staticmethod
    def generate_external_id(prefix: str = "NEXI") -> str:
        """Generate a unique external transaction ID.

        Uses UUID4 to ensure global uniqueness. Max 63 characters.

        Args:
            prefix: Optional prefix for the ID (default: NEXI)

        Returns:
            Unique external ID string (max 63 chars)
        """
        uuid_str = str(uuid.uuid4())
        external_id = f"{prefix}-{uuid_str}"

        if len(external_id) > EXTERNAL_ID_MAX_LENGTH:
            external_id = external_id[:EXTERNAL_ID_MAX_LENGTH]

        return external_id

    def close(self) -> None:
        """Close the HTTP session and release resources."""
        if self.session:
            self.session.close()
            _logger.debug("NEXI API session closed")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures session is closed."""
        self.close()
        return False
