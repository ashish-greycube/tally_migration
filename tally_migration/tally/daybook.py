"""Parse Tally Day Book XML without retaining the XML tree."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

from tally_migration.tally.file_source import (
    decode_tally_bytes,
    reject_unsafe_xml,
    sanitize_tally_xml,
)


SUPPORTED_VOUCHER_TYPES = {
    "Journal",
    "Receipt",
    "Payment",
    "Contra",
    "Sales",
    "Purchase",
    "Credit Note",
    "Debit Note",
}

_LEDGER_LIST_TAGS = {"ALLLEDGERENTRIES.LIST", "LEDGERENTRIES.LIST"}
_INVENTORY_LIST_TAGS = {
    "INVENTORYENTRIES.LIST",
    "ALLINVENTORYENTRIES.LIST",
    "INVENTORYENTRIESIN.LIST",
    "INVENTORYENTRIESOUT.LIST",
}


@dataclass
class DayBookData:
    source_company: str = ""
    vouchers: list[dict] = field(default_factory=list)

    @property
    def summary(self) -> dict:
        by_type = Counter(v["voucher_type"] or "Unknown" for v in self.vouchers)
        cancelled = sum(v["cancelled"] for v in self.vouchers)
        inventory = sum(v["has_inventory"] for v in self.vouchers if not v["cancelled"])
        supported = sum(
            not v["cancelled"]
            and not v["has_inventory"]
            and v["voucher_type"] in SUPPORTED_VOUCHER_TYPES
            for v in self.vouchers
        )
        return {
            "source_company": self.source_company,
            "total": len(self.vouchers),
            "supported": supported,
            "cancelled": cancelled,
            "inventory": inventory,
            "unsupported": len(self.vouchers) - supported - cancelled - inventory,
            "by_type": dict(sorted(by_type.items())),
        }


def parse_daybook(source: bytes | str) -> DayBookData:
    """Return source metadata and normalized vouchers from a Day Book export."""
    text = source if isinstance(source, str) else decode_tally_bytes(source)
    cleaned = sanitize_tally_xml(text)
    reject_unsafe_xml(cleaned)

    data = DayBookData()
    try:
        for _event, elem in ET.iterparse(io.StringIO(cleaned), events=("end",)):
            tag = _local_name(elem.tag)
            if tag == "SVCURRENTCOMPANY" and not data.source_company:
                data.source_company = (elem.text or "").strip()
            elif tag == "REMOTECMPNAME" and not data.source_company:
                data.source_company = (elem.text or "").strip()
            elif tag == "VOUCHER":
                data.vouchers.append(_parse_voucher(elem))
                elem.clear()
    except ET.ParseError as exc:
        raise ValueError(f"Uploaded file is not valid Tally XML: {exc}") from exc
    return data


def _parse_voucher(elem) -> dict:
    entries = []
    has_inventory = False
    for child in list(elem):
        tag = _local_name(child.tag)
        if tag in _LEDGER_LIST_TAGS:
            entries.append(
                {
                    "ledger": _text(child, "LEDGERNAME"),
                    "amount": _text(child, "AMOUNT"),
                    "is_party": _text(child, "ISPARTYLEDGER").casefold() == "yes",
                }
            )
        elif tag in _INVENTORY_LIST_TAGS and _text(child, "STOCKITEMNAME"):
            # Tally service invoices carry an empty inventory list; only a named stock
            # item makes the voucher an inventory voucher.
            has_inventory = True

    return {
        "guid": _text(elem, "GUID") or (elem.get("REMOTEID") or "").strip(),
        "voucher_number": _text(elem, "VOUCHERNUMBER"),
        "voucher_type": _text(elem, "VOUCHERTYPENAME") or (elem.get("VCHTYPE") or "").strip(),
        "posting_date": _tally_date(_text(elem, "DATE")),
        "narration": _text(elem, "NARRATION"),
        "cancelled": _text(elem, "ISCANCELLED").casefold() == "yes",
        "has_inventory": has_inventory,
        "ledger_entries": entries,
    }


def _text(elem, tag: str) -> str:
    for child in elem.iter():
        if _local_name(child.tag) == tag:
            return (child.text or "").strip()
    return ""


def _local_name(tag: str) -> str:
    return str(tag).split("}")[-1].upper()


def _tally_date(value: str) -> str:
    raw = (value or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw
