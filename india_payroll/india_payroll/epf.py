# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# License: GNU General Public License v3. See license.txt

import datetime

import frappe
from frappe.utils import flt, getdate

from india_payroll.india_payroll.code_wages import get_code_wages
from india_payroll.india_payroll.utils import (
	get_slip_ssa_values,
	refresh_gross_pay,
	set_structure_leg,
	structure_carries,
)

# Employee deductions (reduce net pay).  Employer EPF/EPS/EDLI/Admin are
# components of type "Employer Contribution" and live on the Salary Structure's
# employer_contributions table — they're rolled into CTC by Salary Structure
# Assignment and are not injected onto the slip.
EPF_EMPLOYEE_COMPONENT = "Provident Fund"
VPF_COMPONENT = "Voluntary Provident Fund"

EPF_EMPLOYEE_COMPONENTS = (EPF_EMPLOYEE_COMPONENT, VPF_COMPONENT)

# CTC-style structures carry the employer's matching 12% as this earning, taken back by
# the deduction of the same amount; apply_epf sizes both to the employee's share.
EMPLOYER_PF_EARNING = "Employer PF"
EMPLOYER_PF_DEDUCTION = "Provident Fund - Employer"

# --- Statutory constants --------------------------------------------------
# Employer-side rates remain here even though the slip hook no longer applies
# them — the EPF register / ECR report reads them when reconstructing the
# canonical employer split per EPFO statute.
# S.O. 5109(E) raised the PF / EPS / EDLI wage ceiling from ₹15,000 to ₹25,000
# with effect from 17 September 2026. Use the helpers below rather than the
# constants directly, so wage periods before the revision keep the old ceiling.
EPF_WAGE_CEILING = 25_000  # PF / EPS / EDLI statutory ceiling from 17 Sep 2026
EPF_PREVIOUS_WAGE_CEILING = 15_000  # ceiling up to 16 Sep 2026
EPF_WAGE_CEILING_EFFECTIVE_FROM = datetime.date(2026, 9, 17)
EPF_EMPLOYEE_RATE = 0.12  # employee EPF share
EPF_EMPLOYER_RATE = 0.12  # employer total share (split between EPF + EPS)
EPS_RATE = 0.0833  # employer's pension diversion
EDLI_RATE = 0.005  # employer's EDLI premium
EPF_ADMIN_RATE = 0.005  # employer's EPF admin charges


def apply_epf(doc, method=None) -> None:
	"""
	Salary Slip regional deduction hook (see apply_regional_deductions).

	Computes and injects employee EPF-scheme rows on the slip:
	  • Employee contribution (12 %)   → deductions
	  • VPF top-up (optional)          → deductions

	Employer contributions (EPF / EPS / EDLI / Admin) are configured as
	"Employer Contribution" components on the Salary Structure and handled
	by Salary Structure Assignment / CTC — not by this hook. A CTC-style
	structure that carries them as an "Employer PF" earning and "Provident
	Fund - Employer" deduction has both sized to the employee's share.

	Gated by a single `epf_applicable` flag on the Salary Structure Assignment.
	All employees are assumed to be post-1 Sept 2014 EPF members.
	"""
	if not frappe.db.get_single_value("Payroll Settings", "enable_epf"):
		_remove_epf_components(doc)
		return

	if not doc.salary_structure:
		return

	# EPF config (opt-in flag, actual-wage rule, VPF election) lives on the
	# assignment so it can be set per SSA and revised mid-year.
	ssa = get_slip_ssa_values(
		doc,
		["epf_applicable", "contribute_on_actual_pf_wage", "vpf_mode", "vpf_percentage", "vpf_amount"],
	)

	if not ssa.get("epf_applicable"):
		_remove_epf_components(doc)
		return

	if not _required_components_exist():
		frappe.msgprint(
			frappe._(
				"One or more EPF Salary Components are missing. "
				"Please reinstall the India Payroll app or create them manually."
			),
			indicator="orange",
			alert=True,
		)
		return

	contribute_on_actual = bool(ssa.get("contribute_on_actual_pf_wage"))
	sizes_employer_leg = structure_carries(doc.salary_structure, "earnings", EMPLOYER_PF_EARNING)

	# The employer's PF is itself remuneration in the section 2(88) one-half test, so in a
	# month with one-off pay the PF wage and the employer leg depend on each other. Each
	# round moves the wage by 6% of the previous move, so a few rounds settle it.
	for _ in range(5):
		pf_wage = _compute_pf_wage(doc)
		epf_base = (
			pf_wage if contribute_on_actual else get_capped_pf_wage(pf_wage, doc.start_date, doc.end_date)
		)
		employee_epf = _epfo_round(epf_base * EPF_EMPLOYEE_RATE) if pf_wage > 0 else 0
		if not sizes_employer_leg or not _set_employer_pf_leg(doc, employee_epf):
			break

	if pf_wage <= 0:
		# Nothing to contribute on (e.g. no components flagged as PF wage)
		_remove_epf_components(doc)
		return

	vpf = _compute_vpf(
		doc,
		epf_base,
		vpf_mode=ssa.get("vpf_mode"),
		vpf_percentage=ssa.get("vpf_percentage"),
		vpf_amount=ssa.get("vpf_amount"),
	)

	_apply_epf_components(doc, employee_epf=employee_epf, vpf=vpf)


def _set_employer_pf_leg(doc, amount: float) -> bool:
	"""Match the "Employer PF" earning and its "Provident Fund - Employer" deduction to the
	employee's share. Returns whether the earning changed, which moves the PF wage."""
	changed = set_structure_leg(doc, "earnings", EMPLOYER_PF_EARNING, amount)
	if structure_carries(doc.salary_structure, "deductions", EMPLOYER_PF_DEDUCTION):
		set_structure_leg(doc, "deductions", EMPLOYER_PF_DEDUCTION, amount)
	if changed:
		refresh_gross_pay(doc)
	return changed


# ---------------------------------------------------------------------------
# Wage ceiling
# ---------------------------------------------------------------------------


def _wage_ceiling_parts(start_date, end_date) -> list[tuple[int, float]]:
	"""
	(ceiling, share of the wage period) pairs covering the period.

	A period that spans 17 Sep 2026 is split by calendar days, each part
	under its own ceiling — the September 2026 method in EPFO's FAQ on
	S.O. 5109(E) (Q7, Q9).
	"""
	start, end = getdate(start_date), getdate(end_date)
	if end < EPF_WAGE_CEILING_EFFECTIVE_FROM:
		return [(EPF_PREVIOUS_WAGE_CEILING, 1.0)]
	if start >= EPF_WAGE_CEILING_EFFECTIVE_FROM:
		return [(EPF_WAGE_CEILING, 1.0)]

	total_days = (end - start).days + 1
	days_before = (EPF_WAGE_CEILING_EFFECTIVE_FROM - start).days
	return [
		(EPF_PREVIOUS_WAGE_CEILING, days_before / total_days),
		(EPF_WAGE_CEILING, (total_days - days_before) / total_days),
	]


def get_capped_pf_wage(pf_wage: float, start_date, end_date) -> float:
	"""PF wage limited to the statutory ceiling in force during the wage period."""
	return sum(min(pf_wage, ceiling) * share for ceiling, share in _wage_ceiling_parts(start_date, end_date))


def get_eps_wage(pf_wage: float, start_date, end_date) -> float:
	"""
	EPS wage for a post-1 Sept 2014 member: only the parts of the period in
	which the PF wage is within that part's ceiling earn EPS. A ₹20,000 earner
	therefore joins EPS from 17 Sep 2026 (FAQ Q9, Q12).
	"""
	return sum(
		min(pf_wage, ceiling) * share
		for ceiling, share in _wage_ceiling_parts(start_date, end_date)
		if pf_wage <= ceiling
	)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _required_components_exist() -> bool:
	"""Both employee-side EPF salary components must exist before we inject rows."""
	for name in EPF_EMPLOYEE_COMPONENTS:
		if not frappe.db.exists("Salary Component", name):
			return False
	return True


def _compute_pf_wage(doc) -> float:
	"""
	PF wage: section 2(88) wages of the Code on Social Security, 2020 (see
	code_wages.get_code_wages). EPFO applies the ceiling to these wages, not
	to gross pay (wage-ceiling FAQ Q24-Q25).

	HRA, employer PF/ESI carried as earnings by CTC-style structures, and
	anything paid through an Additional Salary (bonus, incentive, arrears) are
	left out — but only up to one half of remuneration; the excess is added
	back, so an incentive month can raise the PF wage.

	Amounts on `doc.earnings` are already prorated for LOP / payment_days by
	the Salary Slip controller, so the returned PF wage reflects NCP days.
	"""
	return get_code_wages(doc)


def _compute_vpf(doc, epf_base: float, *, vpf_mode=None, vpf_percentage=None, vpf_amount=None) -> float:
	"""
	Voluntary Provident Fund — additional employee contribution above 12 %.

	The VPF election lives on the Salary Structure Assignment and is passed in
	by the caller. Two modes (default Amount):
	  • ``Amount`` — a fixed ``vpf_amount`` the employee elected per month,
	    prorated by payment_days / total_working_days so LOP months don't
	    over-deduct.
	  • ``Percentage`` — ``vpf_percentage`` of the EPF base (which already
	    follows the contribute-on-actual / capped rule and slip proration).

	The employer does not match VPF.  Falls back to Amount mode when
	``vpf_mode`` is unset (existing records before the field was added);
	combined with vpf_amount defaulting to 0, this means no surprise VPF
	deduction appears for employees who never opted in.
	"""
	if (vpf_mode or "Amount") == "Amount":
		amount = flt(vpf_amount)
		if amount <= 0:
			return 0.0
		total_days = flt(doc.total_working_days)
		if total_days > 0:
			amount = amount * flt(doc.payment_days) / total_days
		return _epfo_round(amount)

	vpf_pct = flt(vpf_percentage)
	if vpf_pct <= 0:
		return 0.0
	return _epfo_round(epf_base * vpf_pct / 100.0)


def _apply_epf_components(doc, *, employee_epf: float, vpf: float) -> None:
	"""Replace any existing employee EPF rows on the slip with fresh amounts."""
	doc.deductions = [d for d in doc.deductions if d.salary_component not in EPF_EMPLOYEE_COMPONENTS]

	if employee_epf > 0:
		doc.append(
			"deductions",
			{"salary_component": EPF_EMPLOYEE_COMPONENT, "amount": employee_epf},
		)

	if vpf > 0:
		doc.append(
			"deductions",
			{"salary_component": VPF_COMPONENT, "amount": vpf},
		)


def _remove_epf_components(doc) -> None:
	"""Strip employee EPF rows from deductions."""
	doc.deductions = [d for d in doc.deductions if d.salary_component not in EPF_EMPLOYEE_COMPONENTS]


def _epfo_round(amount: float) -> int:
	"""
	Round to the nearest rupee per EPFO conventions (half-up).

	Python's built-in `round()` uses banker's rounding, which can give
	surprising results at .5 boundaries (e.g. EPS = 8.33 % * 15,000 = 1249.5
	must become ₹1,250, not ₹1,249).  We use explicit half-up here.
	"""
	a = flt(amount)
	if a >= 0:
		return int(a + 0.5)
	return -int(-a + 0.5)
