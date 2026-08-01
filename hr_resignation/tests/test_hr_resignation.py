# -*- coding: utf-8 -*-
"""Tests for hr_resignation.

The cron 'HR Resignation: Update Employee Status' ran `model.
update_employee_status()` against a model that never defined the method, so
it raised AttributeError on every single daily run. These tests pin down the
implemented behaviour and keep the cron wired to a method that exists.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestHrResignation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.today()
        cls.company = cls.env.company

    def _make_employee(self, name, **values):
        return self.env['hr.employee'].create(dict({
            'name': name,
            'company_id': self.company.id,
        }, **values))

    def _make_contract(self, employee, state='open'):
        return self.env['hr.contract'].create({
            'name': 'Contract %s' % employee.name,
            'employee_id': employee.id,
            'company_id': self.company.id,
            'wage': 1000.0,
            'date_start': self.today - timedelta(days=365),
            'state': state,
        })

    def _make_resignation(self, employee, revealing_date=None, **values):
        """Create a draft resignation.

        Always created in draft: `_check_joined_date` looks for a confirmed or
        approved resignation for the employee and would match the record being
        created if it were born approved.
        """
        resignation = self.env['hr.resignation'].create(dict({
            'employee_id': employee.id,
            'expected_revealing_date': revealing_date or self.today,
            'joined_date': self.today - timedelta(days=365),
            'reason': 'Testing',
            'resignation_type': 'resigned',
        }, **values))
        return resignation

    def _approve(self, resignation, approved_date=None):
        """Move a draft resignation to approved without the button.

        action_approve_resignation also mutates the employee, which is exactly
        what these tests want to attribute to the cron instead.
        """
        resignation.write({
            'state': 'approved',
            'resign_confirm_date': self.today,
            'approved_revealing_date': (
                approved_date or resignation.expected_revealing_date),
        })
        return resignation

    # -- sequence / batch create -------------------------------------

    def test_create_assigns_sequence_for_every_record_in_a_batch(self):
        """create() must be batch-aware: one sequence per record, not one
        for the batch."""
        employee_a = self._make_employee('Batch A')
        employee_b = self._make_employee('Batch B')
        resignations = self.env['hr.resignation'].create([
            {
                'employee_id': employee_a.id,
                'expected_revealing_date': self.today,
                'reason': 'Testing A',
            },
            {
                'employee_id': employee_b.id,
                'expected_revealing_date': self.today,
                'reason': 'Testing B',
            },
        ])
        self.assertEqual(len(resignations), 2)
        for resignation in resignations:
            self.assertNotEqual(resignation.name, 'New')
            self.assertTrue(resignation.name)
        self.assertNotEqual(
            resignations[0].name, resignations[1].name,
            "each record in a batch must consume its own sequence number")

    # -- the cron itself ---------------------------------------------

    def test_cron_calls_a_method_that_exists(self):
        """Regression guard for the AttributeError that fired daily."""
        cron = self.env.ref('hr_resignation.ir_cron_employee_resignation')
        self.assertIn('update_employee_status', cron.code)
        self.assertTrue(
            hasattr(self.env['hr.resignation'], 'update_employee_status'),
            "the cron code references a method the model does not define")

    def test_cron_archives_employee_once_revealing_date_arrives(self):
        employee = self._make_employee('Leaver Today')
        self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        self.assertTrue(employee.active)
        processed = self.env['hr.resignation'].update_employee_status()

        self.assertIn(resignation, processed)
        self.assertFalse(employee.active, "employee should be archived")
        self.assertTrue(employee.resigned)
        self.assertFalse(employee.fired)
        self.assertEqual(employee.resign_date, self.today)
        self.assertEqual(employee.departure_date, self.today)

    def test_cron_leaves_future_departures_alone(self):
        employee = self._make_employee('Leaver Next Month')
        self._make_contract(employee)
        future = self.today + timedelta(days=30)
        resignation = self._make_resignation(employee, future)
        self._approve(resignation, future)

        processed = self.env['hr.resignation'].update_employee_status()

        self.assertNotIn(resignation, processed)
        self.assertTrue(employee.active, "employee must stay active until the "
                                         "approved last working day")

    def test_cron_ignores_resignations_that_are_not_approved(self):
        employee = self._make_employee('Still Draft')
        self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)

        processed = self.env['hr.resignation'].update_employee_status()

        self.assertNotIn(resignation, processed)
        self.assertTrue(employee.active)

    def test_cron_is_idempotent(self):
        """Running twice on the same day must not re-process anybody."""
        employee = self._make_employee('Leaver Idempotent')
        self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        first = self.env['hr.resignation'].update_employee_status()
        second = self.env['hr.resignation'].update_employee_status()

        self.assertIn(resignation, first)
        self.assertNotIn(resignation, second,
                         "an already-archived employee must not be processed "
                         "again on the next daily run")

    def test_cron_uses_approved_date_over_expected_date(self):
        """The manager-approved last day wins over the employee's request."""
        employee = self._make_employee('Leaver Extended')
        self._make_contract(employee)
        resignation = self._make_resignation(
            employee, self.today - timedelta(days=10))
        self._approve(resignation, self.today + timedelta(days=10))

        processed = self.env['hr.resignation'].update_employee_status()

        self.assertNotIn(resignation, processed)
        self.assertTrue(employee.active)

    def test_cron_sets_fired_flag_for_dismissals(self):
        employee = self._make_employee('Dismissed')
        self._make_contract(employee)
        resignation = self._make_resignation(
            employee, self.today, resignation_type='fired')
        self._approve(resignation)

        self.env['hr.resignation'].update_employee_status()

        self.assertTrue(employee.fired)
        self.assertFalse(employee.resigned)

    def test_cron_closes_running_contract(self):
        employee = self._make_employee('Leaver With Contract')
        contract = self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        self.env['hr.resignation'].update_employee_status()

        self.assertEqual(contract.state, 'close')

    def test_cron_deactivates_the_linked_user(self):
        user = self.env['res.users'].create({
            'name': 'Leaver User',
            'login': 'leaver.user@example.com',
        })
        employee = self._make_employee('Leaver User', user_id=user.id)
        self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        self.env['hr.resignation'].update_employee_status()

        self.assertFalse(user.active, "the login must be disabled on exit")
        self.assertFalse(employee.user_id)

    def test_cron_sets_the_departure_reason(self):
        employee = self._make_employee('Leaver Reason')
        self._make_contract(employee)
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        self.env['hr.resignation'].update_employee_status()

        expected = self.env.ref('hr.departure_resigned',
                                raise_if_not_found=False)
        if expected:
            self.assertEqual(employee.departure_reason_id, expected)
        else:
            self.assertTrue(employee.departure_reason_id)

    def test_cron_survives_an_employee_without_a_contract(self):
        """A missing contract must not abort the whole daily run."""
        employee = self._make_employee('No Contract')
        resignation = self._make_resignation(employee, self.today)
        self._approve(resignation)

        processed = self.env['hr.resignation'].update_employee_status()

        self.assertIn(resignation, processed)
        self.assertFalse(employee.active)
