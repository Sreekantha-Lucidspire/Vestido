/** @odoo-module **/
import { Component, onWillStart, onWillUpdateProps, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { deserializeDateTime, formatDateTime } from "@web/core/l10n/dates";
import { Chatter } from "@mail/chatter/web_portal/chatter";

export class WhatsappInfo extends Component {
    static template = "laundry_management.WhatsappInfo";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({ data: null });
        onWillStart(() => this.load(this.props.resId));
        onWillUpdateProps((p) => this.load(p.resId));
    }

    async load(resId) {
        if (!resId) {
            this.state.data = null;
            return;
        }
        const [rec] = await this.orm.read("laundry.order", [resId], [
            "whatsapp_status", "whatsapp_sent_datetime", "whatsapp_sent_by",
        ]);
        this.state.data = {
            status: rec.whatsapp_status || "",
            sentTime: rec.whatsapp_sent_datetime
                ? formatDateTime(deserializeDateTime(rec.whatsapp_sent_datetime))
                : "",
            sentBy: rec.whatsapp_sent_by ? rec.whatsapp_sent_by[1] : "",
        };
    }
}

patch(Chatter, {
    components: { ...Chatter.components, WhatsappInfo },
});