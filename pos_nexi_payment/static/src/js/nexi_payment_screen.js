/** @odoo-module **/

/**
 * NEXI Payment Screen Extensions
 * Extends Odoo POS Payment Screen with NEXI-specific functionality
 */

import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { PaymentScreenStatus } from "@point_of_sale/app/screens/payment_screen/payment_screen_status/payment_screen_status";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { Component, useState, onMounted, onWillUnmount } from "@odoo/owl";
import { NexiPaymentService, NexiPaymentButton } from "./nexi_payment_button";
import { nexiStore } from "./nexi_models";
import { NexiTerminalStatus } from "./nexi_terminal_status";

/**
 * Patch PaymentScreen to add NEXI functionality
 */
patch(PaymentScreen.prototype, {
    setup() {
        this._super(...arguments);
        
        // Initialize NEXI service
        this.nexiService = new NexiPaymentService(
            this.pos,
            this.dialog,
            this.notification
        );
        
        // Reactive state for NEXI UI
        this.nexiState = useState(nexiStore);
        
        // Local UI state
        this.nexiUI = useState({
            showDialog: false,
            showWaiting: false,
            showResult: false,
            dialogData: {
                amount: 0,
                selectedTerminal: null
            }
        });

        onMounted(() => {
            // Load terminals on mount
            this.nexiService.loadTerminals();
        });

        onWillUnmount(() => {
            // Cleanup
            this.nexiService.closePaymentDialog();
        });
    },

    /**
     * Check if NEXI payment is available
     */
    get hasNexiPaymentMethod() {
        return NexiPaymentButton.isAvailable(this.pos);
    },

    /**
     * Get NEXI payment method
     */
    get nexiPaymentMethod() {
        return this.pos.payment_methods.find(
            pm => pm.use_payment_terminal === 'nexi' || pm.is_nexi_payment
        );
    },

    /**
     * Get remaining amount to pay
     */
    get remainingAmount() {
        const order = this.pos.get_order();
        return order ? order.get_due() : 0;
    },

    /**
     * Override to add NEXI button click handler
     */
    async addNewPaymentLine(paymentMethod) {
        if (paymentMethod.use_payment_terminal === 'nexi' || paymentMethod.is_nexi_payment) {
            // Show NEXI payment dialog instead of adding line immediately
            await this.showNexiPaymentDialog();
            return;
        }
        return this._super(...arguments);
    },

    /**
     * Show NEXI payment dialog
     */
    async showNexiPaymentDialog() {
        const amount = Math.abs(this.remainingAmount);
        if (amount <= 0) {
            this.notification.add(_t("The order has already been paid"), { type: "warning" });
            return;
        }

        // Load terminals
        await this.nexiService.loadTerminals();
        
        if (!this.nexiState.hasAvailableTerminal()) {
            this.notification.add(
                _t("No NEXI terminal available. Please verify that terminals are powered on."),
                { type: "danger" }
            );
            return;
        }

        this.nexiUI.dialogData.amount = amount;
        this.nexiUI.showDialog = true;
    },

    /**
     * Start NEXI payment
     */
    async startNexiPayment(terminalId) {
        this.nexiUI.showDialog = false;
        this.nexiUI.showWaiting = true;

        const result = await this.nexiService.startNexiPayment(
            terminalId,
            this.nexiUI.dialogData.amount
        );

        if (!result.success) {
            this.nexiUI.showWaiting = false;
            this.nexiUI.showResult = true;
        }
    },

    /**
     * Cancel NEXI payment
     */
    async cancelNexiPayment() {
        await this.nexiService.handleCancellation();
        this.nexiUI.showDialog = false;
        this.nexiUI.showWaiting = false;
        this.nexiUI.showResult = false;
    },

    /**
     * Retry NEXI payment
     */
    async retryNexiPayment() {
        this.nexiUI.showResult = false;
        await this.nexiService.retryPayment();
    },

    /**
     * Close NEXI result and continue
     */
    closeNexiResult() {
        this.nexiUI.showResult = false;
        this.nexiService.closePaymentDialog();
        
        // Check if payment was successful
        if (this.nexiState.currentTransaction?.isSuccessful()) {
            // Payment complete, refresh payment lines display
            this.render();
        }
    },

    /**
     * Get formatted amount for display
     */
    get formattedNexiAmount() {
        return this.env.utils.formatCurrency(this.nexiUI.dialogData.amount);
    },

    /**
     * Select terminal in dialog
     */
    selectTerminal(terminalId) {
        this.nexiState.selectTerminal(terminalId);
        this.nexiUI.dialogData.selectedTerminal = this.nexiState.selectedTerminal;
    },

    /**
     * Get current polling time display
     */
    get pollingTimeDisplay() {
        const elapsed = this.nexiState.pollCount * 2; // 2 seconds per poll
        const minutes = Math.floor(elapsed / 60);
        const seconds = elapsed % 60;
        return `${minutes}:${seconds.toString().padStart(2, '0')}`;
    }
});

/**
 * Patch PaymentScreenStatus to add terminal indicator
 */
patch(PaymentScreenStatus.prototype, {
    setup() {
        this._super(...arguments);
        this.nexiState = useState(nexiStore);
    },

    /**
     * Get terminal status for display
     */
    get nexiTerminalStatus() {
        if (!this.nexiState.terminals.length) return null;
        
        const onlineCount = this.nexiState.terminals.filter(t => t.isAvailable()).length;
        return {
            total: this.nexiState.terminals.length,
            online: onlineCount,
            hasIssues: onlineCount < this.nexiState.terminals.length
        };
    }
});

/**
 * NEXI Payment Dialog Component
 */
export class NexiPaymentDialog extends Component {
    static template = "pos_nexi_payment.NexiPaymentDialog";
    static props = ["close", "service", "amount"];

    setup() {
        this.store = useState(nexiStore);
        this.state = useState({
            selectedTerminalId: null
        });

        onMounted(() => {
            if (this.store.selectedTerminal) {
                this.state.selectedTerminalId = this.store.selectedTerminal.id;
            }
        });
    }

    get availableTerminals() {
        return this.store.getAvailableTerminals();
    }

    get formattedAmount() {
        return this.env.utils.formatCurrency(this.props.amount);
    }

    onTerminalSelect(terminalId) {
        this.state.selectedTerminalId = terminalId;
        this.store.selectTerminal(terminalId);
    }

    async onStartPayment() {
        if (!this.state.selectedTerminalId) {
            return;
        }
        await this.props.service.startNexiPayment(
            this.state.selectedTerminalId,
            this.props.amount
        );
        this.props.close();
    }

    onCancel() {
        this.props.service.closePaymentDialog();
        this.props.close();
    }
}

/**
 * NEXI Waiting Screen Component
 */
export class NexiWaitingScreen extends Component {
    static template = "pos_nexi_payment.NexiWaitingScreen";
    static props = ["close", "service", "transaction"];

    setup() {
        this.store = useState(nexiStore);
        this.timerState = useState({
            elapsed: 0
        });

        this.timerInterval = setInterval(() => {
            this.timerState.elapsed += 1;
        }, 1000);

        onWillUnmount(() => {
            clearInterval(this.timerInterval);
        });
    }

    get formattedTime() {
        const minutes = Math.floor(this.timerState.elapsed / 60);
        const seconds = this.timerState.elapsed % 60;
        return `${minutes}:${seconds.toString().padStart(2, '0')}`;
    }

    get displayStatus() {
        const statusMap = {
            'pending': _t("Waiting for terminal response..."),
            'processing': _t("Processing payment on terminal..."),
            'awaiting_confirm': _t("Payment authorized, confirming..."),
        };
        return statusMap[this.store.currentTransaction?.state] ||
               _t("Processing payment...");
    }

    async onCancel() {
        await this.props.service.handleCancellation();
        this.props.close();
    }
}

/**
 * NEXI Result Screen Component
 */
export class NexiResultScreen extends Component {
    static template = "pos_nexi_payment.NexiPaymentResult";
    static props = ["close", "service", "success", "message", "transaction"];

    setup() {
        this.store = useState(nexiStore);
    }

    get isSuccess() {
        return this.props.success !== false && 
               this.store.currentTransaction?.isSuccessful();
    }

    get errorMessage() {
        return this.props.message || this.store.errorMessage;
    }

    get transactionData() {
        return this.props.transaction || this.store.currentTransaction;
    }

    get formattedAmount() {
        if (this.transactionData) {
            return this.env.utils.formatCurrency(this.transactionData.amount);
        }
        return "";
    }

    async onRetry() {
        await this.props.service.retryPayment();
        this.props.close();
    }

    async onClose() {
        // Auto-print to terminal on success (non-blocking)
        if (this.isSuccess) {
            await this._printToTerminal();
        }
        this.props.service.closePaymentDialog();
        this.props.close();
    }

    async onPrintReceipt() {
        // Trigger Odoo receipt print
        const order = this.env.pos.get_order();
        if (order) {
            await this.env.pos.printReceipt(order);
        }
        // Also print to NEXI terminal if enabled
        await this._printToTerminal();
    }

    async _printToTerminal() {
        const paymentMethod = this.env.pos.payment_methods.find(
            pm => (pm.use_payment_terminal === 'nexi' || pm.is_nexi_payment)
                  && pm.nexi_print_enabled
        );
        if (!paymentMethod) return;

        try {
            await rpc('/pos_nexi/terminal/print', {
                payment_method_id: paymentMethod.id,
                external_id: this.transactionData?.external_id,
            });
        } catch (e) {
            console.warn('NEXI terminal print failed:', e);
        }
    }
}

// Register components
if (!window.NEXI_COMPONENTS_REGISTERED) {
    window.NEXI_COMPONENTS_REGISTERED = true;
}
