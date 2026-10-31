# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import frappe
from erpnext.setup.doctype.employee.test_employee import make_employee
from frappe.utils import flt
from hrms.payroll.doctype.salary_slip.test_salary_slip import make_salary_component
from hrms.payroll.doctype.salary_structure.salary_structure import make_salary_slip
from hrms.payroll.doctype.salary_structure.test_salary_structure import (
	create_salary_structure_assignment,
	make_salary_structure,
)
from hrms.tests.utils import HRMSTestSuite

from india_payroll.india_payroll.epf import (
	EPF_EMPLOYEE_COMPONENT,
	EPF_PREVIOUS_WAGE_CEILING,
	VPF_COMPONENT,
	_compute_pf_wage,
	get_capped_pf_wage,
	get_eps_wage,
)
from india_payroll.install import create_epf_components

# The earning component used as Basic across all EPF tests. Basic is
# PF-eligible; formula `base` keeps gross_pay equal to the SSA base for
# predictable assertions. HRA and Additional-Salary earnings are excluded from
# PF wage (see test_pf_wage_excludes_hra_and_additional_salary).
_EPF_BASIC_COMPONENT = "EPF Test Basic"
_EPF_TEST_EARNINGS = [
	{
		"salary_component": _EPF_BASIC_COMPONENT,
		"abbr": "EPFTB",
		"formula": "base",
		"type": "Earning",
		"amount_based_on_formula": 1,
		"depends_on_payment_days": 0,
	}
]

_TEST_EMAILS = [
	"test_epf_below_ceiling@indiapayroll.com",
	"test_epf_all_earnings@indiapayroll.com",
	"test_epf_above_ceiling_capped@indiapayroll.com",
	"test_epf_above_ceiling_actual@indiapayroll.com",
	"test_epf_vpf@indiapayroll.com",
	"test_epf_vpf_amount@indiapayroll.com",
	"test_epf_vpf_amount_lop@indiapayroll.com",
	"test_epf_not_applicable@indiapayroll.com",
	"test_epf_disabled_setting@indiapayroll.com",
	"test_epf_net_pay@indiapayroll.com",
	"test_epf_preview@indiapayroll.com",
	"test_epf_revised_ceiling@indiapayroll.com",
	"test_epf_new_bracket@indiapayroll.com",
	"test_epf_september_split@indiapayroll.com",
	"test_epf_one_off_pay_leg@indiapayroll.com",
	"test_epf_unpaired_leg@indiapayroll.com",
]


class TestEPF(HRMSTestSuite):
	"""
	Covers the employee-side EPF deduction injected by ``apply_epf`` onto the
	salary slip.  Employer EPF / EPS / EDLI / Admin are now "Employer
	Contribution" components evaluated by Salary Structure Assignment into
	CTC — they don't appear on the slip and are not exercised here.
	"""

	def setUp(self):
		create_epf_components()
		self._ensure_epf_test_component()
		self._cleanup()

	def _ensure_epf_test_component(self):
		"""Create the EPF-specific basic earning component if absent."""
		if not frappe.db.exists("Salary Component", _EPF_BASIC_COMPONENT):
			make_salary_component(_EPF_TEST_EARNINGS, False, ["_Test Company"])

	def _cleanup(self):
		for email in _TEST_EMAILS:
			frappe.db.delete("Salary Slip", {"employee_name": email})
			emp = frappe.db.get_value("Employee", {"employee_name": email}, "name")
			if emp:
				frappe.db.delete("Salary Structure Assignment", {"employee": emp})

	def _make_salary_slip(
		self,
		email: str,
		structure_name: str,
		gross_pay: float,
		*,
		epf_applicable: bool = True,
		posting_date: str = "2026-04-01",
		start_date: str = "2026-04-01",
		end_date: str = "2026-04-30",
	):
		"""
		Create a salary slip whose Basic (PF-eligible) equals `gross_pay`.

		Sets epf_applicable on the Salary Structure Assignment so the hook treats
		the employee as opted into EPF deduction.
		"""
		employee = make_employee(email, company="_Test Company")

		salary_structure = make_salary_structure(
			structure_name,
			"Monthly",
			company="_Test Company",
			currency="INR",
			earnings=_EPF_TEST_EARNINGS,
			deductions=[],
		)

		ssa = create_salary_structure_assignment(
			employee,
			salary_structure.name,
			from_date=start_date,
			company="_Test Company",
			base=gross_pay,
		)
		# EPF opt-in now lives on the assignment, not the Employee master.
		frappe.db.set_value(
			"Salary Structure Assignment", ssa.name, "epf_applicable", 1 if epf_applicable else 0
		)

		salary_slip = make_salary_slip(
			salary_structure.name,
			employee=employee,
			posting_date=posting_date,
		)
		salary_slip.start_date = start_date
		salary_slip.end_date = end_date

		return employee, salary_slip

	def _set_ssa(self, employee: str, values: dict) -> str:
		"""Set India Payroll statutory config on the employee's salary structure assignment."""
		ssa = frappe.db.get_value(
			"Salary Structure Assignment", {"employee": employee}, "name", order_by="from_date desc"
		)
		frappe.db.set_value("Salary Structure Assignment", ssa, values)
		return ssa

	@staticmethod
	def _amount(slip, table: str, component: str) -> float:
		row = next(
			(r for r in getattr(slip, table) if r.salary_component == component),
			None,
		)
		return flt(row.amount) if row else 0.0

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_at_ceiling_standard_12_percent(self):
		"""PF wage ₹15,000 (= ceiling) should deduct ₹1,800 (12%)."""
		gross = float(EPF_PREVIOUS_WAGE_CEILING)
		_, slip = self._make_salary_slip(
			"test_epf_below_ceiling@indiapayroll.com",
			"Test EPF At Ceiling Structure",
			gross,
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 1_800)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_above_ceiling_default_caps_at_15000(self):
		"""
		PF wage ₹25,000 with `contribute_on_actual_pf_wage` unset (default).
		Employee EPF capped at ₹15,000 → ₹1,800.
		"""
		gross = 25_000.0
		_, slip = self._make_salary_slip(
			"test_epf_above_ceiling_capped@indiapayroll.com",
			"Test EPF Above Ceiling Capped Structure",
			gross,
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 1_800)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_above_ceiling_contribute_on_actual(self):
		"""
		PF wage ₹25,000 with `contribute_on_actual_pf_wage = 1`.
		Employee EPF: 12% * 25,000 = ₹3,000.
		"""
		gross = 25_000.0
		employee, slip = self._make_salary_slip(
			"test_epf_above_ceiling_actual@indiapayroll.com",
			"Test EPF Above Ceiling Actual Structure",
			gross,
		)
		self._set_ssa(employee, {"contribute_on_actual_pf_wage": 1})
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 3_000)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_vpf_percentage_mode(self):
		"""
		vpf_mode = Percentage, vpf_percentage = 5: 5% extra on the EPF base.
		Employee EPF: 12% * 15,000 = ₹1,800
		VPF:          5% * 15,000 = ₹750
		"""
		gross = float(EPF_PREVIOUS_WAGE_CEILING)
		employee, slip = self._make_salary_slip(
			"test_epf_vpf@indiapayroll.com",
			"Test EPF VPF Structure",
			gross,
		)
		self._set_ssa(employee, {"vpf_mode": "Percentage", "vpf_percentage": 5})
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 1_800)
		self.assertEqual(self._amount(slip, "deductions", VPF_COMPONENT), 750)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_vpf_amount_mode(self):
		"""
		vpf_mode = Amount, vpf_amount = ₹2,000: fixed lumpsum deduction
		regardless of PF wage. vpf_percentage is ignored.
		"""
		gross = float(EPF_PREVIOUS_WAGE_CEILING)
		employee, slip = self._make_salary_slip(
			"test_epf_vpf_amount@indiapayroll.com",
			"Test EPF VPF Amount Structure",
			gross,
		)
		self._set_ssa(employee, {"vpf_mode": "Amount", "vpf_amount": 2_000, "vpf_percentage": 5})
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 1_800)
		self.assertEqual(self._amount(slip, "deductions", VPF_COMPONENT), 2_000)

	def test_vpf_amount_mode_prorates_on_lop(self):
		"""
		Amount mode prorates the elected monthly lumpsum by
		payment_days / total_working_days so LOP months don't over-deduct.

		Tested as a unit against ``_compute_vpf`` so we can dictate
		payment_days / total_working_days without staging an attendance
		fixture; the slip-side proration of earnings is exercised separately
		by Percentage mode tests.
		"""
		from india_payroll.india_payroll.epf import _compute_vpf

		vpf_args = {"vpf_mode": "Amount", "vpf_amount": 3_000}

		# Full month: no proration.
		full = frappe._dict(payment_days=30, total_working_days=30)
		self.assertEqual(_compute_vpf(full, 15_000, **vpf_args), 3_000)

		# 5 LOP days in a 30-day month → 25/30 * 3000 = 2500.
		lop = frappe._dict(payment_days=25, total_working_days=30)
		self.assertEqual(_compute_vpf(lop, 15_000, **vpf_args), 2_500)

		# Fractional payment_days (half-day LOP) — 24.5/30 * 3000 = 2450.
		half_day = frappe._dict(payment_days=24.5, total_working_days=30)
		self.assertEqual(_compute_vpf(half_day, 15_000, **vpf_args), 2_450)

	def test_pf_wage_excludes_hra_and_additional_salary(self):
		"""
		PF wage must count only PF-eligible earnings. HRA (identified from the
		Company master's ``hra_component``) and any earning sourced from an
		Additional Salary (bonuses/incentives, flagged by ``additional_salary``)
		are excluded — only Basic remains.
		"""
		from india_payroll.india_payroll.epf import _compute_pf_wage

		# Point the Company at an HRA component so the lookup resolves.
		frappe.db.set_value("Company", "_Test Company", "hra_component", "HRA")

		doc = frappe._dict(
			company="_Test Company",
			earnings=[
				frappe._dict(salary_component="Basic Salary", amount=20_000),
				frappe._dict(salary_component="HRA", amount=8_000),
				frappe._dict(salary_component="Bonus", amount=5_000, additional_salary="ADSAL-0001"),
			],
		)

		# Only Basic counts → 20,000 (HRA and the Additional-Salary bonus dropped).
		self.assertEqual(_compute_pf_wage(doc), 20_000)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_not_applicable_no_epf_rows(self):
		"""
		Employee.epf_applicable = 0 must suppress the employee EPF deduction,
		even when EPF is enabled in Payroll Settings.
		"""
		_, slip = self._make_salary_slip(
			"test_epf_not_applicable@indiapayroll.com",
			"Test EPF Not Applicable Structure",
			15_000.0,
			epf_applicable=False,
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 0)
		self.assertEqual(self._amount(slip, "deductions", VPF_COMPONENT), 0)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 0, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_disabled_setting_no_epf_rows(self):
		"""
		Master switch off → no employee EPF rows even if Employee.epf_applicable
		is on.
		"""
		_, slip = self._make_salary_slip(
			"test_epf_disabled_setting@indiapayroll.com",
			"Test EPF Disabled Setting Structure",
			15_000.0,
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 0)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_epf_injected_in_preview(self):
		"""
		Regression: statutory deductions must be injected during the salary slip
		preview (``process_salary_structure``), not only on save.

		The injectors run via the ``apply_regional_deductions`` regional override
		called inside ``calculate_net_pay``, so driving the preview path alone (no
		insert/save) must still produce the EPF deduction row.
		"""
		gross = float(EPF_PREVIOUS_WAGE_CEILING)
		_, slip = self._make_salary_slip(
			"test_epf_preview@indiapayroll.com",
			"Test EPF Preview Structure",
			gross,
		)

		# Drive only the preview path — never insert the slip.
		slip.process_salary_structure(for_preview=1)

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 1_800)
		self.assertEqual(slip.docstatus, 0, "Preview must not persist the salary slip")

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_net_pay_reduced_only_by_employee_share(self):
		"""
		Net pay must drop only by Employee PF + VPF.  Employer contributions
		are now off-slip (Salary Structure / CTC), so they cannot shift net
		pay or gross even structurally.
		"""
		gross = float(EPF_PREVIOUS_WAGE_CEILING)
		employee, slip = self._make_salary_slip(
			"test_epf_net_pay@indiapayroll.com",
			"Test EPF Net Pay Structure",
			gross,
		)
		self._set_ssa(employee, {"vpf_mode": "Percentage", "vpf_percentage": 5})
		slip.insert()

		expected_deduction = 1_800 + 750  # employee 12% + VPF 5%
		self.assertAlmostEqual(slip.total_deduction, expected_deduction, places=2)
		self.assertAlmostEqual(slip.gross_pay, gross, places=2)
		self.assertAlmostEqual(slip.net_pay, gross - expected_deduction, places=2)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_revised_ceiling_from_october_2026(self):
		"""
		S.O. 5109(E): from the October 2026 wage month the PF wage is capped at
		₹25,000, so a ₹40,000 PF wage deducts 12% * 25,000 = ₹3,000 (FAQ Q13).
		"""
		_, slip = self._make_salary_slip(
			"test_epf_revised_ceiling@indiapayroll.com",
			"Test EPF Revised Ceiling Structure",
			40_000.0,
			posting_date="2026-10-31",
			start_date="2026-10-01",
			end_date="2026-10-31",
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 3_000)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_new_bracket_contributes_on_actual_wage(self):
		"""A ₹20,000 PF wage in October 2026 is within the new ceiling: ₹2,400 (FAQ Q12)."""
		_, slip = self._make_salary_slip(
			"test_epf_new_bracket@indiapayroll.com",
			"Test EPF New Bracket Structure",
			20_000.0,
			posting_date="2026-10-31",
			start_date="2026-10-01",
			end_date="2026-10-31",
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 2_400)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_september_2026_splits_at_revision_date(self):
		"""
		September 2026 is split: 1-16 Sep under ₹15,000, 17-30 Sep under ₹25,000.
		₹20,000 wage → 15,000 * 16/30 + 20,000 * 14/30 = ₹17,333.33 → ₹2,080
		(FAQ Q7, scenario C).
		"""
		_, slip = self._make_salary_slip(
			"test_epf_september_split@indiapayroll.com",
			"Test EPF September Split Structure",
			20_000.0,
			posting_date="2026-09-30",
			start_date="2026-09-01",
			end_date="2026-09-30",
		)
		slip.insert()

		self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 2_080)

	def test_wage_ceiling_helpers(self):
		"""Unit coverage of the ceiling split, including EPS joining from 17 Sep 2026."""
		self.assertEqual(get_capped_pf_wage(20_000, "2026-08-01", "2026-08-31"), 15_000)
		self.assertEqual(get_capped_pf_wage(20_000, "2026-10-01", "2026-10-31"), 20_000)
		self.assertEqual(get_capped_pf_wage(40_000, "2026-10-01", "2026-10-31"), 25_000)
		self.assertAlmostEqual(get_capped_pf_wage(20_000, "2026-09-01", "2026-09-30"), 17_333.33, places=2)
		self.assertAlmostEqual(get_capped_pf_wage(40_000, "2026-09-01", "2026-09-30"), 19_666.67, places=2)

		# ₹20,000 earner: outside EPS before the revision, a member from 17 Sep (FAQ Q7 scenario B).
		self.assertEqual(get_eps_wage(20_000, "2026-08-01", "2026-08-31"), 0)
		self.assertAlmostEqual(get_eps_wage(20_000, "2026-09-01", "2026-09-30"), 9_333.33, places=2)
		self.assertEqual(get_eps_wage(20_000, "2026-10-01", "2026-10-31"), 20_000)
		# Above the revised ceiling: no EPS for a post-1 Sept 2014 member (FAQ Q13, ₹35,000 row).
		self.assertEqual(get_eps_wage(35_000, "2026-10-01", "2026-10-31"), 0)
		# ₹10,000 earner was always within the ceiling.
		self.assertEqual(get_eps_wage(10_000, "2026-09-01", "2026-09-30"), 10_000)

	def _one_off_pay_slip(self, email: str, structure_name: str, *, employer_deduction: bool):
		"""Basic 15,000 + HRA 12,000 + an "Employer PF" earning (12% of Basic), with or
		without the "Provident Fund - Employer" deduction that takes it back, and a 10,000
		incentive paid by Additional Salary. Returns the inserted October slip."""
		earnings = [
			{"salary_component": "EPF Code Basic", "abbr": "EPFCB", "formula": "base * 0.5"},
			{"salary_component": "EPF Code HRA", "abbr": "EPFCHRA", "formula": "base * 0.4"},
			{"salary_component": "Employer PF", "abbr": "EMPF", "formula": "EPFCB * 0.12"},
		]
		deductions = [{"salary_component": "Provident Fund - Employer", "abbr": "EMPFD", "formula": "EMPF"}]
		for rows, row_type in ((earnings, "Earning"), (deductions, "Deduction")):
			for c in rows:
				c.update(type=row_type, amount_based_on_formula=1, depends_on_payment_days=0)
				if not frappe.db.exists("Salary Component", c["salary_component"]):
					make_salary_component([dict(c)], False, ["_Test Company"])
		if not frappe.db.exists("Salary Component", "EPF Test Incentive"):
			make_salary_component(
				[{"salary_component": "EPF Test Incentive", "abbr": "EPFTI", "type": "Earning"}],
				False,
				["_Test Company"],
			)

		employee = make_employee(email, company="_Test Company")
		structure = make_salary_structure(
			structure_name,
			"Monthly",
			company="_Test Company",
			currency="INR",
			earnings=earnings,
			deductions=deductions if employer_deduction else [],
		)
		ssa = create_salary_structure_assignment(
			employee, structure.name, from_date="2026-10-01", company="_Test Company", base=30_000
		)
		frappe.db.set_value("Salary Structure Assignment", ssa.name, "epf_applicable", 1)
		frappe.get_doc(
			{
				"doctype": "Additional Salary",
				"employee": employee,
				"company": "_Test Company",
				"salary_component": "EPF Test Incentive",
				"amount": 10_000,
				"payroll_date": "2026-10-15",
				"overwrite_salary_structure_amount": 0,
			}
		).insert().submit()

		slip = make_salary_slip(structure.name, employee=employee, posting_date="2026-10-31")
		slip.insert()
		return slip

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_one_off_pay_settles_pf_wage_and_employer_leg(self):
		"""
		The incentive takes the exclusions past one half of remuneration, so the
		excess is added back to the PF wage. The employer leg is itself in that test;
		the two settle at PF wage 19,681 with 2,362 on every PF row.
		"""
		previous_hra = frappe.db.get_value("Company", "_Test Company", "hra_component")
		frappe.db.set_value("Company", "_Test Company", "hra_component", "EPF Code HRA")
		try:
			slip = self._one_off_pay_slip(
				"test_epf_one_off_pay_leg@indiapayroll.com",
				"Test EPF One-off Pay Structure",
				employer_deduction=True,
			)
			self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 2_362)
			self.assertEqual(self._amount(slip, "earnings", "Employer PF"), 2_362)
			self.assertEqual(self._amount(slip, "deductions", "Provident Fund - Employer"), 2_362)
			self.assertAlmostEqual(slip.gross_pay, 15_000 + 12_000 + 10_000 + 2_362, places=2)
			self.assertAlmostEqual(_compute_pf_wage(slip), 19_681, places=2)
		finally:
			frappe.db.set_value("Company", "_Test Company", "hra_component", previous_hra)

	@HRMSTestSuite.change_settings(
		"Payroll Settings",
		{"enable_epf": 1, "enable_professional_tax": 0, "enable_esic": 0, "enable_lwf": 0},
	)
	def test_employer_pf_earning_without_deduction_is_left_alone(self):
		"""
		Without the deduction that takes it back, the "Employer PF" earning would be
		paid out, so the hook leaves it at the structure's 1,800. The PF wage still
		adds back the excess: remuneration 38,800, exclusions 23,800 → 19,400 → 2,328.
		"""
		previous_hra = frappe.db.get_value("Company", "_Test Company", "hra_component")
		frappe.db.set_value("Company", "_Test Company", "hra_component", "EPF Code HRA")
		try:
			slip = self._one_off_pay_slip(
				"test_epf_unpaired_leg@indiapayroll.com",
				"Test EPF Unpaired Leg Structure",
				employer_deduction=False,
			)
			self.assertEqual(self._amount(slip, "earnings", "Employer PF"), 1_800)
			self.assertEqual(self._amount(slip, "deductions", "Provident Fund - Employer"), 0)
			self.assertEqual(self._amount(slip, "deductions", EPF_EMPLOYEE_COMPONENT), 2_328)
		finally:
			frappe.db.set_value("Company", "_Test Company", "hra_component", previous_hra)
