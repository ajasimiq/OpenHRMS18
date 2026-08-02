# -*- coding: utf-8 -*-
#############################################################################
#    A part of Open HRMS Project <https://www.openhrms.com>
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2025-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Cybrosys Techno Solutions(<https://www.cybrosys.com>)
#
#    You can modify it under the terms of the GNU LESSER
#    GENERAL PUBLIC LICENSE (LGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU LESSER GENERAL PUBLIC LICENSE (LGPL v3) for more details.
#
#    You should have received a copy of the GNU LESSER GENERAL PUBLIC LICENSE
#    (LGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
#############################################################################
from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

date_format = "%Y-%m-%d"
RESIGNATION_TYPE = [('resigned', 'Normal Resignation'),
                    ('fired', 'Fired by the company')]


class HrResignation(models.Model):
    """ Model for HR Resignations. This model is used to track employee
        resignations."""
    _name = 'hr.resignation'
    _description = 'HR Resignation'
    _inherit = 'mail.thread'
    _rec_name = 'employee_id'

    name = fields.Char(string='Order Reference', copy=False,
                       readonly=True, index=True,
                       default=lambda self: _('New'))
    employee_id = fields.Many2one('hr.employee', string="Employee",
                                  default=lambda
                                      self: self.env.user.employee_id.id,
                                  help='Name of the employee for '
                                       'whom the request is creating')
    department_id = fields.Many2one('hr.department', string="Department",
                                    related='employee_id.department_id',
                                    help='Department of the employee')
    resign_confirm_date = fields.Date(string="Confirmed Date",
                                      help='Date on which the request '
                                           'is confirmed by the employee.',
                                      tracking=True)
    approved_revealing_date = fields.Date(
        string="Approved Last Day Of Employee",
        help='Date on which the request is confirmed by the manager.',
        tracking=True)
    joined_date = fields.Date(string="Join Date",
                              help='Joining date of the employee.'
                                   'i.e Start date of the first contract')
    expected_revealing_date = fields.Date(string="Last Day of Employee",
                                          required=True,
                                          help='Employee requested date on '
                                               'which employee is revealing '
                                               'from the company.')
    reason = fields.Text(string="Reason", required=True,
                         help='Specify reason for leaving the company')
    notice_period = fields.Integer(string="Notice Period",
                                compute="_compute_notice_period",
                                help="Notice Period of the employee in days")
    state = fields.Selection(
        [('draft', 'Draft'), ('confirm', 'Confirm'), ('approved', 'Approved'),
         ('cancel', 'Rejected')],
        string='Status', default='draft', tracking=True)
    resignation_type = fields.Selection(selection=RESIGNATION_TYPE,
                                        help="Select the type of resignation: "
                                             "normal resignation or "
                                             "fired by the company")
    change_employee = fields.Boolean(string="Change Employee",
                                     compute="_compute_change_employee",
                                     help="Checks , if the user has permission"
                                          " to change the employee")
    employee_contract = fields.Char(
        string="Contract Template",
        compute="_compute_notice_period",
        store=True,
        help="Current Contract of the employee"
    )

    @api.depends('employee_id')
    def _compute_change_employee(self):
        """ Check whether the user has the permission to change the employee"""
        res_user = self.env.user
        self.change_employee = res_user.has_group('hr.group_hr_user')

    @api.constrains('employee_id')
    def _check_employee_id(self):
        """ Constraint method to check if the current user has the permission
             to create a resignation request for the specified employee.
        """
        for resignation in self:
            if not self.env.user.has_group('hr.group_hr_user'):
                if (resignation.employee_id.user_id.id and
                        resignation.employee_id.user_id.id != self.env.uid):
                    raise ValidationError(
                        _('You cannot create a request for other employees'))

    @api.constrains('employee_id')
    def _check_joined_date(self):
        """ Check if there is an active resignation request for the
            same employee with a confirmed or approved state, based on the
            'joined_date' of the current resignation."""
        for resignation in self:
            resignation_request = self.env['hr.resignation'].search(
                [('employee_id', '=', resignation.employee_id.id),
                 ('state', 'in', ['confirm', 'approved'])])
            if resignation_request:
                raise ValidationError(
                    _('There is a resignation request in confirmed or'
                      ' approved state for this employee'))

    @api.depends(
        'employee_id',
        'employee_id.joining_date',
        'employee_id.version_id.date_start',
        'employee_id.version_id.date_end'
    )
    def _compute_notice_period(self):
        """Compute notice period for each resignation."""
        today = fields.Date.today()
        for rec in self:
            rec.joined_date = rec.employee_id.joining_date if rec.employee_id else False
            rec.employee_contract = False
            rec.notice_period = 0

            if rec.employee_id:
                contract = self.env['hr.version'].sudo().search([
                    ('employee_id', '=', rec.employee_id.id),
                    '|', ('date_start', '=', False),
                    ('date_start', '<=', today),
                    '|', ('date_end', '=', False),
                    ('date_end', '>=', today),
                ], limit=1)

                if contract:
                    rec.employee_contract = contract.contract_template_id.name
                    rec.notice_period = contract.notice_days

    @api.model_create_multi
    def create(self, vals_list):
        """Override of the create method to assign a sequence for the record."""
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'hr.resignation') or _('New')
        return super(HrResignation, self).create(vals_list)

    def action_confirm_resignation(self):
        """ Method triggered by the 'Confirm' button to confirm the
        resignation request."""
        for resignation in self:
            if resignation.joined_date:
                if (resignation.joined_date >=
                        resignation.expected_revealing_date):
                    raise ValidationError(
                        _('Last date of the Employee must '
                          'be anterior to Joining date'))
            else:
                raise ValidationError(
                    _('Please set a Joining Date for employee'))
            resignation.state = 'confirm'
            resignation.resign_confirm_date = str(fields.Datetime.now())

    def action_cancel_resignation(self):
        """ Method triggered by the 'Cancel' button to cancel the resignation
            request."""
        for resignation in self:
            resignation.state = 'cancel'

    def action_reject_resignation(self):
        """ Method triggered by the 'Reject' button to reject the
            resignation request."""
        for resignation in self:
            resignation.state = 'cancel'

    def action_reset_to_draft(self):
        """ Method triggered by the 'Set to Draft' button to reset the
        resignation request to the 'draft' state."""
        for resignation in self:
            resignation.state = 'draft'
            resignation.employee_id.active = True
            resignation.employee_id.resigned = False
            resignation.employee_id.fired = False

    def action_approve_resignation(self):
        """ Method triggered by the 'Approve' button to
               approve the resignation."""
        for resignation in self:
            if (resignation.expected_revealing_date and
                    resignation.resign_confirm_date):
                employee_contract = self.env['hr.version'].sudo().search(
                    [('employee_id', '=', self.employee_id.id)])
                if not employee_contract:
                    raise ValidationError(
                        _("There are no Contracts found for this employee"))
                for contract in employee_contract:
                    resignation.state = 'approved'
                    resignation.approved_revealing_date = (
                            resignation.resign_confirm_date + timedelta(
                        days=contract.notice_days))

                # Changing state of the employee if resigning today
                if resignation.expected_revealing_date <= fields.Date.today():
                    resignation._process_employee_departure(
                        resignation.expected_revealing_date)
            else:
                raise ValidationError(_('Please Enter Valid Dates.'))

    def _get_departure_reason(self):
        """ Return the hr.departure.reason matching the resignation type.

            Resolved through the standard external id first: 'name' is a
            translated field, so matching on the English label alone fails on
            a non-English database. Falls back to a name search for databases
            where the reason records were re-created by hand."""
        self.ensure_one()
        if self.resignation_type == 'fired':
            xml_id, label = 'hr.departure_fired', 'Fired'
        else:
            xml_id, label = 'hr.departure_resigned', 'Resigned'
        reason = self.env.ref(xml_id, raise_if_not_found=False)
        if not reason:
            reason = self.env['hr.departure.reason'].search(
                [('name', '=', label)], limit=1)
        return reason

    def _process_employee_departure(self, revealing_date):
        """ Apply an approved resignation to the employee record.

            Shared by the 'Approve' button and by the daily
            'update_employee_status' cron so that both offboard an employee
            in exactly the same way. Returns True when the employee was
            archived, False when there was nothing left to do."""
        self.ensure_one()
        employee = self.employee_id
        if not employee or not employee.active:
            return False
        today = fields.Date.today()
        employee.active = False
        # Changing fields in the employee table with respect to resignation
        employee.resign_date = revealing_date
        if self.resignation_type == 'fired':
            employee.fired = True
        else:
            employee.resigned = True
        # Odoo 19 replaced hr.contract with hr.version and dropped the
        # draft/open/close state machine entirely, so a contract can no longer
        # be "closed". The running version is the one whose date window covers
        # today, and ending employment means stamping its contract end date.
        # contract_date_end is the stored, writable field (date_end is
        # computed) and is restricted to HR managers, hence the sudo.
        running_versions = self.env['hr.version'].sudo().search([
            ('employee_id', '=', employee.id),
            '|', ('date_start', '=', False), ('date_start', '<=', today),
            '|', ('date_end', '=', False), ('date_end', '>=', today),
        ])
        for version in running_versions:
            if not version.contract_date_end:
                version.contract_date_end = revealing_date
        employee.departure_reason_id = self._get_departure_reason()
        employee.departure_date = self.approved_revealing_date or revealing_date
        # Removing and deactivating user
        if employee.user_id:
            employee.user_id.active = False
            employee.user_id = False
        return True

    @api.model
    def update_employee_status(self):
        """ Daily cron: offboard employees whose last working day has arrived.

            Approving a resignation only archives the employee when the last
            working day has already passed. Every other approved request is
            picked up here once that day is reached, which is what the
            'HR Resignation: Update Employee Status' cron defined in
            data/ir_cron_data.xml calls. Returns the resignations processed."""
        today = fields.Date.today()
        processed = self.browse()
        for resignation in self.search([('state', '=', 'approved')]):
            employee = resignation.employee_id
            if not employee or not employee.active:
                continue
            revealing_date = (resignation.approved_revealing_date or
                              resignation.expected_revealing_date)
            if not revealing_date or revealing_date > today:
                continue
            if resignation._process_employee_departure(revealing_date):
                processed |= resignation
        return processed
