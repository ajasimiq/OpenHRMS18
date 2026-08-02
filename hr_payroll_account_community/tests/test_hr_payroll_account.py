# -*- coding: utf-8 -*-
###############################################################################
#
#    Cybrosys Technologies Pvt. Ltd.
#
#    Copyright (C) 2024-TODAY Cybrosys Technologies(<https://www.cybrosys.com>)
#    Author: Akhil Ashok (odoo@cybrosys.com)
#
#    You can modify it under the terms of the GNU AFFERO
#    GENERAL PUBLIC LICENSE (AGPL v3), Version 3.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU AFFERO GENERAL PUBLIC LICENSE (AGPL v3) for more details.
#
#    You should have received a copy of the GNU AFFERO GENERAL PUBLIC LICENSE
#    (AGPL v3) along with this program.
#    If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
"""Tests for hr_payroll_account_community.

Rewritten to be self-contained. The previous version could not even be
imported on Odoo 18, and could not have run if it had:

* it loaded ``account/test/account_minimal_test.xml``, a fixture removed
  from Odoo years ago, through a ``tools.convert_file`` signature that no
  longer exists;
* it then referenced ``hr_payroll_account_community.expenses_journal``,
  an xmlid that fixture used to provide and which is defined nowhere in
  this repo;
* it leaned on demo-only records (``base.res_partner_12``, ``hr.dep_rd``,
  ``hr_contract.hr_contract_type_emp``) that are absent from any database
  installed without demo data;
* and it called ``hr.payslip.compute_sheet()``, which does not exist --
  the method is ``action_compute_sheet()``.

So this suite builds everything it needs: accounts, a journal, a salary
rule carrying debit/credit accounts, a structure, an employee and a
contract. It depends on no demo data and no chart of accounts, which
matters because a payroll database frequently has neither.
"""
from datetime import date
from dateutil.relativedelta import relativedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHrPayrollAccount(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.today = fields.Date.today()
        cls.date_from = cls.today.replace(day=1)
        cls.date_to = cls.date_from + relativedelta(months=1, days=-1)

        cls.account_expense = cls._make_account('Salary Expense', 'TSTEXP',
                                                'expense')
        cls.account_payable = cls._make_account('Salary Payable', 'TSTPAY',
                                                'liability_current')
        cls.account_adjust = cls._make_account('Payroll Adjustment', 'TSTADJ',
                                               'liability_current')

        cls.journal = cls.env['account.journal'].create({
            'name': 'Salary Journal',
            'code': 'TSLR',
            'type': 'general',
            'company_id': cls.company.id,
            # action_payslip_done falls back to this when debit and credit
            # do not balance; without it the posting raises UserError.
            'default_account_id': cls.account_adjust.id,
        })

        cls.category = cls.env['hr.salary.rule.category'].create({
            'name': 'Test Basic',
            'code': 'TSTBASIC',
        })
        cls.structure = cls._make_structure('Structure With Accounts', 'TSTWA',
                                            with_accounts=True)
        cls.structure_no_accounts = cls._make_structure(
            'Structure Without Accounts', 'TSTNA', with_accounts=False)

        cls.employee = cls.env['hr.employee'].create({
            'name': 'John Payroll',
            'company_id': cls.company.id,
        })
        cls.contract = cls._make_contract(cls.employee, cls.structure)

        # A second employee/contract pair on the accountless structure.
        # The negative test has to drive that structure from the *contract*:
        # hr_payroll_community builds payslip lines from the contract's
        # structure, so setting only the payslip's struct_id is silently
        # ignored and the accountless case would never be exercised.
        cls.employee_plain = cls.env['hr.employee'].create({
            'name': 'Jane NoAccounts',
            'company_id': cls.company.id,
        })
        cls.contract_plain = cls._make_contract(cls.employee_plain,
                                                cls.structure_no_accounts)

    # -- fixtures ----------------------------------------------------

    @classmethod
    def _make_account(cls, name, code, account_type):
        """Odoo 18 shares accounts across companies via company_ids."""
        return cls.env['account.account'].create({
            'name': name,
            'code': code,
            'account_type': account_type,
            'company_ids': [(6, 0, [cls.company.id])],
        })

    @classmethod
    def _make_structure(cls, name, code, with_accounts):
        rule_vals = {
            'name': '%s Rule' % name,
            'code': code,
            'sequence': 1,
            'category_id': cls.category.id,
            'condition_select': 'none',
            'amount_select': 'code',
            'amount_python_compute': 'result = contract.wage',
        }
        if with_accounts:
            rule_vals.update({
                'account_debit_id': cls.account_expense.id,
                'account_credit_id': cls.account_payable.id,
            })
        rule = cls.env['hr.salary.rule'].create(rule_vals)
        return cls.env['hr.payroll.structure'].create({
            'name': name,
            'code': code,
            'company_id': cls.company.id,
            # parent_id defaults to an existing base structure, and payslip
            # lines are built from the whole parent chain. Pinning it to False
            # keeps these payslips to this structure's own rule, so the tests
            # do not silently depend on which base structure (saudi_gosi's, in
            # this database) happens to be installed.
            'parent_id': False,
            'rule_ids': [(6, 0, [rule.id])],
        })

    @classmethod
    def _make_contract(cls, employee, structure):
        return cls.env['hr.contract'].create({
            'name': 'Contract for %s' % employee.name,
            'employee_id': employee.id,
            'company_id': cls.company.id,
            'wage': 5000.0,
            'date_start': cls.date_from - relativedelta(years=1),
            'date_end': cls.today + relativedelta(days=365),
            'struct_id': structure.id,
            'journal_id': cls.journal.id,
            'state': 'open',
        })

    def _make_payslip(self, contract=None):
        contract = contract or self.contract
        return self.env['hr.payslip'].create({
            'name': 'Payslip for %s' % contract.employee_id.name,
            'employee_id': contract.employee_id.id,
            'contract_id': contract.id,
            'struct_id': contract.struct_id.id,
            'journal_id': self.journal.id,
            'date_from': self.date_from,
            'date_to': self.date_to,
        })

    # -- lifecycle ---------------------------------------------------

    def test_payslip_lifecycle_creates_accounting_entry(self):
        """draft -> compute -> cancel -> draft -> done, with a posted move."""
        payslip = self._make_payslip()
        self.assertEqual(payslip.state, 'draft')

        payslip.action_compute_sheet()
        self.assertTrue(payslip.line_ids,
                        "computing the sheet should produce payslip lines")

        payslip.action_payslip_cancel()
        self.assertEqual(payslip.state, 'cancel')

        payslip.action_payslip_draft()
        self.assertEqual(payslip.state, 'draft')

        payslip.action_compute_sheet()
        payslip.action_payslip_done()

        self.assertEqual(payslip.state, 'done')
        self.assertTrue(payslip.move_id,
                        "accounting entry has not been created")
        self.assertEqual(payslip.move_id.state, 'posted')

    def test_move_lines_use_the_rule_accounts(self):
        payslip = self._make_payslip()
        payslip.action_compute_sheet()
        payslip.action_payslip_done()

        accounts = payslip.move_id.line_ids.mapped('account_id')
        self.assertIn(self.account_expense, accounts)
        self.assertIn(self.account_payable, accounts)

    def test_move_is_balanced(self):
        payslip = self._make_payslip()
        payslip.action_compute_sheet()
        payslip.action_payslip_done()

        lines = payslip.move_id.line_ids
        self.assertAlmostEqual(sum(lines.mapped('debit')),
                               sum(lines.mapped('credit')), places=2,
                               msg="the generated entry must balance")

    def test_cancelling_a_done_payslip_removes_the_move(self):
        payslip = self._make_payslip()
        payslip.action_compute_sheet()
        payslip.action_payslip_done()
        move = payslip.move_id
        self.assertTrue(move.exists())

        payslip.action_payslip_cancel()

        self.assertEqual(payslip.state, 'cancel')
        self.assertFalse(move.exists(),
                         "cancelling must unlink the accounting entry")

    def test_done_without_configured_accounts_is_refused(self):
        """The module's own guard: no rule carries debit/credit accounts."""
        payslip = self._make_payslip(contract=self.contract_plain)
        payslip.action_compute_sheet()
        self.assertFalse(
            payslip.line_ids.mapped('salary_rule_id.account_debit_id'),
            "this fixture must not reach any account, or the guard below "
            "would not be what is being tested")

        with self.assertRaises(UserError):
            payslip.action_payslip_done()

    # -- journal wiring ----------------------------------------------

    def test_payslip_defaults_to_a_general_journal(self):
        default = self.env['hr.payslip'].default_get(['journal_id'])
        self.assertTrue(default.get('journal_id'),
                        "a general journal should be picked by default")

    def test_journal_follows_the_contract(self):
        other_journal = self.env['account.journal'].create({
            'name': 'Other Salary Journal',
            'code': 'TSLR2',
            'type': 'general',
            'company_id': self.company.id,
            'default_account_id': self.account_adjust.id,
        })
        self.contract.journal_id = other_journal

        payslip = self.env['hr.payslip'].new({
            'employee_id': self.employee.id,
            'contract_id': self.contract.id,
        })
        payslip.onchange_contract_id()

        self.assertEqual(payslip.journal_id, other_journal,
                         "the payslip journal should follow its contract")

    def test_create_honours_journal_id_from_context(self):
        """The batch wizard passes the journal down through the context."""
        payslip = self.env['hr.payslip'].with_context(
            journal_id=self.journal.id).create({
                'name': 'Context Payslip',
                'employee_id': self.employee.id,
                'contract_id': self.contract.id,
                'struct_id': self.structure.id,
                'date_from': self.date_from,
                'date_to': self.date_to,
            })
        self.assertEqual(payslip.journal_id, self.journal)
