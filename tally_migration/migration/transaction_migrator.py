"""Import accounting-only Tally Day Book vouchers as Journal Entries."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from tally_migration.erpnext.importers.base import ImportResult, atomic
from tally_migration.migration.master_migrator import notify_run_finished
from tally_migration.naming import company_scoped
from tally_migration.tally.daybook import SUPPORTED_VOUCHER_TYPES, parse_daybook
from tally_migration.tally.mappings import is_system_ledger


COMMIT_BATCH_SIZE = 50


def create_transaction_log(company: str, source_file: str, file_name: str):
    log = frappe.new_doc("Tally Migration Log")
    log.company = company
    log.tally_company = f"File: {file_name or source_file}"
    log.source_file = source_file
    log.migration_type = "Transactions"
    log.status = "Running"
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log


def masters_opening_date(company: str) -> str | None:
    """The opening-balance date of the company's latest completed Masters import, or None.

    Opening balances already carry every transaction up to this date, so Day Book vouchers
    dated before it must not be imported again."""
    value = frappe.db.get_value(
        "Tally Migration Log",
        {"company": company, "migration_type": "Masters",
         "status": ["in", ["Completed", "Completed with Errors"]],
         "posting_date": ["is", "set"]},
        "posting_date", order_by="creation desc")
    return str(value) if value else None


class TransactionMigrator:
    """Import safe accounting vouchers and report unsupported inventory vouchers."""

    def __init__(self, company: str, raw_source: bytes | str, log):
        self.company = company
        self.raw_source = raw_source
        self.log = log
        self.abbr = frappe.get_cached_value("Company", company, "abbr") or ""
        self._control_accounts: dict[str, str] = {}
        self.opening_date = masters_opening_date(company)
        self.pre_opening = 0

    def run(self) -> dict:
        result = ImportResult("Journal Entry")
        try:
            self._progress(5, "Reading Day Book file...")
            daybook = parse_daybook(self.raw_source)
            if daybook.source_company:
                self.log.db_set("tally_company", daybook.source_company, update_modified=False)

            self._ensure_custom_fields()
            existing = self._existing_guids()
            total = len(daybook.vouchers)
            self._progress(10, f"Found {total} vouchers.")

            pending = 0
            for index, voucher in enumerate(daybook.vouchers, 1):
                self._import_one(voucher, existing, result)
                pending += 1
                if pending >= COMMIT_BATCH_SIZE:
                    frappe.db.commit()
                    pending = 0
                self._progress(10 + int(index * 80 / max(total, 1)), f"Importing voucher {index} of {total}...")
            frappe.db.commit()

            if self.pre_opening:
                result.add_warning(
                    f"{self.pre_opening} vouchers",
                    f"Dated before the Masters opening-balance date ({self.opening_date}), so "
                    "they are already included in the opening balances and were skipped")
            summary = {"Transactions": result.as_dict()}
            self._finalize(daybook.summary, result, summary)
            self._progress(100, "Day Book migration complete.")
            return {**summary, "log_name": self.log.name}
        except Exception as exc:
            self._fail(exc)
            raise

    def _import_one(self, voucher: dict, existing: set[str], result: ImportResult) -> None:
        label = self._label(voucher)
        if voucher["cancelled"]:
            result.skipped += 1
            return
        if voucher["has_inventory"]:
            result.skipped += 1
            result.add_warning(label, "Inventory voucher was not imported by the accounting-only Day Book importer")
            return
        if self.opening_date and voucher["posting_date"] and voucher["posting_date"] < self.opening_date:
            result.skipped += 1
            self.pre_opening += 1
            return
        if voucher["voucher_type"] not in SUPPORTED_VOUCHER_TYPES:
            result.skipped += 1
            result.add_warning(label, f"Unsupported Tally voucher type '{voucher['voucher_type'] or 'Unknown'}'")
            return
        system_ledger = next(
            (e["ledger"] for e in voucher["ledger_entries"] if is_system_ledger(e["ledger"])), None)
        if system_ledger:
            # Tally's own system ledgers (e.g. "Profit & Loss A/c") are never migrated
            # as Accounts - ERPNext computes its P&L itself - so there is no account to
            # post this leg to. Skip rather than guess at a substitute account.
            result.skipped += 1
            result.add_warning(
                label,
                f"References Tally's system ledger '{system_ledger}', which ERPNext "
                "maintains itself and has no matching Account - post this entry "
                "manually in ERPNext if it is needed",
            )
            return
        if not voucher["guid"]:
            result.add_error(label, "Tally GUID is missing, so the voucher cannot be imported safely")
            return
        if voucher["guid"] in existing:
            result.skipped += 1
            return

        try:
            with atomic():
                accounts, voucher_type = self._accounts(voucher)
                doc = frappe.get_doc(
                    {
                        "doctype": "Journal Entry",
                        "voucher_type": voucher_type,
                        "company": self.company,
                        "posting_date": voucher["posting_date"],
                        "user_remark": voucher["narration"] or self._label(voucher),
                        "tally_guid": voucher["guid"],
                        "tally_voucher_no": voucher["voucher_number"],
                        "accounts": accounts,
                    }
                )
                doc.insert(ignore_permissions=True)
                doc.submit()
                result.add_created(doc.name, "Journal Entry", self._label(voucher))
            existing.add(voucher["guid"])
        except Exception as exc:
            result.add_error(label, exc)

    def _accounts(self, voucher: dict) -> tuple[list[dict], str]:
        rows = []
        debit = Decimal("0")
        credit = Decimal("0")
        for entry in voucher["ledger_entries"]:
            if not entry["ledger"]:
                continue
            amount = _signed_amount(entry["amount"])
            if not amount:
                continue
            party = self._party(entry["ledger"]) if entry["is_party"] else None
            if party:
                # A Customer/Supplier ledger has no Account of its own in the chart of
                # accounts - it posts through the company's control account - so this
                # must not fall through to _account_name, which would look for (and
                # fail to find) a literal Account named after the party.
                row = {"account": self._control_account(party["party_type"]), **party}
            else:
                row = {"account": self._account_name(entry["ledger"])}
            if amount < 0:
                row["debit_in_account_currency"] = abs(amount)
                debit += abs(amount)
            else:
                row["credit_in_account_currency"] = amount
                credit += amount
            rows.append(row)

        if len(rows) < 2:
            frappe.throw("Voucher does not contain at least two usable ledger entries")
        if abs(debit - credit) > Decimal("0.01"):
            frappe.throw(f"Voucher is not balanced: debit {debit} does not equal credit {credit}")

        # ERPNext refuses a plain "Journal Entry" that debits/credits a Depreciation-
        # type account (see Journal Entry.validate_depr_account_and_depr_entry_voucher_
        # type) - it must be typed "Depreciation Entry" instead. We have no Asset to
        # link (Tally's Day Book carries no such reference), so this posts as an
        # unlinked depreciation entry - the same shape an accountant gets recording
        # depreciation without ERPNext's Asset module; it does not touch any Asset's
        # depreciation schedule.
        voucher_type = "Depreciation Entry" if self._touches_depreciation_account(rows) else "Journal Entry"
        return rows, voucher_type

    @staticmethod
    def _touches_depreciation_account(rows: list[dict]) -> bool:
        return any(
            frappe.get_cached_value("Account", row["account"], "account_type") == "Depreciation"
            for row in rows
        )

    def _account_name(self, tally_name: str) -> str:
        name = company_scoped(tally_name, self.abbr)
        row = frappe.db.get_value(
            "Account", {"name": name, "company": self.company, "is_group": 0}, "name"
        )
        if not row:
            frappe.throw(f"Account '{tally_name}' does not exist for {self.company}")
        return row

    def _party(self, tally_name: str) -> dict | None:
        customer = frappe.db.get_value("Customer", {"customer_name": tally_name}, "name")
        supplier = frappe.db.get_value("Supplier", {"supplier_name": tally_name}, "name")
        if customer and supplier:
            frappe.throw(
                f"'{tally_name}' exists as both a Customer and Supplier. "
                "Rename one record before importing this voucher."
            )
        if customer:
            return {"party_type": "Customer", "party": customer}
        if supplier:
            return {"party_type": "Supplier", "party": supplier}
        return None

    def _control_account(self, party_type: str) -> str:
        if party_type in self._control_accounts:
            return self._control_accounts[party_type]
        field = "default_receivable_account" if party_type == "Customer" else "default_payable_account"
        account_type = "Receivable" if party_type == "Customer" else "Payable"
        account = frappe.get_cached_value("Company", self.company, field)
        if not account:
            account = frappe.db.get_value(
                "Account",
                {"company": self.company, "account_type": account_type, "is_group": 0},
                "name",
            )
        if not account:
            frappe.throw(f"Company has no {account_type} account")
        self._control_accounts[party_type] = account
        return account

    def _existing_guids(self) -> set[str]:
        return set(
            frappe.get_all(
                "Journal Entry",
                filters={"company": self.company, "tally_guid": ["!=", ""]},
                pluck="tally_guid",
                limit_page_length=0,
            )
        )

    @staticmethod
    def _ensure_custom_fields() -> None:
        fields = [
            {
                "fieldname": "tally_guid",
                "label": "Tally GUID",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "tally_voucher_no",
                "label": "Tally Voucher Number",
                "fieldtype": "Data",
                "read_only": 1,
                "no_copy": 1,
            },
        ]
        create_custom_fields({"Journal Entry": fields})
        frappe.db.commit()

    def _finalize(self, counts: dict, result: ImportResult, summary: dict) -> None:
        self.log.reload()
        self.log.status = "Completed with Errors" if result.failed else "Completed"
        self.log.extracted_counts = frappe.as_json(counts)
        self.log.import_summary = frappe.as_json(summary)
        self.log.created_records = frappe.as_json({"Transactions": result.created_docs})
        self.log.set("errors", [])
        for error in result.errors:
            self.log.append(
                "errors",
                {
                    "status": "Failed",
                    "record_type": "Transactions",
                    "record_name": error["name"],
                    "reason": error["reason"],
                },
            )
        for warning in result.warnings:
            self.log.append(
                "errors",
                {
                    "status": "Skipped",
                    "record_type": "Transactions",
                    "record_name": warning["name"],
                    "reason": warning["reason"],
                },
            )
        self.log.error_log = "\n".join(
            [f"[Failed] {x['name']}: {x['reason']}" for x in result.errors]
            + [f"[Skipped] {x['name']}: {x['reason']}" for x in result.warnings]
        )
        self.log.save(ignore_permissions=True)
        frappe.db.commit()
        notify_run_finished(self.log)

    def _fail(self, exc: Exception) -> None:
        frappe.db.rollback()
        self.log.reload()
        self.log.status = "Failed"
        self.log.error_log = frappe.get_traceback() or str(exc)
        self.log.save(ignore_permissions=True)
        frappe.db.commit()
        notify_run_finished(self.log)

    def _progress(self, percent: int, description: str) -> None:
        payload = {
            "title": "Tally Day Book Migration",
            "percent": percent,
            "description": description,
        }
        try:
            frappe.publish_realtime("tally_migration_progress", payload, user=self.log.owner)
            frappe.cache().set_value(
                f"tally_migration_progress:{self.log.name}",
                {"percent": percent, "description": description},
                expires_in_sec=6 * 60 * 60,
            )
        except Exception:
            # Progress is helpful but must never make an otherwise valid import fail.
            pass

    @staticmethod
    def _label(voucher: dict) -> str:
        number = voucher["voucher_number"] or voucher["guid"] or "Unknown"
        return f"{voucher['voucher_type'] or 'Voucher'} {number}"


_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _signed_amount(value: str) -> Decimal:
    raw = (value or "").replace(",", "").strip()
    if not raw:
        return Decimal("0")
    segment = raw.rsplit("=", 1)[-1].strip()
    numbers = _NUMBER.findall(segment)
    if not numbers:
        raise InvalidOperation(f"Could not read amount '{value}'")
    amount = Decimal(numbers[-1])
    if segment.startswith("-"):
        amount = -abs(amount)
    return amount
