# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

"""
Wages under section 2(88) of the Code on Social Security, 2020 (in force from 21 Nov 2025).

ESIC (RO Vijayawada circular 62-P/11/12/CoSS/PICI/2026, 18 Sep 2026) and EPFO (wage-ceiling FAQ Q25)
apply the statutory ceilings and contributions to these wages, not to gross pay:

	wages = all remuneration - excluded items, where the excluded items (HRA, employer PF / pension
	contributions, bonus, commission, overtime, conveyance ...) count only up to one half of all
	remuneration; any excess is deemed remuneration and added back to wages.

On a salary slip the excluded items are the company's HRA component, employer PF / NPS contributions
carried as earnings by CTC-style structures, and anything paid through an Additional Salary
(incentives, bonus, arrears). Employer ESI is not remuneration payable to the employee, so it is left
out of both sides; this also keeps the employer ESI leg from depending on itself.
"""

import frappe
from frappe.utils import flt

EXCLUSION_LIMIT = 0.5

# Employer contributions to a provident or pension fund — excluded item (c) of section 2(88).
EMPLOYER_FUND_EARNINGS = frozenset({"Employer PF", "Employer NPS Contribution"})

# Paid to ESIC by the employer, never to the employee: not remuneration at all.
NOT_REMUNERATION_EARNINGS = frozenset({"Employer ESI"})


def split_remuneration(rows, hra_component: str | None, field: str = "amount") -> tuple[float, float]:
	"""Return (all remuneration, excluded items) for a slip's earning rows."""
	total = excluded = 0.0
	for row in rows:
		component = row.get("salary_component")
		if row.get("do_not_include_in_total") or component in NOT_REMUNERATION_EARNINGS:
			continue
		amount = flt(row.get(field))
		total += amount
		if (
			(hra_component and component == hra_component)
			or component in EMPLOYER_FUND_EARNINGS
			or row.get("additional_salary")
		):
			excluded += amount
	return total, excluded


def wages_from_split(total: float, excluded: float) -> float:
	"""Section 2(88): remuneration less exclusions, plus exclusions above one half of remuneration."""
	return flt(total - excluded + max(0.0, excluded - EXCLUSION_LIMIT * total), 2)


def get_hra_component(company: str | None) -> str | None:
	return frappe.db.get_value("Company", company, "hra_component") if company else None


def get_code_wages(doc, *, use_default_amount: bool = False) -> float:
	"""
	Section 2(88) wages on a salary slip.

	``use_default_amount`` reads the full-month (unprorated) amounts for coverage tests; Additional
	Salary rows carry no default amount, so a one-off payment never decides coverage. The plain
	``amount`` gives the wages actually paid this month, which contributions are levied on.
	"""
	field = "default_amount" if use_default_amount else "amount"
	return wages_from_split(*split_remuneration(doc.earnings, get_hra_component(doc.company), field))
