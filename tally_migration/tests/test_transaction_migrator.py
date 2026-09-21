"""Integration tests for the accounting-only Day Book (Transaction) migrator.

Hits a real Frappe/ERPNext database - run via ``bench run-tests``. Journal Entries
are submitted documents, so cleanup cancels + deletes them explicitly (a plain
rollback cannot undo a commit, and delete_doc refuses to delete a submitted doc).
"""
import unittest
from xml.sax.saxutils import escape

import frappe

from tally_migration.migration.transaction_migrator import (
    TransactionMigrator,
    create_transaction_log,
)
from tally_migration.tests.utils import TEST_PREFIX, require_company


def _voucher_xml(guid, vtype, date, entries, narration="", cancelled=False, inventory=False):
    lines = "".join(
        f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(ledger)}</LEDGERNAME><AMOUNT>{amount}</AMOUNT>"
        f"<ISPARTYLEDGER>{'Yes' if is_party else 'No'}</ISPARTYLEDGER></ALLLEDGERENTRIES.LIST>"
        for ledger, amount, is_party in entries
    )
    inv = (
        "<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Widget</STOCKITEMNAME></ALLINVENTORYENTRIES.LIST>"
        if inventory else ""
    )
    return (
        f'<TALLYMESSAGE><VOUCHER VCHTYPE="{vtype}"><GUID>{guid}</GUID><DATE>{date}</DATE>'
        f"<VOUCHERNUMBER>{guid}</VOUCHERNUMBER>"
        f'<ISCANCELLED>{"Yes" if cancelled else "No"}</ISCANCELLED>'
        f"<NARRATION>{narration}</NARRATION>{lines}{inv}</VOUCHER></TALLYMESSAGE>"
    )


def _envelope(*vouchers) -> str:
    return f'<ENVELOPE><BODY><REQUESTDATA>{"".join(vouchers)}</REQUESTDATA></BODY></ENVELOPE>'


class TestTransactionMigrator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        cls.company = require_company()
        accounts = frappe.get_all(
            "Account",
            filters={
                "company": cls.company,
                "is_group": 0,
                "account_type": ["not in", ["Receivable", "Payable"]],
            },
            fields=["name", "account_name"],
            order_by="name",
            limit_page_length=2,
        )
        if len(accounts) < 2:
            raise unittest.SkipTest(
                "Company does not have two non-party leaf accounts to test with")
        cls.acc_a, cls.acc_b = accounts[0], accounts[1]
        # The importer looks ledgers up by their Tally name (= Account.account_name);
        # the full ERPNext name is "<account_name> - <ABBR>".
        cls.ledger_a = cls.acc_a.account_name
        cls.ledger_b = cls.acc_b.account_name

    def _run(self, xml):
        log = create_transaction_log(self.company, "", "test.xml")
        self._logs_to_clean.append(log.name)
        TransactionMigrator(self.company, xml, log).run()
        log.reload()
        return log

    def setUp(self):
        self._logs_to_clean = []

    def tearDown(self):
        for name in self._logs_to_clean:
            frappe.delete_doc(
                "Tally Migration Log", name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def _cleanup_je(self, guid):
        for name in frappe.get_all(
                "Journal Entry", filters={"tally_guid": guid}, pluck="name"):
            doc = frappe.get_doc("Journal Entry", name)
            if doc.docstatus == 1:
                doc.cancel()
            frappe.delete_doc("Journal Entry", name, force=True, ignore_permissions=True)
        frappe.db.commit()

    def test_balanced_voucher_creates_submitted_journal_entry(self):
        guid = f"{TEST_PREFIX}-je-1"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260401", [
            (self.ledger_a, "-500", False),
            (self.ledger_b, "500", False),
        ]))
        try:
            log = self._run(xml)
            self.assertEqual(log.status, "Completed", msg=log.error_log)
            names = frappe.get_all(
                "Journal Entry", filters={"tally_guid": guid}, pluck="name")
            self.assertEqual(len(names), 1)
            doc = frappe.get_doc("Journal Entry", names[0])
            self.assertEqual(doc.docstatus, 1)
            self.assertEqual(len(doc.accounts), 2)
        finally:
            self._cleanup_je(guid)

    def test_rerun_skips_existing_guid(self):
        guid = f"{TEST_PREFIX}-je-2"
        xml = _envelope(_voucher_xml(guid, "Payment", "20260402", [
            (self.ledger_a, "-200", False),
            (self.ledger_b, "200", False),
        ]))
        try:
            self._run(xml)
            log2 = self._run(xml)  # re-run the same voucher (same GUID)
            names = frappe.get_all(
                "Journal Entry", filters={"tally_guid": guid}, pluck="name")
            self.assertEqual(
                len(names), 1, "re-run must not create a duplicate Journal Entry")
            summary = frappe.parse_json(log2.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["skipped"], 1)
        finally:
            self._cleanup_je(guid)

    def test_inventory_voucher_is_skipped_with_warning(self):
        guid = f"{TEST_PREFIX}-je-3"
        xml = _envelope(_voucher_xml(guid, "Sales", "20260403", [
            (self.ledger_a, "-300", False),
            (self.ledger_b, "300", False),
        ], inventory=True))
        try:
            log = self._run(xml)
            summary = frappe.parse_json(log.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["warned"], 1)
            self.assertFalse(frappe.db.exists("Journal Entry", {"tally_guid": guid}))
        finally:
            self._cleanup_je(guid)

    def test_cancelled_voucher_is_skipped_silently(self):
        guid = f"{TEST_PREFIX}-je-4"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260404", [
            (self.ledger_a, "-100", False),
            (self.ledger_b, "100", False),
        ], cancelled=True))
        try:
            log = self._run(xml)
            summary = frappe.parse_json(log.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertEqual(summary["warned"], 0)
            self.assertEqual(summary["failed"], 0)
        finally:
            self._cleanup_je(guid)

    def test_unbalanced_voucher_is_recorded_as_error_not_fatal(self):
        guid = f"{TEST_PREFIX}-je-5"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260405", [
            (self.ledger_a, "-500", False),
            (self.ledger_b, "400", False),
        ]))
        try:
            log = self._run(xml)
            # A single bad voucher must not fail the whole run.
            self.assertEqual(log.status, "Completed with Errors")
            summary = frappe.parse_json(log.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["failed"], 1)
            self.assertIn("not balanced", summary["errors"][0]["reason"].lower())
        finally:
            self._cleanup_je(guid)

    def test_party_ledger_posts_to_control_account(self):
        customer_name = f"{TEST_PREFIX} Daybook Customer"
        receivable = frappe.get_cached_value(
            "Company", self.company, "default_receivable_account"
        ) or frappe.db.get_value(
            "Account",
            {"company": self.company, "account_type": "Receivable", "is_group": 0},
            "name",
        )
        if not receivable:
            raise unittest.SkipTest("Company has no Receivable account configured")
        if not frappe.db.exists("Customer", {"customer_name": customer_name}):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_group": frappe.db.get_value(
                    "Customer Group", {"is_group": 0}, "name") or "All Customer Groups",
                "territory": frappe.db.get_value(
                    "Territory", {"is_group": 0}, "name") or "All Territories",
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        guid = f"{TEST_PREFIX}-je-6"
        xml = _envelope(_voucher_xml(guid, "Receipt", "20260406", [
            (customer_name, "-150", True),
            (self.ledger_a, "150", False),
        ]))
        try:
            log = self._run(xml)
            names = frappe.get_all(
                "Journal Entry", filters={"tally_guid": guid}, pluck="name")
            self.assertEqual(len(names), 1, msg=log.error_log)
            doc = frappe.get_doc("Journal Entry", names[0])
            party_row = next(r for r in doc.accounts if r.party)
            self.assertEqual(party_row.account, receivable)
            self.assertEqual(party_row.party_type, "Customer")
            self.assertEqual(party_row.party, customer_name)
        finally:
            self._cleanup_je(guid)
            frappe.delete_doc(
                "Customer", customer_name, force=True, ignore_permissions=True)
            frappe.db.commit()

    def test_system_ledger_is_skipped_with_warning(self):
        """Tally's 'Profit & Loss A/c' is never migrated as an Account (ERPNext
        computes its own P&L), so a voucher referencing it must be skipped with a
        clear warning instead of failing with 'Account ... does not exist'."""
        guid = f"{TEST_PREFIX}-je-7"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260407", [
            ("Profit & Loss A/c", "-250", False),
            (self.ledger_a, "250", False),
        ]))
        try:
            log = self._run(xml)
            summary = frappe.parse_json(log.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["failed"], 0)
            self.assertEqual(summary["warned"], 1)
            self.assertIn("system ledger", summary["warnings"][0]["reason"].lower())
            self.assertFalse(frappe.db.exists("Journal Entry", {"tally_guid": guid}))
        finally:
            self._cleanup_je(guid)

    def test_voucher_before_opening_date_is_skipped_and_counted(self):
        from unittest import mock
        guid = f"{TEST_PREFIX}-je-9"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260301", [
            (self.ledger_a, "-90", False),
            (self.ledger_b, "90", False),
        ]))
        try:
            with mock.patch(
                    "tally_migration.migration.transaction_migrator.masters_opening_date",
                    return_value="2026-04-01"):
                log = self._run(xml)
            summary = frappe.parse_json(log.import_summary)["Transactions"]
            self.assertEqual(summary["created"], 0)
            self.assertEqual(summary["skipped"], 1)
            self.assertIn("opening", summary["warnings"][0]["reason"].lower())
            self.assertFalse(frappe.db.exists("Journal Entry", {"tally_guid": guid}))
        finally:
            self._cleanup_je(guid)

    def test_depreciation_account_sets_depreciation_entry_voucher_type(self):
        depreciation_account = frappe.db.get_value(
            "Account",
            {"company": self.company, "account_type": "Depreciation", "is_group": 0},
            ["name", "account_name"], as_dict=True,
        )
        if not depreciation_account:
            raise unittest.SkipTest("Company has no Depreciation account configured")
        guid = f"{TEST_PREFIX}-je-8"
        xml = _envelope(_voucher_xml(guid, "Journal", "20260408", [
            (depreciation_account.account_name, "-75", False),
            (self.ledger_a, "75", False),
        ]))
        try:
            log = self._run(xml)
            names = frappe.get_all(
                "Journal Entry", filters={"tally_guid": guid}, pluck="name")
            self.assertEqual(len(names), 1, msg=log.error_log)
            doc = frappe.get_doc("Journal Entry", names[0])
            self.assertEqual(doc.voucher_type, "Depreciation Entry")
        finally:
            self._cleanup_je(guid)


if __name__ == "__main__":
    unittest.main()
