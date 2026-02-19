# POS NEXI Payment -- Full Integration

**Technical Name:** `pos_nexi_payment`
**Version:** 18.0.1.0.0
**Category:** Point of Sale
**License:** LGPL-3
**Author:** Kim Asplund
**Website:** https://asplund.kim

---

## Overview

`pos_nexi_payment` is a comprehensive Odoo 18 module that integrates NEXI payment terminals with the Odoo Point of Sale. It provides full terminal management, real-time transaction tracking, refund processing, unconfirmed transaction recovery, audit logging, and secure API key storage.

This module is designed for businesses that need:

- Complete visibility into every payment terminal transaction
- Backend management of terminals, transactions, and audit trails
- Refund processing with multi-layer authorization
- Automated recovery of interrupted or unconfirmed transactions
- Role-based access control over payment operations
- Encrypted API credential storage

The NEXI POS API communicates via HTTPS POST requests with HTTP Basic Auth. Amounts are expressed as integers in minor currency units (cents). The module handles all conversion between Odoo's decimal amounts and the API's integer format.

---

## Full vs Light Module

This repository contains two NEXI integration modules. They are **mutually exclusive** -- never install both on the same database.

| Feature | `pos_nexi_payment` (Full) | `pos_nexi_light` (Light) |
|---|---|---|
| Custom models | `nexi.terminal`, `nexi.transaction`, `nexi.audit.log` | None (extends `pos.payment.method` only) |
| Backend views | Terminal list/form, Transaction list/form, Audit log | Minimal |
| Transaction tracking | Full lifecycle with state machine | Basic status only |
| Refunds | Backend + POS, multi-layer authorization | Not included |
| Unconfirmed recovery | Automated cron + manual button | Not included |
| Audit logging | Comprehensive (`nexi.audit.log` model) | Standard Odoo logging only |
| API key storage | Fernet encryption at rest | Plain configuration |
| Security groups | 3 tiers (User, Manager, Admin) | None |
| Record rules | Multi-company + user/manager visibility | None |
| Cron jobs | 3 scheduled actions | None |
| Dependencies | `base`, `mail`, `point_of_sale`, `account` | `base`, `point_of_sale` |
| Python dependencies | `cryptography` (recommended) | None |

**Choose the Full module** when you need backend transaction monitoring, refund workflows, audit compliance, or manage multiple terminals across locations.

**Choose the Light module** for a simple single-terminal setup where you only need basic card payment acceptance in the POS.

---

## Features

### Terminal Management
- Register and configure multiple NEXI payment terminals
- Per-terminal API credentials (key ID + encrypted secret)
- Activate, deactivate, and test terminal connections
- Heartbeat tracking with last-seen timestamps
- Transaction count and success rate statistics per terminal
- Associate terminals with specific POS configurations
- Configurable min/max transaction amounts per terminal

### Transaction Lifecycle
- Full state machine: `pending` -> `processing` -> `awaiting_confirm` -> `confirmed`
- Also: `failed`, `cancelled`
- Transaction types: `purchase` and `refund`
- PostgreSQL advisory locks prevent race conditions on concurrent processing
- Automatic card detail extraction from API responses (card scheme, masked PAN, authorization code, entry mode, acquirer references, receipt text)
- Chatter integration via `mail.thread` for state change tracking

### Refunds
- Initiate refunds from the backend transaction form (Refund button on confirmed purchases)
- Initiate refunds from the POS frontend via controller endpoint
- Links refund transactions to their original purchase via `original_transaction_id`
- Multi-layer authorization: NEXI Manager group required, amounts above configurable threshold require explicit manager role
- All refund attempts are audit logged

### Unconfirmed Transaction Recovery
- Manual recovery button on the terminal form (queries NEXI API for stuck transactions)
- Automated cron job runs every 15 minutes across all active terminals
- Reconciles API-side unconfirmed transactions with local records
- Auto-confirms transactions with `result_code == SUCCESS`
- Creates new local records for transactions not found in the database

### Audit Logging
- Dedicated `nexi.audit.log` model records all sensitive operations
- Logged actions include: payment initiation, confirmation, cancellation, refund, API key changes, permission denials, rate limit violations, webhook processing
- Automatic redaction of sensitive data (API keys, card numbers, PINs) in log details
- Client IP address capture
- Configurable retention with automatic cleanup (default 90 days)

### Receipt Printing
- Card details section injected into the POS receipt template via `t-inherit-mode="extension"`
- Displays: card scheme, masked PAN, authorization code, entry mode, reference
- Terminal-side printing supported via `POST /terminal/print` API endpoint
- Customer and merchant receipt text stored from API response

### Cron Jobs
- **Recover Unconfirmed Transactions** -- every 15 minutes
- **Poll Pending Transactions** -- every 5 minutes (recovers stalled transactions)
- **Cleanup Old Transactions** -- daily (removes failed/cancelled transactions older than 90 days)

### POS Frontend
- OWL-based payment dialog with terminal selection
- Full-screen waiting overlay during terminal processing with elapsed timer
- Success/error result screens with transaction details
- Terminal status indicator in POS chrome (online/offline/busy/partial)
- Auto-refresh terminal status every 30 seconds
- Reactive state management via `nexiStore` singleton
- Payment line extension showing card type, reference, and authorization code
- Responsive design for tablet and mobile POS devices
- Accessibility: high contrast and reduced motion support

---

## Prerequisites

- **Odoo 18.0** (Community or Enterprise)
- **Python 3.10+**
- **PostgreSQL 14+** (advisory locks used for transaction safety)
- Odoo modules: `base`, `mail`, `point_of_sale`, `account`
- **NEXI merchant account** with API credentials (key ID and key secret)
- Python package: `requests` (included with Odoo)
- Python package: `cryptography` (recommended for API key encryption at rest)

---

## Installation

### Docker (Recommended)

If you are running Odoo in Docker, add the module to your addons path and install the `cryptography` package:

```dockerfile
# In your Dockerfile
RUN pip3 install cryptography

# Copy the module
COPY ./Nexi_pos/pos_nexi_payment /mnt/extra-addons/pos_nexi_payment
```

Then update the addons path in your Odoo configuration:

```ini
[options]
addons_path = /mnt/extra-addons,/opt/odoo/addons
```

Restart Odoo and install the module from Apps (remove the "Apps" filter, search for "NEXI POS Payment").

### Manual Installation

1. Copy the `pos_nexi_payment` directory into your Odoo addons path.

2. Install the `cryptography` Python package:

   ```bash
   pip3 install cryptography
   ```

3. Update the Odoo addons list:

   ```bash
   odoo -u base --stop-after-init
   ```

4. Install the module from **Settings > Apps** (search "NEXI POS Payment") or via CLI:

   ```bash
   odoo -i pos_nexi_payment --stop-after-init
   ```

> **Note:** The module will function without `cryptography` installed, but API key secrets will be stored in plaintext. A warning will appear in the Odoo log. For production deployments, always install `cryptography`.

---

## Configuration

### 1. System Parameters

Navigate to **Settings > Technical > Parameters > System Parameters** and set:

| Key | Default | Description |
|-----|---------|-------------|
| `nexi_payment.base_url` | `https://api.npay.eu/pos/v1/` | NEXI API base URL. Change for sandbox/staging environments. |
| `nexi_payment.refund_threshold` | `100.0` | Refund amounts above this value require NEXI Manager role. |

### 2. Encryption Key

For production, set the encryption key in your `odoo.conf`:

```ini
[options]
nexi_encryption_key = your-32-character-encryption-key
```

If not set, the module falls back to deriving a key from the Odoo `admin_passwd`. If neither is available, API key secrets are stored in plaintext (not recommended for production).

### 3. Security Groups

The module creates three security groups under the **NEXI Payment** category:

| Group | XML ID | Inherits | Permissions |
|-------|--------|----------|-------------|
| **NEXI User** | `pos_nexi_payment.group_nexi_user` | POS User | Process payments, view own transactions, read terminal info |
| **NEXI Manager** | `pos_nexi_payment.group_nexi_manager` | NEXI User + POS Manager | Configure terminals, process refunds, view all transactions, recover unconfirmed |
| **NEXI Administrator** | `pos_nexi_payment.group_nexi_admin` | NEXI Manager | Manage API keys, delete terminals/transactions, full audit log access |

Assign groups via **Settings > Users & Companies > Users > [user] > NEXI Payment** section.

The system administrator (`admin`) and root user are automatically added to the NEXI Administrator group on install.

### 4. Terminal Configuration

1. Navigate to **Point of Sale > Configuration > NEXI > Terminals**.
2. Click **Create** and fill in:
   - **Terminal Name**: A human-readable label (e.g., "Reception Terminal 1").
   - **Terminal ID**: The identifier provided by NEXI during onboarding (max 32 characters, alphanumeric + hyphens/underscores).
   - **API Key ID**: The key ID for HTTP Basic Auth (visible only to NEXI Administrator group).
   - **API Key Secret**: The key secret (encrypted at rest, visible only to NEXI Administrator group).
   - **Company**: The company this terminal belongs to.
   - **POS Configurations**: Select which POS configs can use this terminal.
   - **Min/Max Amount**: Optional per-terminal transaction limits.
3. Click **Test Connection** to verify the API credentials against the NEXI API (`POST /terminal/status`).
4. Click **Activate** to make the terminal available for payments.

### 5. Payment Method Linking

1. Navigate to **Point of Sale > Configuration > Payment Methods**.
2. Create or edit a payment method.
3. Enable the **NEXI Payment Terminal** checkbox (`is_nexi_payment`).
4. Select the **NEXI Terminal** from the dropdown (only active terminals are shown).
5. Save.
6. Add this payment method to your POS configuration(s).

> **Constraint:** A payment method cannot have both `is_nexi_payment` enabled and another `use_payment_terminal` value set. Choose one integration per method.

### 6. Cron Jobs

The module installs three scheduled actions (under **Settings > Technical > Automation > Scheduled Actions**):

| Name | Interval | Description |
|------|----------|-------------|
| NEXI: Recover Unconfirmed Transactions | 15 minutes | Calls NEXI API `POST /transaction/unconfirmed` for each active terminal. Reconciles with local records and auto-confirms successful ones. |
| NEXI: Poll Pending Transactions | 5 minutes | Polls `POST /transaction/get` for transactions stuck in `processing` or `awaiting_confirm` for more than 5 minutes (up to 5 error retries). |
| NEXI: Cleanup Old Transactions | 1 day | Deletes `failed` and `cancelled` transaction records older than 90 days. |

All cron jobs are active by default. Disable them individually if not needed.

---

## Usage

### Terminal Management

From **Point of Sale > Configuration > NEXI > Terminals**:

- **Test Connection**: Calls `POST /terminal/status` to verify API credentials and terminal reachability. Updates state to `active` on success or `error` on failure.
- **Activate / Deactivate**: Toggle the terminal's operational state. Only `active` terminals can process payments.
- **Recover Unconfirmed**: Queries the NEXI API for transactions stuck in `AWAITING_CONFIRM` state. Creates or updates local records accordingly and auto-confirms successful ones.
- **View Transactions**: Opens a filtered list of all transactions for this terminal.

### POS Payment Workflow

1. In the POS, ring up items and go to the Payment screen.
2. Select the NEXI payment method.
3. A dialog appears showing the amount and available terminals. Select a terminal and click **Start Payment**.
4. A full-screen waiting overlay appears. The customer completes the card operation on the physical terminal.
5. The POS polls the server every 2 seconds for up to 3 minutes.
6. On success, a result screen shows transaction details (card scheme, masked PAN, authorization code, entry mode, reference). Click **Continue** to finalize or **Print Receipt** to print.
7. On failure, a troubleshooting screen appears with a **Retry** button.
8. The cashier can **Cancel Payment** at any time during the waiting phase.

### Transaction Monitoring

From **Point of Sale > Configuration > NEXI > Transactions**:

- View all transactions with color-coded state badges (green = confirmed, red = failed, yellow = pending, blue = processing).
- Filter by state, terminal, transaction type (purchase/refund), card scheme, or date.
- Group by state, terminal, card scheme, or date.
- Open a transaction form to see full details including:
  - Card details tab: scheme, masked PANs, entry mode, cardholder verification, EMV application info
  - Authorization tab: authorization code, response code, acquirer reference, acquirer terminal ID, authorized/captured amounts
  - Receipt text tab: customer and merchant receipt text from the terminal
  - Technical tab (Manager+): purchase/confirm request times, error count, last error

### Refunds

**From the backend (Transaction form):**

1. Open a confirmed, successful purchase transaction.
2. Click the **Refund** button (visible only to NEXI Manager+ when `state == confirmed`, `result_code == SUCCESS`, `transaction_type == purchase`).
3. A new refund transaction is created, linked to the original via `original_transaction_id`, and the refund is initiated on the terminal.
4. The refund follows the same state machine as a purchase.

**From the POS frontend:**

1. Call the `/pos_nexi/payment/refund` endpoint with `payment_method_id`, `amount`, and optionally `original_external_id`.
2. Requires NEXI Manager group membership.
3. The refund transaction is created and initiated on the terminal.

**From a POS payment line:**

1. On a pos.payment record linked to a NEXI transaction, the **Void** action (`action_void_nexi_payment`) processes a full reversal.
2. Multi-layer authorization is enforced: basic refund permission check, threshold check, and audit logging.

### Recovery of Unconfirmed Transactions

If a transaction is interrupted (network failure, POS crash, terminal timeout), it may remain in `AWAITING_CONFIRM` state on the NEXI side without a corresponding confirmed record in Odoo.

**Automatic recovery:**
The `NEXI: Recover Unconfirmed Transactions` cron job runs every 15 minutes, querying each active terminal via `POST /transaction/unconfirmed`.

**Manual recovery:**
On any terminal form, click the **Recover Unconfirmed** button. The module will:
1. Query the NEXI API for unconfirmed transactions on that terminal.
2. For each returned transaction:
   - If a matching record exists locally: update its state and card details.
   - If no local record exists: create a new `nexi.transaction` record.
3. Auto-confirm transactions where `result_code == SUCCESS`.
4. Display a notification with counts of recovered and updated transactions.

### Receipts

The module extends the standard POS receipt template (`point_of_sale.OrderReceipt`) via `t-inherit-mode="extension"` to inject a NEXI card details section after the payment lines. This section displays:

- Card type (scheme)
- Masked PAN
- Authorization code
- Card entry mode
- NEXI reference

Receipt text from the terminal (customer and merchant copies) is stored on the transaction record and available for printing.

---

## Models Reference

### `nexi.terminal`

**Description:** NEXI Payment Terminal configuration.
**Inherits:** `nexi.security.mixin`

| Field | Type | Required | Groups | Description |
|-------|------|----------|--------|-------------|
| `name` | Char | Yes | | Human-readable terminal name |
| `terminal_id` | Char | Yes | | Unique NEXI terminal identifier (max 32 chars, alphanumeric + `-_`) |
| `api_key_id` | Char | Yes | `group_nexi_admin` | API key ID for HTTP Basic Auth |
| `api_key_encrypted` | Char | Yes | `group_nexi_admin` | Encrypted API key secret (Fernet) |
| `min_amount` | Monetary | | | Minimum transaction amount (default 0.01) |
| `max_amount` | Monetary | | | Maximum transaction amount (default 999999.99) |
| `currency_id` | Many2one(`res.currency`) | | | Default currency |
| `company_id` | Many2one(`res.company`) | Yes | | Owning company |
| `pos_config_ids` | Many2many(`pos.config`) | | | Allowed POS configurations |
| `state` | Selection | Yes | | `inactive` / `active` / `error` |
| `last_heartbeat` | Datetime | | | Last successful API communication |
| `transaction_count` | Integer | | | Computed: total transactions |
| `success_rate` | Float | | | Computed: percentage of confirmed transactions |
| `active` | Boolean | | | Archive flag (default True) |
| `notes` | Text | | | Free-form notes |

**SQL Constraints:**
- `terminal_id_unique`: Terminal ID must be unique.

**Key Methods:**

| Method | Description |
|--------|-------------|
| `action_activate()` | Set state to `active`, update heartbeat |
| `action_deactivate()` | Set state to `inactive` |
| `action_test_connection()` | Call `POST /terminal/status`, update state based on result |
| `action_update_heartbeat()` | Update `last_heartbeat` to now |
| `action_recover_unconfirmed()` | Query NEXI API for unconfirmed transactions, reconcile locally |
| `action_view_transactions()` | Open filtered transaction list |
| `action_migrate_api_keys()` | Migrate plaintext API keys to encrypted storage |
| `get_api_client()` | Return a configured `NexiApiClient` instance |
| `get_available_terminals(pos_config_id=None)` | Return active terminals, optionally filtered by POS config |
| `cron_migrate_api_keys()` | Scheduled action entry point for key migration |

### `nexi.transaction`

**Description:** NEXI Payment Transaction.
**Inherits:** `mail.thread`, `mail.activity.mixin`, `nexi.security.mixin`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `transaction_type` | Selection | Yes | `purchase` / `refund` |
| `original_transaction_id` | Many2one(`nexi.transaction`) | | Original purchase (for refunds) |
| `refund_ids` | One2many(`nexi.transaction`) | | Linked refund transactions |
| `external_id` | Char | Yes | Unique UUID (auto-generated, max 63 chars) |
| `terminal_id` | Char | Yes | NEXI terminal identifier |
| `amount` | Monetary | Yes | Transaction amount |
| `currency_id` | Many2one(`res.currency`) | Yes | Transaction currency |
| `state` | Selection | Yes | `pending` / `processing` / `awaiting_confirm` / `confirmed` / `failed` / `cancelled` |
| `result_code` | Char | | NEXI result code (`SUCCESS`, `ABORTED`, `REJECTED`, `NETWORK_ERROR`, `NOT_ACCEPTED`) |
| `result_message` | Char | | Human-readable result description |
| `pos_order_id` | Many2one(`pos.order`) | | Linked POS order |
| `payment_method_id` | Many2one(`pos.payment.method`) | | Payment method used |
| `terminal_ref` | Many2one(`nexi.terminal`) | | Computed: terminal config reference |
| `confirm_attempts` | Integer | | Number of confirmation API calls made |
| `purchase_request_time` | Datetime | | When purchase was initiated |
| `confirm_request_time` | Datetime | | When confirm was sent |
| `notification_received` | Boolean | | Whether async notification was received |
| `notification_data` | Text | | Raw notification JSON |
| `webhook_verified` | Boolean | | Whether last webhook was HMAC verified |
| `webhook_timestamp` | Datetime | | Timestamp from verified webhook |
| `error_count` | Integer | | Number of errors encountered |
| `last_error` | Text | | Last error message |
| `reference` | Char | | NEXI transaction reference |
| `card_scheme` | Char | | Card brand (VISA, MASTERCARD, etc.) |
| `authorization_code` | Char | | Issuer authorization code |
| `authorization_response_code` | Char | | Raw authorization network response code |
| `card_entry_mode` | Char | | CHIP, CONTACTLESS, SWIPE, MANUAL |
| `card_number_customer` | Char | | Masked PAN for customer receipt |
| `card_number_merchant` | Char | | Masked PAN for merchant receipt |
| `cardholder_verification` | Char | | PIN, SIGNATURE, NONE |
| `application_name` | Char | | EMV application name |
| `application_identifier` | Char | | EMV AID |
| `acquirer_reference` | Char | | Acquirer reference number |
| `acquirer_terminal_id` | Char | | Terminal ID at acquirer |
| `acquirer_timestamp` | Char | | Acquirer timestamp |
| `authorized_amount` | Float | | Amount authorized by issuer |
| `captured_amount` | Float | | Amount captured/settled |
| `receipt_details_customer` | Text | | Full customer receipt text |
| `receipt_details_merchant` | Text | | Full merchant receipt text |
| `company_id` | Many2one(`res.company`) | Yes | Owning company |

**SQL Constraints:**
- `external_id_unique`: External ID must be unique.

**Key Methods:**

| Method | Description |
|--------|-------------|
| `action_purchase()` | Initiate purchase on terminal (advisory lock, API call, state update) |
| `action_confirm()` | Confirm transaction via `POST /transaction/confirm` |
| `action_cancel()` | Cancel pending/processing transaction via `POST /transaction/confirm` with `ABORTED` |
| `action_void()` | Void a confirmed transaction (reversal) |
| `action_refund()` | Create a linked refund transaction and initiate it on the terminal |
| `action_purchase_refund()` | Initiate refund on terminal (calls `POST /transaction/refund`) |
| `action_retry()` | Create a new transaction with same details for retry |
| `action_poll_status()` | Poll `POST /transaction/get` and update local state |
| `process_notification(data)` | Process async webhook notification |
| `handle_webhook(terminal_id, payload)` | Entry point for webhook processing |
| `get_receipt_data()` | Return dict with all card/transaction details for receipt |
| `is_successful()` | True if confirmed with SUCCESS result |
| `can_be_confirmed()` | True if in confirmable state |
| `_cron_cleanup_old_transactions(days=90)` | Delete old failed/cancelled transactions |
| `_cron_poll_pending_transactions()` | Poll stalled transactions (>5 min, <5 errors) |
| `_cron_recover_unconfirmed()` | Recover unconfirmed from all active terminals |

### `nexi.audit.log`

**Description:** Security audit log for NEXI payment operations.
**Defined in:** `security/nexi_security.py`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `action` | Char | Yes | Action identifier (e.g., `payment_initiated`, `refund_access_denied`) |
| `user_id` | Many2one(`res.users`) | Yes | User who performed the action |
| `message` | Text | Yes | Human-readable description |
| `details` | Text | | Structured data (sensitive values auto-redacted) |
| `ip_address` | Char | | Client IP address |
| `create_date` | Datetime | | Automatic timestamp |

**Key Methods:**

| Method | Description |
|--------|-------------|
| `cleanup_old_logs(days=90)` | Delete audit logs older than specified days |

### `nexi.security.mixin` (Abstract)

**Description:** Security mixin inherited by `nexi.terminal` and `nexi.transaction`.

| Method | Description |
|--------|-------------|
| `_audit_log(action, details, message)` | Write audit log entry for current record |
| `_audit_log_model(action, details, message)` | Write audit log entry at model level |
| `_sanitize_input(value, input_type)` | Sanitize user input (`string`, `external_id`, `terminal_id`, `amount`, `email`) |
| `_check_rate_limit(action, max_attempts, window_seconds)` | Check in-memory rate limit |
| `_enforce_rate_limit(action, max_attempts, window_seconds)` | Raise exception if rate limit exceeded |
| `_validate_permission(permission)` | Check user has specified permission group |
| `_has_permission(permission)` | Non-raising permission check |
| `_generate_request_hash(data)` | SHA-256 hash for request deduplication |
| `_redact_sensitive_data(data)` | Mask sensitive fields in dicts for logging |

### `NexiApiClient` (Plain Python class)

**Description:** HTTP client for the NEXI POS API. Not an Odoo model.
**Defined in:** `models/nexi_api_client.py`

| Method | API Endpoint | Description |
|--------|-------------|-------------|
| `purchase(requested_amount, external_id, terminal_id, ...)` | `POST /transaction/purchase` | Initiate a purchase |
| `confirm(external_id, terminal_id, result_code)` | `POST /transaction/confirm` | Confirm a transaction |
| `abort(external_id, terminal_id)` | `POST /transaction/confirm` (ABORTED) | Abort a pending transaction |
| `continue_transaction(external_id, terminal_id, ...)` | `POST /transaction/continue` | Resume interrupted transaction |
| `get_transaction(external_id)` | `POST /transaction/get` | Get transaction details |
| `refund(requested_amount, external_id, terminal_id, ...)` | `POST /transaction/refund` | Initiate a refund |
| `get_unconfirmed(terminal_id)` | `POST /transaction/unconfirmed` | List unconfirmed transactions |
| `get_terminal_status(terminal_id)` | `POST /terminal/status` | Check terminal connectivity |
| `print_receipt(terminal_id, contents_base64, ...)` | `POST /terminal/print` | Queue receipt print job |
| `get_events(terminal_id, wait_seconds)` | `POST /event/list` | Poll for terminal events |
| `generate_external_id(prefix)` | -- | Generate UUID-based external ID |

Features:
- HTTP Basic Auth (`key_id:key_secret`)
- Configurable retry with exponential backoff (3 retries, status codes 429/500/502/503/504)
- Connection pooling (10 connections)
- 30-second default timeout
- Input validation for all parameters
- Custom exception hierarchy: `NexiApiError` > `NexiNetworkError`, `NexiValidationError`, `NexiAuthenticationError`
- Context manager support (`with NexiApiClient(...) as client:`)

### Extended Odoo Models

**`pos.payment.method`** (inherits `pos.payment.method`):

| Field | Type | Description |
|-------|------|-------------|
| `is_nexi_payment` | Boolean | Enable NEXI terminal for this method |
| `nexi_terminal_id` | Many2one(`nexi.terminal`) | Linked NEXI terminal |

Key methods: `nexi_payment_request(order)`, `nexi_cancel_payment(external_id)`, `nexi_poll_payment_status(external_id)`, `_validate_payment_amount(amount, order)`, `_calculate_payment_amount(order)`.

**`pos.payment`** (inherits `pos.payment`):

| Field | Type | Description |
|-------|------|-------------|
| `nexi_transaction_id` | Many2one(`nexi.transaction`) | Linked NEXI transaction |
| `nexi_external_id` | Char | Related external ID (stored) |
| `is_nexi_payment` | Boolean | Related from payment method (stored) |

Key methods: `get_payment_receipt_data()`, `action_retry_nexi_payment()`, `action_void_nexi_payment()`, `sync_nexi_status()`.

**`pos.order`** (inherits `pos.order`):

| Field | Type | Description |
|-------|------|-------------|
| `nexi_transaction_ids` | One2many(`nexi.transaction`) | All linked transactions |
| `nexi_payment_count` | Integer | Computed count of NEXI payments |
| `has_pending_nexi_payments` | Boolean | Computed: any pending transactions |

Key methods: `add_nexi_payment(amount, payment_method_id)`, `validate_nexi_payments()`, `cancel_pending_nexi_payments()`, `action_view_nexi_transactions()`. Overrides `action_pos_order_paid()` and `action_pos_order_invoice()` to block completion when NEXI payments are pending or failed.

**`pos.session`** (inherits `pos.session`):

| Field | Type | Description |
|-------|------|-------------|
| `nexi_terminal_ids` | Many2many(`nexi.terminal`) | Computed: terminals in session |
| `has_nexi_terminal` | Boolean | Computed: any terminals present |
| `active_nexi_transactions_count` | Integer | Computed: pending transaction count |

Key methods: `open_nexi_terminal()`, `close_nexi_terminal()`, `get_nexi_transactions(domain)`, `get_nexi_reconciliation_data()`. Overrides `action_pos_session_open()`, `action_pos_session_closing_control()`, `action_pos_session_close()` for terminal lifecycle management.

---

## Controller Endpoints Reference

All endpoints use `type='json'` (Odoo JSON-RPC). Send requests as `Content-Type: application/json` with params in `{"jsonrpc": "2.0", "params": {...}}`.

### Webhook Controller (`NexiWebhookController`)

| Route | Auth | Method | Description |
|-------|------|--------|-------------|
| `/nexi/webhook/<terminal_id>` | `none` | POST | Receive webhook/notification from NEXI. Rate limited (100/min/IP). Validates terminal ID format, finds terminal, processes notification via `handle_webhook()`. |

### POS Controller (`NexiPosController`)

| Route | Auth | Method | Params | Description |
|-------|------|--------|--------|-------------|
| `/pos_nexi/terminals` | `user` | POST | `config_id` | List active terminals for a POS config |
| `/pos_nexi/payment/initiate` | `user` | POST | `order_id`, `amount`, `terminal_id`, `currency_id`, `pos_session_id`, `config_id` | Initiate payment on terminal |
| `/pos_nexi/payment/status` | `user` | POST | `external_id` | Poll transaction status with card details |
| `/pos_nexi/payment/cancel` | `user` | POST | `external_id` | Cancel a pending payment |
| `/pos_nexi/payment/refund` | `user` | POST | `payment_method_id`, `amount`, `original_external_id` | Initiate refund (requires NEXI Manager) |
| `/pos_nexi/transaction/unconfirmed` | `user` | POST | `terminal_id` | Recover unconfirmed transactions (requires NEXI Manager) |
| `/pos_nexi/terminal/print` | `user` | POST | `payment_method_id`, `external_id` | Print receipt on terminal |
| `/pos_nexi/terminal/test` | `user` | POST | `terminal_id` | Test terminal connection |
| `/nexi/pos/payment_status` | `user` | POST | `external_id` | Get payment status (alternate route) |
| `/nexi/pos/terminal_status` | `user` | POST | `terminal_id` | Get terminal status |

All endpoints sanitize input parameters and return structured JSON responses. Sensitive data is never exposed in error messages.

---

## Security Architecture

### Authentication & Authorization

- **API Authentication**: HTTP Basic Auth with `key_id:key_secret` per terminal.
- **API Key Encryption**: Fernet symmetric encryption at rest. Keys are encrypted on `create()` and `write()`, decrypted only when building API client instances.
- **Encryption Key Source** (in priority order):
  1. `nexi_encryption_key` in `odoo.conf`
  2. SHA-256 of `admin_passwd` from `odoo.conf`
  3. Plaintext fallback (development only, logged as warning)

### Security Groups

| Group | Terminal | Transaction | Audit Log |
|-------|----------|-------------|-----------|
| NEXI User | Read | Read + Create | -- |
| NEXI Manager | Read + Write + Create | Read + Write + Create | Read + Create |
| NEXI Admin | Full CRUD | Full CRUD | Full CRUD |

### Record Rules

| Rule | Scope | Domain |
|------|-------|--------|
| Terminal: Multi-Company | Global | `company_id in company_ids` |
| Transaction: Multi-Company | Global | `company_id in company_ids` |
| Transaction: User Own | NEXI User | `create_uid = user.id` |
| Transaction: Manager All | NEXI Manager | `(1, '=', 1)` (all records) |

### Field-Level Security

- `api_key_id` and `api_key_encrypted` on `nexi.terminal` are restricted to `group_nexi_admin` via the `groups` attribute.

### Webhook Security

- HMAC-SHA256 signature verification via `X-Nexi-Signature` header (when present)
- Timestamp validation via `X-Nexi-Timestamp` header (5-minute tolerance to prevent replay attacks)
- Rate limiting: 100 requests per minute per IP address
- Terminal ID format validation
- External ID sanitization

> **Note:** The NEXI POS API primarily uses event polling (`POST /event/list`) rather than webhooks. The webhook endpoint is maintained for backwards compatibility and edge cases.

### Controller Security

- All POS endpoints require `auth='user'` (authenticated Odoo session).
- Refund and recovery endpoints additionally check for `group_nexi_manager` membership.
- Payment status endpoints validate company ownership (cross-company access requires `base.group_system`).
- All input parameters are sanitized against injection patterns.

### Rate Limiting

| Operation | Limit | Window |
|-----------|-------|--------|
| Payment initiation per terminal | 10 per minute | 60 seconds |
| Webhook calls per IP | 100 per minute | 60 seconds |
| Refund operations per user | 10 per minute | 60 seconds |
| General operations (mixin) | Configurable | Configurable |

### Sensitive Data Handling

- API keys are masked in all log output (first 4 + last 4 characters shown).
- Audit log entries automatically redact fields matching patterns: `api_key`, `api_secret`, `password`, `token`, `credit_card`, `cvv`, `pin`, `authorization`.
- The `_redact_sensitive_data()` mixin method is available for any model inheriting `nexi.security.mixin`.

### Transaction Safety

- PostgreSQL advisory locks (`pg_advisory_lock` / `pg_advisory_try_lock`) prevent race conditions when multiple workers process the same transaction.
- Lock namespace `123456789` with SHA-256-derived lock IDs from `external_id`.
- Locks are always released in `finally` blocks.

---

## Cron Jobs Reference

| XML ID | Name | Model | Method | Interval | Description |
|--------|------|-------|--------|----------|-------------|
| `ir_cron_nexi_recover_unconfirmed` | NEXI: Recover Unconfirmed Transactions | `nexi.transaction` | `_cron_recover_unconfirmed()` | 15 minutes | Query all active terminals for stuck unconfirmed transactions |
| `ir_cron_nexi_poll_pending` | NEXI: Poll Pending Transactions | `nexi.transaction` | `_cron_poll_pending_transactions()` | 5 minutes | Poll status of transactions in `processing`/`awaiting_confirm` older than 5 minutes |
| `ir_cron_nexi_cleanup_old` | NEXI: Cleanup Old Transactions | `nexi.transaction` | `_cron_cleanup_old_transactions()` | 1 day | Delete `failed`/`cancelled` transactions older than 90 days |

---

## Troubleshooting

### Terminal shows "Error" state after Test Connection

- Verify the **API Key ID** and **API Key Secret** are correct (check with your NEXI account manager).
- Verify the physical terminal is powered on and connected to the network.
- Check the Odoo log for the specific error message (`NEXI API error: ...`).
- Verify the `nexi_payment.base_url` system parameter points to the correct API environment.

### Payment stays in "Processing" state

- The NEXI API uses long-polling with a default 25-second wait. The terminal may still be waiting for the customer to present a card.
- Click **Poll Status** on the transaction form to manually refresh.
- The `NEXI: Poll Pending Transactions` cron will automatically retry every 5 minutes for transactions older than 5 minutes.
- If the terminal was disconnected during the transaction, use **Recover Unconfirmed** on the terminal form.

### "Cannot mark order as paid" error

- This occurs when there are pending or failed NEXI transactions on the order.
- Check the order's NEXI Transactions smart button to see which transactions are blocking.
- Cancel failed transactions or wait for pending ones to complete.

### API key encryption warnings in logs

- `No encryption key configured for NEXI module`: Set `nexi_encryption_key` in `odoo.conf`.
- `cryptography library not installed`: Run `pip3 install cryptography`.
- Existing plaintext keys can be migrated via the **Migrate API Keys** action on any terminal form, or automatically via the cron if configured.

### Rate limit errors ("Too many attempts")

- Payment initiation is limited to 10 per terminal per minute.
- Refund operations are limited to 10 per user per minute.
- Wait 60 seconds and retry. If this happens frequently, check for runaway polling or duplicate requests.

### Session close blocked by pending transactions

- The module prevents session close when there are `pending`, `processing`, or `awaiting_confirm` transactions.
- Complete or cancel all pending NEXI transactions before closing the session.
- Use the order's `cancel_pending_nexi_payments()` method to bulk-cancel stuck transactions.

### Frontend terminal status shows "Offline"

- The POS frontend polls terminal status every 30 seconds via `/pos_nexi/terminals`.
- Click the terminal status indicator in the POS header and press the refresh button.
- Verify the terminal is `active` in the backend and that `action_test_connection()` succeeds.

---

## Running Tests

```bash
# All module tests
odoo -i pos_nexi_payment --test-enable --stop-after-init

# Specific test file
odoo -i pos_nexi_payment --test-enable --test-tags nexi_api
```

Test suites:
- `test_nexi_api_client` -- Unit tests for the API client (validation, retry logic, error handling)
- `test_nexi_transaction` -- Transaction state machine, advisory locks, card detail extraction
- `test_nexi_security` -- Encryption, input sanitization, rate limiting, permission checks
- `test_pos_integration` -- End-to-end POS payment workflow

---

## Internationalization

The module ships with translation files for:
- Italian (`i18n/it.po`) -- Primary UI language for user-facing POS strings
- Finnish (`i18n/fi.po`)
- Swedish (`i18n/sv.po`)

The translation template is at `i18n/pos_nexi_payment.pot`.

---

## License

This module is licensed under the **GNU Lesser General Public License v3 (LGPL-3)**.

See the [LICENSE](https://www.gnu.org/licenses/lgpl-3.0.html) file for details.
