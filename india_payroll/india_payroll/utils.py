# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.utils import flt


def get_effective_ssa_values(
	employee: str,
	company: str,
	salary_structure: str,
	on_date,
	fields: list[str],
) -> "frappe._dict":
	row = frappe.db.get_value(
		"Salary Structure Assignment",
		filters={
			"employee": employee,
			"company": company,
			"salary_structure": salary_structure,
			"from_date": ("<=", on_date),
			"docstatus": 1,
		},
		fieldname=fields,
		order_by="from_date desc",
		as_dict=True,
	)
	return row or frappe._dict()


def get_slip_ssa_values(doc, fields: list[str]) -> "frappe._dict":
	values = get_effective_ssa_values(doc.employee, doc.company, doc.salary_structure, doc.start_date, fields)
	if values or not doc.end_date:
		return values

	# A mid-month joiner can have the first SSA effective after the payroll
	# period's start date. Frappe HR still creates and prorates that employee's
	# slip correctly, so fall back to the period end for the statutory fields.
	# Keep the start-date lookup first so mid-month revisions for existing
	# employees do not silently change the assignment used by the core slip.
	return get_effective_ssa_values(doc.employee, doc.company, doc.salary_structure, doc.end_date, fields)


# CTC-style structures carry employer contributions as structure rows (an "Employer PF"
# earning taken back by a "Provident Fund - Employer" deduction, an "Employer ESI" earning
# taken back by the 4% ESI row). The statutory hooks size those rows to the contribution.


def structure_carries(salary_structure: str | None, parentfield: str, component: str) -> bool:
	return bool(
		salary_structure
		and frappe.db.exists(
			"Salary Detail",
			{
				"parenttype": "Salary Structure",
				"parent": salary_structure,
				"parentfield": parentfield,
				"salary_component": component,
			},
		)
	)


def set_structure_leg(
	doc, parentfield: str, component: str, amount: float, *, add_missing: bool = True
) -> bool:
	"""Set the structure row of ``component`` to ``amount`` - added when missing (unless
	``add_missing`` is off), dropped at zero - and return whether the slip changed. Additional
	Salary rows are left as entered.

	The row no longer prorates: ``amount`` is already the contribution on the wages paid. An
	existing row keeps its full-month ``default_amount``, which coverage tests read.
	"""
	rows = [
		r for r in doc.get(parentfield) if r.salary_component == component and not r.get("additional_salary")
	]
	if not rows and not add_missing:
		return False
	if len(rows) <= 1 and flt(rows[0].amount if rows else 0) == flt(amount):
		return False

	setattr(doc, parentfield, [r for r in doc.get(parentfield) if not any(r is x for x in rows[1:])])
	if amount:
		row = rows[0] if rows else doc.append(parentfield, _structure_leg_row(component, amount))
		row.amount = amount
		row.additional_amount = 0
		row.depends_on_payment_days = 0
	elif rows:
		setattr(doc, parentfield, [r for r in doc.get(parentfield) if r is not rows[0]])
	return True


def refresh_gross_pay(doc) -> None:
	"""Recompute gross pay after an earning changed, as Salary Slip.calculate_net_pay does."""
	doc.gross_pay = doc.get_component_totals("earnings", depends_on_payment_days=1)
	doc.base_gross_pay = flt(flt(doc.gross_pay) * flt(doc.exchange_rate), doc.precision("base_gross_pay"))


def _structure_leg_row(component: str, amount: float) -> dict:
	c = frappe.get_cached_doc("Salary Component", component)
	return {
		"salary_component": c.name,
		"abbr": c.salary_component_abbr,
		"default_amount": amount,
		"depends_on_payment_days": 0,
		"do_not_include_in_total": c.do_not_include_in_total,
		"is_tax_applicable": c.is_tax_applicable,
		"is_flexible_benefit": c.is_flexible_benefit,
		"variable_based_on_taxable_salary": c.variable_based_on_taxable_salary,
		"exempted_from_income_tax": c.exempted_from_income_tax,
	}
