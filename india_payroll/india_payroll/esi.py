import datetime
import math

import frappe
from frappe.query_builder import DocType
from frappe.utils import flt, getdate

from india_payroll.india_payroll.code_wages import get_code_wages
from india_payroll.india_payroll.utils import (
	get_slip_ssa_values,
	refresh_gross_pay,
	set_structure_leg,
	structure_carries,
)

ESI_EMPLOYEE_COMPONENT = "Employee State Insurance"

# CTC-style structures carry the employer's 3.25% as this earning; the single 4%
# deduction row takes it back, so the employee bears only 0.75%.
EMPLOYER_ESI_COMPONENT = "Employer ESI"

# Combined ESI contribution rate deducted on the salary slip:
# employee share (0.75%) + employer share (3.25%) = 4%
ESI_RATE = 0.04
EMPLOYER_ESI_RATE = 0.0325

ESI_WAGE_CEILING = 21_000
ESI_WAGE_CEILING_DISABILITY = 25_000


def apply_esi(doc, method=None) -> None:
	"""
	Salary Slip regional deduction hook (see apply_regional_deductions).

	Injects ESI contributions when the employee is covered; otherwise removes
	any previously injected ESI rows.

	Coverage and contributions are both measured on wages under section 2(88)
	of the Code on Social Security, 2020 (see code_wages), which ESIC applies
	from 21 Nov 2025: HRA, employer PF and one-off payments are left out up to
	one half of remuneration. Coverage is decided on the *full* monthly wage
	from the structure — not the payment-days-prorated amount — so a high
	earner is not pulled into ESI in an LOP month, and it holds for the rest of
	the contribution period once ESI has been deducted. The contribution itself
	is levied on the wages actually paid.
	"""
	if not frappe.db.get_single_value("Payroll Settings", "enable_esic"):
		return

	if not doc.salary_structure:
		return

	if not frappe.db.exists("Salary Component", ESI_EMPLOYEE_COMPONENT):
		frappe.msgprint(
			frappe._(
				"Salary Component <b>{0}</b> not found. "
				"Please reinstall the India Payroll app or create it manually."
			).format(ESI_EMPLOYEE_COMPONENT),
			indicator="orange",
			alert=True,
		)
		return

	# Determine the applicable wage ceiling (PwD flag lives on the assignment)
	is_disabled = get_slip_ssa_values(doc, ["is_person_with_disability"]).get("is_person_with_disability")
	wage_ceiling = ESI_WAGE_CEILING_DISABILITY if is_disabled else ESI_WAGE_CEILING

	covered = _esi_wage(doc, use_default_amount=True) <= wage_ceiling or _covered_earlier_in_period(doc)
	wages = _esi_wage(doc) if covered else 0.0

	_set_employer_esi_leg(doc, wages)

	if not covered:
		# Wage above the ceiling — not covered. Strip any stale ESI rows.
		_remove_esi_components(doc)
		return

	_update_esi_in_salary_slip(doc, flt(wages * ESI_RATE, 2))


def _esi_wage(doc, *, use_default_amount: bool = False) -> float:
	"""Return ESI wages: section 2(88) wages (see code_wages.get_code_wages).

	``default_amount`` is used for the coverage test so LOP does not bring a
	high earner into coverage. ``amount`` is used for the contribution itself
	so joining-date and LOP proration are respected.
	"""
	return get_code_wages(doc, use_default_amount=use_default_amount)


def contribution_period_start(date) -> datetime.date:
	"""ESI contribution periods run 1 Apr to 30 Sep and 1 Oct to 31 Mar."""
	d = getdate(date)
	if 4 <= d.month <= 9:
		return datetime.date(d.year, 4, 1)
	return datetime.date(d.year if d.month >= 10 else d.year - 1, 10, 1)


def _covered_earlier_in_period(doc) -> bool:
	"""An employee covered earlier in the contribution period stays covered to its end,
	even if a raise has taken the wage above the ceiling since."""
	period_start = contribution_period_start(doc.start_date)
	if not doc.employee or getdate(doc.start_date) <= period_start:
		return False

	slip, detail = DocType("Salary Slip"), DocType("Salary Detail")
	return bool(
		frappe.qb.from_(slip)
		.join(detail)
		.on((detail.parent == slip.name) & (detail.parenttype == "Salary Slip"))
		.select(slip.name)
		.where(
			(slip.employee == doc.employee)
			& (slip.docstatus == 1)
			& (slip.start_date >= period_start)
			& (slip.start_date < getdate(doc.start_date))
			& (detail.parentfield == "deductions")
			& (detail.salary_component == ESI_EMPLOYEE_COMPONENT)
			& (detail.amount > 0)
		)
		.limit(1)
		.run()
	)


def _set_employer_esi_leg(doc, wages: float) -> None:
	"""
	Size the employer's 3.25% "Employer ESI" earning on the same wages as the
	4% deduction, for structures that carry that earning, and refresh gross pay.

	The leg is rounded up to the rupee so the employee never bears more than
	0.75%. An Additional Salary on the component is left as entered.
	"""
	if not structure_carries(doc.salary_structure, "earnings", EMPLOYER_ESI_COMPONENT):
		return

	amount = math.ceil(flt(wages * EMPLOYER_ESI_RATE, 2))
	if set_structure_leg(doc, "earnings", EMPLOYER_ESI_COMPONENT, amount):
		refresh_gross_pay(doc)


def _remove_esi_components(doc) -> None:
	"""Remove the ESI employee component from the salary slip deductions."""
	doc.deductions = [d for d in doc.deductions if d.salary_component != ESI_EMPLOYEE_COMPONENT]


def _update_esi_in_salary_slip(doc, esi: float) -> None:
	"""
	Replace any existing Employee ESI row with the freshly computed amount.

	The single row covers the full 4% ESI contribution (employee 0.75% +
	employer 3.25%). In CTC-style structures the employer's share comes back
	as the "Employer ESI" earning (see _set_employer_esi_leg).
	"""
	# Remove stale row first
	doc.deductions = [d for d in doc.deductions if d.salary_component != ESI_EMPLOYEE_COMPONENT]

	if esi > 0:
		doc.append(
			"deductions",
			{
				"salary_component": ESI_EMPLOYEE_COMPONENT,
				"amount": esi,
			},
		)
