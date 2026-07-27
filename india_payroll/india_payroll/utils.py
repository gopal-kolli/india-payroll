# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import frappe


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
	values = get_effective_ssa_values(
		doc.employee, doc.company, doc.salary_structure, doc.start_date, fields
	)
	if values or not doc.end_date:
		return values

	# A mid-month joiner can have the first SSA effective after the payroll
	# period's start date. Frappe HR still creates and prorates that employee's
	# slip correctly, so fall back to the period end for the statutory fields.
	# Keep the start-date lookup first so mid-month revisions for existing
	# employees do not silently change the assignment used by the core slip.
	return get_effective_ssa_values(
		doc.employee, doc.company, doc.salary_structure, doc.end_date, fields
	)
