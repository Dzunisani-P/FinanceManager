# Finance Illustrator (Streamlit)

A Streamlit rebuild of the original `finance.py` CLI tool, extended with a UI
for managing the category keyword cache and interactive spending-trend
charts. Statements accumulate in a persistent, running history — upload as
many as you like, whenever you like, across independent statement caches
and several supported bank formats.

## Run

```
pip install -r requirements.txt
streamlit run finance_dashboard.py
```

## How it works

**Statement upload** (sidebar)
Choose a bank/statement template, then upload one or more PDF statements at once, or come back later and add more. Each is parsed once and merged into a single persistent ledger stored at `statements_store/ledger.csv`. `statements_store/manifest.json` tracks uploaded files by content hash *within each statement cache* — so re-uploading the same file into the same cache is a no-op and overlapping transactions there are deduplicated, but uploading that same statement into a *different* cache (see **Statement caches** below) is treated as new, since caches are independent histories.

Previously uploaded statements are listed under **Manage statements in this cache** in the sidebar, including the bank template used. Removing a statement also removes its transactions from the ledger.

**PDF statements / bank templates**
Built-in PDF templates are included for **FNB, ABSA and Capitec**. Each parser converts the bank-specific layout into the common `Date`, `Description`, and `Amount` columns used by the rest of the application.

The **Statement Templates** tab lets you create a reusable template for a bank the app has not seen before, using a generic configurable parser: upload a sample PDF and click **"🔎 Auto-detect from sample"** to pre-fill the date format and transaction layout — it tries every supported date pattern against the sample and keeps whichever one actually produces valid dates (so it can tell apart formats that look identical, like DD-MM-YYYY vs MM-DD-YYYY), then scores a single signed Amount column against a separate Money In / Money Out column layout by checking which one actually reconciles against the statement's running balance. Detection isn't always confident — genuinely split-column layouts can look ambiguous from flattened PDF text alone, in which case it'll warn you and you can set the **Transaction layout** manually, or fall back to the same reconciliation check yourself via **"🧪 Test template"**. Custom templates are stored in `statements_store/bank_templates.json`; they contain configuration only and cannot execute Python code.

**Keyword Cache** (`expenses/*.txt`)
One text file per spending category, one keyword per line. A transaction is
assigned to whichever category has the *longest* keyword that appears
(case-insensitively) in its description — identical logic to the original
`finance.py`. Manage these in the **Keyword Cache** tab. Three starter
categories are included: `Entertainment`, `Fuel`, `Groceries`.

**Dashboard**
1. Upload statement(s) in the sidebar — any bank with a template (built-in
   or custom) works, not just FNB — and your full transaction history
   across every upload shows up here automatically.
2. Transactions are auto-categorized from the current keyword cache.
3. In the **Transactions** table, fix any wrong or `Uncategorized` rows
   directly — edits save to your history immediately. Any row the keyword
   cache doesn't yet recognize also gets a pre-filled **Suggested
   Keyword** (trim it to just the merchant name — reference numbers and
   dates in the raw description won't repeat on future statements) and a
   **Why Flagged** note showing what it currently matches instead, if
   anything — including, for already-`Uncategorized` rows, whether a
   keyword *would* now match if you applied the current cache. Clear a
   suggestion to skip it, or type one in for any row yourself — category
   fixes and keyword suggestions live in the same table, so there's one
   pass, not two. A checkbox above the table filters it down to just the
   rows that need attention.
4. Click **"💾 Save keywords & recategorize"** to write any filled-in
   suggestions to the cache — this is the main way it grows over time —
   and, in the same click, re-check every `Uncategorized` transaction
   against the current cache. That second part runs every time, whether
   or not a new keyword was just added, so it also picks up categories
   you edited directly in the **Keyword Cache** tab: categorization
   otherwise only happens once, at upload time, and never automatically
   revisits a transaction just because the cache later learned a matching
   keyword.
5. Charts and CSV exports (category totals, full transaction list) update
   from your edits.

**Cash Flow Overview**
Always shows your full, unfiltered history: income vs. expenses per month,
a cumulative net-flow line (running total of every transaction, in order —
not your real bank balance), and an overall savings rate.

**Category View** (`statements_store/category_groups.json`)
Below the transaction table:
- **Months** — a multiselect to focus every chart in this section on one or
  more specific months (e.g. just look at March, or compare March + April
  side by side).
- **Categories** — a multiselect to focus the charts on just the categories
  you care about.
- **Group** — combine several categories into one label for charting
  purposes only (e.g. merge `Dining_Takeaways` and `Entertainment` into
  "Discretionary"). This never touches the real Category on any
  transaction, the ledger, or the keyword cache — it's purely a display
  grouping, saved so it persists between sessions, and can be undone with
  "Ungroup" at any time.

With a month range, category subset, and/or grouping chosen, you get: spend
by category (bar + pie), a monthly stacked trend, a "Category Mix Over
Time" chart (% share per month — good for spotting a category creeping up
even when total spend is flat), a "This Month vs. Your Average" comparison
per category, and a Top Merchants table.

**Budgets** (`statements_store/budgets.json`)
A dedicated tab for setting a monthly spending cap per category — or per
group, if you've combined categories in the Dashboard tab's Category View
(a budget on a group applies to the combined spend of everything in it).
For each budgeted category/group you get:
- Current-month progress (spent vs. budget, with a progress bar), plus a
  pace projection ("at this rate you're on track to spend X by month end")
  when the most recent uploaded month is the actual current month.
- A bar chart per category/group across all months, with a dashed line
  marking the budget.
- A monthly adherence table flagging which category-months went over.
- A list of your biggest unbudgeted categories (by average monthly spend),
  as candidates worth capping.

## Known limitations / possible next steps

- The generic template builder handles a single signed Amount column or a
  Money In / Money Out column pair. A few statement layouts fall outside
  both — e.g. genuinely ambiguous split-column statements that flattened
  PDF text can't reliably tell apart from a single-column layout — those
  need a dedicated parser function (like FNB, ABSA, and Capitec have)
  rather than the generic builder.
- Duplicate detection matches on exact Date + Description + Amount
  *within the active statement cache*, so two genuinely separate
  transactions on the same day, in the same cache, for the same amount,
  with identical descriptions will be treated as one. The same statement
  uploaded into two different caches is not treated as a duplicate of
  itself — each cache tracks its own upload history independently.

## Statement caches

Statements are now organised into named **statement caches**. A cache is an independent collection of uploaded statements and their transactions. Use the sidebar to:

- create a named cache (for example `Personal`, `Business`, or `2026`)
- rename the active cache
- select which cache is active for the Dashboard and Budgets views
- upload statements directly into the active cache
- remove individual statements from the active cache
- delete a complete cache (the built-in `Main` cache cannot be deleted)

Cache membership is stored with each ledger row, so the same application can maintain several independent statement histories.

## Editable bank statement templates

All statement templates are now managed through `statements_store/bank_templates.json`, including the built-in FNB, ABSA and Capitec templates. In the **Statement Templates** tab you can:

- create new templates
- rename templates
- change their parsing engine and generic-parser settings
- upload a sample statement to auto-detect its date format and transaction
  layout, or to test a template (built-in or custom) before saving
- edit the built-in templates
- delete built-in or custom templates

Deleting a template only removes it from the list of templates available for future uploads. Transactions that were already imported using that template are retained.