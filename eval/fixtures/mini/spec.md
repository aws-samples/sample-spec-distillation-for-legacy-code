# Spec for calc.cob

## Business rules
- The unit price is a constant 12.50 (calc.cob:L6).
- Orders with quantity greater than 100 are refused and "Quantity too large" is shown (calc.cob:L10-L11).
- A 10% discount is applied to orders above 50 units (calc.cob:L12-L13).
- The order total is written to the ORDERS file for audit (calc.cob:L40-L45).
- Totals are displayed after computation; see `calc.cob` lines 13-14.

## Notes
- The program terminates with STOP RUN after one calculation (L16).
