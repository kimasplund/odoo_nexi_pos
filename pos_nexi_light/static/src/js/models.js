/** @odoo-module **/

import { register_payment_method } from "@point_of_sale/app/store/pos_store";
import { PaymentNexi } from "./payment_nexi";
import { PosPayment } from "@point_of_sale/app/models/pos_payment";
import { patch } from "@web/core/utils/patch";

register_payment_method("nexi", PaymentNexi);

// Extend PosPayment to serialize NEXI card detail fields
patch(PosPayment.prototype, {
    setup() {
        this._super(...arguments);
        this.transaction_id = this.transaction_id || '';
        this.nexi_external_id = this.nexi_external_id || '';
        this.card_type = this.card_type || '';
        this.nexi_authorization_code = this.nexi_authorization_code || '';
        this.nexi_card_number = this.nexi_card_number || '';
        this.nexi_card_entry_mode = this.nexi_card_entry_mode || '';
        this.nexi_reference = this.nexi_reference || '';
        this.nexi_receipt_customer = this.nexi_receipt_customer || '';
        this.nexi_receipt_merchant = this.nexi_receipt_merchant || '';
    },

    export_as_JSON() {
        const json = this._super(...arguments);
        json.transaction_id = this.transaction_id || '';
        json.nexi_external_id = this.nexi_external_id || '';
        json.card_type = this.card_type || '';
        json.nexi_authorization_code = this.nexi_authorization_code || '';
        json.nexi_card_number = this.nexi_card_number || '';
        json.nexi_card_entry_mode = this.nexi_card_entry_mode || '';
        json.nexi_reference = this.nexi_reference || '';
        json.nexi_receipt_customer = this.nexi_receipt_customer || '';
        json.nexi_receipt_merchant = this.nexi_receipt_merchant || '';
        return json;
    },

    init_from_JSON(json) {
        this._super(...arguments);
        this.transaction_id = json.transaction_id || '';
        this.nexi_external_id = json.nexi_external_id || '';
        this.card_type = json.card_type || '';
        this.nexi_authorization_code = json.nexi_authorization_code || '';
        this.nexi_card_number = json.nexi_card_number || '';
        this.nexi_card_entry_mode = json.nexi_card_entry_mode || '';
        this.nexi_reference = json.nexi_reference || '';
        this.nexi_receipt_customer = json.nexi_receipt_customer || '';
        this.nexi_receipt_merchant = json.nexi_receipt_merchant || '';
    },
});
