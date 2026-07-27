# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from india_payroll.india_payroll.utils import get_slip_ssa_values


class TestUtils(IntegrationTestCase):
	def test_mid_month_joiner_falls_back_to_period_end(self):
		slip = frappe._dict(
			employee="HR-EMP-TEST",
			company="_Test Company",
			salary_structure="Test Structure",
			start_date="2026-07-01",
			end_date="2026-07-31",
		)
		expected = frappe._dict(epf_applicable=1, employment_state="Telangana")

		with patch(
			"india_payroll.india_payroll.utils.get_effective_ssa_values",
			side_effect=[frappe._dict(), expected],
		) as get_values:
			result = get_slip_ssa_values(slip, ["epf_applicable", "employment_state"])

		self.assertEqual(result, expected)
		self.assertEqual(get_values.call_count, 2)
		self.assertEqual(get_values.call_args_list[0].args[3], "2026-07-01")
		self.assertEqual(get_values.call_args_list[1].args[3], "2026-07-31")

	def test_existing_employee_keeps_period_start_assignment(self):
		slip = frappe._dict(
			employee="HR-EMP-TEST",
			company="_Test Company",
			salary_structure="Test Structure",
			start_date="2026-07-01",
			end_date="2026-07-31",
		)
		expected = frappe._dict(epf_applicable=1)

		with patch(
			"india_payroll.india_payroll.utils.get_effective_ssa_values",
			return_value=expected,
		) as get_values:
			result = get_slip_ssa_values(slip, ["epf_applicable"])

		self.assertEqual(result, expected)
		get_values.assert_called_once()
