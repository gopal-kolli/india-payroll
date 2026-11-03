# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from hrms.payroll.doctype.salary_slip.test_salary_slip import make_salary_component
from hrms.payroll.doctype.salary_structure.salary_structure import make_salary_slip
from hrms.payroll.doctype.salary_structure.test_salary_structure import (
	create_salary_structure_assignment,
	make_salary_structure,
)
from hrms.tests.utils import HRMSTestSuite

from india_payroll.india_payroll.esi import EMPLOYER_ESI_COMPONENT
from india_payroll.india_payroll.professional_tax import STATE_PT_CONFIG, _slab_amount, get_pt_salary
from india_payroll.install import create_professional_tax_component


class TestProfessionalTax(HRMSTestSuite):
	def setUp(self):
		create_professional_tax_component()

	@HRMSTestSuite.change_settings("Payroll Settings", {"enable_professional_tax": 1})
	def test_maharashtra_professional_tax_applied_on_salary_slip(self):
		"""
		A male employee in Maharashtra with gross pay > ₹10,000 should have
		₹200 Professional Tax deducted in a standard (non-February) month.
		"""
		employee = make_employee(
			"test_maharashtra_pt@indiapayroll.com",
			company="_Test Company",
			gender="Male",
		)

		salary_structure = make_salary_structure(
			"Test PT Salary Structure",
			"Monthly",
			employee=employee,
			company="_Test Company",
			currency="INR",
		)

		ssa = create_salary_structure_assignment(
			employee,
			salary_structure.name,
			from_date="2026-04-01",
			company="_Test Company",
		)
		frappe.db.set_value("Salary Structure Assignment", ssa.name, "employment_state", "Maharashtra")

		salary_slip = make_salary_slip(
			salary_structure.name,
			employee=employee,
			posting_date="2026-04-01",
		)
		salary_slip.start_date = "2026-04-01"
		salary_slip.end_date = "2026-04-30"
		salary_slip.insert()

		pt_rows = [d for d in salary_slip.deductions if d.salary_component == "Professional Tax"]
		self.assertEqual(len(pt_rows), 1)
		self.assertEqual(pt_rows[0].amount, 200)

	@HRMSTestSuite.change_settings("Payroll Settings", {"enable_professional_tax": 1})
	def test_maharashtra_february_amount_for_female_employee(self):
		"""
		A female employee in Maharashtra with gross pay > ₹10,000 should have
		₹300 Professional Tax deducted in February (women exemption does not apply
		above ₹10,000; February special amount applies instead of the usual ₹200).
		"""
		employee = make_employee(
			"test_maharashtra_pt_female_feb@indiapayroll.com",
			company="_Test Company",
			gender="Female",
		)

		salary_structure = make_salary_structure(
			"Test PT Salary Structure Female Feb",
			"Monthly",
			employee=employee,
			company="_Test Company",
			currency="INR",
		)

		ssa = create_salary_structure_assignment(
			employee,
			salary_structure.name,
			from_date="2026-02-01",
			company="_Test Company",
		)

		frappe.db.set_value("Salary Structure Assignment", ssa.name, "employment_state", "Maharashtra")

		salary_slip = make_salary_slip(
			salary_structure.name,
			employee=employee,
			posting_date="2026-02-01",
		)
		salary_slip.start_date = "2026-02-01"
		salary_slip.end_date = "2026-02-28"
		salary_slip.insert()

		pt_rows = [d for d in salary_slip.deductions if d.salary_component == "Professional Tax"]
		self.assertEqual(len(pt_rows), 1)
		self.assertEqual(pt_rows[0].amount, 300)

	@HRMSTestSuite.change_settings("Payroll Settings", {"enable_professional_tax": 1})
	def test_kerala_half_yearly_first_month_of_period(self):
		"""
		Kerala is half-yearly. For the first month of a half-year period
		(April-September) with no prior submitted slips, the full half-yearly
		PT slab amount is charged in one go.

		Default salary structure: Basic ₹50,000 + HRA ₹3,000 + SA ₹25,000
		→ gross_pay = ₹78,000 → Kerala slab (upto ₹99,999) = ₹1,440.
		Prior deductions = ₹0  →  PT charged this month = ₹1,440.
		"""
		employee = make_employee(
			"test_kerala_pt@indiapayroll.com",
			company="_Test Company",
			gender="Male",
		)

		salary_structure = make_salary_structure(
			"Test PT Salary Structure Kerala",
			"Monthly",
			employee=employee,
			company="_Test Company",
			currency="INR",
		)

		ssa = create_salary_structure_assignment(
			employee,
			salary_structure.name,
			from_date="2026-04-01",
			company="_Test Company",
		)
		frappe.db.set_value("Salary Structure Assignment", ssa.name, "employment_state", "Kerala")

		salary_slip = make_salary_slip(
			salary_structure.name,
			employee=employee,
			posting_date="2026-04-01",
		)
		salary_slip.start_date = "2026-04-01"
		salary_slip.end_date = "2026-04-30"
		salary_slip.insert()

		pt_rows = [d for d in salary_slip.deductions if d.salary_component == "Professional Tax"]
		self.assertEqual(len(pt_rows), 1)
		self.assertEqual(pt_rows[0].amount, 1440)

	def test_pt_salary_excludes_employer_contribution_legs(self):
		"""
		A CTC-style month: cash salary ₹18,714, plus the
		Employer PF and Employer ESI legs in gross pay (₹20,349). PT is on the
		salary, so the Telangana slab is ₹150, not the ₹200 the gross would give.
		"""
		row = lambda component, amount: frappe._dict(  # noqa: E731
			salary_component=component, amount=amount, do_not_include_in_total=0
		)
		slip = frappe._dict(
			gross_pay=20_349,
			earnings=[
				row("Basic", 10_000),
				row("House Rent Allowance", 8_000),
				row("Special Allowance", 714),
				row("Employer PF", 1_286),
				row("Employer ESI", 349),
			],
		)
		slabs = STATE_PT_CONFIG["Telangana"]["slabs"]
		self.assertEqual(get_pt_salary(slip), 18_714)
		self.assertEqual(_slab_amount(get_pt_salary(slip), slabs), 150)
		self.assertEqual(_slab_amount(slip.gross_pay, slabs), 200)

	@HRMSTestSuite.change_settings("Payroll Settings", {"enable_professional_tax": 1, "enable_esic": 0})
	def test_telangana_pt_ignores_employer_esi_leg_in_gross(self):
		"""
		Salary ₹18,714 + an Employer ESI earning of ₹1,635 gives gross ₹20,349.
		Telangana PT is ₹150 on the salary, not ₹200 on the gross.
		"""
		components = [
			{"salary_component": "PT Test Salary", "abbr": "PTS", "formula": "base", "type": "Earning"},
			{
				"salary_component": EMPLOYER_ESI_COMPONENT,
				"abbr": "EESI",
				"formula": "1635",
				"type": "Earning",
			},
		]
		for c in components:
			c.update(amount_based_on_formula=1, depends_on_payment_days=0)
			if not frappe.db.exists("Salary Component", c["salary_component"]):
				make_salary_component([dict(c)], False, ["_Test Company"])

		employee = make_employee("test_telangana_pt_legs@indiapayroll.com", company="_Test Company")
		structure = make_salary_structure(
			"Test PT Telangana Employer Legs",
			"Monthly",
			company="_Test Company",
			currency="INR",
			earnings=components,
			deductions=[],
		)
		ssa = create_salary_structure_assignment(
			employee, structure.name, from_date="2026-10-01", company="_Test Company", base=18_714
		)
		frappe.db.set_value("Salary Structure Assignment", ssa.name, "employment_state", "Telangana")

		slip = make_salary_slip(structure.name, employee=employee, posting_date="2026-10-01")
		slip.start_date, slip.end_date = "2026-10-01", "2026-10-31"
		slip.insert()

		self.assertAlmostEqual(slip.gross_pay, 20_349, places=2)
		pt_rows = [d for d in slip.deductions if d.salary_component == "Professional Tax"]
		self.assertEqual([d.amount for d in pt_rows], [150])
