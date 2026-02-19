/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { PaymentInterface } from "@point_of_sale/app/payment/payment_interface";
import { ErrorPopup } from "@point_of_sale/app/errors/popups/error_popup";

export class PaymentNexi extends PaymentInterface {
    /**
     * NEXI Payment Terminal Integration for Odoo POS
     * 
     * This class handles communication between the POS and NEXI payment terminals.
     * It follows the standard Odoo POS payment interface pattern.
     */

    setup() {
        super.setup();
        this.pollTimeout = null;
        this.pollCount = 0;
        this.maxPollCount = 120; // Poll for 2 minutes (1 second intervals)
    }

    // ========================================================================
    // Payment Interface Implementation
    // ========================================================================

    /**
     * Send a payment request to the NEXI terminal
     * @param {Object} data - Payment data from POS
     * @returns {Promise} - Resolves when payment is complete
     */
    async send_payment_request(data) {
        const line = this.pos.get_order().selected_paymentline;

        // Validate configuration
        if (!this.payment_method.nexi_terminal_id) {
            this._showError(_t('NEXI Terminal ID is not configured'));
            return Promise.reject();
        }

        const amount = line.get_amount();

        try {
            // Show waiting popup
            this._showWaitingPopup();

            let result;

            if (amount < 0) {
                // Refund flow — negative amount means return/refund
                result = await rpc('/web/dataset/call_kw/pos.payment.method/nexi_refund_payment', {
                    model: 'pos.payment.method',
                    method: 'nexi_refund_payment',
                    args: [[this.payment_method.id], Math.abs(amount)],
                    kwargs: {},
                });
            } else {
                // Normal purchase flow
                result = await rpc('/web/dataset/call_kw/pos.payment.method/nexi_create_payment', {
                    model: 'pos.payment.method',
                    method: 'nexi_create_payment',
                    args: [[this.payment_method.id], amount],
                    kwargs: {},
                });
            }

            if (result.error) {
                throw new Error(result.error);
            }

            // Store external ID for polling
            line.set_payment_status('waitingCard');
            line.nexi_external_id = result.external_id;

            // Start polling for status
            return this._pollForStatus(result.external_id);

        } catch (error) {
            this._hideWaitingPopup();
            this._showError(_t('Failed to send payment to terminal: ') + error.message);
            line.set_payment_status('retry');
            return Promise.reject();
        }
    }

    /**
     * Send a cancel request to the NEXI terminal
     * @param {Object} data - Payment data from POS
     * @returns {Promise} - Resolves when cancel is complete
     */
    async send_payment_cancel(data) {
        const line = this.pos.get_order().selected_paymentline;
        const externalId = line.nexi_external_id;

        if (!externalId) {
            return Promise.resolve();
        }

        try {
            await rpc('/web/dataset/call_kw/pos.payment.method/nexi_cancel_payment', {
                model: 'pos.payment.method',
                method: 'nexi_cancel_payment',
                args: [[this.payment_method.id], externalId],
                kwargs: {},
            });

            line.set_payment_status('retry');
            return Promise.resolve();

        } catch (error) {
            this._showError(_t('Failed to cancel payment: ') + error.message);
            return Promise.reject();
        }
    }

    /**
     * Handle payment completion
     * @param {Object} data - Final payment data
     */
    async _handlePaymentSuccess(data) {
        const line = this.pos.get_order().selected_paymentline;
        
        // Confirm the payment with NEXI
        try {
            await rpc('/web/dataset/call_kw/pos.payment.method/nexi_confirm_payment', {
                model: 'pos.payment.method',
                method: 'nexi_confirm_payment',
                args: [[this.payment_method.id], data.external_id],
                kwargs: {},
            });

            line.set_payment_status('done');
            line.transaction_id = data.transaction_id || data.external_id;

            // Card details from status response or notification
            const raw = data.raw_data || data;
            const pmd = raw.payment_method_details || {};
            line.card_type = pmd.card_scheme || data.card_scheme || '';
            line.nexi_authorization_code = pmd.authorization_code || data.authorization_code || '';
            line.nexi_card_number = pmd.card_number_customer || data.card_number_customer || '';
            line.nexi_card_entry_mode = pmd.card_entry_mode || data.card_entry_mode || '';
            line.nexi_reference = raw.reference || data.reference || '';
            line.nexi_receipt_customer = raw.receipt_details_customer || data.receipt_details_customer || '';
            line.nexi_receipt_merchant = raw.receipt_details_merchant || data.receipt_details_merchant || '';

            // Auto-print to terminal if enabled (non-blocking)
            if (this.payment_method.nexi_print_enabled) {
                try {
                    await rpc('/pos_nexi_light/terminal/print', {
                        payment_method_id: this.payment_method.id,
                        external_id: data.external_id,
                    });
                } catch (e) {
                    console.warn('NEXI terminal print failed:', e);
                }
            }

            return Promise.resolve();

        } catch (error) {
            line.set_payment_status('retry');
            this._showError(_t('Payment confirmed on terminal but failed to finalize: ') + error.message);
            return Promise.reject();
        }
    }

    /**
     * Handle payment failure
     * @param {Object} data - Error data
     */
    _handlePaymentFailure(data) {
        const line = this.pos.get_order().selected_paymentline;
        line.set_payment_status('retry');
        
        let message = _t('Payment failed');
        if (data.result_code) {
            message += ': ' + data.result_code;
        }
        
        this._showError(message);
        return Promise.reject();
    }

    // ========================================================================
    // Polling Logic
    // ========================================================================

    /**
     * Poll NEXI for transaction status
     * @param {String} externalId - The external transaction ID
     * @returns {Promise} - Resolves when transaction is complete
     */
    async _pollForStatus(externalId) {
        return new Promise((resolve, reject) => {
            this.pollCount = 0;
            this._poll(externalId, resolve, reject);
        });
    }

    /**
     * Single poll iteration
     * @param {String} externalId - The external transaction ID
     * @param {Function} resolve - Promise resolve function
     * @param {Function} reject - Promise reject function
     */
    async _poll(externalId, resolve, reject) {
        this.pollCount++;

        // Check for max polling
        if (this.pollCount > this.maxPollCount) {
            this._hideWaitingPopup();
            this._showError(_t('Payment timeout - no response from terminal'));
            reject();
            return;
        }

        try {
            // Check status via backend
            const status = await rpc('/web/dataset/call_kw/pos.payment.method/get_latest_nexi_status', {
                model: 'pos.payment.method',
                method: 'get_latest_nexi_status',
                args: [[this.payment_method.id]],
                kwargs: {},
            });

            if (status && status.external_id === externalId) {
                // We have a status update
                const state = status.state;
                const resultCode = status.result_code;

                if (state === 'awaiting_confirm' || state === 'confirmed') {
                    // Payment successful
                    this._hideWaitingPopup();
                    if (resultCode === 'SUCCESS') {
                        resolve(this._handlePaymentSuccess(status));
                    } else {
                        reject(this._handlePaymentFailure(status));
                    }
                    return;

                } else if (state === 'failed' || state === 'cancelled') {
                    // Payment failed
                    this._hideWaitingPopup();
                    reject(this._handlePaymentFailure(status));
                    return;
                }
                // Otherwise continue polling
            }

            // Continue polling
            this.pollTimeout = setTimeout(() => {
                this._poll(externalId, resolve, reject);
            }, 1000); // Poll every second

        } catch (error) {
            // Continue polling on error
            this.pollTimeout = setTimeout(() => {
                this._poll(externalId, resolve, reject);
            }, 1000);
        }
    }

    // ========================================================================
    // UI Helpers
    // ========================================================================

    /**
     * Show waiting popup
     */
    _showWaitingPopup() {
        this.env.services.popup.add(ErrorPopup, {
            title: _t('NEXI Payment'),
            body: _t('Please complete the payment on the terminal...'),
        });
    }

    /**
     * Hide waiting popup
     */
    _hideWaitingPopup() {
        if (this.pollTimeout) {
            clearTimeout(this.pollTimeout);
            this.pollTimeout = null;
        }
        // Close any open popups
        this.env.services.popup.closeAll();
    }

    /**
     * Show error popup
     * @param {String} message - Error message
     */
    _showError(message) {
        this.env.services.popup.add(ErrorPopup, {
            title: _t('NEXI Error'),
            body: message,
        });
    }
}
