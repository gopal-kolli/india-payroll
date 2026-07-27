import frappe
from frappe.utils import flt

from india_payroll.india_payroll.utils import get_slip_ssa_values

ESI_EMPLOYEE_COMPONENT = "Employee State Insurance"

# Combined ESI contribution rate deducted on the salary slip:
# employee share (0.75%) + employer share (3.25%) = 4%
ESI_RATE = 0.04

ESI_WAGE_CEILING = 21_000
ESI_WAGE_CEILING_DISABILITY = 25_000

# Legacy CTC-style HRMS structures may represent employer contributions as
# earnings and offset them with deductions. They are not "wages" under
# section 2(22) of the ESI Act and must not affect coverage or contribution.
ESI_NON_WAGE_EARNINGS = frozenset({"Employer PF", "Employer ESI"})


def apply_esi(doc, method=None) -> None:
	"""
	Salary Slip regional deduction hook (see apply_regional_deductions).

	Injects ESI contributions when the employee's wage is within the prescribed
	ceiling; otherwise removes any previously injected ESI rows.

	Coverage (the wage-ceiling test) is decided on the *full* monthly ESI wage
	from the structure assignment — not the payment-days-prorated slip amount —
	so a high earner is not wrongly pulled into ESI in an LOP month. Employer
	PF/ESI contribution earnings are excluded because they are not wages under
	section 2(22) of the ESI Act. The contribution itself is levied on the
	actual ESI wages paid.
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

	if _esi_wage(doc, use_default_amount=True) > wage_ceiling:
		# Wage above the ceiling — not covered. Strip any stale ESI rows.
		_remove_esi_components(doc)
		return

	esi = flt(_esi_wage(doc) * ESI_RATE, 2)

	_update_esi_in_salary_slip(doc, esi)


def _esi_wage(doc, *, use_default_amount: bool = False) -> float:
	"""Return ESI wages, excluding employer-contribution earnings.

	``default_amount`` is used for the coverage test so LOP does not bring a
	high earner into coverage. ``amount`` is used for the contribution itself
	so joining-date and LOP proration are respected.
	"""
	amount_field = "default_amount" if use_default_amount else "amount"
	return sum(
		flt(e.get(amount_field))
		for e in doc.earnings
		if not e.get("do_not_include_in_total")
		and e.get("salary_component") not in ESI_NON_WAGE_EARNINGS
	)


def _remove_esi_components(doc) -> None:
	"""Remove the ESI employee component from the salary slip deductions."""
	doc.deductions = [d for d in doc.deductions if d.salary_component != ESI_EMPLOYEE_COMPONENT]


def _update_esi_in_salary_slip(doc, esi: float) -> None:
	"""
	Replace any existing Employee ESI row with the freshly computed amount.

	The single row covers the full 4% ESI contribution (employee 0.75% +
	employer 3.25%).  The employer's share is part of the CTC and is not
	shown as a separate component.
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
