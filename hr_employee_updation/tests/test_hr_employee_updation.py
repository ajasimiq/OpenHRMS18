# -*- coding: utf-8 -*-
"""Tests for hr_employee_updation.

`expiry_mail_reminder` is run by the daily 'HR Employee Data Expiration'
cron. It built its mail body by concatenating `employee.identification_id`
onto a str, but that field is optional while the search only filters on the
expiry *date* -- so one employee with an expiry date and no document number
raised TypeError and killed the run for everybody behind them.
"""
from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestExpiryMailReminder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.today()
        cls.Employee = cls.env['hr.employee']

    def _mails_for(self, employee):
        return self.env['mail.mail'].search(
            [('email_to', '=', employee.work_email)])

    def test_reminder_runs_when_id_number_is_missing(self):
        """The regression: expiring ID, no ID number recorded."""
        employee = self.Employee.create({
            'name': 'No ID Number',
            'work_email': 'no.id.number@example.com',
            'id_expiry_date': self.today + timedelta(days=3),
            # identification_id deliberately left empty
        })
        # Must not raise TypeError.
        self.Employee.expiry_mail_reminder()
        self.assertTrue(self._mails_for(employee),
                        "a reminder should still be queued without a number")

    def test_reminder_runs_when_passport_number_is_missing(self):
        employee = self.Employee.create({
            'name': 'No Passport Number',
            'work_email': 'no.passport@example.com',
            'passport_expiry_date': self.today + timedelta(days=30),
            # passport_id deliberately left empty
        })
        self.Employee.expiry_mail_reminder()
        self.assertTrue(self._mails_for(employee))

    def test_one_bad_record_does_not_stop_the_others(self):
        """The real damage was collateral: everyone after the crash was
        silently skipped."""
        broken = self.Employee.create({
            'name': 'Missing Number',
            'work_email': 'missing.number@example.com',
            'id_expiry_date': self.today + timedelta(days=1),
        })
        healthy = self.Employee.create({
            'name': 'Has Number',
            'work_email': 'has.number@example.com',
            'identification_id': 'ID-12345',
            'id_expiry_date': self.today + timedelta(days=1),
        })

        self.Employee.expiry_mail_reminder()

        self.assertTrue(self._mails_for(broken))
        self.assertTrue(self._mails_for(healthy),
                        "a record with no document number must not prevent "
                        "later employees from being notified")

    def test_reminder_includes_the_number_when_present(self):
        employee = self.Employee.create({
            'name': 'Complete Record',
            'work_email': 'complete.record@example.com',
            'identification_id': 'ID-98765',
            'id_expiry_date': self.today + timedelta(days=2),
        })
        self.Employee.expiry_mail_reminder()
        mail = self._mails_for(employee)
        self.assertTrue(mail)
        self.assertIn('ID-98765', mail[0].body_html)

    def test_reminder_skips_employees_with_no_work_email(self):
        """Without a recipient the mail can only fail at send time."""
        employee = self.Employee.create({
            'name': 'No Work Email',
            'identification_id': 'ID-55555',
            'id_expiry_date': self.today + timedelta(days=1),
        })
        self.Employee.expiry_mail_reminder()
        self.assertFalse(
            self.env['mail.mail'].search(
                [('subject', 'ilike', 'ID-55555')]),
            "no undeliverable mail should be queued")

    def test_far_future_expiry_is_not_notified(self):
        employee = self.Employee.create({
            'name': 'Far Future',
            'work_email': 'far.future@example.com',
            'identification_id': 'ID-11111',
            'id_expiry_date': self.today + timedelta(days=365),
        })
        self.Employee.expiry_mail_reminder()
        self.assertFalse(self._mails_for(employee))


@tagged('post_install', '-at_install')
class TestFieldDefinitions(TransactionCase):
    """Odoo logs a warning when two fields of one model share a label, and
    the duplicated labels made the employee form genuinely ambiguous."""

    def test_expiry_and_attachment_labels_are_distinct(self):
        fields_ = self.env['hr.employee']._fields
        pairs = [
            ('id_expiry_date', 'passport_expiry_date'),
            ('identification_attachment_ids', 'passport_attachment_ids'),
        ]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                self.assertNotEqual(
                    fields_[left].string, fields_[right].string,
                    "%s and %s must not share a label" % (left, right))

    def test_family_model_declares_no_invalid_parameters(self):
        """`invisible` is a view attribute and `tracking` needs mail.thread;
        both were silently dropped by the ORM after a warning."""
        family = self.env['hr.employee.family']
        self.assertFalse(
            hasattr(family, '_mail_thread'),
            "hr.employee.family does not inherit mail.thread, so no field "
            "on it may declare tracking")
        for field in family._fields.values():
            self.assertFalse(
                getattr(field, 'tracking', False),
                "field %r declares tracking on a non-mail.thread model"
                % field.name)
