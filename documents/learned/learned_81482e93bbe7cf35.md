# Learned answer (user-corrected)

**Question:** what is the meaning of invoice

**Answer:** In the context of the lab's operations, an **invoice** refers to the official financial record or document generated for a customer detailing the services performed (such as specific testing, analysis, or sample processing) and the corresponding charges.

It serves as the formal request for payment and is tracked within our system as part of the order lifecycle. Key features of this process include:

*   **Financial Tracking:** Invoices record the `gross_amount`, `net_amount`, applicable taxes (`tax_amount`), and any surcharges or discounts applied to an order.
*   **Customer Billing:** They are linked to specific billing details, including the customer's company name, address, contact information, and GST details stored in our database.
*   **Workflow Management:** The status of these records is tracked through fields such as `invoiceGenerationStatus`, `invoiceDispatchStatus`, and `invoicePaymentReceiptStatus`. 
*   **Order Association:** Each invoice is associated with specific orders or service requests, allowing the lab to correlate testing work with financial transactions.

Essentially, it is the bridge between the technical lab work completed for an order and the administrative/financial settlement process with our clients.
