# -*- coding: utf-8 -*-
"""End-to-end tests for the hr_reminder controllers.

Background: a deployed copy of this module had its route declared as
`type='jsonrpc'` -- the Odoo 19 spelling. Odoo 18's `http.route` asserts the
dispatcher type against `_dispatchers`, which only holds 'http' and 'json',
so importing the controller raised AssertionError, `hr_reminder` failed to
load, and the whole registry went down with it:

    CRITICAL odoo.modules.module: Couldn't load module hr_reminder
    ERROR    odoo.modules.registry: Failed to load registry

TestReminderRouteRegistration is the cheap guard against that spelling
coming back. The HttpCase below exercises the routes for real, over HTTP,
so a controller that cannot be imported or dispatched fails the build.
"""
import json

from datetime import timedelta

from odoo import fields, http
from odoo.tests import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestReminderRouteRegistration(TransactionCase):

    def test_routes_use_a_dispatcher_type_odoo18_understands(self):
        """The exact assertion odoo.http makes when the module is imported."""
        from odoo.addons.hr_reminder.controllers import hr_reminder

        valid = set(http._dispatchers.keys())
        self.assertTrue(valid, "could not introspect odoo.http._dispatchers")

        checked = 0
        for name in dir(hr_reminder.Reminders):
            endpoint = getattr(hr_reminder.Reminders, name, None)
            routing = getattr(endpoint, 'original_routing', None)
            if routing is None:
                continue
            checked += 1
            self.assertIn(
                routing.get('type', 'http'), valid,
                "%s declares a route type that Odoo 18 cannot dispatch; the "
                "module will fail to load and take the registry with it"
                % name)
        self.assertTrue(checked, "no routed endpoints were discovered")


@tagged('post_install', '-at_install')
class TestReminderRoutes(HttpCase):

    def setUp(self):
        super().setUp()
        self.today = fields.Date.today()
        # A plain internal user -- base.group_user only, deliberately NOT an
        # HR officer. ir.model.access.csv grants that group read=0 on
        # hr.reminder, yet the systray widget ships in web.assets_backend and
        # therefore loads for exactly this user. Before the fix, every one of
        # them got an AccessError on opening the systray.
        self.employee_user = self.env['res.users'].create({
            'name': 'Systray Employee',
            'login': 'systray.employee',
            'password': 'systray.employee.pw',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        model = self.env['ir.model'].search([('model', '=', 'hr.employee')],
                                            limit=1)
        field = self.env['ir.model.fields'].search([
            ('model_id', '=', model.id),
            ('ttype', 'in', ('date', 'datetime')),
        ], limit=1)
        self.reminder = self.env['hr.reminder'].create({
            'name': 'Test Reminder Today',
            'model_id': model.id,
            'field_id': field.id,
            'search_by': 'today',
            'company_id': self.env.company.id,
        })
        self.authenticate('systray.employee', 'systray.employee.pw')

    def _call(self, route, params=None):
        response = self.url_open(
            route,
            data=json.dumps({
                'jsonrpc': '2.0',
                'method': 'call',
                'params': params or {},
            }),
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(
            response.status_code, 200,
            "%s did not return 200 -- the controller is not dispatchable"
            % route)
        payload = response.json()
        self.assertNotIn(
            'error', payload,
            "%s raised: %s" % (route, payload.get('error')))
        return payload['result']

    def test_all_reminder_route_responds(self):
        result = self._call('/hr_reminder/all_reminder')
        self.assertIsInstance(result, list)
        names = [entry['name'] for entry in result]
        self.assertIn('Test Reminder Today', names,
                      "a 'today' reminder should be returned by the systray "
                      "endpoint")

    def test_all_reminder_respects_the_expiry_date(self):
        self.reminder.expiry_date = self.today - timedelta(days=1)
        self.reminder.search_by = 'set_period'
        self.reminder.date_from = self.today - timedelta(days=10)
        self.reminder.date_to = self.today + timedelta(days=10)

        result = self._call('/hr_reminder/all_reminder')

        names = [entry['name'] for entry in result]
        self.assertNotIn('Test Reminder Today', names,
                         "an expired reminder must not be returned")

    def test_all_reminder_returns_reminders_inside_a_period(self):
        self.reminder.write({
            'search_by': 'set_period',
            'date_from': self.today - timedelta(days=1),
            'date_to': self.today + timedelta(days=1),
            'expiry_date': False,
        })

        result = self._call('/hr_reminder/all_reminder')

        names = [entry['name'] for entry in result]
        self.assertIn('Test Reminder Today', names)

    def test_reminder_active_route_responds(self):
        result = self._call('/hr_reminder/reminder_active',
                            {'reminder_name': 'Test Reminder Today'})
        self.assertIsInstance(result, list)
        self.assertIn('hr.employee', result,
                      "the endpoint should report the reminder's model")

    def test_reminder_active_with_an_unknown_name_is_not_an_error(self):
        result = self._call('/hr_reminder/reminder_active',
                            {'reminder_name': 'Does Not Exist'})
        self.assertEqual(result, [])
