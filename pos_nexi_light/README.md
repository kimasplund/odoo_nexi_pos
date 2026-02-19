# POS NEXI Light

NEXI payment terminal integration for Odoo 18 Point of Sale -- the lightweight edition.

| | |
|---|---|
| **Version** | 18.0.1.0.0 |
| **Odoo** | 18.0 |
| **Category** | Sales / Point of Sale |
| **License** | LGPL-3 |
| **Author** | Kim Asplund |
| **Website** | https://asplund.kim |
| **Dependencies** | `point_of_sale` |

---

## Table of Contents

1. [Overview](#overview)
2. [Light vs Full Module](#light-vs-full-module)
3. [Prerequisites](#prerequisites)
4. [Installation](#installation)
5. [Configuration](#configuration)
6. [Usage](#usage)
7. [Terminal Receipt Printing](#terminal-receipt-printing)
8. [Technical Reference](#technical-reference)
9. [Security](#security)
10. [Internationalization](#internationalization)
11. [Troubleshooting](#troubleshooting)
12. [License](#license)

---

## Overview

`pos_nexi_light` connects Odoo 18 Point of Sale to NEXI payment terminals through the NEXI nPay POS API (`https://api.npay.eu/pos/v1`). It follows the same architectural pattern used by Odoo's official terminal integrations (`pos_adyen`, `pos_stripe`): a single `pos.payment.method` extension with no new models.

**What it does:**

- Sends purchase and refund requests to a NEXI terminal from the POS screen.
- Polls for transaction status and auto-confirms completed payments.
- Receives asynchronous webhook notifications from NEXI for real-time updates.
- Displays card details (scheme, masked PAN, auth code) on the POS receipt.
- Optionally prints receipts on the terminal's built-in thermal printer.

**Who it is for:**

Small to mid-size shops that need a straightforward, low-maintenance NEXI integration without the overhead of dedicated terminal management models, audit logs, or background cron jobs.

---

## Light vs Full Module

Two NEXI modules exist in this repository. They are **mutually exclusive** -- never install both on the same database.

| Aspect | pos_nexi_light (this module) | pos_nexi_payment (full module) |
|---|---|---|
| **New Odoo models** | 0 | 3 (`nexi.terminal`, `nexi.transaction`, `nexi.audit.log`) |
| **Architecture** | Extends `pos.payment.method` only | Dedicated models with relational structure |
| **Terminal management** | Config fields on payment method | Separate terminal records with activate/deactivate actions |
| **Transaction tracking** | Via `nexi_latest_response` buffer | Full `nexi.transaction` model with state machine |
| **Audit logging** | Server logs only | Dedicated `nexi.audit.log` model |
| **Refund handling** | Inline on payment method | Linked to original transaction |
| **Recovery / retry** | Manual | Automatic via cron jobs |
| **Cron jobs** | None | Heartbeat polling, stale transaction recovery |
| **Maintenance effort** | Low | Higher |
| **Best for** | Single-terminal shops, simple setups | Multi-terminal environments, compliance requirements |

**Rule of thumb:** If you need audit trails, multi-terminal dashboards, or automatic transaction recovery, use `pos_nexi_payment`. If you want something that installs in minutes and just works, use this module.

---

## Prerequisites

- **Odoo 18.0** (Community or Enterprise)
- The `point_of_sale` module installed and configured
- A **NEXI merchant account** with nPay POS API access
- API credentials from NEXI onboarding:
  - API Key ID
  - API Key Secret
- Your NEXI physical terminal ID (e.g., `TERM001`)
- Network connectivity between the Odoo server and `https://api.npay.eu` (production) or `https://api-test.npay.eu` (test)
- For terminal receipt printing: `Pillow` (PIL) Python package (included in standard Odoo) and optionally `wkhtmltoimage` for HTML receipt rendering

---

## Installation

### Docker (recommended)

If your Odoo instance runs in Docker, mount the module into the addons directory:

```yaml
# docker-compose.yml
services:
  odoo:
    image: odoo:18.0
    volumes:
      - ./pos_nexi_light:/mnt/extra-addons/pos_nexi_light
    environment:
      - ADDONS_PATH=/mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
```

Then restart and install:

```bash
docker compose restart odoo
docker compose exec odoo odoo -d your_database -i pos_nexi_light --stop-after-init
```

### Manual

```bash
# Copy the module into your Odoo addons path
cp -r pos_nexi_light /opt/odoo/addons/

# Update the module list and install
./odoo-bin -c /etc/odoo/odoo.conf -d your_database --update base --stop-after-init
./odoo-bin -c /etc/odoo/odoo.conf -d your_database -i pos_nexi_light --stop-after-init
```

After installation, restart the Odoo service and verify the module appears under **Apps** with status *Installed*.

---

## Configuration

### Step 1: Create a Payment Method

1. Navigate to **Point of Sale > Configuration > Payment Methods**.
2. Click **New**.
3. Enter a name (e.g., "NEXI Card" or "Carta NEXI").
4. In the **Use a Payment Terminal** dropdown, select **NEXI**.

### Step 2: Enter NEXI Credentials

Once "NEXI" is selected, the following fields appear:

| Field | Required | Description |
|---|---|---|
| **Terminal ID** | Yes | The unique identifier for your physical NEXI terminal (e.g., `TERM001`). Must be unique across all payment methods. |
| **API Key ID** | Yes | Your NEXI API key ID, provided during merchant onboarding. Used for HTTP Basic Auth. |
| **API Key Secret** | Yes | Your NEXI API key secret. Displayed as a password field. Used together with Key ID for Basic Auth. |
| **Test Mode** | No | When enabled, API calls go to `https://api-test.npay.eu/pos/v1` instead of production. Defaults to **on**. |
| **Print Receipt on Terminal** | No | When enabled, receipts are automatically printed on the terminal's thermal printer after a successful payment. |
| **Terminal Print Content** | No | What to print on the terminal (visible only when printing is enabled): *NEXI Card Receipt*, *Full POS Receipt*, or *Both*. Defaults to *NEXI Card Receipt*. |

### Step 3: Test the Connection

Click the **Test Connection** button in the "NEXI Terminal Status" section at the bottom of the form. This sends a `POST /terminal/status` request to the NEXI API. On success, the terminal status badge turns green ("Online") and the last heartbeat timestamp is updated.

### Step 4: Configure the Webhook

To receive real-time payment notifications from NEXI (instead of relying solely on polling), configure the following URL in your NEXI merchant dashboard:

```
https://your-odoo-domain.com/pos_nexi_light/notification/<payment_method_id>/<webhook_token>
```

- `<payment_method_id>` is the database ID of the payment method record (visible in the URL bar when editing it).
- `<webhook_token>` is auto-generated when the payment method is saved. It is stored in the `nexi_webhook_token` field (visible only to POS Managers).

The webhook token is generated automatically on first save -- you do not need to create it manually.

### Step 5: Assign to POS Configuration

1. Navigate to **Point of Sale > Configuration > Point of Sale**.
2. Edit your POS configuration.
3. Under **Payment Methods**, add the NEXI payment method you just created.
4. Save.

---

## Usage

### Making a Payment

1. Open a POS session.
2. Add products to the cart.
3. Click the **Payment** button.
4. Select the **NEXI** payment method.
5. Click **Send** -- this transmits the payment request to the terminal.
6. A popup appears: *"Please complete the payment on the terminal..."* (Italian: *"Completare il pagamento sul terminale..."*)
7. The customer taps, inserts, or swipes their card on the physical terminal.
8. The POS polls for status every second (up to 2 minutes). When the terminal reports `awaiting_confirm` with `result_code=SUCCESS`, the payment auto-confirms.
9. Card details (card scheme, masked PAN, authorization code) appear on the payment line.

### Processing a Refund

When the payment amount is **negative** (i.e., the cashier enters a return), the module automatically calls the `transaction/refund` endpoint instead of `transaction/purchase`. The flow is otherwise identical.

### Cancelling a Payment

While the terminal is waiting for the customer, the cashier can click **Cancel** in the POS. This sends `transaction/confirm` with `result_code=ABORTED` to abort the pending transaction on the terminal.

### Receipt

After a successful NEXI payment, the POS receipt automatically includes a "NEXI Payment" section showing:

- **Card** -- card scheme (e.g., VISA, Mastercard)
- **PAN** -- masked card number (e.g., `**** **** **** 1234`)
- **Auth** -- authorization code
- **Entry** -- card entry mode (e.g., contactless, chip)
- **Ref** -- NEXI transaction reference

This section only appears for payment lines that have NEXI card details.

---

## Terminal Receipt Printing

The module can print receipts directly on the NEXI terminal's built-in thermal printer using the `POST /terminal/print` API endpoint.

### How It Works

1. After a successful payment, if **Print Receipt on Terminal** is enabled, the module renders the receipt content as a PNG image.
2. **NEXI Card Receipt** mode: the customer receipt text from the NEXI response (`receipt_details_customer`) is rendered as monospace text using Pillow (PIL). The image is sized for 80mm thermal paper (384px wide at 203 DPI).
3. **Full POS Receipt** mode: the full POS receipt HTML is rendered to PNG using `wkhtmltoimage`. If `wkhtmltoimage` is not available, it falls back to stripping HTML tags and rendering as plain text.
4. **Both** mode: prints both receipts sequentially.
5. Large images are automatically split into fragments of up to 262,143 base64 characters each, sent with `continue=True` on all but the last fragment.

### Requirements

- **Pillow (PIL)** -- always available in Odoo's Python environment.
- **wkhtmltoimage** -- included with `wkhtmltopdf` (standard Odoo dependency). Only needed for the "Full POS Receipt" print mode.
- A monospace TrueType font on the server (`DejaVu Sans Mono`, `Liberation Mono`, or `FreeMono`).

---

## Technical Reference

### Module Structure

```
pos_nexi_light/
  __manifest__.py                         # Module metadata, depends on point_of_sale
  __init__.py                             # Imports models/ and controllers/
  models/
    __init__.py
    pos_payment_method.py                 # Core: extends pos.payment.method with NEXI fields and API methods
    nexi_receipt_renderer.py              # Receipt-to-PNG rendering for terminal printing
  controllers/
    __init__.py
    main.py                              # Webhook notification controller
  views/
    pos_payment_method_views.xml         # Form, tree, and search view extensions
  static/src/
    js/
      payment_nexi.js                    # POS frontend: PaymentNexi class (extends PaymentInterface)
      models.js                          # POS frontend: registers payment method, patches PosPayment
    xml/
      pos_nexi_templates.xml             # POS receipt template extension
  data/
    nexi_data.xml                        # Data file placeholder (noupdate)
  tests/
    __init__.py
    test_pos_nexi_light.py               # Unit tests
  i18n/
    pos_nexi_light.pot                   # Translation template
    it.po                                # Italian translations
    fi.po                                # Finnish translations
    sv.po                                # Swedish translations
```

### Fields on `pos.payment.method`

All fields are added via `_inherit = 'pos.payment.method'` (no new models).

| Field | Type | Default | Access | Description |
|---|---|---|---|---|
| `nexi_terminal_id` | `Char` | -- | All | Physical terminal identifier (e.g., `TERM001`). Unique constraint enforced. |
| `nexi_api_key_id` | `Char` | -- | POS Manager | API Key ID for HTTP Basic Auth. |
| `nexi_api_key_secret` | `Char` | -- | POS Manager | API Key Secret for HTTP Basic Auth. Displayed as password. |
| `nexi_webhook_token` | `Char` | Auto-generated | POS Manager | UUID hex token for webhook URL verification. |
| `nexi_test_mode` | `Boolean` | `True` | All | Routes API calls to test environment when enabled. |
| `nexi_print_enabled` | `Boolean` | `False` | All | Enable automatic receipt printing on the terminal. |
| `nexi_print_content` | `Selection` | `nexi_receipt` | All | Print content choice: `nexi_receipt`, `pos_receipt`, or `both`. |
| `nexi_terminal_status` | `Selection` | `unknown` | Readonly | Terminal status: `unknown`, `online`, `offline`, `busy`, `error`. |
| `nexi_last_heartbeat` | `Datetime` | -- | Readonly | Last time the terminal was confirmed online. |
| `nexi_latest_response` | `Char` | -- | POS Manager | JSON buffer for the latest webhook/status response. Writable even during open POS sessions. |

**Fields sent to POS frontend** (via `_load_pos_data_fields`): `nexi_terminal_id`, `nexi_test_mode`, `nexi_terminal_status`, `nexi_print_enabled`, `nexi_print_content`.

### Python API Methods on `pos.payment.method`

All methods operate on a single record (`self.ensure_one()`).

| Method | Arguments | NEXI Endpoint | Description |
|---|---|---|---|
| `nexi_create_payment(amount, external_id=None)` | `amount` in major currency units (e.g., `29.99`) | `POST /transaction/purchase` | Creates a payment request. Converts amount to minor units (cents). Generates UUID `external_id` if not provided. |
| `nexi_refund_payment(amount, original_external_id=None)` | `amount` (positive), optional original ID | `POST /transaction/refund` | Creates a refund request. Uses absolute value of amount. |
| `nexi_check_status(external_id)` | Transaction external ID | `POST /transaction/get` | Polls for current transaction status. |
| `nexi_confirm_payment(external_id, result_code='SUCCESS')` | Transaction external ID, result code | `POST /transaction/confirm` | Confirms a completed transaction. Updates terminal status to `online`. |
| `nexi_cancel_payment(external_id)` | Transaction external ID | `POST /transaction/confirm` | Cancels a pending transaction by sending `result_code=ABORTED`. |
| `nexi_make_request(endpoint, payload)` | Endpoint path, dict payload | Any | Low-level authenticated POST to the NEXI API. Builds Basic Auth header, handles errors and timeouts. |
| `action_nexi_test_connection()` | None | `POST /terminal/status` | Tests connectivity. Returns an `ir.actions.client` notification action. |
| `get_latest_nexi_status()` | None | None (local) | Returns the latest webhook response from the `nexi_latest_response` buffer as a parsed dict. |
| `nexi_print_to_terminal(receipt_text, receipt_html)` | Text and/or HTML content | `POST /terminal/print` | Renders receipt as PNG and sends to terminal printer. |

### Controller Endpoints

| Route | Auth | Method | Description |
|---|---|---|---|
| `/pos_nexi_light/notification/<int:payment_method_id>/<string:token>` | `public` | `POST` (JSON) | Webhook receiver. Validates token, extracts transaction data, stores response in `nexi_latest_response`, updates terminal status, and pushes `NEXI_LATEST_RESPONSE` notification to active POS sessions via `config._notify()`. Always returns `[accepted]`. |
| `/pos_nexi_light/test` | `public` | `GET` (HTTP) | Health check endpoint. Returns `"NEXI POS Integration is running!"`. |

### JavaScript Components

**`PaymentNexi`** (extends `PaymentInterface`):

The POS frontend payment handler. Registered with `register_payment_method("nexi", PaymentNexi)`.

| Method | Description |
|---|---|
| `send_payment_request(data)` | Sends purchase (positive amount) or refund (negative amount) to backend. Starts polling loop. |
| `send_payment_cancel(data)` | Calls `nexi_cancel_payment` on the backend to abort a pending transaction. |
| `_pollForStatus(externalId)` | Polls `get_latest_nexi_status` every 1 second for up to 120 iterations (2 minutes). |
| `_handlePaymentSuccess(data)` | Confirms payment, stores card details on the payment line, triggers terminal print if enabled. |
| `_handlePaymentFailure(data)` | Sets payment status to `retry` and shows error popup. |

**`PosPayment` patch** (in `models.js`):

Extends the `PosPayment` model to serialize and deserialize NEXI-specific fields across the POS lifecycle:

- `nexi_external_id` -- UUID linking to the NEXI transaction
- `card_type` -- card scheme (VISA, Mastercard, etc.)
- `nexi_authorization_code` -- bank authorization code
- `nexi_card_number` -- masked card PAN
- `nexi_card_entry_mode` -- contactless, chip, swipe, etc.
- `nexi_reference` -- NEXI transaction reference
- `nexi_receipt_customer` -- customer receipt text from NEXI
- `nexi_receipt_merchant` -- merchant receipt text from NEXI

### Receipt Template

The `pos_nexi_light.OrderReceiptNexi` template extends `point_of_sale.OrderReceipt` using `t-inherit-mode="extension"`. It inserts a "NEXI Payment" section after the payment lines, showing card type, PAN, auth code, entry mode, and reference for each NEXI payment line.

### NEXI API Details

| Setting | Value |
|---|---|
| Production base URL | `https://api.npay.eu/pos/v1` |
| Test base URL | `https://api-test.npay.eu/pos/v1` |
| Authentication | HTTP Basic Auth (`base64(key_id:key_secret)`) |
| HTTP method | All endpoints use `POST` |
| Request timeout | 30 seconds |
| User-Agent | `OdooPOS/18.0 (nexi-payment-integration)` |

**Transaction states observed by this module**: `pending`, `processing`, `awaiting_confirm`, `confirmed`, `failed`, `cancelled`.

**Amount format**: The API expects amounts in minor currency units (integer cents). The module converts using `currency.rounding` from the payment journal's currency.

**Response envelope**: Transaction responses are wrapped in `{"transaction": {...}}`. The `_unwrap_transaction()` method handles this.

**Card details**: Located in the `payment_method_details` sub-object of the transaction. Receipt text (`receipt_details_customer`, `receipt_details_merchant`) is at the transaction level.

---

## Security

### Authentication

API credentials (`nexi_api_key_id`, `nexi_api_key_secret`) are:
- Restricted to the `point_of_sale.group_pos_manager` security group -- regular POS users cannot read or modify them.
- The API Key Secret is rendered as a password field in the UI.
- Stored in the Odoo database with standard Odoo field-level encryption at rest.
- Transmitted to the NEXI API over HTTPS only, encoded as HTTP Basic Auth headers.

### Webhook Security

- Each payment method gets an auto-generated `nexi_webhook_token` (UUID4 hex, 32 characters) on first save.
- Incoming webhook requests must include the correct token in the URL path. Requests with missing or invalid tokens are silently rejected (logged as warnings).
- The webhook endpoint uses `auth='public'` and `csrf=False` (required for external POST callbacks) but validates via the token.
- Responses always return `[accepted]` regardless of validation outcome, preventing information leakage to potential attackers.

### POS Access Control

- `get_latest_nexi_status()` checks that the calling user belongs to `point_of_sale.group_pos_user` before returning data.
- The `nexi_latest_response`, `nexi_terminal_status`, and `nexi_last_heartbeat` fields are whitelisted in `_is_write_forbidden()` so they can be updated even when a POS session is open, without bypassing the standard write protection for other fields.

### Network

- All API communication uses HTTPS (TLS).
- The module does not store full card numbers -- only the masked PAN provided by NEXI (e.g., `**** **** **** 1234`).

---

## Internationalization

The module includes translations for:

| Language | File | Status |
|---|---|---|
| Italian (it) | `i18n/it.po` | Complete |
| Finnish (fi) | `i18n/fi.po` | Available |
| Swedish (sv) | `i18n/sv.po` | Available |

The Italian translation covers all user-facing strings including field labels, help text, error messages, and receipt labels. Examples:

- "Terminal ID" becomes "ID Terminale"
- "Test Connection" becomes "Test Connessione"
- "Please complete the payment on the terminal..." becomes "Completare il pagamento sul terminale..."
- "Payment failed" becomes "Pagamento non riuscito"

To add a new language, copy `i18n/pos_nexi_light.pot` to `i18n/<lang_code>.po` and fill in the `msgstr` values.

---

## Troubleshooting

### "NEXI API credentials are not configured"

Both **API Key ID** and **API Key Secret** are required. Open the payment method form and verify both fields are filled. This error also appears if someone clears credentials after initial setup.

### "Connection to NEXI timed out"

The Odoo server could not reach the NEXI API within 30 seconds. Check:
- Network connectivity from the Odoo server to `api.npay.eu` (or `api-test.npay.eu` in test mode).
- Firewall rules allowing outbound HTTPS on port 443.
- DNS resolution is working.

### "Could not connect to NEXI"

A `ConnectionError` occurred. The NEXI API server may be unreachable. Verify internet access from the Odoo server host.

### Test Connection button shows "Connection Failed"

The `POST /terminal/status` call failed. Possible causes:
- Invalid API Key ID or Secret.
- Terminal ID does not match any terminal in your NEXI account.
- Test Mode is enabled but credentials are for production (or vice versa).

### Payment times out after 2 minutes

The POS polls for status 120 times at 1-second intervals. If no response arrives:
- Check that the physical terminal is powered on and connected to the network.
- Verify the webhook URL is correctly configured in the NEXI dashboard so that asynchronous notifications reach Odoo.
- Check Odoo server logs for incoming webhook requests.

### Duplicate Terminal ID error

Each `nexi_terminal_id` must be unique across all NEXI payment methods. If you see *"Terminal TERM001 is already used on payment method X"*, choose a different terminal ID or remove the existing payment method.

### Receipt not printing on terminal

- Verify **Print Receipt on Terminal** is enabled on the payment method.
- For "Full POS Receipt" mode, ensure `wkhtmltoimage` is installed on the server (it ships with `wkhtmltopdf`).
- Check Odoo server logs for `NEXI terminal print failed` warnings.
- Ensure the terminal supports the print API endpoint.

### Webhook notifications not arriving

- The webhook URL must be publicly accessible from the internet (NEXI servers must reach it).
- Verify the URL format: `https://your-domain/pos_nexi_light/notification/<id>/<token>`.
- Check that the `nexi_webhook_token` on the payment method matches the token in the URL.
- Look for `NEXI notification` log entries in the Odoo server log.

---

## License

This module is licensed under the **GNU Lesser General Public License v3 (LGPL-3)**.

See the [LICENSE](https://www.gnu.org/licenses/lgpl-3.0.html) file for details.
