# <img width="960" height="540" alt="image" src="https://github.com/user-attachments/assets/7e031b0d-1105-4231-a570-1de05d50e708" />


# Tally Migration

A Frappe/ERPNext app to migrate data from Tally Prime into ERPNext. Built and maintained by [Greycube Technologies](https://greycube.in).

## What It Does

This app provides a step-by-step wizard to import your complete Tally dataset into ERPNext:

**Master Data** (from Tally's Master Data XML export):

- Chart of Accounts
- Customers and Suppliers with addresses
- Stock Items and Units of Measure (UOMs)

**Transactional Data** (from Tally's Day Book XML export):

- Journal Entries — for Journal, Receipt, Payment, and Contra vouchers
- Sales Invoices and Purchase Invoices — for inventory-based Sales, Purchase, Credit Note, and Debit Note vouchers

All imported documents are tagged with the original Tally GUID and Voucher Number for traceability.

## Requirements

- ERPNext v15
- Python >= 3.10
- `beautifulsoup4` with `lxml` parser (for XML parsing)

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench install-app tally_migration
```

## Migration Workflow

### Step 1 — Export Data from Tally

Export two XML files from Tally:

1. **Master Data** — contains Chart of Accounts, Ledgers, Stock Items, and UOMs.
2. **Day Book** — contains all historical vouchers/transactions.

### Step 2 — Process Master Data

1. Open **Tally Migration** from the ERPNext desk.
2. Upload the Master Data file.
3. Set the **Tally Creditors Account** (default: `Sundry Creditors`) and **Tally Debtors Account** (default: `Sundry Debtors`) to match your Tally setup.
4. Click **Process Master Data**.

The app parses the XML and produces intermediate JSON files for Chart of Accounts, parties, addresses, items, and UOMs.

### Step 3 — Import Master Data

Click **Import Master Data**. The app will:

- Create the ERPNext Company and import the Chart of Accounts.
- Create Customers and Suppliers with their addresses.
- Create Stock Items and UOMs.

After import, verify the auto-detected **Default Warehouse**, **Default Cost Center**, and **Default Round Off Account** in the Accounts section.

### Step 4 — Process Day Book Data

Upload the Day Book export file and click **Process Day Book Data**. The app parses all vouchers and prepares them for import.

### Step 5 — Import Day Book Data

Click **Import Day Book Data**. The app will:

- Create missing Fiscal Years automatically.
- Add `Tally GUID` and `Tally Voucher Number` custom fields to Journal Entry, Sales Invoice, and Purchase Invoice.
- Import all vouchers in chunks of 500, submitted and ready for use.
- Disable the temporary "Tally Price List" once import is complete.

Progress is shown in real time via the browser.

## Import Logs

Any records that fail to import are captured in the **Failed Import Log** section with the full error traceback. You can review, correct, and re-attempt them without re-running the entire migration.

## Voucher Type Mapping


| Tally Voucher Type           | ERPNext Document |
| ------------------------------ | ------------------ |
| Journal                      | Journal Entry    |
| Receipt                      | Journal Entry    |
| Payment                      | Journal Entry    |
| Contra                       | Journal Entry    |
| Sales (without inventory)    | Journal Entry    |
| Purchase (with inventory)    | Purchase Invoice |
| Credit Note (with inventory) | Sales Invoice    |
| Debit Note (with inventory)  | Purchase Invoice |
| Sales (with inventory)       | Sales Invoice    |

## Contributing

This app uses `pre-commit` for code formatting and linting. Install and enable it before contributing:

```bash
cd apps/tally_migration
pre-commit install
```

Pre-commit runs the following tools:

- **ruff** — Python linting and formatting
- **eslint** — JavaScript linting
- **prettier** — JavaScript/CSS formatting
- **pyupgrade** — Python syntax modernization

## License

MIT — see [license.txt](license.txt)
