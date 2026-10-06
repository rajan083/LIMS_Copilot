# Learned answer (user-corrected)

**Question:** give me the complete table for the discount vs final

**Answer:** In the database, the field `total` represents the initial cost (the amount before any discounts or surcharges were applied), while the `final_amount` reflects the price after these adjustments.

Here is a table showing the requested figures for several orders where a discount or adjustment was applied:

| Serial ID | Initial Cost (`total`) | Discount Amount | Final Price (`final_amount`) |
| :--- | :--- | :--- | :--- |
| SR01OT-2412310004 | 1000.0 | -1500.0 | 2500.0 |
| SR01FD-2412310006 | 9994.0 | -706.08 | 10700.08 |
| SR01NT-2412310003 | 4000.0 | 2200.0 | 1800.0 |
| SR01BL-2412310002 | 1200.0 | -80.04 | 1280.04 |
| SR01BL-2412310003 | 1200.0 | -80.04 | 1280.04 |
| SR01PH-2501020006 | 24000.0 | 8400.0 | 15600.0 |
| SR01EW-2501010001 | 8050.0 | -5250.21 | 13300.21 |

*(Note: Negative discount amounts in the database effectively increase the final price, acting as surcharges. For example, in the first row, a -1500 discount resulted in a 2500 final price against a 1000 initial cost.)*
