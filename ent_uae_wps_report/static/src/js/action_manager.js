/** @odoo-module */
import { registry } from "@web/core/registry";
import { download } from "@web/core/network/download";

registry.category("ir.actions.report handlers").add('xlsx', async function(action, option, env) {
    if (action.report_type === 'wps_xlsx') {
        await download({
            url: '/xlsx_reports',
            data: action.data,
        });
    }
});
