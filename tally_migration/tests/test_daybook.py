"""Pure tests for the Tally Day Book parser."""

import unittest

from tally_migration.tally.daybook import parse_daybook


SAMPLE = """<ENVELOPE>
<REQUESTDESC><STATICVARIABLES><SVCURRENTCOMPANY>Acme Ltd</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>
<BODY><REQUESTDATA>
<TALLYMESSAGE><VOUCHER REMOTEID="fallback-guid" VCHTYPE="Journal">
<DATE>20260401</DATE><VOUCHERNUMBER>1</VOUCHERNUMBER><ISCANCELLED>No</ISCANCELLED>
<ALLLEDGERENTRIES.LIST><LEDGERNAME>Cash</LEDGERNAME><AMOUNT>-100</AMOUNT></ALLLEDGERENTRIES.LIST>
<ALLLEDGERENTRIES.LIST><LEDGERNAME>Capital</LEDGERNAME><AMOUNT>100</AMOUNT></ALLLEDGERENTRIES.LIST>
</VOUCHER></TALLYMESSAGE>
<TALLYMESSAGE><VOUCHER VCHTYPE="Sales"><GUID>inventory-guid</GUID><DATE>20260402</DATE>
<ISCANCELLED>No</ISCANCELLED><ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>Widget</STOCKITEMNAME></ALLINVENTORYENTRIES.LIST>
</VOUCHER></TALLYMESSAGE>
<TALLYMESSAGE><VOUCHER VCHTYPE="Receipt"><GUID>cancelled-guid</GUID><DATE>20260403</DATE>
<ISCANCELLED>Yes</ISCANCELLED></VOUCHER></TALLYMESSAGE>
</REQUESTDATA></BODY></ENVELOPE>"""


class TestDayBookParser(unittest.TestCase):
    def test_extracts_company_voucher_and_entries(self):
        data = parse_daybook(SAMPLE)
        voucher = data.vouchers[0]
        self.assertEqual(data.source_company, "Acme Ltd")
        self.assertEqual(voucher["guid"], "fallback-guid")
        self.assertEqual(voucher["posting_date"], "2026-04-01")
        self.assertEqual(len(voucher["ledger_entries"]), 2)

    def test_summary_separates_safe_inventory_and_cancelled(self):
        summary = parse_daybook(SAMPLE).summary
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["supported"], 1)
        self.assertEqual(summary["inventory"], 1)
        self.assertEqual(summary["cancelled"], 1)

    def test_empty_inventory_list_is_not_inventory(self):
        xml = ("<ENVELOPE><VOUCHER VCHTYPE=\"Sales\"><GUID>svc</GUID><DATE>20260401</DATE>"
               "<ISCANCELLED>No</ISCANCELLED><ALLINVENTORYENTRIES.LIST></ALLINVENTORYENTRIES.LIST>"
               "<LEDGERENTRIES.LIST><LEDGERNAME>A</LEDGERNAME><AMOUNT>-5</AMOUNT></LEDGERENTRIES.LIST>"
               "<LEDGERENTRIES.LIST><LEDGERNAME>B</LEDGERNAME><AMOUNT>5</AMOUNT></LEDGERENTRIES.LIST>"
               "</VOUCHER></ENVELOPE>")
        data = parse_daybook(xml)
        self.assertFalse(data.vouchers[0]["has_inventory"])
        self.assertEqual(data.summary["supported"], 1)

    def test_invalid_xml_fails(self):
        with self.assertRaises(ValueError):
            parse_daybook("<ENVELOPE><VOUCHER>")


if __name__ == "__main__":
    unittest.main()
