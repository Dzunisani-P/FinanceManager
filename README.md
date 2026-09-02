# Finance Dashboard

A simple dashboard for managing and understanding your personal finances using your bank statements.
You can upload statements from supported banks, automatically categorise your transactions, review your spending, set budgets, and explore your financial history.

## Getting Started

### 1. Start the application

Double-click:
`Finance Dashboard.exe`

The application will start automatically and open in your web browser.

> **Note:** The first time you run the application, setup may take a few minutes. An internet connection is required during the initial setup.
> After setup is complete, future launches should be much faster.

### 2. Upload a Bank Statement

Use the **Statement Upload** section in the sidebar.

1. Select the bank statement format.
2. Select one or more PDF statements.
3. Upload them.

Your transactions will be added to the dashboard automatically.
You can return later and upload additional statements. Your previous statements and transactions will remain available.

#### Supported Banks
The application includes templates for:
* FNB
* ABSA
* Capitec

*Other banks can be added using the **Statement Templates** tab.*

---

### 3. Review Your Transactions

Once statements have been uploaded, open the **Dashboard**.
* Your transactions will be automatically categorised using the application's keyword list.
* You can review the transactions in the **Transactions** table.
* If a transaction has been categorised incorrectly, you can change its category directly in the table.
* Transactions that could not be categorised will be marked `Uncategorized`.

#### Adding Keywords
The dashboard can suggest a keyword for transactions that have not yet been recognised.
Review the suggested keyword and adjust it if necessary.

For example, if a transaction contains:
```text
WOOLWORTHS CAPE TOWN 123456
```
you could use:
```text
WOOLWORTHS
```
The shorter merchant name is generally better because it is more likely to match the same merchant on future statements.

Click:
> 💾 **Save keywords & recategorize**

This saves your new keywords and checks your uncategorized transactions again.
Over time, the keyword list becomes more useful as you add merchants.

---

## Key Features & Structure

### Statement Caches
Statement caches allow you to keep separate sets of financial information.

For example, you could create:
* **Personal**
* **Business**
* **2026**

Each cache has its own statements and transactions.

Use the sidebar to:
* Create a new cache
* Rename a cache
* Switch between caches
* Upload statements to the selected cache
* Remove statements
* Delete a cache

*The **Main** cache is included by default and cannot be deleted.*
This is useful if you do not want different sets of financial information mixed together.

---

### Dashboard Overview
The Dashboard gives you an overview of your financial activity.

#### Cash Flow Overview
This section shows:
* Income and expenses by month
* Your cumulative net cash flow
* Your overall savings rate

#### Category View
Use the filters to explore your spending:
* **Months:** Select one or more months to focus on.
* **Categories:** Select the categories you want to view.
* **Groups:** Combine categories for reporting purposes.

> **Example Group:** You could combine *Dining*, *Takeaways*, and *Entertainment* into a group called **Discretionary**.
> Grouping only changes how information is displayed. It does not change the categories assigned to your transactions.

The Dashboard can show:
* Spending by category
* Monthly spending trends
* Category mix over time
* This month's spending compared with your average
* Your top merchants

---

### Budgets
The **Budgets** tab allows you to set monthly spending limits. You can create a budget for an individual category or for a group of categories.

*Example:* `Groceries — R4,000/month`

The Budgets section shows:
* How much you have spent
* How much remains
* Your progress towards the budget
* Your expected spending by the end of the month
* Spending against your budget over previous months
* Which categories have gone over budget
* Highlights categories where you currently have no budget but are spending significant amounts

---

### Statement Templates
The **Statement Templates** tab allows you to add support for other banks. If your bank is not already supported, you can create a template using a sample PDF statement.

The application can try to automatically detect:
* The date format
* The transaction format

You can then test the template before using it. Once a template has been created, it can be reused for future statements from the same bank. Built-in templates for FNB, ABSA, and Capitec can also be edited if necessary.

*Deleting a template does not remove transactions that were already imported using it.*

---

### Keyword Cache
The Keyword Cache contains the words the application uses to categorise transactions. There is one keyword list for each spending category.

*Example for **Groceries**:*
* `WOOLWORTHS`
* `CHECKERS`
* `PICK N PAY`
* `SPAR`

When a transaction description contains one of these keywords, the transaction can be assigned to that category. If more than one keyword matches, the application uses the most specific match.

You can manage these keywords in the **Keyword Cache** tab.

Three starter categories are included:
1. Entertainment
2. Fuel
3. Groceries

You can add your own categories and keywords.

---

## Important Notes

* **Uploading the same statement again:** The application keeps track of statements that have already been uploaded to the current cache. Uploading the same statement again will not create duplicate transactions.
* **Different caches are independent:** The same statement can be uploaded into two different caches. For example, uploading a statement into *Personal* and *Business* will keep it in both caches.
* **Removing a statement:** Removing an uploaded statement also removes the transactions that came from that statement.
* **Transaction categorisation:** Categorisation is based on the keywords currently saved in the Keyword Cache. If you add a new keyword later, click **💾 Save keywords & recategorize** to apply it to transactions that are currently Uncategorized.

---

## Known Limitations

* **Bank Statement Layouts:** Some bank statement formats may not work correctly with the generic template system. If a statement has an unusual layout, the application may not be able to determine the transaction information automatically. In these cases, a dedicated template may be required.
* **Duplicate Detection:** Transaction duplicates are identified using the date, description, and amount. This means that two genuinely separate transactions with exactly the same date, description, and amount may be treated as one transaction.

---

## Tips for Using the Dashboard

1. Upload statements regularly so your financial history stays up to date.
2. Review Uncategorized transactions after uploading new statements.
3. Add useful merchant names as keywords.
4. Use Groups when you want to analyse several categories together.
5. Set budgets for categories where you want to control spending.
6. Keep different types of finances in separate Statement Caches if necessary.

*The more transactions and keywords you add, the more useful the dashboard becomes!*
