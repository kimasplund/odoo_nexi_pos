/** @odoo-module **/

/**
 * NEXI Payment Button and Service
 * Handles NEXI payment flow in POS
 */

import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { browser } from "@web/core/browser/browser";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { nexiStore, NexiTransaction, NexiTerminal } from "./nexi_models";

const POLLING_INTERVAL = 2000; // 2 seconds
const MAX_POLLING_TIME = 180000; // 3 minutes

/**
 * Service class for NEXI payment operations
 */
export class NexiPaymentService {
    constructor(pos, dialog, notification) {
        this.pos = pos;
        this.dialog = dialog;
        this.notification = notification;
        this.store = nexiStore;
        this.pollingTimer = null;
        this.timeoutTimer = null;
    }

    /**
     * Load available terminals from server
     * @returns {Promise<NexiTerminal[]>}
     */
    async loadTerminals() {
        try {
            const configId = this.pos.config.id;
            const result = await rpc("/pos_nexi/terminals", {
                config_id: configId
            });
            
            if (result.success && result.terminals) {
                const terminals = result.terminals.map(t => NexiTerminal.fromServer(t));
                this.store.setTerminals(terminals);
                return terminals;
            }
            return [];
        } catch (error) {
            console.error("Failed to load NEXI terminals:", error);
            return [];
        }
    }

    /**
     * Show NEXI payment dialog
     * @param {number} amount - Payment amount
     */
    async showNexiPaymentDialog(amount) {
        // Load terminals if not already loaded
        if (this.store.terminals.length === 0) {
            await this.loadTerminals();
        }

        // Check if any terminal is available
        if (!this.store.hasAvailableTerminal()) {
            this.dialog.add(ConfirmationDialog, {
                title: _t("No Terminal Available"),
                body: _t("No NEXI terminal is currently available. Please verify that terminals are powered on and connected."),
                confirmLabel: _t("OK"),
            });
            return;
        }

        // Open payment dialog - will be handled by the payment screen component
        this.store.currentTransaction = new NexiTransaction({
            amount: amount,
            state: 'pending'
        });
        
        // Trigger dialog display
        this.pos.showScreen("NexiPaymentDialog", { 
            amount: amount,
            service: this
        });
    }

    /**
     * Start NEXI payment flow
     * @param {number} terminalId - Selected terminal ID
     * @param {number} amount - Payment amount
     * @returns {Promise<Object>}
     */
    async startNexiPayment(terminalId, amount) {
        const order = this.pos.get_order();
        if (!order) {
            throw new Error(_t("No active order"));
        }

        this.store.selectTerminal(terminalId);
        this.store.startProcessing();

        try {
            // Initiate payment on server
            const result = await rpc("/pos_nexi/payment/initiate", {
                order_id: order.uid,
                amount: amount,
                terminal_id: terminalId,
                currency_id: this.pos.currency.id,
                pos_session_id: this.pos.pos_session.id,
                config_id: this.pos.config.id
            });

            if (result.success) {
                // Create transaction from response
                const transaction = NexiTransaction.fromServer(result.transaction);
                this.store.setTransaction(transaction);
                
                // Start polling for status
                this.startPolling(transaction.external_id);
                
                return { success: true, transaction };
            } else {
                throw new Error(result.error || _t("Error initializing payment"));
            }
        } catch (error) {
            this.store.stopProcessing();
            this.handlePaymentError(error);
            return { success: false, error: error.message };
        }
    }

    /**
     * Show waiting screen during payment processing
     */
    showWaitingScreen() {
        this.store.showWaitingScreen = true;
    }

    /**
     * Update payment status in UI
     * @param {string} status - Transaction status
     */
    updatePaymentStatus(status) {
        if (this.store.currentTransaction) {
            this.store.currentTransaction.state = status;
        }
    }

    /**
     * Poll transaction status from server
     * @param {string} externalId - External transaction ID
     */
    startPolling(externalId) {
        this.store.pollCount = 0;
        
        // Set overall timeout
        this.timeoutTimer = browser.setTimeout(() => {
            this.handleTimeout();
        }, MAX_POLLING_TIME);

        // Start polling
        this.pollingTimer = browser.setInterval(async () => {
            this.store.pollCount++;
            
            try {
                const result = await this.pollTransactionStatus(externalId);
                
                if (result.success) {
                    const transaction = NexiTransaction.fromServer(result.transaction);
                    this.store.setTransaction(transaction);

                    // Check if transaction reached final state
                    if (transaction.isFinal()) {
                        this.stopPolling();
                        
                        if (transaction.isSuccessful()) {
                            await this.handlePaymentSuccess(transaction);
                        } else {
                            this.handlePaymentError(
                                new Error(transaction.error_message || _t("Payment failed"))
                            );
                        }
                    }
                }
            } catch (error) {
                console.error("Polling error:", error);
                // Continue polling on error, don't stop immediately
                if (this.store.pollCount > 10) {
                    this.stopPolling();
                    this.handlePaymentError(error);
                }
            }
        }, POLLING_INTERVAL);
    }

    /**
     * Stop polling
     */
    stopPolling() {
        if (this.pollingTimer) {
            browser.clearInterval(this.pollingTimer);
            this.pollingTimer = null;
        }
        if (this.timeoutTimer) {
            browser.clearTimeout(this.timeoutTimer);
            this.timeoutTimer = null;
        }
    }

    /**
     * Poll transaction status from server
     * @param {string} externalId - External transaction ID
     * @returns {Promise<Object>}
     */
    async pollTransactionStatus(externalId) {
        return await rpc("/pos_nexi/payment/status", {
            external_id: externalId
        });
    }

    /**
     * Handle successful payment
     * @param {NexiTransaction} transaction
     */
    async handlePaymentSuccess(transaction) {
        this.stopPolling();
        this.store.stopProcessing();
        
        // Add payment to order
        await this.completePayment(transaction);
        
        // Show success result
        this.store.showResult(true);
        
        // Show notification
        this.notification.add(
            _t("NEXI payment completed successfully!"),
            { type: "success" }
        );
    }

    /**
     * Handle payment error
     * @param {Error} error
     */
    handlePaymentError(error) {
        this.stopPolling();
        this.store.stopProcessing();
        
        let errorMessage = this.getUserFriendlyError(error.message);
        this.store.showResult(false, errorMessage);
        
        console.error("NEXI Payment Error:", error);
    }

    /**
     * Convert technical errors to user-friendly messages
     * @param {string} technicalError
     * @returns {string}
     */
    getUserFriendlyError(technicalError) {
        const errorMap = {
            'terminal_offline': _t("The terminal is offline. Please verify it is powered on and connected."),
            'terminal_busy': _t("The terminal is busy with another transaction. Please wait and try again."),
            'payment_declined': _t("Payment declined by the issuing bank."),
            'payment_timeout': _t("Payment timeout. The customer did not complete the operation on the terminal."),
            'connection_error': _t("Connection error. Please check the network connection."),
            'invalid_amount': _t("Invalid amount. Please verify the order amount."),
            'transaction_not_found': _t("Transaction not found. Please contact technical support."),
        };

        // Check for known error codes in the message
        for (const [code, message] of Object.entries(errorMap)) {
            if (technicalError && technicalError.toLowerCase().includes(code)) {
                return message;
            }
        }

        // Default error message
        return _t("An error occurred during payment. Please try again or use another payment method.");
    }

    /**
     * Complete payment by adding it to the order
     * @param {NexiTransaction} transaction
     */
    async completePayment(transaction) {
        const order = this.pos.get_order();
        if (!order) return;

        // Find NEXI payment method
        const nexiPaymentMethod = this.pos.payment_methods.find(
            pm => pm.use_payment_terminal === 'nexi' || pm.is_nexi_payment
        );

        if (!nexiPaymentMethod) {
            console.error("NEXI payment method not found");
            return;
        }

        // Add payment line to order
        const paymentLine = order.add_paymentline(nexiPaymentMethod);
        if (paymentLine) {
            paymentLine.set_amount(transaction.amount);
            paymentLine.set_payment_status('done');
            paymentLine.transaction_id = transaction.id;
            paymentLine.transaction_ref = transaction.reference || transaction.transaction_ref;
            paymentLine.auth_code = transaction.authorization_code || transaction.auth_code;
            // Card details
            paymentLine.card_type = transaction.card_scheme || '';
            paymentLine.nexi_authorization_code = transaction.authorization_code || '';
            paymentLine.nexi_card_number = transaction.card_number_customer || '';
            paymentLine.nexi_card_entry_mode = transaction.card_entry_mode || '';
            paymentLine.nexi_reference = transaction.reference || '';
            paymentLine.nexi_receipt_customer = transaction.receipt_details_customer || '';
            paymentLine.nexi_receipt_merchant = transaction.receipt_details_merchant || '';
        }
    }

    /**
     * Handle payment timeout
     */
    handleTimeout() {
        this.stopPolling();
        this.store.stopProcessing();
        this.store.showResult(false, _t("Payment timeout. The terminal operation took too long."));
        
        // Try to cancel the transaction on server
        if (this.store.currentTransaction?.external_id) {
            this.cancelPayment(this.store.currentTransaction.external_id).catch(console.error);
        }
    }

    /**
     * Handle user cancellation
     */
    async handleCancellation() {
        this.stopPolling();
        
        if (this.store.currentTransaction?.external_id) {
            await this.cancelPayment(this.store.currentTransaction.external_id);
        }
        
        this.store.reset();
    }

    /**
     * Cancel payment on server
     * @param {string} externalId
     * @returns {Promise<Object>}
     */
    async cancelPayment(externalId) {
        try {
            const result = await rpc("/pos_nexi/payment/cancel", {
                external_id: externalId
            });
            return result;
        } catch (error) {
            console.error("Failed to cancel payment:", error);
            return { success: false, error: error.message };
        }
    }

    /**
     * Retry failed payment
     */
    async retryPayment() {
        const transaction = this.store.currentTransaction;
        if (transaction && this.store.selectedTerminal) {
            this.store.reset();
            await this.startNexiPayment(this.store.selectedTerminal.id, transaction.amount);
        }
    }

    /**
     * Close payment dialog and cleanup
     */
    closePaymentDialog() {
        this.stopPolling();
        this.store.reset();
    }
}

/**
 * Component class for NEXI Payment Button
 * Used in payment screen
 */
export class NexiPaymentButton {
    /**
     * Create button configuration for payment screen
     * @param {Object} paymentMethod - Payment method record
     * @param {NexiPaymentService} service - Payment service instance
     * @returns {Object} Button configuration
     */
    static createButtonConfig(paymentMethod, service) {
        return {
            id: 'nexi_payment_button',
            name: _t("NEXI"),
            icon: 'fa-credit-card',
            className: 'nexi-payment-button',
            onClick: async (amount) => {
                await service.showNexiPaymentDialog(amount);
            }
        };
    }

    /**
     * Check if NEXI payment method is available
     * @param {Object} pos - POS store
     * @returns {boolean}
     */
    static isAvailable(pos) {
        return pos.payment_methods.some(
            pm => pm.use_payment_terminal === 'nexi' || pm.is_nexi_payment
        );
    }
}
